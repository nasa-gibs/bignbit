"""
AWS interactions of the regression suite: locating the deployment's state machine, running an execution of it
and fetching the results it wrote to S3
"""
import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError, PartialCredentialsError

from regression.manifest import is_safe_path_component
from regression.redact import Redactor
from regression.results import STATUS_TIMEOUT

LOGGER = logging.getLogger(__name__)

UNKNOWN_VERSION = 'unknown'
VERSION_TAG = 'Version'

RUNNING_STATUSES = ('RUNNING', 'PENDING_REDRIVE')
EXECUTION_NAME_MAX_LENGTH = 80
EXECUTION_NAME_PREFIX = 'reg-'
HISTORY_MAX_EVENTS = 1000

_STATE_MACHINE_ARN = re.compile(r'^arn:aws[a-z0-9-]*:states:([a-z0-9-]+):(\d{12}):stateMachine:([^:/\s]+)$')
_UNSAFE_NAME_CHARACTERS = re.compile(r'[^A-Za-z0-9_-]')
_EXECUTION_END_EVENTS = ('ExecutionFailed', 'ExecutionAborted', 'ExecutionTimedOut')
_FAILURE_DETAIL_SUFFIXES = ('FailedEventDetails', 'TimedOutEventDetails')


class StateMachineNotFound(Exception):
    """
    Exception for a state machine that does not exist in the target account and region
    """


class DownloadError(Exception):
    """
    Exception for a workflow result that could not be downloaded from S3
    """


class ExecutionInterrupted(Exception):
    """
    Exception for a wait on an execution that was abandoned because the run was interrupted. The execution itself
    is left running.
    """


@dataclass
class ExecutionResult:
    """
    Final state of a state machine execution. The status is a Step Functions execution status, or TIMEOUT when
    the execution was still running after the suite's own time limit.
    """
    status: str
    output: str = None
    error: str = None
    cause: str = None


def create_session(profile: str = None, region: str = None) -> boto3.Session:
    """Create a boto3 session from the default credential chain, or from a named profile when one is given."""
    return boto3.Session(profile_name=profile, region_name=region)


def get_account_id(session: boto3.Session) -> str:
    """
    Look up the account ID of the session's credentials so that it can be redacted from everything the suite writes.

    Returns
    -------
    str
        The account ID, or an empty string if it could not be determined

    Raises
    ------
    botocore.exceptions.NoCredentialsError, botocore.exceptions.PartialCredentialsError
        If the session has no usable credentials, in which case nothing else can work either
    """
    try:
        return session.client('sts').get_caller_identity()['Account']
    except (NoCredentialsError, PartialCredentialsError):
        raise
    except (BotoCoreError, ClientError) as ex:
        LOGGER.warning('Unable to determine the AWS account ID; relying on pattern-based redaction only: %s', ex)
        return ''


def parse_state_machine_arn(arn: str) -> tuple[str, str, str]:
    """
    Split a state machine ARN into its region, account ID and state machine name.

    Raises
    ------
    ValueError
        If the value is not a state machine ARN
    """
    match = _STATE_MACHINE_ARN.match(arn or '')
    if not match:
        raise ValueError('Not a Step Functions state machine ARN (expected arn:aws:states:<region>:<account>:stateMachine:<name>)')
    return match.group(1), match.group(2), match.group(3)


def find_state_machine_arn(sfn_client, name: str) -> str:
    """
    Find a state machine by name.

    Raises
    ------
    StateMachineNotFound
        If no state machine with that name exists in the client's account and region
    """
    paginator = sfn_client.get_paginator('list_state_machines')
    for page in paginator.paginate():
        for state_machine in page['stateMachines']:
            if state_machine['name'] == name:
                return state_machine['stateMachineArn']

    raise StateMachineNotFound(f'State machine {name!r} not found in region {sfn_client.meta.region_name}')


def get_deployed_version(lambda_client, function_name: str) -> str:
    """
    Read the deployed bignbit version from the Version tag the example deployment puts on the module's Lambdas.
    This is best effort: any failure is logged and reported as ``unknown``.
    """
    try:
        tags = lambda_client.get_function(FunctionName=function_name).get('Tags') or {}
    except Exception as ex:  # pylint: disable=broad-exception-caught
        LOGGER.warning('Unable to read the deployed bignbit version from Lambda %s: %s', function_name, ex)
        return UNKNOWN_VERSION
    return tags.get(VERSION_TAG) or UNKNOWN_VERSION


def execution_name(short_name: str, now: datetime = None, token: str = None) -> str:
    """
    Build a recognizable, unique execution name: ``reg-<short name>-<UTC timestamp>-<random token>``.

    Step Functions limits names to 80 characters and forbids whitespace and most punctuation, so characters other
    than letters, digits, hyphens and underscores are replaced and an overlong short name is truncated.
    """
    now = now or datetime.now(timezone.utc)
    suffix = f'-{now.strftime("%Y%m%dT%H%M%S")}-{token or secrets.token_hex(2)[:3]}'
    suffix = _UNSAFE_NAME_CHARACTERS.sub('_', suffix)
    collection = _UNSAFE_NAME_CHARACTERS.sub('_', short_name)
    return EXECUTION_NAME_PREFIX + collection[:EXECUTION_NAME_MAX_LENGTH - len(EXECUTION_NAME_PREFIX) - len(suffix)] + suffix


def find_failure_details(sfn_client, execution_arn: str) -> tuple[str, str]:
    """
    Find the error and cause of the most recent failed step in an execution's history. The workflow routes caught
    errors to a Fail state with a fixed cause, so the execution's own cause does not say what went wrong.

    Returns
    -------
    tuple[str, str]
        The error and cause of the failed step, or None if the history holds none or could not be read
    """
    try:
        history = sfn_client.get_execution_history(executionArn=execution_arn, reverseOrder=True, maxResults=HISTORY_MAX_EVENTS)
    except (BotoCoreError, ClientError) as ex:
        LOGGER.warning('Unable to read the history of execution %s for failure details: %s', execution_arn, ex)
        return None

    for event in history.get('events', []):
        if event.get('type') in _EXECUTION_END_EVENTS:
            continue
        for key, details in event.items():
            if key.endswith(_FAILURE_DETAIL_SUFFIXES) and (details.get('error') or details.get('cause')):
                return details.get('error', ''), details.get('cause', '')
    return None


def _stop_execution(sfn_client, execution_arn: str, timeout: float) -> None:
    """Stop an execution that outlived the suite's time limit so that it does not keep running unattended; best effort."""
    try:
        sfn_client.stop_execution(executionArn=execution_arn, error='RegressionTimeout',
                                  cause=f'Stopped by the regression suite after {timeout:g} seconds')
    except (BotoCoreError, ClientError) as ex:
        LOGGER.warning('Unable to stop the timed out execution %s: %s', execution_arn, ex)


def _pause(seconds: float, stop_event: threading.Event = None) -> bool:
    """Wait between status checks. Returns True if the stop event was set, which ends the wait immediately."""
    if stop_event is None:
        time.sleep(seconds)
        return False
    return stop_event.wait(seconds)


def run_execution(sfn_client, state_machine_arn: str, name: str, input_message: dict,
                  poll_interval: float = 10, timeout: float = 1800, stop_event: threading.Event = None) -> ExecutionResult:
    """
    Start a state machine execution and wait for it to finish.

    Parameters
    ----------
    sfn_client
        Boto3 Step Functions client
    state_machine_arn : str
        ARN of the state machine to run
    name : str
        Name for the execution
    input_message : dict
        Input for the execution
    poll_interval : float
        Seconds between status checks
    timeout : float
        Seconds to wait for the execution to finish before stopping it and giving up
    stop_event : threading.Event
        Optional. When it is set, waiting ends at once with ExecutionInterrupted, and an execution that has not been
        started yet is not started. The execution is left running.

    Returns
    -------
    ExecutionResult
        The final status with the execution's output, or its error and cause. A failed execution whose own error
        is empty is given the error and cause of its most recent failed step, when the history can be read.

    Raises
    ------
    ExecutionInterrupted
        If the stop event is set before the execution finishes
    """
    if stop_event is not None and stop_event.is_set():
        raise ExecutionInterrupted(f'Not starting execution {name}: the run was interrupted')
    execution_arn = sfn_client.start_execution(stateMachineArn=state_machine_arn, name=name,
                                               input=json.dumps(input_message))['executionArn']
    LOGGER.info('Started execution %s; checking every %g seconds', name, poll_interval)

    deadline = time.monotonic() + timeout
    while True:
        description = sfn_client.describe_execution(executionArn=execution_arn)
        status = description['status']
        if status not in RUNNING_STATUSES:
            break

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            LOGGER.warning('Execution %s is still %s after %g seconds; stopping it', name, status, timeout)
            _stop_execution(sfn_client, execution_arn, timeout)
            return ExecutionResult(status=STATUS_TIMEOUT)
        if _pause(min(poll_interval, remaining), stop_event):
            LOGGER.warning('Stopped waiting for execution %s (still %s) because the run was interrupted', name, status)
            raise ExecutionInterrupted(f'Stopped waiting for execution {name}: the run was interrupted')

    LOGGER.info('Execution %s finished with status %s', name, status)
    result = ExecutionResult(status=status, output=description.get('output'),
                             error=description.get('error'), cause=description.get('cause'))
    if status == 'FAILED' and not result.error:
        failure = find_failure_details(sfn_client, execution_arn)
        if failure:
            result.error, result.cause = failure
    return result


def _local_path(directory: Path, name: str) -> Path:
    """Return a path for a downloaded file that stays inside the directory and does not overwrite an earlier download."""
    if not is_safe_path_component(name):
        raise ValueError(f'Unsafe file name in S3 key: {name!r}')
    path = directory / name
    index = 1
    while path.exists():
        index += 1
        path = directory / f'{index}.{name}'
    return path


def download_cnm_files(s3_client, output: dict, cnm_dir, redactor: Redactor, label: str = '') -> list[Path]:
    """
    Download the CNM messages a succeeded execution archived to S3.

    Parameters
    ----------
    s3_client
        Boto3 S3 client
    output : dict
        The execution output; CNM locations are read from ``payload.pobit[].cnm_bucket`` and ``cnm_key``
    cnm_dir : str or Path
        Directory to write the CNM JSON files to
    redactor : Redactor
        Applied to each CNM before it is written, since CNMs may later be checked in as reference data
    label : str
        Identifies the test case in log messages, which interleave when cases run in parallel

    Returns
    -------
    list[Path]
        The downloaded files

    Raises
    ------
    DownloadError
        If a CNM cannot be downloaded; the comparison would be meaningless without it
    """
    cnm_dir = Path(cnm_dir)
    cnm_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    for item in (output.get('payload') or {}).get('pobit') or []:
        bucket, key = item.get('cnm_bucket', ''), item.get('cnm_key', '')
        if not bucket or not key:
            continue

        LOGGER.debug('%s: Downloading CNM s3://%s/%s', label, bucket, key)
        try:
            body = s3_client.get_object(Bucket=bucket, Key=key)['Body'].read().decode('utf-8')
        except (BotoCoreError, ClientError) as ex:
            raise DownloadError(f'Failed to download CNM s3://{bucket}/{key}: {ex}') from ex
        local_path = _local_path(cnm_dir, key.rsplit('/', 1)[-1])
        local_path.write_text(redactor.redact(json.dumps(json.loads(body), indent=4)), encoding='utf-8')
        downloaded.append(local_path)

    LOGGER.info('%s: Downloaded %d CNM file(s)', label, len(downloaded))
    return downloaded


def download_browse_files(s3_client, cnm_files: list[Path], files_dir, label: str = '') -> list[Path]:
    """
    Download the browse images listed in CNM messages, for manual inspection. The comparison does not use them,
    so a file that cannot be downloaded is logged and left out rather than failing the case.

    Parameters
    ----------
    s3_client
        Boto3 S3 client
    cnm_files : list[Path]
        CNM JSON files to read browse image locations from
    files_dir : str or Path
        Directory to write the browse images to
    label : str
        Identifies the test case in log messages, which interleave when cases run in parallel

    Returns
    -------
    list[Path]
        The downloaded files
    """
    files_dir = Path(files_dir)
    files_dir.mkdir(parents=True, exist_ok=True)

    downloaded = []
    for cnm_path in cnm_files:
        cnm_data = json.loads(Path(cnm_path).read_text(encoding='utf-8'))
        for product_file in cnm_data.get('product', {}).get('files', []):
            bucket, key = product_file.get('bucket', ''), product_file.get('key', '')
            if product_file.get('type') != 'browse' or not bucket or not key:
                continue

            try:
                local_path = _local_path(files_dir, key.rsplit('/', 1)[-1])
                s3_client.download_file(bucket, key, str(local_path))
            except (BotoCoreError, ClientError, ValueError) as ex:
                LOGGER.warning('%s: Failed to download browse image s3://%s/%s: %s', label, bucket, key, ex)
                continue
            downloaded.append(local_path)

    LOGGER.info('%s: Downloaded %d browse image(s)', label, len(downloaded))
    return downloaded
