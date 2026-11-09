"""
Result of running regression test cases and how results roll up into a summary and an exit code
"""
from dataclasses import dataclass, field

from regression.manifest import RegressionCase

# Case outcomes
PASSED = 'PASSED'
FAILED = 'FAILED'
MISMATCHED = 'MISMATCHED'
SKIPPED = 'SKIPPED'

# Workflow statuses reported in addition to the Step Functions execution statuses
STATUS_SUCCEEDED = 'SUCCEEDED'
STATUS_TIMEOUT = 'TIMEOUT'
STATUS_ERROR = 'ERROR'
STATUS_SKIPPED = 'SKIPPED'

# Comparison result reported when a succeeded workflow's results could not be retrieved or compared
COMPARISON_ERROR = 'ERROR'

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_NOT_STARTED = 2


@dataclass
class CaseResult:
    """
    What happened when one test case was run.

    The outcome is PASSED when the execution succeeded and its browse images match the reference, MISMATCHED when
    it succeeded but they differ, SKIPPED when the manifest marks the case to be skipped, and FAILED otherwise
    (the execution failed, was aborted or timed out, or the suite hit an error running the case).
    """
    case: RegressionCase
    outcome: str = FAILED
    workflow_status: str = STATUS_ERROR
    comparison: str = ''
    issues: list[str] = field(default_factory=list)
    error: str = ''
    execution_name: str = ''
    duration_seconds: float = 0.0

    def to_dict(self) -> dict:
        """Return the result as a JSON-serializable dictionary."""
        return {
            'short_name': self.case.short_name,
            'provider': self.case.provider,
            'granule_ur': self.case.granule_ur,
            'cmr_env': self.case.cmr_env,
            'outcome': self.outcome,
            'workflow_status': self.workflow_status,
            'comparison': self.comparison,
            'issues': list(self.issues),
            'error': self.error,
            'execution_name': self.execution_name,
            'duration_seconds': round(self.duration_seconds, 1)
        }


def summarize(results: list[CaseResult]) -> dict:
    """
    Count results by outcome.

    Returns
    -------
    dict
        Counts under the keys total, passed, failed, mismatched and skipped
    """
    outcomes = [result.outcome for result in results]
    return {
        'total': len(outcomes),
        'passed': outcomes.count(PASSED),
        'failed': outcomes.count(FAILED),
        'mismatched': outcomes.count(MISMATCHED),
        'skipped': outcomes.count(SKIPPED)
    }


def exit_code(results: list[CaseResult], interrupted: bool = False) -> int:
    """
    Exit code for a completed run: 0 when every case that was not skipped passed, otherwise 1. A run that was
    interrupted before all cases ran is never successful.
    """
    if interrupted or any(result.outcome not in (PASSED, SKIPPED) for result in results):
        return EXIT_FAILURES
    return EXIT_OK
