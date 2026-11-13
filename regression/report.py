"""
Renders the result of a regression run as a GitHub-flavored Markdown report and as JSON.

The Markdown is published to the repository's public wiki and appended to the GitHub Actions job summary, so it
sticks to constructs that render in both (tables and <details> blocks) and is redacted before it is written.
"""
import html
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from regression.config import REPO_ROOT, Deployment
from regression.redact import Redactor
from regression.results import (COMPARISON_ERROR, EXIT_NOT_STARTED, FAILED, MISMATCHED, PASSED, SKIPPED, STATUS_SUCCEEDED,
                                CaseResult, exit_code, summarize)

REPORT_FILENAME = 'report.md'
RESULTS_FILENAME = 'results.json'

UNKNOWN = 'unknown'
NOT_APPLICABLE = '-'

VERDICT_PASSED = 'PASSED'
VERDICT_FAILED = 'FAILED'
VERDICT_INTERRUPTED = 'INTERRUPTED'
VERDICT_ERROR = 'ERROR'

_GITHUB_VARIABLES = ('GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_SHA')
_BACKTICKS = re.compile(r'`+')


@dataclass
class RunInfo:
    """
    Metadata describing a regression run. The deployment is None when the run failed before it was resolved.
    """
    venue: str
    started_at: datetime
    deployment: Deployment = None
    state_machine_name: str = UNKNOWN
    deployed_version: str = UNKNOWN
    duration_seconds: float = 0.0
    source: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    interrupted: bool = False
    max_parallel: int = None

    def to_dict(self) -> dict:
        """Return the run metadata as a JSON-serializable dictionary."""
        deployment = self.deployment
        return {
            'venue': self.venue,
            'region': deployment.region if deployment else UNKNOWN,
            'resource_prefix': deployment.resource_prefix if deployment else UNKNOWN,
            'state_machine': self.state_machine_name,
            'cmr_environment': deployment.cmr_environment if deployment else UNKNOWN,
            'deployed_version': self.deployed_version,
            'max_parallel': self.max_parallel,
            'started_at': self.started_at.isoformat(timespec='seconds'),
            'duration_seconds': round(self.duration_seconds, 1),
            'commit': self.source.get('commit', UNKNOWN),
            'commit_url': self.source.get('commit_url'),
            'run_url': self.source.get('run_url'),
            'notes': list(self.notes)
        }


def _git_commit(repo_root: Path) -> str:
    """Return the commit checked out in the repository, or ``unknown`` if git cannot tell."""
    try:
        completed = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=repo_root, capture_output=True, text=True,
                                   check=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN
    return completed.stdout.strip() or UNKNOWN


def source_info(environ: dict = None, repo_root: Path = REPO_ROOT) -> dict:
    """
    Identify the version of the suite that is running.

    Parameters
    ----------
    environ : dict
        Environment variables; defaults to the process environment
    repo_root : Path
        Repository to ask git about when not running in GitHub Actions

    Returns
    -------
    dict
        ``commit``, plus ``commit_url``, ``run_id`` and ``run_url`` when GITHUB_SERVER_URL, GITHUB_REPOSITORY,
        GITHUB_RUN_ID and GITHUB_SHA are all set
    """
    environ = os.environ if environ is None else environ
    server_url, repository, run_id, sha = (environ.get(name) for name in _GITHUB_VARIABLES)
    if server_url and repository and run_id and sha:
        repository_url = f'{server_url.rstrip("/")}/{repository}'
        return {
            'commit': sha,
            'commit_url': f'{repository_url}/commit/{sha}',
            'run_id': run_id,
            'run_url': f'{repository_url}/actions/runs/{run_id}'
        }
    return {'commit': sha or _git_commit(repo_root)}


def format_duration(seconds: float) -> str:
    """Format a duration as ``1h 02m 03s``, ``2m 03s`` or ``3s``."""
    minutes, secs = divmod(int(round(seconds)), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f'{hours}h {minutes:02d}m {secs:02d}s'
    if minutes:
        return f'{minutes}m {secs:02d}s'
    return f'{secs}s'


def get_verdict(results: list[CaseResult], interrupted: bool = False) -> str:
    """Overall verdict of a run that started: PASSED, FAILED, or INTERRUPTED when it was cut short."""
    if interrupted:
        return VERDICT_INTERRUPTED
    return VERDICT_FAILED if exit_code(results) else VERDICT_PASSED


def _code_cell(text: str) -> str:
    """Format text as an inline code span that is safe inside a Markdown table cell."""
    text = ' '.join(str(text).split()).replace('`', "'").replace('|', '\\|')
    return f'`{text}`' if text else NOT_APPLICABLE


def _fenced(text: str) -> str:
    """Wrap text in a code fence longer than any run of backticks it contains, so the text cannot close it."""
    longest = max((len(run) for run in _BACKTICKS.findall(text)), default=0)
    fence = '`' * max(3, longest + 1)
    return f'{fence}text\n{text}\n{fence}'


def _emphasize(text: str, problem: bool) -> str:
    """Make a problem status stand out in the results table."""
    return f'**{text}**' if problem else text


def _details_table(run_info: RunInfo) -> list[str]:
    """Render the run metadata as a two column table."""
    info = run_info.to_dict()
    commit = info['commit']
    commit_cell = f'[`{commit[:12]}`]({info["commit_url"]})' if info['commit_url'] else _code_cell(commit[:12])

    rows = [
        ('Started', run_info.started_at.strftime('%Y-%m-%d %H:%M:%S UTC')),
        ('Duration', format_duration(run_info.duration_seconds)),
        ('Maximum parallel cases', info['max_parallel'] or NOT_APPLICABLE),
        ('Venue', info['venue']),
        ('Region', info['region']),
        ('Resource prefix', _code_cell(info['resource_prefix'])),
        ('State machine', _code_cell(info['state_machine'])),
        ('CMR environment', info['cmr_environment']),
        ('Deployed bignbit version', _code_cell(info['deployed_version'])),
        ('Suite commit', commit_cell)
    ]
    if info['run_url']:
        rows.append(('Actions run', f'[{run_info.source.get("run_id")}]({info["run_url"]})'))

    lines = ['| | |', '| --- | --- |']
    lines.extend(f'| {label} | {value} |' for label, value in rows)
    return lines


def _failure_block(result: CaseResult) -> list[str]:
    """Render the <details> block for a case that did not pass."""
    if result.outcome == MISMATCHED:
        label = f'MISMATCH ({len(result.issues)} difference(s))'
        body = '\n'.join(result.issues)
    elif result.workflow_status == STATUS_SUCCEEDED:
        label = f'comparison {result.comparison or COMPARISON_ERROR}'
        body = result.error
    else:
        label = f'workflow {result.workflow_status}'
        body = result.error

    lines = [
        '<details>',
        f'<summary><code>{html.escape(result.case.short_name)}</code> / <code>{html.escape(result.case.granule_ur)}</code>: {label}</summary>',
        ''
    ]
    if result.execution_name:
        lines.extend([f'Execution: {_code_cell(result.execution_name)}', ''])
    lines.extend([_fenced(body or 'No further detail available'), '', '</details>', ''])
    return lines


def _results_table(results: list[CaseResult]) -> list[str]:
    """Render one table row per case."""
    lines = [
        '| Collection | Granule UR | Workflow | Comparison | Duration |',
        '| --- | --- | --- | --- | ---: |'
    ]
    for result in results:
        skipped = result.outcome == SKIPPED
        workflow = _emphasize(result.workflow_status, not skipped and result.workflow_status != STATUS_SUCCEEDED)
        comparison = _emphasize(result.comparison, result.outcome != PASSED) if result.comparison else NOT_APPLICABLE
        duration = NOT_APPLICABLE if skipped else format_duration(result.duration_seconds)
        lines.append(f'| {_code_cell(result.case.short_name)} | {_code_cell(result.case.granule_ur)} '
                     f'| {workflow} | {comparison} | {duration} |')
    return lines


def render_markdown(run_info: RunInfo, results: list[CaseResult]) -> str:
    """
    Render the report for a run that started.

    Parameters
    ----------
    run_info : RunInfo
        Metadata of the run
    results : list[CaseResult]
        One result per case

    Returns
    -------
    str
        The Markdown report. It is not redacted yet; write_report takes care of that.
    """
    counts = summarize(results)
    verdict = get_verdict(results, run_info.interrupted)
    ran = counts['total'] - counts['skipped']

    lines = [
        f'# bignbit regression report: {run_info.venue}',
        '',
        f'**Verdict: {verdict}.** {counts["passed"]} of {ran} case(s) run passed, {counts["failed"]} failed, '
        f'{counts["mismatched"]} mismatched and {counts["skipped"]} skipped.',
        '',
        '| Total | Passed | Failed | Mismatched | Skipped |',
        '| ---: | ---: | ---: | ---: | ---: |',
        f'| {counts["total"]} | {counts["passed"]} | {counts["failed"]} | {counts["mismatched"]} | {counts["skipped"]} |',
        '',
        'A case passes when its workflow execution succeeds and the browse image checksums in the CNM messages it '
        'produced match the reference. *Failed* counts cases whose workflow did not succeed (including timeouts and '
        'errors raised by the suite); *mismatched* counts cases whose workflow succeeded but whose results differ.',
        ''
    ]
    lines.extend(f'- {note}' for note in run_info.notes)
    if run_info.notes:
        lines.append('')

    lines.extend(['## Run details', ''])
    lines.extend(_details_table(run_info))
    lines.append('')

    problems = [result for result in results if result.outcome in (FAILED, MISMATCHED)]
    if problems:
        lines.extend([f'## Cases that did not pass ({len(problems)})', ''])
        for result in problems:
            lines.extend(_failure_block(result))

    lines.extend(['## Results', ''])
    lines.extend(_results_table(results))
    lines.append('')
    return '\n'.join(lines)


def render_error_markdown(run_info: RunInfo, message: str) -> str:
    """
    Render the report for a run that could not start.

    Parameters
    ----------
    run_info : RunInfo
        Whatever metadata was known when the run failed
    message : str
        Why the run could not start

    Returns
    -------
    str
        The Markdown report, not yet redacted
    """
    lines = [
        f'# bignbit regression report: {run_info.venue}',
        '',
        f'**Verdict: {VERDICT_ERROR}.** The regression run could not start, so no test cases were run.',
        '',
        _fenced(message),
        '',
        '## Run details',
        ''
    ]
    lines.extend(_details_table(run_info))
    lines.append('')
    return '\n'.join(lines)


def build_results(run_info: RunInfo, results: list[CaseResult], error: str = None) -> dict:
    """
    Build the machine-readable equivalent of the report.

    Returns
    -------
    dict
        ``run`` (metadata, verdict, exit code, summary counts and any startup error) and ``cases`` (one object per case)
    """
    run = run_info.to_dict()
    run['verdict'] = VERDICT_ERROR if error else get_verdict(results, run_info.interrupted)
    run['exit_code'] = EXIT_NOT_STARTED if error else exit_code(results, run_info.interrupted)
    run['summary'] = summarize(results)
    run['error'] = error
    return {'run': run, 'cases': [result.to_dict() for result in results]}


def write_report(report_dir, run_info: RunInfo, results: list[CaseResult], redactor: Redactor, error: str = None) -> Path:
    """
    Write ``report.md`` and ``results.json``, redacting AWS account IDs from both.

    Parameters
    ----------
    report_dir : str or Path
        Directory to write to; created if needed
    run_info : RunInfo
        Metadata of the run
    results : list[CaseResult]
        One result per case; empty when the run could not start
    redactor : Redactor
        Applied to the full content of both files
    error : str
        Why the run could not start, if it did not

    Returns
    -------
    Path
        Path of the Markdown report
    """
    report_dir = Path(report_dir)
    report_dir.mkdir(parents=True, exist_ok=True)

    markdown = render_error_markdown(run_info, error) if error else render_markdown(run_info, results)
    report_path = report_dir / REPORT_FILENAME
    report_path.write_text(redactor.redact(markdown), encoding='utf-8')

    results_json = json.dumps(build_results(run_info, results, error), indent=2)
    (report_dir / RESULTS_FILENAME).write_text(redactor.redact(results_json) + '\n', encoding='utf-8')
    return report_path
