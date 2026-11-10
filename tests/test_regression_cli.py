"""Unit tests for the regression suite's command line interface and exit codes, run against moto"""
# pylint: disable=redefined-outer-name
import _thread
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest
from botocore.exceptions import NoCredentialsError
from moto import mock_aws

from regression_helpers import (ACCOUNT_ID, MUR_CASE, OPERA_CASE, OTHER_REGION, PREFIX, REGION, STATE_MACHINE_NAME,
                                create_state_machine, granule_cmr, list_executions, make_cnm,
                                set_fake_aws_credentials, upload_cnms, workflow_pause, write_cnm)
from regression import aws, cli, cmr, config, runner
from regression.manifest import RegressionCase
from regression.redact import PLACEHOLDER
from regression.results import CaseResult

SKIPPED_CASE = RegressionCase('SKIPPED_COLLECTION', 'POCLOUD', 'skipped-granule', 'UAT', 'X')
OPS_CASE = RegressionCase('OPS_ONLY_COLLECTION', 'POCLOUD', 'ops-granule', 'OPS')


@pytest.fixture
def env(monkeypatch, tmp_path):
    """
    A fake sandbox deployment in moto plus the files and arguments for running the CLI against it. The workflow
    finishes with whatever ``env.outcomes`` holds for a granule and CMR knows every granule.
    """
    set_fake_aws_credentials(monkeypatch)
    for name in ('GITHUB_SERVER_URL', 'GITHUB_REPOSITORY', 'GITHUB_RUN_ID', 'GITHUB_SHA', 'EDL_USER', 'EDL_PASS'):
        monkeypatch.delenv(name, raising=False)

    with mock_aws():
        outcomes = {}

        monkeypatch.setattr(aws, '_pause', workflow_pause(
            lambda execution_input: outcomes.get(execution_input['payload']['granules'][0]['granuleId']) or {'status': 'RUNNING'}))

        def get_granule(short_name, provider, granule_ur, cmr_env, token=None):  # pylint: disable=unused-argument
            return granule_cmr(RegressionCase(short_name, provider, granule_ur, cmr_env))

        test_cases = tmp_path / 'test_cases.csv'
        rows = [MUR_CASE, OPERA_CASE, SKIPPED_CASE, OPS_CASE]
        test_cases.write_text('short_name,provider,granule_ur,cmr_env,skip\n' + ''.join(
            f'{case.short_name},{case.provider},{case.granule_ur},{case.cmr_env},{case.skip}\n' for case in rows), encoding='utf-8')

        paths = SimpleNamespace(test_cases=test_cases, reference=tmp_path / 'reference', workdir=tmp_path / 'workdir',
                                report=tmp_path / 'report')
        arguments = ['run', '--test-cases', str(test_cases), '--reference-dir', str(paths.reference), '--workdir', str(paths.workdir),
                     '--report-dir', str(paths.report), '--poll-interval', '0.001']

        # No ~/.netrc, so CMR is searched anonymously unless a test provides credentials
        monkeypatch.setenv('HOME', str(tmp_path))
        with mock.patch.object(cmr, 'get_granule', side_effect=get_granule) as cmr_lookup, \
                mock.patch.object(cmr, 'get_user_token', return_value='cmr-token') as token_lookup:
            yield SimpleNamespace(paths=paths, arguments=arguments, outcomes=outcomes, cmr_lookup=cmr_lookup,
                                  token_lookup=token_lookup)


def passing(env, case, checksum='abc'):
    """Make a case pass: reference CNMs exist and the workflow succeeds with the same checksum"""
    cnm = make_cnm('SST_LL', f'{case.granule_ur}.r00c00.png', 'abc')
    write_cnm(env.paths.reference / case.short_name / case.granule_ur / case.cmr_env / 'cnm', 'reference', cnm)
    env.outcomes[case.granule_ur] = {'status': 'SUCCEEDED', 'output': upload_cnms(case, [dict(cnm, product={
        'files': [dict(cnm['product']['files'][0], checksum=checksum)]})])}


def read_report(env):
    """Return the Markdown report and the parsed results.json"""
    return ((env.paths.report / 'report.md').read_text(encoding='utf-8'),
            json.loads((env.paths.report / 'results.json').read_text(encoding='utf-8')))


def assert_no_account_id(directory):
    """Fail if any file written under a directory contains the account ID"""
    files = [path for path in directory.rglob('*') if path.is_file()]
    assert files
    for path in files:
        assert ACCOUNT_ID not in path.read_text(encoding='utf-8'), f'account ID found in {path}'


def test_run_exits_0_when_every_case_passes(env):
    """A full passing run: exit code 0, a PASSED report, and no account ID in anything written to disk"""
    state_machine_arn = create_state_machine()
    passing(env, MUR_CASE)
    passing(env, OPERA_CASE)

    assert cli.main(env.arguments) == 0

    markdown, results = read_report(env)
    assert markdown.startswith('# bignbit regression report: sandbox\n')
    assert '**Verdict: PASSED.** 2 of 2 case(s) run passed, 0 failed, 0 mismatched and 1 skipped.' in markdown
    assert f'| State machine | `{STATE_MACHINE_NAME}` |' in markdown
    assert '| Region | us-east-1 |' in markdown
    assert '| Deployed bignbit version | `unknown` |' in markdown
    assert '- 1 manifest row(s) for another CMR environment were not run.' in markdown
    assert 'arn:aws' not in markdown
    assert results['run']['exit_code'] == 0
    assert results['run']['summary'] == {'total': 3, 'passed': 2, 'failed': 0, 'mismatched': 0, 'skipped': 1}
    assert [(entry['short_name'], entry['outcome']) for entry in results['cases']] == [
        (MUR_CASE.short_name, 'PASSED'), (OPERA_CASE.short_name, 'PASSED'), (SKIPPED_CASE.short_name, 'SKIPPED')]
    assert len(list_executions(state_machine_arn)) == 2
    assert_no_account_id(env.paths.report)
    assert_no_account_id(env.paths.workdir)
    output = (env.paths.workdir / MUR_CASE.short_name / MUR_CASE.granule_ur / 'UAT' / 'output.json').read_text(encoding='utf-8')
    assert f'arn:aws:lambda:{REGION}:{PLACEHOLDER}:function' in output


def test_run_exits_1_and_keeps_going_when_cases_fail(env):
    """A failed workflow and a mismatch give exit code 1; both are reported and the account ID is redacted"""
    create_state_machine()
    passing(env, OPERA_CASE, checksum='CHANGED')
    env.outcomes[MUR_CASE.granule_ur] = {
        'status': 'FAILED', 'error': 'HarmonyError',
        'cause': json.dumps({'errorMessage': f'denied for arn:aws:iam::{ACCOUNT_ID}:role/{PREFIX}-bignbit | account {ACCOUNT_ID}'})}

    assert cli.main(env.arguments) == 1

    markdown, results = read_report(env)
    assert '**Verdict: FAILED.** 0 of 2 case(s) run passed, 1 failed, 1 mismatched and 1 skipped.' in markdown
    assert f'HarmonyError: denied for arn:aws:iam::{PLACEHOLDER}:role/{PREFIX}-bignbit | account {PLACEHOLDER}' in markdown
    assert f'Checksum mismatch: SST_LL [{OPERA_CASE.granule_ur}.r00c00.png]' in markdown
    assert results['run']['exit_code'] == 1
    assert [entry['outcome'] for entry in results['cases']] == ['FAILED', 'MISMATCHED', 'SKIPPED']
    assert_no_account_id(env.paths.report)
    assert_no_account_id(env.paths.workdir)


def test_run_exits_1_on_timeout(env):
    """A case that exceeds --execution-timeout is reported as TIMEOUT"""
    create_state_machine()

    assert cli.main(env.arguments + ['--only', MUR_CASE.short_name, '--execution-timeout', '0.01']) == 1

    markdown, results = read_report(env)
    assert '| **TIMEOUT** | - |' in markdown
    assert f'- Limited to collection(s): `{MUR_CASE.short_name}`.' in markdown
    assert [(entry['short_name'], entry['workflow_status']) for entry in results['cases']] == [(MUR_CASE.short_name, 'TIMEOUT')]


def test_run_exits_1_when_interrupted(env, monkeypatch):
    """An interrupted run still writes a report of the cases that completed, and never exits 0"""
    create_state_machine()
    skipped = runner.run_case(SKIPPED_CASE, None)

    def run_case(case, context):
        if case.skipped:
            return skipped
        if case.short_name == OPERA_CASE.short_name:
            _thread.interrupt_main()
            assert context.stop_event.wait(5), 'the stop event was never set'
            raise aws.ExecutionInterrupted('stopped')
        return CaseResult(case=case, outcome='PASSED', workflow_status='SUCCEEDED', comparison='MATCH')

    monkeypatch.setattr(runner, 'run_case', run_case)

    assert cli.main(env.arguments + ['--max-parallel', '1']) == 1

    markdown, results = read_report(env)
    assert '**Verdict: INTERRUPTED.**' in markdown
    assert '- The run was interrupted after 2 of 3 case(s).' in markdown
    assert results['run']['exit_code'] == 1
    assert [(entry['short_name'], entry['outcome']) for entry in results['cases']] == [
        (MUR_CASE.short_name, 'PASSED'), (SKIPPED_CASE.short_name, 'SKIPPED')]


def test_run_reports_the_parallelism(env):
    """The report and results.json say how many cases were allowed to run at once"""
    create_state_machine()
    passing(env, MUR_CASE)
    passing(env, OPERA_CASE)

    assert cli.main(env.arguments + ['--max-parallel', '2']) == 0

    markdown, results = read_report(env)
    assert '| Maximum parallel cases | 2 |' in markdown
    assert results['run']['max_parallel'] == 2
    assert [entry['outcome'] for entry in results['cases']] == ['PASSED', 'PASSED', 'SKIPPED']


def test_run_defaults_to_the_default_parallelism(env):
    """Without --max-parallel the suite default is used and reported"""
    create_state_machine()
    passing(env, MUR_CASE)

    assert cli.main(env.arguments + ['--only', MUR_CASE.short_name]) == 0

    assert read_report(env)[1]['run']['max_parallel'] == runner.DEFAULT_MAX_PARALLEL


def test_run_uses_venue_region_and_workspace(env):
    """The state machine is looked up by the venue's prefix plus workspace, in the venue's region"""
    state_machine_arn = create_state_machine('podaac-sit-svc-issue-193-BrowseImageWorkflow', region=OTHER_REGION)
    create_state_machine('podaac-sit-svc-BrowseImageWorkflow', region=OTHER_REGION)
    create_state_machine('podaac-sit-svc-issue-193-BrowseImageWorkflow', region=REGION)
    passing(env, MUR_CASE)
    cnm = make_cnm('SST_LL', f'{MUR_CASE.granule_ur}.r00c00.png', 'abc')
    env.outcomes[MUR_CASE.granule_ur] = {
        'status': 'SUCCEEDED',
        'output': upload_cnms(MUR_CASE, [cnm], bucket='podaac-sit-svc-issue-193-internal', region=OTHER_REGION)}

    exit_status = cli.main(env.arguments + ['--venue', 'SIT', '--workspace', 'issue-193', '--only', MUR_CASE.short_name])

    assert exit_status == 0
    assert len(list_executions(state_machine_arn, region=OTHER_REGION)) == 1
    markdown, results = read_report(env)
    assert markdown.startswith('# bignbit regression report: sit\n')
    assert results['run']['region'] == 'us-west-2'
    assert results['run']['resource_prefix'] == 'podaac-sit-svc-issue-193'
    assert results['run']['state_machine'] == 'podaac-sit-svc-issue-193-BrowseImageWorkflow'
    input_message = json.loads((env.paths.workdir / MUR_CASE.short_name / MUR_CASE.granule_ur / 'UAT' / 'input.json').read_text(encoding='utf-8'))
    assert input_message['meta']['stack'] == 'podaac-sit-svc-issue-193'


def test_run_with_state_machine_arn_and_prefix(env):
    """--state-machine-arn skips the lookup; its name, never the ARN, is reported"""
    state_machine_arn = create_state_machine('some-custom-machine')
    passing(env, MUR_CASE)

    exit_status = cli.main(env.arguments + ['--state-machine-arn', state_machine_arn, '--prefix', 'custom-prefix', '--only', MUR_CASE.short_name])

    assert exit_status == 0
    markdown, results = read_report(env)
    assert results['run']['state_machine'] == 'some-custom-machine'
    assert results['run']['resource_prefix'] == 'custom-prefix'
    assert 'arn:aws' not in markdown
    assert len(list_executions(state_machine_arn)) == 1


def test_run_reads_the_deployed_version(env):
    """The deployed version is read from the Version tag of the deployment's get_dataset_configuration Lambda"""
    create_state_machine()
    passing(env, MUR_CASE)

    with mock.patch.object(aws, 'get_deployed_version', return_value='0.10.1a0') as get_deployed_version:
        assert cli.main(env.arguments + ['--only', MUR_CASE.short_name]) == 0

    assert get_deployed_version.call_args.args[1] == f'{PREFIX}-bignbit-get_dataset_configuration'
    assert '| Deployed bignbit version | `0.10.1a0` |' in read_report(env)[0]


def test_run_exits_2_when_the_state_machine_is_missing(env):
    """No state machine means the run cannot start: exit code 2 and an error report without ARNs"""
    create_state_machine('some-other-machine')

    assert cli.main(env.arguments) == 2

    markdown, results = read_report(env)
    assert markdown.startswith('# bignbit regression report: sandbox\n')
    assert '**Verdict: ERROR.** The regression run could not start, so no test cases were run.' in markdown
    assert f"StateMachineNotFound: State machine '{STATE_MACHINE_NAME}' not found in region us-east-1" in markdown
    assert results['run']['exit_code'] == 2
    assert results['run']['verdict'] == 'ERROR'
    assert results['cases'] == []
    assert not env.paths.workdir.exists()
    assert_no_account_id(env.paths.report)


def test_run_exits_2_without_credentials(env):
    """Missing credentials are a startup error, not 52 failed cases"""
    create_state_machine()

    with mock.patch.object(aws, 'get_account_id', side_effect=NoCredentialsError()):
        assert cli.main(env.arguments) == 2

    markdown, results = read_report(env)
    assert 'NoCredentialsError: Unable to locate credentials' in markdown
    assert results['run']['exit_code'] == 2


def test_run_searches_cmr_anonymously_without_credentials(env):
    """Without Earthdata Login credentials no token is requested and the cases still run"""
    create_state_machine()
    passing(env, MUR_CASE)
    passing(env, OPERA_CASE)

    assert cli.main(env.arguments) == 0

    env.token_lookup.assert_not_called()
    assert {call.kwargs['token'] for call in env.cmr_lookup.call_args_list} == {None}


def test_run_gets_one_cmr_token_for_all_cases(env, monkeypatch):
    """With credentials one token is fetched at the start of the run and used for every CMR search"""
    monkeypatch.setenv('EDL_USER', 'edl-user')
    monkeypatch.setenv('EDL_PASS', 'edl-pass')
    create_state_machine()
    passing(env, MUR_CASE)
    passing(env, OPERA_CASE)

    assert cli.main(env.arguments) == 0

    env.token_lookup.assert_called_once_with('edl-user', 'edl-pass', 'UAT')
    assert [call.kwargs['token'] for call in env.cmr_lookup.call_args_list] == ['cmr-token', 'cmr-token']


def test_run_exits_2_when_no_cmr_token_can_be_obtained(env, monkeypatch, capsys):
    """Credentials that do not work stop the run, and neither the password nor the token reaches the report or log"""
    monkeypatch.setenv('EDL_USER', 'edl-user')
    monkeypatch.setenv('EDL_PASS', 'edl-pass')
    create_state_machine()
    env.token_lookup.side_effect = cmr.CmrError('Unable to get an Earthdata Login token for CMR UAT as user edl-user: '
                                                'request failed with HTTP 401 (HTTPError), password edl-pass')

    assert cli.main(env.arguments) == 2

    markdown, results = read_report(env)
    assert 'Unable to get an Earthdata Login token for CMR UAT as user edl-user' in markdown
    stderr = capsys.readouterr().err
    assert 'password ***' in stderr
    assert 'edl-pass' not in markdown and 'edl-pass' not in stderr
    assert results['run']['exit_code'] == 2
    env.cmr_lookup.assert_not_called()


def test_run_exits_2_when_tfvars_are_missing(env, monkeypatch, tmp_path):
    """A venue whose tfvars file is missing cannot be resolved"""
    monkeypatch.setattr(config, 'TFVARS_DIR', tmp_path / 'no-tfvars-here')

    assert cli.main(env.arguments) == 2

    markdown, results = read_report(env)
    assert 'Unable to read tfvars file for venue sandbox' in markdown
    assert '| Region | unknown |' in markdown
    assert results['run']['error'].startswith('Unable to read tfvars file for venue sandbox')


@pytest.mark.parametrize('extra_arguments, message', [
    (['--only', 'NOT_IN_MANIFEST'], 'Collection(s) not found in the test case manifest: NOT_IN_MANIFEST'),
    (['--only', OPS_CASE.short_name], 'No test cases to run'),
    (['--test-cases', '/nonexistent/test_cases.csv'], 'Unable to read test case manifest'),
    (['--state-machine-arn', 'gitc-sandbox-BrowseImageWorkflow'], '--state-machine-arn: Not a Step Functions state machine ARN'),
])
def test_run_exits_2_on_unusable_selection(env, extra_arguments, message):
    """Nothing to run, or an argument that cannot be honoured, is exit code 2 with the reason in the report"""
    create_state_machine()

    assert cli.main(env.arguments + extra_arguments) == 2

    markdown, results = read_report(env)
    assert message in markdown
    assert results['run']['exit_code'] == 2


@pytest.mark.parametrize('extra_arguments, message', [
    (['--venue', 'ops'], "argument --venue: invalid choice: 'ops'"),
    (['--max-parallel', '0'], "argument --max-parallel: '0' is not greater than zero"),
    (['--max-parallel', '-2'], "argument --max-parallel: '-2' is not greater than zero"),
    (['--max-parallel', 'many'], "argument --max-parallel: 'many' is not a whole number"),
    (['--max-parallel', '2.5'], "argument --max-parallel: '2.5' is not a whole number"),
    (['--poll-interval', '0'], "argument --poll-interval: '0' is not greater than zero"),
    (['--execution-timeout', 'soon'], "argument --execution-timeout: 'soon' is not a number"),
    (['--no-such-option'], 'unrecognized arguments: --no-such-option'),
])
def test_run_exits_2_on_bad_arguments(env, capsys, extra_arguments, message):
    """Bad arguments exit 2 and still leave an error report in the requested report directory"""
    assert cli.main(env.arguments + extra_arguments) == 2

    assert message in capsys.readouterr().err
    markdown, results = read_report(env)
    assert '**Verdict: ERROR.**' in markdown
    assert message in markdown
    assert results['run']['exit_code'] == 2
    assert not env.paths.workdir.exists()


def test_bad_arguments_without_a_command_write_no_report(env, capsys):
    """Without the run command there is no report to write"""
    assert cli.main([]) == 2
    assert cli.main(['--report-dir', str(env.paths.report)]) == 2

    assert 'usage: python -m regression' in capsys.readouterr().err
    assert not env.paths.report.exists()


def test_report_dir_is_found_in_unparseable_arguments():
    """The report directory is recovered from both spellings of the option, else the default is used"""
    # pylint: disable=protected-access
    assert cli._peek_report_dir(['run', '--report-dir', 'some/dir', '--bogus']) == Path('some/dir')
    assert cli._peek_report_dir(['run', '--report-dir=other/dir']) == Path('other/dir')
    assert cli._peek_report_dir(['run', '--bogus']) == config.DEFAULT_REPORT_DIR


def test_venue_argument_is_case_insensitive():
    """The pipeline's upper case venue names are accepted"""
    assert cli.parse_args(['run', '--venue', 'SANDBOX']).venue == 'sandbox'
    assert cli.parse_args(['run']).venue == 'sandbox'


def test_run_defaults():
    """Defaults match the documented command line contract and resolve inside the repository"""
    args = cli.parse_args(['run'])

    assert args.test_cases == config.REPO_ROOT / 'regression' / 'test_cases.csv'
    assert args.reference_dir == config.REPO_ROOT / 'regression' / 'reference_data'
    assert args.workdir == config.REPO_ROOT / 'build' / 'regression' / 'workdir'
    assert args.report_dir == config.REPO_ROOT / 'build' / 'regression' / 'report'
    assert (args.poll_interval, args.execution_timeout, args.download_browse) == (10, 1800, False)
    assert args.max_parallel == runner.DEFAULT_MAX_PARALLEL == 8
    assert cli.parse_args(['run', '--max-parallel', '3']).max_parallel == 3
    assert (args.region, args.prefix, args.workspace, args.state_machine_arn, args.aws_profile, args.only) == (None,) * 6


def test_update_reference_command(env):
    """update-reference promotes the cnm directory of the named collections and nothing else"""
    create_state_machine()
    passing(env, MUR_CASE, checksum='NEW')
    assert cli.main(env.arguments + ['--only', MUR_CASE.short_name]) == 1

    exit_status = cli.main(['update-reference', '--workdir', str(env.paths.workdir), '--reference-dir', str(env.paths.reference),
                            MUR_CASE.short_name])

    assert exit_status == 0
    reference_dir = env.paths.reference / MUR_CASE.short_name / MUR_CASE.granule_ur / MUR_CASE.cmr_env
    assert [path.name for path in reference_dir.iterdir()] == ['cnm']
    assert '"NEW"' in next((reference_dir / 'cnm').iterdir()).read_text(encoding='utf-8')
    # The promoted results are now the baseline, so the same workflow results pass
    assert cli.main(env.arguments + ['--only', MUR_CASE.short_name]) == 0


def test_update_reference_all_and_failures(env):
    """--all covers every collection in the workdir; unknown or unsafe names and an empty workdir exit 1"""
    common = ['update-reference', '--workdir', str(env.paths.workdir), '--reference-dir', str(env.paths.reference)]
    assert cli.main(common + ['--all']) == 1

    write_cnm(env.paths.workdir / 'A' / 'granule-a' / 'UAT' / 'cnm', 'a', make_cnm('A_LL', 'a.png', 'abc'))
    write_cnm(env.paths.workdir / 'B' / 'granule-b' / 'UAT' / 'cnm', 'b', make_cnm('B_LL', 'b.png', 'abc'))

    assert cli.main(common + ['A', 'MISSING']) == 1
    assert cli.main(common + ['../A']) == 1
    assert sorted(path.name for path in env.paths.reference.iterdir()) == ['A']
    assert cli.main(common + ['--all']) == 0
    assert sorted(path.name for path in env.paths.reference.iterdir()) == ['A', 'B']


def test_update_reference_requires_names_or_all(env, capsys):
    """Either collection names or --all must be given, not both and not neither"""
    assert cli.main(['update-reference']) == 2
    assert cli.main(['update-reference', '--all', 'A']) == 2

    assert 'give either one or more SHORT_NAMEs or --all' in capsys.readouterr().err
    assert not env.paths.report.exists()


def test_module_entry_point():
    """``python -m regression`` from the repository root reaches the command line interface"""
    completed = subprocess.run([sys.executable, '-m', 'regression', 'update-reference'], cwd=config.REPO_ROOT,
                               capture_output=True, text=True, check=False, timeout=120)

    assert completed.returncode == 2
    assert 'usage: python -m regression update-reference' in completed.stderr
