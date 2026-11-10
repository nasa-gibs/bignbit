"""
Looks up granule metadata in CMR
"""
import logging
import netrc
import os
from datetime import datetime, timedelta

import requests

LOGGER = logging.getLogger('regression')

CMR_HOSTS = {
    'UAT': 'cmr.uat.earthdata.nasa.gov',
    'OPS': 'cmr.earthdata.nasa.gov'
}
URS_HOSTS = {
    'UAT': 'uat.urs.earthdata.nasa.gov',
    'OPS': 'urs.earthdata.nasa.gov'
}
EDL_USER_VARIABLE = 'EDL_USER'
EDL_PASS_VARIABLE = 'EDL_PASS'
REQUEST_TIMEOUT_SECONDS = 60

# Earthdata Login allows each user two tokens. A token that expires within this margin is not used, so that it
# cannot expire part way through a run.
MAX_USER_TOKENS = 2
TOKEN_EXPIRY_MARGIN = timedelta(days=1)


class CmrError(Exception):
    """
    Exception for a granule that cannot be resolved to exactly one CMR record, or a failure to authenticate to CMR
    """


def cmr_host(cmr_env: str) -> str:
    """Return the CMR hostname for a CMR environment (UAT or OPS)."""
    try:
        return CMR_HOSTS[cmr_env.upper()]
    except KeyError as ex:
        raise CmrError(f'Unknown CMR environment {cmr_env!r}') from ex


def urs_host(cmr_env: str) -> str:
    """Return the Earthdata Login hostname for a CMR environment (UAT or OPS)."""
    try:
        return URS_HOSTS[cmr_env.upper()]
    except KeyError as ex:
        raise CmrError(f'Unknown CMR environment {cmr_env!r}') from ex


def get_edl_credentials(cmr_env: str) -> tuple[str, str, str] | None:
    """
    Find Earthdata Login credentials for a CMR environment: the EDL_USER and EDL_PASS environment variables if they
    are set, otherwise the entry in ~/.netrc for the environment's Earthdata Login host (uat.urs.earthdata.nasa.gov
    for UAT, urs.earthdata.nasa.gov for OPS).

    Returns
    -------
    tuple[str, str, str] | None
        Username, password and a description of where they came from, or None if there are no credentials

    Raises
    ------
    CmrError
        If only one of EDL_USER and EDL_PASS is set, or ~/.netrc cannot be parsed
    """
    edl_user = os.environ.get(EDL_USER_VARIABLE)
    edl_pass = os.environ.get(EDL_PASS_VARIABLE)
    if edl_user or edl_pass:
        if not (edl_user and edl_pass):
            raise CmrError(f'Set both {EDL_USER_VARIABLE} and {EDL_PASS_VARIABLE}, or neither to use ~/.netrc')
        return edl_user, edl_pass, f'the {EDL_USER_VARIABLE} and {EDL_PASS_VARIABLE} environment variables'

    host = urs_host(cmr_env)
    try:
        entry = netrc.netrc().hosts.get(host)
    except FileNotFoundError:
        return None
    except netrc.NetrcParseError as ex:
        # The message can quote the offending line, so only the location is reported
        raise CmrError(f'Unable to parse {ex.filename} at line {ex.lineno}') from None
    if not entry or not entry[0] or not entry[2]:
        return None
    return entry[0], entry[2], f'~/.netrc ({host})'


def _parse_expiration(value: str) -> datetime:
    """Parse a token expiration date, which Earthdata Login gives either as MM/DD/YYYY or in ISO 8601."""
    try:
        return datetime.strptime(value, '%m/%d/%Y')
    except ValueError:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=None)


def get_user_token(edl_user: str, edl_pass: str, cmr_env: str) -> str:
    """
    Return an Earthdata Login user token for CMR: an existing token of the user that is not about to expire, or a new
    one. If the user already has the maximum number of tokens and none can be used, the one that expires first is
    revoked to make room.

    Raises
    ------
    CmrError
        If Earthdata Login rejects the credentials or a request fails
    """
    urs_url = f'https://{urs_host(cmr_env)}/api/users'
    try:
        with requests.Session() as session:
            session.auth = (edl_user, edl_pass)

            response = session.get(f'{urs_url}/tokens', timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            tokens = sorted(((_parse_expiration(t['expiration_date']), t['access_token']) for t in response.json()),
                            key=lambda token: token[0])

            usable = [access_token for expiration, access_token in tokens
                      if expiration > datetime.now() + TOKEN_EXPIRY_MARGIN]
            if usable:
                return usable[-1]

            if len(tokens) >= MAX_USER_TOKENS:
                response = session.post(f'{urs_url}/revoke_token', params={'token': tokens[0][1]},
                                        timeout=REQUEST_TIMEOUT_SECONDS)
                response.raise_for_status()

            response = session.post(f'{urs_url}/token', timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            return response.json()['access_token']
    except requests.RequestException as ex:
        # A revoke request URL contains a token, so neither the message nor the chained exception may be kept
        status = f' with HTTP {ex.response.status_code}' if ex.response is not None else ''
        raise CmrError(f'Unable to get an Earthdata Login token for CMR {cmr_env.upper()} as user {edl_user}: '
                       f'request failed{status} ({type(ex).__name__})') from None
    except (KeyError, TypeError, ValueError) as ex:
        raise CmrError(f'Unable to get an Earthdata Login token for CMR {cmr_env.upper()} as user {edl_user}: '
                       f'unexpected response ({type(ex).__name__})') from None


def get_token(cmr_env: str, credentials: tuple[str, str, str] | None) -> str | None:
    """
    Get a CMR token with the Earthdata Login credentials found by ``get_edl_credentials``. Without credentials CMR
    is searched anonymously, which does not find collections and granules restricted by an access control list.

    Returns
    -------
    str | None
        The token, or None if there are no credentials

    Raises
    ------
    CmrError
        If a token cannot be obtained with the credentials
    """
    if credentials is None:
        LOGGER.warning('No Earthdata Login credentials for CMR %s: set %s and %s or add %s to ~/.netrc. CMR will be '
                       'searched anonymously, so access controlled granules will not be found.', cmr_env.upper(),
                       EDL_USER_VARIABLE, EDL_PASS_VARIABLE, urs_host(cmr_env))
        return None
    edl_user, edl_pass, source = credentials
    LOGGER.info('Authenticating to CMR %s as Earthdata Login user %s (credentials from %s)', cmr_env.upper(), edl_user, source)
    return get_user_token(edl_user, edl_pass, cmr_env)


def get_granule(short_name: str, provider: str, granule_ur: str, cmr_env: str, token: str | None = None) -> dict:
    """
    Find a single granule in CMR. When a token is given it is sent as a bearer token in the ``token`` query
    parameter; collections and granules restricted by a CMR access control list are only returned when it is.

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
    token : str
        Earthdata Login token from ``get_token``; the search is anonymous without one

    Returns
    -------
    dict
        The granule's CMR search result item (``meta`` and ``umm``)

    Raises
    ------
    CmrError
        If the request fails or the search does not return exactly one granule
    """
    params = {'short_name': short_name, 'provider': provider, 'granule_ur': granule_ur}
    if token:
        params['token'] = f'Bearer {token}'

    try:
        response = requests.get(f'https://{cmr_host(cmr_env)}/search/granules.umm_json',
                                params=params,
                                timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.RequestException as ex:
        # The request URL contains the token, so neither the message nor the chained exception may be kept:
        # errors end up in the published report and the job log.
        status = f' with HTTP {ex.response.status_code}' if ex.response is not None else ''
        raise CmrError(f'CMR {cmr_env.upper()} granule search for {granule_ur} failed{status} '
                       f'({type(ex).__name__})') from None

    items = response.json().get('items', [])
    if len(items) != 1:
        hint = '' if items or token else (f'; if the collection is access controlled, set {EDL_USER_VARIABLE} and '
                                          f'{EDL_PASS_VARIABLE} or add {urs_host(cmr_env)} to ~/.netrc')
        raise CmrError(f'Expected 1 granule with UR {granule_ur} (short_name={short_name}, provider={provider}) '
                       f'in CMR {cmr_env.upper()}, found {len(items)}{hint}')
    return items[0]
