"""
Runs regression test cases against a deployed browse image workflow, one after another
"""
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from regression import aws, cmr, compare, messages
from regression.config import Deployment
from regression.manifest import RegressionCase
from regression.redact import Redactor
from regression.results import (COMPARISON_ERROR, FAILED, MISMATCHED, PASSED, SKIPPED, STATUS_ERROR, STATUS_SKIPPED,
                                STATUS_SUCCEEDED, STATUS_TIMEOUT, CaseResult)

LOGGER = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 4000


@dataclass
class RunContext:
    """
    Everything needed to run test cases against one deployment
    """
    deployment: Deployment
    state_machine_arn: str
    sfn_client: object
    s3_client: object
    redactor: Redactor
    workdir: Path
    reference_dir: Path
    poll_interval: float = 10
    execution_timeout: float = 1800
    download_browse: bool = False


def case_workdir(workdir, case: RegressionCase) -> Path:
    """Return the directory a case's inputs and results are written to: ``<workdir>/<short_name>/<granule_ur>/<cmr_env>``."""
    return Path(workdir) / case.short_name / case.granule_ur / case.cmr_env


def describe_failure(execution: aws.ExecutionResult, timeout: float) -> str:
    """
    Describe why an execution did not succeed. Lambda failures carry a JSON cause; its ``errorMessage`` is used when
    present, otherwise the cause is reported as is.
    """
    if execution.status == STATUS_TIMEOUT:
        return f'The execution did not finish within {timeout:g} seconds'

    cause = execution.cause or ''
    try:
        message = json.loads(cause).get('errorMessage', cause)
    except (ValueError, AttributeError):
        message = cause

    description = ': '.join(str(part) for part in (execution.error, message) if part)
    description = description or f'The execution ended with status {execution.status}'
    if len(description) > MAX_ERROR_LENGTH:
        description = description[:MAX_ERROR_LENGTH] + ' ... (truncated)'
    return description


def _write_json(path: Path, data: dict, redactor: Redactor) -> None:
    """Write redacted, indented JSON, creating the parent directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(redactor.redact(json.dumps(data, indent=4)), encoding='utf-8')


def _execute_case(case: RegressionCase, context: RunContext, result: CaseResult) -> None:
    """Run one case, recording progress on the result as it goes so that an exception leaves it accurate."""
    workdir = case_workdir(context.workdir, case)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    granule_cmr = cmr.get_granule(case.short_name, case.provider, case.granule_ur, case.cmr_env)
    input_message = messages.build_input_message(case, granule_cmr, context.deployment)
    _write_json(workdir / 'input.json', input_message, context.redactor)

    result.execution_name = aws.execution_name(case.short_name)
    execution = aws.run_execution(context.sfn_client, context.state_machine_arn, result.execution_name, input_message,
                                  poll_interval=context.poll_interval, timeout=context.execution_timeout)
    result.workflow_status = execution.status

    if execution.status != STATUS_SUCCEEDED:
        result.error = describe_failure(execution, context.execution_timeout)
        _write_json(workdir / 'error.json',
                    {'status': execution.status, 'error': execution.error, 'cause': execution.cause},
                    context.redactor)
        return

    output = json.loads(execution.output or '{}')
    _write_json(workdir / 'output.json', output, context.redactor)

    cnm_files = aws.download_cnm_files(context.s3_client, output, workdir / 'cnm', context.redactor)
    if context.download_browse:
        aws.download_browse_files(context.s3_client, cnm_files, workdir / 'files')

    reference = compare.load_cnm_files(case.short_name, case.granule_ur, case.cmr_env,
                                       Path(context.reference_dir) / case.short_name / case.granule_ur / 'cnm')
    current = compare.load_cnm_files(case.short_name, case.granule_ur, case.cmr_env, workdir / 'cnm')
    comparison = compare.compare(reference, current)

    result.comparison = comparison.status
    result.issues = comparison.issues
    result.outcome = PASSED if comparison.status == compare.MATCH else MISMATCHED


def run_case(case: RegressionCase, context: RunContext) -> CaseResult:
    """
    Run one test case: look the granule up in CMR, run the workflow for it, download the CNM messages it produced
    and compare them with the reference.

    Parameters
    ----------
    case : RegressionCase
        The case to run
    context : RunContext
        The deployment to run it against and where to write files

    Returns
    -------
    CaseResult
        The outcome. Errors are captured on the result rather than raised, so one failing case never stops the rest.
    """
    if case.skipped:
        return CaseResult(case=case, outcome=SKIPPED, workflow_status=STATUS_SKIPPED)

    result = CaseResult(case=case, outcome=FAILED, workflow_status=STATUS_ERROR)
    started = time.monotonic()
    try:
        _execute_case(case, context, result)
    except Exception as ex:  # pylint: disable=broad-exception-caught
        LOGGER.exception('Error running regression for %s (%s)', case.short_name, case.granule_ur)
        result.outcome = FAILED
        result.error = f'{type(ex).__name__}: {ex}'[:MAX_ERROR_LENGTH]
        if result.workflow_status == STATUS_SUCCEEDED:
            result.comparison = COMPARISON_ERROR
        try:
            _write_json(case_workdir(context.workdir, case) / 'error.json',
                        {'status': STATUS_ERROR, 'error': type(ex).__name__, 'cause': str(ex)}, context.redactor)
        except OSError:
            LOGGER.warning('Unable to write error.json for %s', case.short_name)
    result.duration_seconds = time.monotonic() - started
    return result


def run_cases(cases: list[RegressionCase], context: RunContext) -> tuple[list[CaseResult], bool]:
    """
    Run test cases sequentially.

    Parameters
    ----------
    cases : list[RegressionCase]
        The cases to run, including any marked to be skipped
    context : RunContext
        The deployment to run them against

    Returns
    -------
    tuple[list[CaseResult], bool]
        One result per case that was reached, and whether the run was interrupted before reaching all of them
    """
    results = []
    try:
        for number, case in enumerate(cases, start=1):
            if case.skipped:
                LOGGER.info('[%d/%d] Skipping %s (%s)', number, len(cases), case.short_name, case.granule_ur)
            else:
                LOGGER.info('[%d/%d] Running regression for %s with granule UR %s', number, len(cases), case.short_name, case.granule_ur)

            result = run_case(case, context)
            results.append(result)

            if not case.skipped:
                LOGGER.info('[%d/%d] %s: %s (workflow %s, comparison %s) in %.0f seconds', number, len(cases), case.short_name,
                            result.outcome, result.workflow_status, result.comparison or 'n/a', result.duration_seconds)
    except KeyboardInterrupt:
        LOGGER.warning('Interrupted after %d of %d case(s)', len(results), len(cases))
        return results, True
    return results, False
