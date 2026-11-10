"""Unit tests for the regression suite's result roll-up and report rendering"""
# pylint: disable=redefined-outer-name
import json
from datetime import datetime, timezone
from unittest import mock

import pytest

from regression import report
from regression.config import Deployment
from regression.manifest import RegressionCase
from regression.redact import PLACEHOLDER, Redactor
from regression.results import FAILED, MISMATCHED, PASSED, SKIPPED, CaseResult, exit_code, summarize

ACCOUNT_ID = '123456789012'
OTHER_ACCOUNT_ID = '999988887777'

GITHUB_ENVIRONMENT = {
    'GITHUB_SERVER_URL': 'https://github.com',
    'GITHUB_REPOSITORY': 'nasa-gibs/bignbit',
    'GITHUB_RUN_ID': '4242',
    'GITHUB_SHA': '3e4426c0a1b2c3d4e5f60718293a4b5c6d7e8f90'
}


def case(short_name, granule_ur=None, skip=''):
    """Build a test case"""
    return RegressionCase(short_name, 'POCLOUD', granule_ur or f'{short_name}-granule', 'UAT', skip)


def passed(short_name='PASSING_COLLECTION'):
    """A case whose workflow succeeded and matched"""
    return CaseResult(case(short_name), outcome=PASSED, workflow_status='SUCCEEDED', comparison='MATCH',
                      execution_name=f'reg-{short_name}-20261109T120000-abc', duration_seconds=75)


def mismatched():
    """A case whose workflow succeeded but whose checksums differ"""
    return CaseResult(case('MISMATCHED_COLLECTION'), outcome=MISMATCHED, workflow_status='SUCCEEDED', comparison='MISMATCH',
                      issues=['Checksum mismatch: MUR_sst_LL [granule.r00c00.png]', 'CNM file count: reference=2, current=3'],
                      execution_name='reg-MISMATCHED_COLLECTION-20261109T120000-abc', duration_seconds=3725)


def failed():
    """A case whose workflow failed with an error message that would break a table and leak an account ID"""
    error = (f'HarmonyError: job failed | see arn:aws:lambda:us-east-1:{OTHER_ACCOUNT_ID}:function:x\n'
             f'second line for account {ACCOUNT_ID} with ``` a fence and </details> in it')
    return CaseResult(case('FAILED_COLLECTION', 'granule|with|pipes'), outcome=FAILED, workflow_status='FAILED', error=error,
                      execution_name='reg-FAILED_COLLECTION-20261109T120000-abc', duration_seconds=12)


def timed_out():
    """A case that exceeded the suite's execution timeout"""
    return CaseResult(case('SLOW_COLLECTION'), outcome=FAILED, workflow_status='TIMEOUT',
                      error='The execution did not finish within 1800 seconds', duration_seconds=1800)


def skipped():
    """A case the manifest marks to be skipped"""
    return CaseResult(case('SKIPPED_COLLECTION', skip='X'), outcome=SKIPPED, workflow_status='SKIPPED')


@pytest.fixture
def run_info():
    """Metadata of a sandbox run started from GitHub Actions"""
    return report.RunInfo(
        venue='sandbox',
        started_at=datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc),
        deployment=Deployment(venue='sandbox', region='us-east-1', prefix='gitc-sandbox', cmr_environment='UAT'),
        state_machine_name='gitc-sandbox-BrowseImageWorkflow',
        deployed_version='0.10.1a0',
        duration_seconds=3725,
        max_parallel=8,
        source=report.source_info(GITHUB_ENVIRONMENT))


def test_summarize_and_exit_code():
    """Counts are by outcome and the exit code is 0 only when everything that ran passed"""
    results = [passed(), mismatched(), failed(), timed_out(), skipped()]

    assert summarize(results) == {'total': 5, 'passed': 1, 'failed': 2, 'mismatched': 1, 'skipped': 1}
    assert exit_code(results) == 1
    assert exit_code([passed(), skipped()]) == 0
    assert exit_code([passed(), mismatched()]) == 1
    assert exit_code([passed(), timed_out()]) == 1
    assert exit_code([passed()], interrupted=True) == 1


def test_format_duration():
    """Durations are shown in the largest sensible units"""
    assert report.format_duration(0.4) == '0s'
    assert report.format_duration(59.6) == '1m 00s'
    assert report.format_duration(125) == '2m 05s'
    assert report.format_duration(3725) == '1h 02m 05s'


def test_source_info_from_github_actions():
    """Inside GitHub Actions the commit and run are linked"""
    assert report.source_info(GITHUB_ENVIRONMENT) == {
        'commit': '3e4426c0a1b2c3d4e5f60718293a4b5c6d7e8f90',
        'commit_url': 'https://github.com/nasa-gibs/bignbit/commit/3e4426c0a1b2c3d4e5f60718293a4b5c6d7e8f90',
        'run_id': '4242',
        'run_url': 'https://github.com/nasa-gibs/bignbit/actions/runs/4242'
    }


def test_source_info_outside_github_actions(tmp_path):
    """Outside GitHub Actions only the commit is reported, from git, or unknown when git cannot tell"""
    completed = mock.Mock(stdout='abc123\n')
    with mock.patch.object(report.subprocess, 'run', return_value=completed) as run:
        assert report.source_info({}, tmp_path) == {'commit': 'abc123'}
    assert run.call_args.kwargs['cwd'] == tmp_path

    with mock.patch.object(report.subprocess, 'run', side_effect=OSError('no git')):
        assert report.source_info({'GITHUB_SHA': ''}, tmp_path) == {'commit': 'unknown'}

    assert report.source_info({'GITHUB_SHA': 'feedbeef'}, tmp_path) == {'commit': 'feedbeef'}


def test_render_markdown_passing_run(run_info):
    """A passing run has a PASSED verdict, the run metadata and no failure section"""
    markdown = report.render_markdown(run_info, [passed('A'), passed('B'), skipped()])

    lines = markdown.splitlines()
    assert lines[0] == '# bignbit regression report: sandbox'
    assert '**Verdict: PASSED.** 2 of 2 case(s) run passed, 0 failed, 0 mismatched and 1 skipped.' in lines
    assert '| 3 | 2 | 0 | 0 | 1 |' in lines
    assert '| Started | 2026-11-09 12:00:00 UTC |' in lines
    assert '| Duration | 1h 02m 05s |' in lines
    assert '| Maximum parallel cases | 8 |' in lines
    assert '| Venue | sandbox |' in lines
    assert '| Region | us-east-1 |' in lines
    assert '| Resource prefix | `gitc-sandbox` |' in lines
    assert '| State machine | `gitc-sandbox-BrowseImageWorkflow` |' in lines
    assert '| CMR environment | UAT |' in lines
    assert '| Deployed bignbit version | `0.10.1a0` |' in lines
    assert '| Suite commit | [`3e4426c0a1b2`](https://github.com/nasa-gibs/bignbit/commit/3e4426c0a1b2c3d4e5f60718293a4b5c6d7e8f90) |' in lines
    assert '| Actions run | [4242](https://github.com/nasa-gibs/bignbit/actions/runs/4242) |' in lines
    assert '| `A` | `A-granule` | SUCCEEDED | MATCH | 1m 15s |' in lines
    assert '| `SKIPPED_COLLECTION` | `SKIPPED_COLLECTION-granule` | SKIPPED | - | - |' in lines
    assert 'did not pass' not in markdown
    assert '<details>' not in markdown
    assert 'arn:aws' not in markdown


def test_render_markdown_failing_run(run_info):
    """Failures stand out in the table and each gets a details block with its mismatch lines or error"""
    results = [passed(), mismatched(), failed(), timed_out(), skipped()]

    markdown = report.render_markdown(run_info, results)
    lines = markdown.splitlines()

    assert '**Verdict: FAILED.** 1 of 4 case(s) run passed, 2 failed, 1 mismatched and 1 skipped.' in lines
    assert '| 5 | 1 | 2 | 1 | 1 |' in lines
    assert '## Cases that did not pass (3)' in lines
    assert '| `MISMATCHED_COLLECTION` | `MISMATCHED_COLLECTION-granule` | SUCCEEDED | **MISMATCH** | 1h 02m 05s |' in lines
    assert '| `SLOW_COLLECTION` | `SLOW_COLLECTION-granule` | **TIMEOUT** | - | 30m 00s |' in lines
    assert ('<summary><code>MISMATCHED_COLLECTION</code> / <code>MISMATCHED_COLLECTION-granule</code>: '
            'MISMATCH (2 difference(s))</summary>') in lines
    assert 'Checksum mismatch: MUR_sst_LL [granule.r00c00.png]' in lines
    assert '<summary><code>SLOW_COLLECTION</code> / <code>SLOW_COLLECTION-granule</code>: workflow TIMEOUT</summary>' in lines
    assert 'Execution: `reg-FAILED_COLLECTION-20261109T120000-abc`' in lines
    assert markdown.count('<details>') == markdown.count('\n</details>\n') == 3
    # The failure section comes before the full table so that problems are the first thing a reader sees
    assert lines.index('## Cases that did not pass (3)') < lines.index('## Results')


def test_error_text_cannot_break_the_table_or_the_details_block(run_info):
    """Pipes and newlines never reach a table cell unescaped and error text cannot close its own code fence"""
    markdown = report.render_markdown(run_info, [failed()])
    lines = markdown.splitlines()

    # Every table row has the same number of unescaped pipes as its header
    table = [line for line in lines if line.startswith('| ')]
    results_header = lines.index('| Collection | Granule UR | Workflow | Comparison | Duration |')
    assert lines[results_header + 2] == '| `FAILED_COLLECTION` | `granule\\|with\\|pipes` | **FAILED** | - | 12s |'
    for row in table:
        assert '\n' not in row
    assert all(line.replace('\\|', '').count('|') == 6 for line in lines[results_header:results_header + 3])

    # The error is inside a fence that is longer than the backtick run it contains
    opening = lines.index('````text')
    closing = lines.index('````', opening)
    body = lines[opening + 1:closing]
    assert body[0].startswith('HarmonyError: job failed | see arn:aws:lambda')
    assert '``` a fence and </details> in it' in body[1]
    assert lines[closing + 2] == '</details>'


def test_render_markdown_interrupted_run(run_info):
    """An interrupted run is never reported as passed"""
    run_info.interrupted = True
    run_info.notes.append('The run was interrupted after 1 of 52 case(s).')

    markdown = report.render_markdown(run_info, [passed()])

    assert '**Verdict: INTERRUPTED.**' in markdown
    assert '- The run was interrupted after 1 of 52 case(s).' in markdown.splitlines()


def test_render_error_markdown_before_deployment_is_known():
    """A run that could not start still names the venue and explains why"""
    run_info = report.RunInfo(venue='sit', started_at=datetime(2026, 11, 9, tzinfo=timezone.utc), source={'commit': 'abc123'})

    markdown = report.render_error_markdown(run_info, 'Unable to read tfvars file for venue sit')
    lines = markdown.splitlines()

    assert lines[0] == '# bignbit regression report: sit'
    assert '**Verdict: ERROR.** The regression run could not start, so no test cases were run.' in lines
    assert 'Unable to read tfvars file for venue sit' in lines
    assert '| Region | unknown |' in lines
    assert '| State machine | `unknown` |' in lines
    assert '| Suite commit | `abc123` |' in lines
    assert 'Actions run' not in markdown


def test_write_report_redacts_both_files(run_info, tmp_path):
    """Neither the Markdown report nor results.json may contain an account ID, known or not"""
    report_dir = tmp_path / 'nested' / 'report'
    results = [passed(), mismatched(), failed(), skipped()]

    report_path = report.write_report(report_dir, run_info, results, Redactor([ACCOUNT_ID]))

    assert report_path == report_dir / 'report.md'
    markdown = report_path.read_text(encoding='utf-8')
    results_text = (report_dir / 'results.json').read_text(encoding='utf-8')
    for text in (markdown, results_text):
        assert ACCOUNT_ID not in text
        assert OTHER_ACCOUNT_ID not in text
        assert f'arn:aws:lambda:us-east-1:{PLACEHOLDER}:function:x' in text
        assert f'account {PLACEHOLDER} with' in text

    data = json.loads(results_text)
    assert data['run'] == {
        'venue': 'sandbox',
        'region': 'us-east-1',
        'resource_prefix': 'gitc-sandbox',
        'state_machine': 'gitc-sandbox-BrowseImageWorkflow',
        'cmr_environment': 'UAT',
        'deployed_version': '0.10.1a0',
        'max_parallel': 8,
        'started_at': '2026-11-09T12:00:00+00:00',
        'duration_seconds': 3725,
        'commit': GITHUB_ENVIRONMENT['GITHUB_SHA'],
        'commit_url': f'https://github.com/nasa-gibs/bignbit/commit/{GITHUB_ENVIRONMENT["GITHUB_SHA"]}',
        'run_url': 'https://github.com/nasa-gibs/bignbit/actions/runs/4242',
        'notes': [],
        'verdict': 'FAILED',
        'exit_code': 1,
        'summary': {'total': 4, 'passed': 1, 'failed': 1, 'mismatched': 1, 'skipped': 1},
        'error': None
    }
    assert [entry['outcome'] for entry in data['cases']] == ['PASSED', 'MISMATCHED', 'FAILED', 'SKIPPED']
    assert data['cases'][1] == {
        'short_name': 'MISMATCHED_COLLECTION',
        'provider': 'POCLOUD',
        'granule_ur': 'MISMATCHED_COLLECTION-granule',
        'cmr_env': 'UAT',
        'outcome': 'MISMATCHED',
        'workflow_status': 'SUCCEEDED',
        'comparison': 'MISMATCH',
        'issues': ['Checksum mismatch: MUR_sst_LL [granule.r00c00.png]', 'CNM file count: reference=2, current=3'],
        'error': '',
        'execution_name': 'reg-MISMATCHED_COLLECTION-20261109T120000-abc',
        'duration_seconds': 3725
    }


def test_write_report_for_a_run_that_could_not_start(run_info, tmp_path):
    """The error report is redacted too and results.json records exit code 2"""
    error = f'StateMachineNotFound: nothing at arn:aws:states:us-east-1:{ACCOUNT_ID}:stateMachine:missing'

    report_path = report.write_report(tmp_path, run_info, [], Redactor(), error=error)

    markdown = report_path.read_text(encoding='utf-8')
    assert '**Verdict: ERROR.**' in markdown
    assert ACCOUNT_ID not in markdown
    data = json.loads((tmp_path / 'results.json').read_text(encoding='utf-8'))
    assert data['run']['verdict'] == 'ERROR'
    assert data['run']['exit_code'] == 2
    assert data['run']['error'] == f'StateMachineNotFound: nothing at arn:aws:states:us-east-1:{PLACEHOLDER}:stateMachine:missing'
    assert data['cases'] == []
