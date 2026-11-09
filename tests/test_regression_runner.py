"""Unit tests for the regression suite's run loop, run against moto"""
# pylint: disable=redefined-outer-name
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

import boto3
import pytest
from moto import mock_aws

from regression_helpers import (ACCOUNT_ID, MUR_CASE, OPERA_CASE, PREFIX, REGION, create_state_machine, finish_running_executions,
                                granule_cmr, list_executions, make_cnm, set_fake_aws_credentials, upload_cnms, write_cnm)
from regression import aws, cmr, runner
from regression.config import Deployment
from regression.manifest import RegressionCase
from regression.redact import PLACEHOLDER, Redactor

MODIS_CASE = RegressionCase('MODIS_AQUA_L3_SST_THERMAL_DAILY_4KM_DAYTIME_V2019.0', 'POCLOUD',
                            'AQUA_MODIS.20260125.L3m.DAY.SST.sst.4km', 'UAT')


@pytest.fixture
def stack(monkeypatch, tmp_path):
    """
    A fake deployment in moto: the state machine, a run context pointing at it, a CMR that knows every granule and
    a workflow that finishes with whatever ``stack.outcomes`` holds for the granule (and keeps running otherwise).
    """
    set_fake_aws_credentials(monkeypatch)
    with mock_aws():
        outcomes = {}

        def workflow_finishes(_seconds):
            def outcome_for(execution_input):
                granule_id = execution_input['payload']['granules'][0]['granuleId']
                return outcomes.get(granule_id) or {'status': 'RUNNING'}
            finish_running_executions(outcome_for)

        monkeypatch.setattr(aws.time, 'sleep', workflow_finishes)

        def get_granule(short_name, provider, granule_ur, cmr_env):
            return granule_cmr(RegressionCase(short_name, provider, granule_ur, cmr_env))

        with mock.patch.object(cmr, 'get_granule', side_effect=get_granule) as cmr_lookup:
            state_machine_arn = create_state_machine()
            context = runner.RunContext(
                deployment=Deployment(venue='sandbox', region=REGION, prefix=PREFIX, cmr_environment='UAT'),
                state_machine_arn=state_machine_arn,
                sfn_client=boto3.client('stepfunctions', region_name=REGION),
                s3_client=boto3.client('s3', region_name=REGION),
                redactor=Redactor([ACCOUNT_ID]),
                workdir=tmp_path / 'workdir',
                reference_dir=tmp_path / 'reference',
                poll_interval=0.001,
                execution_timeout=30)
            yield SimpleNamespace(context=context, outcomes=outcomes, cmr_lookup=cmr_lookup, state_machine_arn=state_machine_arn)


def add_reference(stack, case, checksums):
    """Write reference CNMs for a case, one per tile"""
    reference_cnm_dir = stack.context.reference_dir / case.short_name / case.granule_ur / 'cnm'
    for tile_id, checksum in checksums.items():
        write_cnm(reference_cnm_dir, f'{case.granule_ur}.{tile_id}', make_cnm('SST_LL', f'{case.granule_ur}.{tile_id}.png', checksum))


def succeed(stack, case, checksums):
    """Make the workflow succeed for a case, archiving one CNM per tile"""
    cnms = [make_cnm('SST_LL', f'{case.granule_ur}.{tile_id}.png', checksum) for tile_id, checksum in checksums.items()]
    stack.outcomes[case.granule_ur] = {'status': 'SUCCEEDED', 'output': upload_cnms(case, cnms)}


def test_run_case_passes(stack):
    """A succeeded workflow whose CNM checksums match the reference passes"""
    add_reference(stack, MUR_CASE, {'r00c00': 'abc', 'r00c01': 'def'})
    succeed(stack, MUR_CASE, {'r00c00': 'abc', 'r00c01': 'def'})

    result = runner.run_case(MUR_CASE, stack.context)

    assert (result.outcome, result.workflow_status, result.comparison) == ('PASSED', 'SUCCEEDED', 'MATCH')
    assert result.issues == []
    assert result.error == ''
    assert result.duration_seconds > 0
    stack.cmr_lookup.assert_called_once_with(MUR_CASE.short_name, 'POCLOUD', MUR_CASE.granule_ur, 'UAT')

    executions = list_executions(stack.state_machine_arn)
    assert [execution['name'] for execution in executions] == [result.execution_name]
    assert result.execution_name.startswith('reg-MUR-JPL-L4-GLOB-v4_1-')


def test_run_case_writes_redacted_workdir(stack):
    """The workdir holds the input, the output and the CNMs, none of them containing the account ID"""
    add_reference(stack, MUR_CASE, {'r00c00': 'abc'})
    succeed(stack, MUR_CASE, {'r00c00': 'abc'})
    workdir = runner.case_workdir(stack.context.workdir, MUR_CASE)
    (workdir / 'cnm').mkdir(parents=True)
    (workdir / 'cnm' / 'stale-from-previous-run.cnm.json').write_text('{}', encoding='utf-8')

    runner.run_case(MUR_CASE, stack.context)

    assert workdir == stack.context.workdir / MUR_CASE.short_name / MUR_CASE.granule_ur / 'UAT'
    assert sorted(path.name for path in workdir.iterdir()) == ['cnm', 'input.json', 'output.json']
    assert len(list((workdir / 'cnm').iterdir())) == 1
    input_message = json.loads((workdir / 'input.json').read_text(encoding='utf-8'))
    assert input_message['meta']['stack'] == PREFIX
    assert input_message['payload']['granules'][0]['granuleId'] == MUR_CASE.granule_ur
    output = json.loads((workdir / 'output.json').read_text(encoding='utf-8'))
    assert output['meta']['workflow_tasks']['0']['arn'] == f'arn:aws:lambda:{REGION}:{PLACEHOLDER}:function:{PREFIX}-bignbit-send_to_gitc'
    for path in workdir.rglob('*'):
        if path.is_file():
            assert ACCOUNT_ID not in path.read_text(encoding='utf-8'), path


def test_run_case_starts_the_workflow_with_the_opera_message(stack):
    """OPERA cases are started with their browse file in the deployment's private bucket"""
    add_reference(stack, OPERA_CASE, {'003002': 'abc'})
    succeed(stack, OPERA_CASE, {'003002': 'abc'})

    result = runner.run_case(OPERA_CASE, stack.context)

    execution_arn = list_executions(stack.state_machine_arn)[0]['executionArn']
    started_with = json.loads(stack.context.sfn_client.describe_execution(executionArn=execution_arn)['input'])
    granule = started_with['payload']['granules'][0]
    assert granule['provider'] == 'opera'
    assert granule['dataType'] == OPERA_CASE.short_name
    assert granule['files'][0]['bucket'] == f'{PREFIX}-private'
    assert granule['files'][0]['fileName'] == f'{OPERA_CASE.granule_ur}_BROWSE.tif'
    assert result.outcome == 'PASSED'


def test_run_case_mismatch(stack):
    """A succeeded workflow whose checksums differ is a mismatch listing each difference"""
    add_reference(stack, MUR_CASE, {'r00c00': 'abc', 'r00c01': 'def'})
    succeed(stack, MUR_CASE, {'r00c00': 'abc', 'r00c01': 'CHANGED'})

    result = runner.run_case(MUR_CASE, stack.context)

    assert (result.outcome, result.workflow_status, result.comparison) == ('MISMATCHED', 'SUCCEEDED', 'MISMATCH')
    assert result.issues == [f'Checksum mismatch: SST_LL [{MUR_CASE.granule_ur}.r00c01.png]']


def test_run_case_without_reference_data(stack):
    """A case without reference data cannot pass"""
    succeed(stack, MUR_CASE, {'r00c00': 'abc'})

    result = runner.run_case(MUR_CASE, stack.context)

    assert (result.outcome, result.comparison, result.issues) == ('MISMATCHED', 'MISMATCH', ['No reference data found for comparison'])


def test_run_case_workflow_failed(stack):
    """A failed workflow is reported with the Lambda's error message and nothing is downloaded or compared"""
    add_reference(stack, MUR_CASE, {'r00c00': 'abc'})
    cause = json.dumps({'errorMessage': f'no config in arn:aws:s3:::{PREFIX}-internal for {ACCOUNT_ID}', 'errorType': 'X', 'stackTrace': []})
    stack.outcomes[MUR_CASE.granule_ur] = {'status': 'FAILED', 'error': 'MissingDatasetConfiguration', 'cause': cause}

    result = runner.run_case(MUR_CASE, stack.context)

    assert (result.outcome, result.workflow_status, result.comparison) == ('FAILED', 'FAILED', '')
    assert result.error == f'MissingDatasetConfiguration: no config in arn:aws:s3:::{PREFIX}-internal for {ACCOUNT_ID}'
    workdir = runner.case_workdir(stack.context.workdir, MUR_CASE)
    assert sorted(path.name for path in workdir.iterdir()) == ['error.json', 'input.json']
    error_file = (workdir / 'error.json').read_text(encoding='utf-8')
    assert ACCOUNT_ID not in error_file
    assert json.loads(error_file)['error'] == 'MissingDatasetConfiguration'


def test_run_case_timeout(stack):
    """A workflow that never finishes is reported as TIMEOUT once the execution timeout passes"""
    context = replace(stack.context, execution_timeout=0.01)

    result = runner.run_case(MUR_CASE, context)

    assert (result.outcome, result.workflow_status, result.comparison) == ('FAILED', 'TIMEOUT', '')
    assert result.error == 'The execution did not finish within 0.01 seconds'
    assert [execution['status'] for execution in list_executions(stack.state_machine_arn)] == ['ABORTED']


def test_run_case_cmr_error(stack):
    """A granule that cannot be found in CMR fails the case before anything is started"""
    stack.cmr_lookup.side_effect = cmr.CmrError('Expected 1 granule with UR x, found 0')

    result = runner.run_case(MUR_CASE, stack.context)

    assert (result.outcome, result.workflow_status, result.comparison) == ('FAILED', 'ERROR', '')
    assert result.error == 'CmrError: Expected 1 granule with UR x, found 0'
    assert result.execution_name == ''
    assert list_executions(stack.state_machine_arn) == []
    error_file = runner.case_workdir(stack.context.workdir, MUR_CASE) / 'error.json'
    assert json.loads(error_file.read_text(encoding='utf-8')) == {
        'status': 'ERROR', 'error': 'CmrError', 'cause': 'Expected 1 granule with UR x, found 0'}


def test_run_case_cnm_download_error(stack):
    """Results that cannot be downloaded after a succeeded workflow are a comparison error, not a pass or a mismatch"""
    add_reference(stack, MUR_CASE, {'r00c00': 'abc'})
    succeed(stack, MUR_CASE, {'r00c00': 'abc'})
    stack.outcomes[MUR_CASE.granule_ur]['output']['payload']['pobit'][0]['cnm_key'] = 'bignbit-cnm-output/missing.cnm.json'

    result = runner.run_case(MUR_CASE, stack.context)

    assert (result.outcome, result.workflow_status, result.comparison) == ('FAILED', 'SUCCEEDED', 'ERROR')
    assert result.error.startswith(f'DownloadError: Failed to download CNM s3://{PREFIX}-internal/bignbit-cnm-output/missing.cnm.json')


def test_run_case_skipped(stack):
    """A case marked to be skipped is reported without touching CMR or AWS"""
    result = runner.run_case(replace(MUR_CASE, skip='X'), stack.context)

    assert (result.outcome, result.workflow_status, result.comparison) == ('SKIPPED', 'SKIPPED', '')
    stack.cmr_lookup.assert_not_called()
    assert list_executions(stack.state_machine_arn) == []
    assert not stack.context.workdir.exists()


def test_run_case_downloads_browse_images_only_when_asked(stack):
    """Browse images are not downloaded unless --download-browse is given"""
    add_reference(stack, MUR_CASE, {'r00c00': 'abc'})
    succeed(stack, MUR_CASE, {'r00c00': 'abc'})
    file_name = f'{MUR_CASE.granule_ur}.r00c00.png'
    stack.context.s3_client.create_bucket(Bucket=f'{PREFIX}-bignbit-staging')
    stack.context.s3_client.put_object(Bucket=f'{PREFIX}-bignbit-staging', Key=f'bignbit-harmony-output/{file_name}', Body=b'png')
    workdir = runner.case_workdir(stack.context.workdir, MUR_CASE)

    assert runner.run_case(MUR_CASE, stack.context).outcome == 'PASSED'
    assert not (workdir / 'files').exists()

    succeed(stack, MUR_CASE, {'r00c00': 'abc'})
    assert runner.run_case(MUR_CASE, replace(stack.context, download_browse=True)).outcome == 'PASSED'
    assert (workdir / 'files' / file_name).read_bytes() == b'png'


def test_run_cases_continues_after_failures(stack):
    """Cases run in order and a failing, erroring or skipped case never stops the ones after it"""
    broken = RegressionCase('BROKEN_COLLECTION', 'POCLOUD', 'broken-granule', 'UAT')
    skipped = RegressionCase('SKIPPED_COLLECTION', 'POCLOUD', 'skipped-granule', 'UAT', 'X')
    cases = [broken, MUR_CASE, skipped, MODIS_CASE, OPERA_CASE]
    known_granule = stack.cmr_lookup.side_effect

    def get_granule(short_name, provider, granule_ur, cmr_env):
        if short_name == broken.short_name:
            raise ConnectionError('CMR is unreachable')
        return known_granule(short_name, provider, granule_ur, cmr_env)

    stack.cmr_lookup.side_effect = get_granule
    stack.outcomes[MUR_CASE.granule_ur] = {'status': 'FAILED', 'error': 'HarmonyError', 'cause': 'job failed'}
    add_reference(stack, MODIS_CASE, {'r00c00': 'abc'})
    succeed(stack, MODIS_CASE, {'r00c00': 'DIFFERENT'})
    add_reference(stack, OPERA_CASE, {'r00c00': 'abc'})
    succeed(stack, OPERA_CASE, {'r00c00': 'abc'})

    results, interrupted = runner.run_cases(cases, stack.context)

    assert interrupted is False
    assert [result.case for result in results] == cases
    assert [result.outcome for result in results] == ['FAILED', 'FAILED', 'SKIPPED', 'MISMATCHED', 'PASSED']
    assert [result.workflow_status for result in results] == ['ERROR', 'FAILED', 'SKIPPED', 'SUCCEEDED', 'SUCCEEDED']
    assert results[0].error == 'ConnectionError: CMR is unreachable'
    assert results[1].error == 'HarmonyError: job failed'
    assert len(list_executions(stack.state_machine_arn)) == 3


def test_run_cases_interrupted(stack):
    """An interrupt stops the run but keeps the results gathered so far"""
    add_reference(stack, MUR_CASE, {'r00c00': 'abc'})
    succeed(stack, MUR_CASE, {'r00c00': 'abc'})
    lookups = []

    def get_granule(short_name, provider, granule_ur, cmr_env):
        lookups.append(short_name)
        if len(lookups) == 2:
            raise KeyboardInterrupt()
        return granule_cmr(RegressionCase(short_name, provider, granule_ur, cmr_env))

    stack.cmr_lookup.side_effect = get_granule

    results, interrupted = runner.run_cases([MUR_CASE, MODIS_CASE, OPERA_CASE], stack.context)

    assert interrupted is True
    assert [(result.case, result.outcome) for result in results] == [(MUR_CASE, 'PASSED')]
    assert lookups == [MUR_CASE.short_name, MODIS_CASE.short_name]


@pytest.mark.parametrize('error, cause, expected', [
    ('HarmonyError', '{"errorMessage": "job failed", "errorType": "HarmonyError"}', 'HarmonyError: job failed'),
    ('States.Timeout', 'not json', 'States.Timeout: not json'),
    ('States.TaskFailed', '["a", "list"]', 'States.TaskFailed: ["a", "list"]'),
    (None, 'Workflow failed', 'Workflow failed'),
    (None, None, 'The execution ended with status ABORTED'),
])
def test_describe_failure(error, cause, expected):
    """The Lambda errorMessage is preferred, then the raw cause, then the bare status"""
    execution = aws.ExecutionResult(status='ABORTED', error=error, cause=cause)

    assert runner.describe_failure(execution, 1800) == expected


def test_describe_failure_truncates_long_messages():
    """A huge stack trace cannot blow up the report"""
    execution = aws.ExecutionResult(status='FAILED', error='Boom', cause='x' * 10_000)

    description = runner.describe_failure(execution, 1800)

    assert len(description) < 4100
    assert description.endswith('... (truncated)')
