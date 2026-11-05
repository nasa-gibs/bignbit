import json
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

import bignbit.utils
from bignbit.get_dataset_configuration import (
    CMA,
    InvalidDatasetConfiguration,
    MissingDatasetConfiguration,
    get_collection_config,
)

# Minimal config that satisfies the schema's Harmony branch (sendToHarmony -> imgVariables required)
VALID_CONFIG = {'sendToHarmony': True, 'imgVariables': [{'id': 'all'}]}


def _make_cma(tmp_path):
    """Build a CMA instance with a mock logger so log calls can be asserted on."""
    cma = CMA(input={}, config={}, path=str(tmp_path))
    cma.logger = MagicMock()
    return cma


def test_validate_config_valid(tmp_path):
    """A schema-valid config logs nothing and does not raise."""
    cma = _make_cma(tmp_path)

    cma._validate_config(dict(VALID_CONFIG), 'some/key.cfg')

    cma.logger.warning.assert_not_called()
    cma.logger.error.assert_not_called()


def test_validate_config_unknown_parameter_warns(tmp_path):
    """An unrecognized parameter is warned about and ignored, not raised."""
    cma = _make_cma(tmp_path)
    config = {**VALID_CONFIG, 'bogusParameter': 'nope'}

    cma._validate_config(config, 'some/key.cfg')

    cma.logger.warning.assert_called_once()
    warning_message = cma.logger.warning.call_args.args[0]
    assert 'bogusParameter' in warning_message
    assert 'some/key.cfg' in warning_message
    cma.logger.error.assert_not_called()


def test_validate_config_invalid_value_raises(tmp_path):
    """A genuine schema violation logs an error (with the offending path) and raises."""
    cma = _make_cma(tmp_path)
    config = {**VALID_CONFIG, 'height': 'tall'}  # should be integer or null

    with pytest.raises(InvalidDatasetConfiguration):
        cma._validate_config(config, 'some/key.cfg')

    cma.logger.error.assert_called()
    error_messages = [call.args[0] for call in cma.logger.error.call_args_list]
    assert any('$.height' in message for message in error_messages)
    cma.logger.warning.assert_not_called()


def test_validate_config_unknown_and_invalid(tmp_path):
    """A config with both an unknown parameter and a real violation warns and still raises."""
    cma = _make_cma(tmp_path)
    config = {**VALID_CONFIG, 'sendToHarmony': 'yes', 'bogusParameter': 1}

    with pytest.raises(InvalidDatasetConfiguration):
        cma._validate_config(config, 'some/key.cfg')

    cma.logger.warning.assert_called_once()
    cma.logger.error.assert_called()


def test_get_validator_is_cached():
    """The compiled validator is built once and reused across calls."""
    assert CMA._get_validator() is CMA._get_validator()


@mock_aws
def test_get_collection_config_fetches_and_parses():
    """get_collection_config downloads and parses the configuration JSON from s3."""
    bignbit.utils.AWS_ACCOUNT_ID = None
    bucket_name = 'test-config-bucket'
    config_key = 'big-config/MUR25.cfg'

    s3_client = boto3.client('s3', region_name='us-east-1')
    s3_client.create_bucket(Bucket=bucket_name)
    s3_client.put_object(Bucket=bucket_name, Key=config_key, Body=json.dumps(VALID_CONFIG).encode('utf-8'))

    result = get_collection_config(bucket_name, config_key)

    assert result == VALID_CONFIG


@mock_aws
def test_get_collection_config_missing_raises():
    """A missing configuration object raises MissingDatasetConfiguration."""
    bignbit.utils.AWS_ACCOUNT_ID = None
    bucket_name = 'test-config-bucket'

    s3_client = boto3.client('s3', region_name='us-east-1')
    s3_client.create_bucket(Bucket=bucket_name)

    with pytest.raises(MissingDatasetConfiguration):
        get_collection_config(bucket_name, 'does/not/exist.cfg')


@mock_aws
def test_process_adds_config_and_outputcrs_default(tmp_path):
    """process() attaches the fetched config and defaults outputCrs when absent."""
    bignbit.utils.AWS_ACCOUNT_ID = None
    bucket_name = 'test-config-bucket'
    config_key = 'big-config/MUR25.cfg'

    s3_client = boto3.client('s3', region_name='us-east-1')
    s3_client.create_bucket(Bucket=bucket_name)
    s3_client.put_object(Bucket=bucket_name, Key=config_key, Body=json.dumps(VALID_CONFIG).encode('utf-8'))

    cma = CMA(input={}, config={'config_bucket_name': bucket_name, 'config_key_name': config_key},
              path=str(tmp_path))
    result = cma.process()

    config = result['datasetConfigurationForBIG']['config']
    assert config['sendToHarmony'] is True
    assert config['outputCrs'] == ['EPSG:4326']


@mock_aws
def test_process_preserves_supplied_outputcrs(tmp_path):
    """process() does not overwrite an outputCrs that is already present in the config."""
    bignbit.utils.AWS_ACCOUNT_ID = None
    bucket_name = 'test-config-bucket'
    config_key = 'big-config/polar.cfg'
    stored_config = {**VALID_CONFIG, 'outputCrs': ['EPSG:3413']}

    s3_client = boto3.client('s3', region_name='us-east-1')
    s3_client.create_bucket(Bucket=bucket_name)
    s3_client.put_object(Bucket=bucket_name, Key=config_key, Body=json.dumps(stored_config).encode('utf-8'))

    cma = CMA(input={}, config={'config_bucket_name': bucket_name, 'config_key_name': config_key},
              path=str(tmp_path))
    result = cma.process()

    assert result['datasetConfigurationForBIG']['config']['outputCrs'] == ['EPSG:3413']


@mock_aws
def test_process_raises_on_invalid_config(tmp_path):
    """process() propagates InvalidDatasetConfiguration for a schema-invalid config."""
    bignbit.utils.AWS_ACCOUNT_ID = None
    bucket_name = 'test-config-bucket'
    config_key = 'big-config/bad.cfg'
    bad_config = {**VALID_CONFIG, 'sendToHarmony': 'yes'}

    s3_client = boto3.client('s3', region_name='us-east-1')
    s3_client.create_bucket(Bucket=bucket_name)
    s3_client.put_object(Bucket=bucket_name, Key=config_key, Body=json.dumps(bad_config).encode('utf-8'))

    cma = CMA(input={}, config={'config_bucket_name': bucket_name, 'config_key_name': config_key},
              path=str(tmp_path))

    with pytest.raises(InvalidDatasetConfiguration):
        cma.process()
