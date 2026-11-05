"""
Simple CMA lambda that gets the dataset configuration associated with the collection being processed
"""
import json
import logging
import os

import boto3
import importlib_resources
from cumulus_logger import CumulusLogger
from cumulus_process import Process
from jsonschema import Draft202012Validator

from bignbit import utils

CUMULUS_LOGGER = CumulusLogger('get_dataset_configuration')


class MissingDatasetConfiguration(Exception):
    """
    Exception for missing dataset configuration
    """


class InvalidDatasetConfiguration(Exception):
    """
    Exception for a dataset configuration that fails JSON schema validation
    """


class CMA(Process):
    """
    A cumulus message adapter
    """

    _VALIDATE_DATASET_CONFIG = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = CUMULUS_LOGGER

    @classmethod
    def _get_validator(cls):
        """Lazily build and cache the config schema validator on first use, reused for every invocation."""
        if cls._VALIDATE_DATASET_CONFIG is None:
            schema_ref = importlib_resources.files('bignbit').joinpath('data/dataset_config.schema.json')
            schema = json.loads(schema_ref.read_text(encoding='utf-8'))
            cls._VALIDATE_DATASET_CONFIG = Draft202012Validator(schema)
        return cls._VALIDATE_DATASET_CONFIG

    def _validate_config(self, config: dict, config_key_name: str) -> None:
        """
        Validate the dataset configuration against the bundled JSON schema. Unrecognized parameters
        (additionalProperties violations) are only warned about so that newer config keys do not
        hard-fail an older deployment; any other schema violation raises InvalidDatasetConfiguration.
        """
        has_hard_errors = False
        for error in self._get_validator().iter_errors(config):
            if error.validator == 'additionalProperties':
                self.logger.warning(
                    f"Ignoring unrecognized parameter(s) in dataset configuration '{config_key_name}': {error.message}")
            else:
                self.logger.error(
                    f"Invalid value at '{error.json_path}' in dataset configuration '{config_key_name}': {error.message}")
                has_hard_errors = True

        if has_hard_errors:
            raise InvalidDatasetConfiguration(
                f"Dataset configuration '{config_key_name}' failed schema validation; see logs for details")

    def process(self):
        """
        Download dataset configuration file from s3

        Returns
        -------
        dict
          Same input sent to this function with a new key 'datasetConfigurationForBIG' added

        """
        config_bucket_name = self.config['config_bucket_name']
        config_key_name = self.config['config_key_name']

        config = get_collection_config(config_bucket_name, config_key_name)
        self._validate_config(config, config_key_name)

        # Handle the default behavior when outputCrs is not supplied in the config, since we expect it to exist
        # when running through the state machine Map loops. These loops are outside of a lambda.
        config['outputCrs'] = config.get('outputCrs', ['EPSG:4326'])

        self.input['datasetConfigurationForBIG'] = {'config': config}
        return self.input


def get_collection_config(config_bucket_name: str, config_key_name: str) -> dict:
    """
    Retrieve the dataset configuration from the given s3 bucket and key

    Parameters
    ----------
    config_bucket_name: str
      Bucket name containing the configuration

    config_key_name: str
      Key to object storing configuration

    Returns
    -------
    dict
      The configuration json document as a dict
    """
    s3_client = boto3.client('s3')

    try:
        object_result = s3_client.get_object(Bucket=config_bucket_name, Key=config_key_name,
                                             ExpectedBucketOwner=utils.get_aws_account_id())
    except s3_client.exceptions.NoSuchKey as ex:
        raise MissingDatasetConfiguration(
            f"Dataset configuration not found s3://{config_bucket_name}/{config_key_name}") from ex

    return json.load(object_result['Body'])


def lambda_handler(event, context):
    """handler that gets called by aws lambda
    Parameters
    ----------
    event: dictionary
        event from a lambda call
    context: dictionary
        context from a lambda call
    Returns
    ----------
        dict
            A CMA json message
    """

    levels = {
        'critical': logging.CRITICAL,
        'error': logging.ERROR,
        'warn': logging.WARNING,
        'warning': logging.WARNING,
        'info': logging.INFO,
        'debug': logging.DEBUG
    }

    logging_level = os.environ.get('LOGGING_LEVEL', 'info')
    CUMULUS_LOGGER.logger.setLevel(levels.get(logging_level, 'info'))
    CUMULUS_LOGGER.setMetadata(event, context)

    return CMA.cumulus_handler(event, context=context)


if __name__ == "__main__":
    CMA()
