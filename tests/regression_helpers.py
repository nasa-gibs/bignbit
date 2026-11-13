"""Shared helpers for the unit tests of the regression suite (regression/)"""
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import boto3
from moto.core import DEFAULT_ACCOUNT_ID
from moto.stepfunctions.models import stepfunctions_backends

from regression.manifest import RegressionCase

ACCOUNT_ID = DEFAULT_ACCOUNT_ID
REGION = 'us-east-1'
OTHER_REGION = 'us-west-2'
PREFIX = 'gitc-sandbox'
STATE_MACHINE_NAME = f'{PREFIX}-BrowseImageWorkflow'
AUDIT_BUCKET = f'{PREFIX}-internal'
ROLE_ARN = f'arn:aws:iam::{ACCOUNT_ID}:role/step-role'

# moto's default Step Functions backend does not interpret the definition, so any valid one will do
STATE_MACHINE_DEFINITION = json.dumps({'StartAt': 'Done', 'States': {'Done': {'Type': 'Succeed'}}})

MUR_CASE = RegressionCase(short_name='MUR-JPL-L4-GLOB-v4.1', provider='POCLOUD',
                          granule_ur='20260309090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1', cmr_env='UAT')
OPERA_CASE = RegressionCase(short_name='OPERA_L3_DSWX-HLS_V1', provider='POCLOUD',
                            granule_ur='OPERA_L3_DSWx-HLS_T32VMJ_20250920T103741Z_20250922T082308Z_S2A_30_v1.0', cmr_env='UAT')


def set_fake_aws_credentials(monkeypatch):
    """Point boto3 at fake credentials so that nothing can reach a real AWS account, even outside moto."""
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    monkeypatch.setenv('AWS_SESSION_TOKEN', 'testing')
    monkeypatch.setenv('AWS_DEFAULT_REGION', REGION)
    monkeypatch.delenv('AWS_PROFILE', raising=False)
    monkeypatch.delenv('AWS_REGION', raising=False)


def granule_cmr(case, concept_id='G1281443657-POCLOUD'):
    """Return a CMR UMM-JSON search result item for a test case"""
    return {
        'meta': {'concept-id': concept_id, 'provider-id': case.provider},
        'umm': {'GranuleUR': case.granule_ur, 'CollectionReference': {'ShortName': case.short_name, 'Version': '1'}}
    }


def make_cnm(collection, file_name, checksum, checksum_type='MD5'):
    """Return a CNM message listing one browse image and its metadata file"""
    return {
        'version': '1.5.1',
        'collection': collection,
        'product': {
            'files': [
                {'fileName': file_name, 'bucket': f'{PREFIX}-bignbit-staging', 'key': f'bignbit-harmony-output/{file_name}',
                 'checksum': checksum, 'checksumType': checksum_type, 'type': 'browse'},
                {'fileName': f'{file_name}.xml', 'bucket': f'{PREFIX}-bignbit-staging', 'key': f'bignbit-harmony-output/{file_name}.xml',
                 'checksum': 'ffff', 'checksumType': 'SHA512', 'type': 'metadata'}
            ]
        }
    }


def write_cnm(cnm_dir, name, cnm):
    """Write a CNM message to ``<cnm_dir>/<name>.cnm.json`` and return its path"""
    cnm_dir = Path(cnm_dir)
    cnm_dir.mkdir(parents=True, exist_ok=True)
    path = cnm_dir / f'{name}.cnm.json'
    path.write_text(json.dumps(cnm, indent=4), encoding='utf-8')
    return path


def create_state_machine(name=STATE_MACHINE_NAME, region=REGION):
    """Create a state machine in moto and return its ARN"""
    sfn_client = boto3.client('stepfunctions', region_name=region)
    return sfn_client.create_state_machine(name=name, definition=STATE_MACHINE_DEFINITION, roleArn=ROLE_ARN)['stateMachineArn']


def upload_cnms(case, cnms, bucket=AUDIT_BUCKET, region=REGION):
    """
    Upload CNM messages to moto S3 the way the workflow archives them and return the execution output that
    points at them. The output also carries an ARN, as real execution outputs do.
    """
    s3_client = boto3.client('s3', region_name=region)
    location = {} if region == 'us-east-1' else {'CreateBucketConfiguration': {'LocationConstraint': region}}
    s3_client.create_bucket(Bucket=bucket, **location)
    pobit = []
    for index, cnm in enumerate(cnms):
        key = f'bignbit-cnm-output/{cnm["collection"]}/{case.granule_ur}.2026-06-10T19_24_2{index}.277Z.cnm.json'
        s3_client.put_object(Bucket=bucket, Key=key, Body=json.dumps(cnm).encode('utf-8'))
        pobit.append({'cnm_bucket': bucket, 'cnm_key': key})
    return {
        'meta': {'workflow_tasks': {'0': {'arn': f'arn:aws:lambda:{region}:{ACCOUNT_ID}:function:{PREFIX}-bignbit-send_to_gitc'}}},
        'payload': {'pobit': pobit}
    }


# moto's backends are plain objects, so cases running in parallel threads take turns completing executions
_BACKEND_LOCK = threading.Lock()


def workflow_pause(outcome_for):
    """
    Return a stand-in for ``aws._pause`` that completes the running executions instead of waiting, as
    ``finish_running_executions`` does, then reports whether the stop event is set like the real one.
    """
    def pause(_seconds, stop_event=None):
        finish_running_executions(outcome_for)
        return stop_event is not None and stop_event.is_set()
    return pause


def finish_running_executions(outcome_for, regions=(REGION, OTHER_REGION)):
    """
    Complete every execution that is still running in moto, which never finishes executions by itself.

    ``outcome_for`` is called with the execution's parsed input and returns a dict with ``status`` and optionally
    ``output``, ``error`` and ``cause``. A status of RUNNING leaves the execution running.
    """
    with _BACKEND_LOCK:
        executions = [execution
                      for region in regions
                      for state_machine in list(stepfunctions_backends[ACCOUNT_ID][region].state_machines)
                      for execution in list(state_machine.executions)
                      if execution.status == 'RUNNING']
        for execution in executions:
            outcome = outcome_for(json.loads(execution.execution_input))
            if outcome['status'] == 'RUNNING':
                continue
            execution.status = outcome['status']
            execution.stop_date = datetime.now(timezone.utc)
            execution.output = json.dumps(outcome['output']) if 'output' in outcome else None
            execution.error = outcome.get('error')
            execution.cause = outcome.get('cause')


def list_executions(state_machine_arn, region=REGION):
    """Return the executions moto recorded for a state machine"""
    return boto3.client('stepfunctions', region_name=region).list_executions(stateMachineArn=state_machine_arn)['executions']
