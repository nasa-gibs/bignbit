"""
Looks up granule metadata in CMR
"""
import os

import requests

CMR_HOSTS = {
    'UAT': 'cmr.uat.earthdata.nasa.gov',
    'OPS': 'cmr.earthdata.nasa.gov'
}
EDL_TOKEN_VARIABLE = 'EDL_TOKEN'
REQUEST_TIMEOUT_SECONDS = 60


class CmrError(Exception):
    """
    Exception for a granule that cannot be resolved to exactly one CMR record
    """


def cmr_host(cmr_env: str) -> str:
    """Return the CMR hostname for a CMR environment (UAT or OPS)."""
    try:
        return CMR_HOSTS[cmr_env.upper()]
    except KeyError as ex:
        raise CmrError(f'Unknown CMR environment {cmr_env!r}') from ex


def get_granule(short_name: str, provider: str, granule_ur: str, cmr_env: str) -> dict:
    """
    Find a single granule in CMR. The search is unauthenticated unless the EDL_TOKEN environment variable is set,
    in which case its value is sent as a bearer token.

    Parameters
    ----------
    short_name : str
        Collection short name
    provider : str
        CMR provider id
    granule_ur : str
        Granule UR
    cmr_env : str
        CMR environment (UAT or OPS)

    Returns
    -------
    dict
        The granule's CMR search result item (``meta`` and ``umm``)

    Raises
    ------
    CmrError
        If the search does not return exactly one granule
    requests.RequestException
        If the request fails
    """
    headers = {}
    edl_token = os.environ.get(EDL_TOKEN_VARIABLE)
    if edl_token:
        headers['Authorization'] = f'Bearer {edl_token}'

    response = requests.get(f'https://{cmr_host(cmr_env)}/search/granules.umm_json',
                            params={'short_name': short_name, 'provider': provider, 'granule_ur': granule_ur},
                            headers=headers,
                            timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    items = response.json().get('items', [])
    if len(items) != 1:
        raise CmrError(f'Expected 1 granule with UR {granule_ur} (short_name={short_name}, provider={provider}) '
                       f'in CMR {cmr_env.upper()}, found {len(items)}')
    return items[0]
