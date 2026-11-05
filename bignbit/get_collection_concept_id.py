"""
Get the collection concept id that a granule belongs to
"""
import logging
import os

import requests
from cumulus_logger import CumulusLogger
from cumulus_process import Process

from bignbit import utils

CUMULUS_LOGGER = CumulusLogger('get_collection_concept_id')


class CMA(Process):
    """
    A cumulus message adapter
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = CUMULUS_LOGGER

    def process(self) -> dict:
        """
        Using the collection shortname, provider, and environment; look up the collection concept id

        Returns
        ----------
        dict
          The input with new key 'collection_concept_id' added in the payload
        """
        collection_shortname = self.config['collection_shortname']
        collection_version = self.config['collection_version']
        cmr_provider = self.config['cmr_provider']
        cmr_environment = self.config['cmr_environment']
        dataset_config = self.input['datasetConfigurationForBIG']['config']
        # Use override for collection concept id from dataset config if provided
        collection_id = dataset_config.get('concept_id') or dataset_config.get('collectionId')
        if not collection_id:
            collection_id = get_collection_concept_id(collection_shortname, collection_version, cmr_provider, cmr_environment)
        self.input['collection_concept_id'] = collection_id
        return self.input


def get_collection_concept_id(collection_shortname: str, collection_version: str, cmr_provider: str, cmr_environment: str) -> str:
    """
    Retrieve the collection concept id from CMR
    Parameters
    ----------
    collection_shortname
      Shortname of the collection
    collection_version
      Version of the collection
    cmr_provider
      Collection provider
    cmr_environment
      CMR environment to query

    Returns
    -------
    str
      the collection concept id
    """
    cmr_search_collections_url = f'https://cmr.{"uat." if cmr_environment == "UAT" else ""}earthdata.nasa.gov/search/collections.umm_json'
    edl_user, edl_pass = utils.get_edl_creds()
    token = utils.get_cmr_user_token(edl_user, edl_pass, cmr_environment)

    umm_json_response = requests.get(cmr_search_collections_url,
                                     headers={'Authorization': f'Bearer {token}'},
                                     params={
                                         'provider': cmr_provider,
                                         'short_name': collection_shortname,
                                         'version': collection_version
                                     },
                                     timeout=10)
    umm_json_response.raise_for_status()
    umm_json = umm_json_response.json()

    return umm_json['items'][0]['meta']['concept-id']


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
