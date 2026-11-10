"""
Runs regression test cases against a deployed browse image workflow, several at a time
"""
import json
import logging
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, replace
from pathlib import Path

from regression import aws, cmr, compare, messages
from regression.config import Deployment
from regression.manifest import RegressionCase
from regression.redact import Redactor
from regression.results import (COMPARISON_ERROR, FAILED, MISMATCHED, PASSED, SKIPPED, STATUS_ERROR, STATUS_SKIPPED,
                                STATUS_SUCCEEDED, STATUS_TIMEOUT, CaseResult)

LOGGER = logging.getLogger(__name__)

MAX_ERROR_LENGTH = 4000
DEFAULT_MAX_PARALLEL = 8

# How often the main thread wakes up while waiting for cases, so that it is never blocked for long when interrupted
_WAIT_SLICE_SECONDS = 0.1


@dataclass
class RunContext:  # pylint: disable=too-many-instance-attributes
    """
    Everything needed to run test cases against one deployment. The clients, the redactor and the stop event are
    shared by the threads that run cases in parallel. ``stop_event`` is set when the run is interrupted; ``run_cases``
    creates it unless one is given. ``cmr_token`` is the Earthdata Login token for CMR searches, or None to search
    anonymously.
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
    max_parallel: int = DEFAULT_MAX_PARALLEL
    stop_event: threading.Event = None
    cmr_token: str | None = None


def case_workdir(workdir, case: RegressionCase) -> Path:
    """
    Return the directory a case's inputs and results are written to: ``<workdir>/<short_name>/<granule_ur>/<cmr_env>``.
    The reference data uses the same layout, rooted at the reference directory.
    """
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
    if context.stop_event is not None and context.stop_event.is_set():
        raise aws.ExecutionInterrupted(f'Not starting {case.short_name}: the run was interrupted')

    workdir = case_workdir(context.workdir, case)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)

    granule_cmr = cmr.get_granule(case.short_name, case.provider, case.granule_ur, case.cmr_env, token=context.cmr_token)
    input_message = messages.build_input_message(case, granule_cmr, context.deployment)
    _write_json(workdir / 'input.json', input_message, context.redactor)

    result.execution_name = aws.execution_name(case.short_name)
    execution = aws.run_execution(context.sfn_client, context.state_machine_arn, result.execution_name, input_message,
                                  poll_interval=context.poll_interval, timeout=context.execution_timeout,
                                  stop_event=context.stop_event)
    result.workflow_status = execution.status

    if execution.status != STATUS_SUCCEEDED:
        result.error = describe_failure(execution, context.execution_timeout)
        _write_json(workdir / 'error.json',
                    {'status': execution.status, 'error': execution.error, 'cause': execution.cause},
                    context.redactor)
        return

    output = json.loads(execution.output or '{}')
    _write_json(workdir / 'output.json', output, context.redactor)

    cnm_files = aws.download_cnm_files(context.s3_client, output, workdir / 'cnm', context.redactor, label=result.execution_name)
    if context.download_browse:
        aws.download_browse_files(context.s3_client, cnm_files, workdir / 'files', label=result.execution_name)

    reference = compare.load_cnm_files(case.short_name, case.granule_ur, case.cmr_env,
                                       case_workdir(context.reference_dir, case) / 'cnm')
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

    Raises
    ------
    ExecutionInterrupted
        If the context's stop event is set before the case finishes. The case is not recorded as failed.
    """
    if case.skipped:
        return CaseResult(case=case, outcome=SKIPPED, workflow_status=STATUS_SKIPPED)

    result = CaseResult(case=case, outcome=FAILED, workflow_status=STATUS_ERROR)
    started = time.monotonic()
    try:
        _execute_case(case, context, result)
    except aws.ExecutionInterrupted:
        raise
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


def _finished_counter():
    """Return a function that counts finished cases across threads, so that completion lines say how many are done."""
    finished = 0
    lock = threading.Lock()

    def count() -> int:
        nonlocal finished
        with lock:
            finished += 1
            return finished
    return count


def _run_logged(case: RegressionCase, context: RunContext, count_finished, total: int) -> CaseResult:
    """Run one case in a worker thread, logging a start line and a completion line that each name the case."""
    LOGGER.info('Starting regression for %s with granule UR %s', case.short_name, case.granule_ur)
    result = run_case(case, context)
    LOGGER.info('[%d/%d finished] %s (%s): %s (workflow %s, comparison %s) in %.0f seconds', count_finished(), total,
                case.short_name, result.execution_name or 'no execution', result.outcome, result.workflow_status,
                result.comparison or 'n/a', result.duration_seconds)
    return result


def _failed_result(case: RegressionCase, ex: BaseException) -> CaseResult:
    """Result for a case whose worker raised something ``run_case`` should have captured, so that no case is lost."""
    LOGGER.error('Unexpected error running regression for %s (%s)', case.short_name, case.granule_ur, exc_info=ex)
    return CaseResult(case=case, outcome=FAILED, workflow_status=STATUS_ERROR, error=f'{type(ex).__name__}: {ex}'[:MAX_ERROR_LENGTH])


def _completed_results(futures: dict, cases: list[RegressionCase]) -> dict:
    """Collect the results of finished workers by case index, leaving out cancelled cases and those stopped part way."""
    results = {}
    for future, index in futures.items():
        if future.cancelled():
            continue
        error = future.exception()
        if error is None:
            results[index] = future.result()
        elif not isinstance(error, aws.ExecutionInterrupted):
            results[index] = _failed_result(cases[index], error)
    return results


def run_cases(cases: list[RegressionCase], context: RunContext) -> tuple[list[CaseResult], bool]:
    """
    Run test cases, up to ``context.max_parallel`` at a time.

    Cases marked to be skipped are reported without occupying a worker. If the main thread is interrupted, the
    stop event is set so that waiting workers return at once, cases that have not started are cancelled and the
    workers still running are waited for.

    Parameters
    ----------
    cases : list[RegressionCase]
        The cases to run, including any marked to be skipped
    context : RunContext
        The deployment to run them against

    Returns
    -------
    tuple[list[CaseResult], bool]
        The results in the order of ``cases`` whatever order the cases finished in, and whether the run was
        interrupted. After an interrupt only the cases that completed are included; cases that were still running or
        had not started are left out.
    """
    stop_event = context.stop_event or threading.Event()
    context = replace(context, stop_event=stop_event)
    to_run = [case for case in cases if not case.skipped]
    count_finished = _finished_counter()

    results = {}
    for index, case in enumerate(cases):
        if case.skipped:
            LOGGER.info('Skipping %s (%s)', case.short_name, case.granule_ur)
            results[index] = run_case(case, context)

    interrupted = False
    executor = ThreadPoolExecutor(max_workers=max(1, min(context.max_parallel, len(to_run) or 1)), thread_name_prefix='regression')
    futures = {}
    try:
        for index, case in enumerate(cases):
            if not case.skipped:
                # Added one at a time so that an interrupt during submission still leaves every submitted case in sight
                futures[executor.submit(_run_logged, case, context, count_finished, len(to_run))] = index
        pending = set(futures)
        while pending:
            # Waiting in short slices means the main thread is always back in Python code soon enough to raise
            # KeyboardInterrupt, on every platform
            _, pending = wait(pending, timeout=_WAIT_SLICE_SECONDS, return_when=FIRST_COMPLETED)
    except KeyboardInterrupt:
        interrupted = True
        LOGGER.warning('Interrupted; stopping the %d case(s) in progress and cancelling those not started', len(
            [future for future in futures if future.running()]))
        stop_event.set()
        for future in futures:
            future.cancel()
    finally:
        executor.shutdown(wait=True, cancel_futures=True)

    results.update(_completed_results(futures, cases))

    ordered = [results[index] for index in sorted(results)]
    if interrupted:
        LOGGER.warning('Interrupted after %d of %d case(s)', len(ordered), len(cases))
    return ordered, interrupted
