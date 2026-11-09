"""Unit tests for the regression suite's AWS interactions, run against moto"""
# pylint: disable=redefined-outer-name,unused-argument
import json
import re
from datetime import datetime, timezone
from unittest import mock

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws

from regression_helpers import (ACCOUNT_ID, AUDIT_BUCKET, MUR_CASE, REGION, STATE_MACHINE_NAME, create_state_machine,
                                finish_running_executions, list_executions, make_cnm, set_fake_aws_credentials, upload_cnms)
from regression import aws
from regression.redact import PLACEHOLDER, Redactor

LONGEST_SHORT_NAME = 'MODIS_TERRA_L3_SST_THERMAL_MONTHLY_9KM_NIGHTTIME_V2019.0'
NOW = datetime(2026, 11, 9, 12, 30, 45, tzinfo=timezone.utc)


@pytest.fixture
def moto_aws(monkeypatch):
    """Run the test against moto with fake credentials"""
    set_fake_aws_credentials(monkeypatch)
    with mock_aws():
        yield


@pytest.fixture
def sfn_client(moto_aws):
    """A Step Functions client backed by moto"""
    return boto3.client('stepfunctions', region_name=REGION)


@pytest.fixture
def s3_client(moto_aws):
    """An S3 client backed by moto"""
    return boto3.client('s3', region_name=REGION)


def test_execution_name_is_recognizable_and_valid():
    """Names carry the collection and start time and use only characters Step Functions and CloudWatch accept"""
    name = aws.execution_name(LONGEST_SHORT_NAME, NOW, 'a1f')

    assert name == 'reg-MODIS_TERRA_L3_SST_THERMAL_MONTHLY_9KM_NIGHTTIME_V2019_0-20261109T123045-a1f'
    assert len(name) == 80
    assert re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name)


def test_execution_name_truncates_overlong_short_names():
    """The collection part is shortened so that the name never exceeds 80 characters and keeps its unique suffix"""
    name = aws.execution_name('X' * 200 + ' with spaces/and:colons', NOW, 'a1f')

    assert len(name) == 80
    assert name.endswith('-20261109T123045-a1f')
    assert re.fullmatch(r'[A-Za-z0-9_-]{80}', name)


def test_execution_name_is_unique_per_call():
    """Two runs of the same case get different names, which Step Functions requires"""
    names = {aws.execution_name('MUR-JPL-L4-GLOB-v4.1') for _ in range(20)}

    assert len(names) > 1
    assert all(re.fullmatch(r'reg-MUR-JPL-L4-GLOB-v4_1-\d{8}T\d{6}-[0-9a-f]{3}', name) for name in names)


def test_parse_state_machine_arn():
    """Region, account and name are taken from a state machine ARN; anything else is rejected"""
    arn = f'arn:aws:states:us-west-2:{ACCOUNT_ID}:stateMachine:podaac-sit-svc-BrowseImageWorkflow'

    assert aws.parse_state_machine_arn(arn) == ('us-west-2', ACCOUNT_ID, 'podaac-sit-svc-BrowseImageWorkflow')
    for invalid in ('', None, 'podaac-sit-svc-BrowseImageWorkflow', f'arn:aws:states:us-west-2:{ACCOUNT_ID}:execution:machine:run',
                    f'arn:aws:lambda:us-west-2:{ACCOUNT_ID}:function:machine'):
        with pytest.raises(ValueError, match='Not a Step Functions state machine ARN'):
            aws.parse_state_machine_arn(invalid)


def test_find_state_machine_arn(sfn_client):
    """The state machine is found by exact name among others"""
    create_state_machine(f'{STATE_MACHINE_NAME}-other')
    expected = create_state_machine(STATE_MACHINE_NAME)
    create_state_machine(f'other-{STATE_MACHINE_NAME}')

    assert aws.find_state_machine_arn(sfn_client, STATE_MACHINE_NAME) == expected


def test_find_state_machine_arn_not_found(sfn_client):
    """A missing state machine is reported by name and region, without an ARN"""
    create_state_machine('some-other-machine')

    with pytest.raises(aws.StateMachineNotFound, match=f"'{STATE_MACHINE_NAME}' not found in region {REGION}") as excinfo:
        aws.find_state_machine_arn(sfn_client, STATE_MACHINE_NAME)
    assert 'arn:aws' not in str(excinfo.value)


@pytest.mark.usefixtures('moto_aws')
def test_get_account_id():
    """The account ID comes from STS"""
    assert aws.get_account_id(aws.create_session(region=REGION)) == ACCOUNT_ID


def test_get_account_id_is_best_effort():
    """An STS failure other than missing credentials only costs the literal redaction"""
    session = mock.Mock()
    session.client.return_value.get_caller_identity.side_effect = ClientError(
        {'Error': {'Code': 'AccessDenied', 'Message': 'denied'}}, 'GetCallerIdentity')

    assert aws.get_account_id(session) == ''


def test_get_account_id_without_credentials():
    """Missing credentials are not swallowed, because nothing else could work either"""
    session = mock.Mock()
    session.client.return_value.get_caller_identity.side_effect = NoCredentialsError()

    with pytest.raises(NoCredentialsError):
        aws.get_account_id(session)


def test_get_deployed_version():
    """The version is read from the Version tag of the given Lambda"""
    lambda_client = mock.Mock()
    lambda_client.get_function.return_value = {'Configuration': {}, 'Tags': {'Version': '0.10.1a0', 'application': 'bignbit'}}

    assert aws.get_deployed_version(lambda_client, 'gitc-sandbox-bignbit-get_dataset_configuration') == '0.10.1a0'
    lambda_client.get_function.assert_called_once_with(FunctionName='gitc-sandbox-bignbit-get_dataset_configuration')


@pytest.mark.usefixtures('moto_aws')
def test_get_deployed_version_is_best_effort():
    """A missing tag, a missing function or any other error is reported as unknown and never raised"""
    untagged = mock.Mock()
    untagged.get_function.return_value = {'Configuration': {}}
    broken = mock.Mock()
    broken.get_function.side_effect = RuntimeError('boom')

    assert aws.get_deployed_version(untagged, 'function') == 'unknown'
    assert aws.get_deployed_version(broken, 'function') == 'unknown'
    # moto has no such function, so this is a real ResourceNotFoundException from the client
    assert aws.get_deployed_version(boto3.client('lambda', region_name=REGION), 'missing-function') == 'unknown'


def test_run_execution_succeeded(sfn_client, monkeypatch):
    """A succeeded execution returns its output and was started with the given name and input"""
    state_machine_arn = create_state_machine()
    sleeps = []

    def workflow_finishes(seconds):
        sleeps.append(seconds)
        finish_running_executions(lambda execution_input: {'status': 'SUCCEEDED', 'output': {'echo': execution_input}})

    monkeypatch.setattr(aws.time, 'sleep', workflow_finishes)

    result = aws.run_execution(sfn_client, state_machine_arn, 'reg-test-1', {'payload': {'granules': []}}, poll_interval=7, timeout=60)

    assert result.status == 'SUCCEEDED'
    assert json.loads(result.output) == {'echo': {'payload': {'granules': []}}}
    assert sleeps == [7]
    assert [execution['name'] for execution in list_executions(state_machine_arn)] == ['reg-test-1']


def test_run_execution_failed_with_its_own_error(sfn_client, monkeypatch):
    """A failed execution that carries an error and cause is reported as is"""
    state_machine_arn = create_state_machine()
    monkeypatch.setattr(aws.time, 'sleep', lambda _: finish_running_executions(
        lambda _: {'status': 'FAILED', 'error': 'MissingDatasetConfiguration', 'cause': '{"errorMessage": "no config"}'}))

    result = aws.run_execution(sfn_client, state_machine_arn, 'reg-test-1', {})

    assert (result.status, result.error, result.cause) == ('FAILED', 'MissingDatasetConfiguration', '{"errorMessage": "no config"}')
    assert result.output is None


def test_run_execution_timeout_stops_the_execution(sfn_client, monkeypatch):
    """An execution that outlives the timeout is reported as TIMEOUT and stopped so it cannot overlap the next case"""
    state_machine_arn = create_state_machine()
    monkeypatch.setattr(aws.time, 'sleep', lambda _: None)

    result = aws.run_execution(sfn_client, state_machine_arn, 'reg-test-1', {}, poll_interval=0.001, timeout=0.01)

    assert result.status == 'TIMEOUT'
    assert [execution['status'] for execution in list_executions(state_machine_arn)] == ['ABORTED']


def test_run_execution_timeout_when_stopping_fails(monkeypatch):
    """Failing to stop a timed out execution does not turn the timeout into an error"""
    client = mock.Mock()
    client.start_execution.return_value = {'executionArn': 'arn:execution'}
    client.describe_execution.return_value = {'status': 'RUNNING'}
    client.stop_execution.side_effect = ClientError({'Error': {'Code': 'AccessDeniedException', 'Message': 'denied'}}, 'StopExecution')
    monkeypatch.setattr(aws.time, 'sleep', lambda _: None)

    assert aws.run_execution(client, 'arn:machine', 'reg-test-1', {}, poll_interval=0.001, timeout=0.01).status == 'TIMEOUT'
    client.stop_execution.assert_called_once()


def test_run_execution_failed_looks_up_the_failed_step():
    """
    The workflow's Fail state has a fixed cause and no error, so the error of the step that actually failed is
    taken from the execution history
    """
    client = mock.Mock()
    client.start_execution.return_value = {'executionArn': 'arn:execution'}
    client.describe_execution.return_value = {'status': 'FAILED', 'cause': 'Workflow failed'}
    client.get_execution_history.return_value = {'events': [
        {'type': 'ExecutionFailed', 'executionFailedEventDetails': {'cause': 'Workflow failed'}},
        {'type': 'FailStateEntered', 'stateEnteredEventDetails': {'name': 'WorkflowFailed'}},
        {'type': 'MapIterationFailed', 'mapIterationFailedEventDetails': {'name': 'Transfer Image Sets', 'index': 3}},
        {'type': 'LambdaFunctionFailed', 'lambdaFunctionFailedEventDetails': {'error': 'HarmonyError', 'cause': '{"errorMessage": "job failed"}'}},
        {'type': 'LambdaFunctionFailed', 'lambdaFunctionFailedEventDetails': {'error': 'Earlier', 'cause': 'retried'}}
    ]}

    result = aws.run_execution(client, 'arn:machine', 'reg-test-1', {})

    assert (result.status, result.error, result.cause) == ('FAILED', 'HarmonyError', '{"errorMessage": "job failed"}')
    client.get_execution_history.assert_called_once_with(executionArn='arn:execution', reverseOrder=True, maxResults=1000)


def test_find_failure_details_is_best_effort(sfn_client):
    """Without failure details in the history, or without permission to read it, the execution's own cause stands"""
    denied = mock.Mock()
    denied.get_execution_history.side_effect = ClientError({'Error': {'Code': 'AccessDeniedException', 'Message': 'denied'}},
                                                           'GetExecutionHistory')
    state_machine_arn = create_state_machine()
    execution_arn = sfn_client.start_execution(stateMachineArn=state_machine_arn, name='reg-test-1', input='{}')['executionArn']

    assert aws.find_failure_details(denied, 'arn:execution') is None
    # moto returns a canned history of a successful execution, which holds no failure details
    assert aws.find_failure_details(sfn_client, execution_arn) is None


def test_download_cnm_files(s3_client, tmp_path):
    """CNMs are downloaded from the locations in the execution output, pretty-printed and redacted"""
    cnm = make_cnm('MUR_sst_LL', 'g.r00c00.png', 'abc')
    cnm['trace'] = f'arn:aws:sqs:us-east-1:{ACCOUNT_ID}:gitc-in.fifo'
    output = upload_cnms(MUR_CASE, [cnm, make_cnm('MUR_ice_LL', 'g_ice.r00c00.png', 'def')])
    output['payload']['pobit'].append({'cnm_bucket': AUDIT_BUCKET})
    output['payload']['pobit'].append({'cnm_key': 'orphan-key'})

    downloaded = aws.download_cnm_files(s3_client, output, tmp_path / 'cnm', Redactor([ACCOUNT_ID]))

    assert sorted(path.name for path in downloaded) == sorted(path.name for path in (tmp_path / 'cnm').iterdir())
    assert len(downloaded) == 2
    assert all(path.name.startswith(MUR_CASE.granule_ur) and path.name.endswith('.cnm.json') for path in downloaded)
    first = downloaded[0].read_text(encoding='utf-8')
    assert first.startswith('{\n    "version"')
    assert ACCOUNT_ID not in first
    expected = dict(cnm, trace=f'arn:aws:sqs:us-east-1:{PLACEHOLDER}:gitc-in.fifo')
    assert json.loads(first) == expected


def test_download_cnm_files_without_pobit(s3_client, tmp_path):
    """An output without CNM locations downloads nothing, leaving the comparison to report what is missing"""
    for output in ({}, {'payload': None}, {'payload': {}}, {'payload': {'pobit': None}}):
        assert aws.download_cnm_files(s3_client, output, tmp_path / 'cnm', Redactor()) == []
    assert (tmp_path / 'cnm').is_dir()


def test_download_cnm_files_keeps_cnms_with_the_same_file_name(s3_client, tmp_path):
    """CNMs of different GIBS collections with the same key basename do not overwrite each other"""
    s3_client.create_bucket(Bucket=AUDIT_BUCKET)
    pobit = []
    for collection in ('MUR_sst_LL', 'MUR_ice_LL'):
        key = f'bignbit-cnm-output/{collection}/granule.2026-06-10T19_24_23.277Z.cnm.json'
        s3_client.put_object(Bucket=AUDIT_BUCKET, Key=key, Body=json.dumps(make_cnm(collection, 'g.png', 'abc')).encode('utf-8'))
        pobit.append({'cnm_bucket': AUDIT_BUCKET, 'cnm_key': key})

    downloaded = aws.download_cnm_files(s3_client, {'payload': {'pobit': pobit}}, tmp_path, Redactor())

    assert [path.name for path in downloaded] == ['granule.2026-06-10T19_24_23.277Z.cnm.json', '2.granule.2026-06-10T19_24_23.277Z.cnm.json']
    assert {json.loads(path.read_text(encoding='utf-8'))['collection'] for path in downloaded} == {'MUR_sst_LL', 'MUR_ice_LL'}


def test_download_cnm_files_missing_object(s3_client, tmp_path):
    """A CNM that cannot be downloaded is an error for the case, not a silently shorter result"""
    s3_client.create_bucket(Bucket=AUDIT_BUCKET)

    with pytest.raises(aws.DownloadError, match=f'Failed to download CNM s3://{AUDIT_BUCKET}/missing.cnm.json: .*NoSuchKey'):
        aws.download_cnm_files(s3_client, {'payload': {'pobit': [{'cnm_bucket': AUDIT_BUCKET, 'cnm_key': 'missing.cnm.json'}]}},
                               tmp_path, Redactor())


def test_download_browse_files(s3_client, tmp_path):
    """Only browse images are downloaded, and one that is missing is skipped rather than raised"""
    staging_bucket = 'gitc-sandbox-bignbit-staging'
    s3_client.create_bucket(Bucket=staging_bucket)
    s3_client.put_object(Bucket=staging_bucket, Key='bignbit-harmony-output/g.r00c00.png', Body=b'png-bytes')
    s3_client.put_object(Bucket=staging_bucket, Key='bignbit-harmony-output/g.r00c00.png.xml', Body=b'<xml/>')
    cnm_files = []
    for index, file_name in enumerate(('g.r00c00.png', 'g.r00c01.png')):
        path = tmp_path / f'{index}.cnm.json'
        path.write_text(json.dumps(make_cnm('MUR_sst_LL', file_name, 'abc')), encoding='utf-8')
        cnm_files.append(path)

    downloaded = aws.download_browse_files(s3_client, cnm_files, tmp_path / 'files')

    assert [path.name for path in downloaded] == ['g.r00c00.png']
    assert (tmp_path / 'files' / 'g.r00c00.png').read_bytes() == b'png-bytes'
    assert [path.name for path in (tmp_path / 'files').iterdir()] == ['g.r00c00.png']
