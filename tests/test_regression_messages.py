"""Unit tests for the regression suite's CMR lookup and workflow input message generation"""
# pylint: disable=redefined-outer-name
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import pytest
import requests

from regression_helpers import MUR_CASE, OPERA_CASE, granule_cmr
from regression import cmr, messages
from regression.config import Deployment
from regression.manifest import RegressionCase

SANDBOX = Deployment(venue='sandbox', region='us-east-1', prefix='gitc-sandbox', cmr_environment='UAT')


@pytest.fixture
def cmr_get():
    """Replace requests.get in the CMR module with a mock returning one granule, so no request leaves the test"""
    response = mock.Mock()
    response.json.return_value = {'hits': 1, 'items': [granule_cmr(MUR_CASE)]}
    with mock.patch.object(cmr.requests, 'get', return_value=response) as patched:
        yield patched


@pytest.fixture
def no_credentials(monkeypatch, tmp_path):
    """No EDL_USER/EDL_PASS and a home directory without a .netrc"""
    monkeypatch.delenv('EDL_USER', raising=False)
    monkeypatch.delenv('EDL_PASS', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    return tmp_path


def write_netrc(home, text):
    """Write a .netrc readable only by its owner, as netrc requires for files with passwords"""
    path = home / '.netrc'
    path.write_text(text, encoding='utf-8')
    path.chmod(0o600)


@pytest.fixture
def urs():
    """
    A fake Earthdata Login token API: ``urs.tokens`` is the list of the user's tokens and each request is recorded in
    ``urs.calls`` as (method, url, params)
    """
    state = SimpleNamespace(tokens=[], calls=[], created={'access_token': 'new-token', 'expiration_date': '1/1/2099'})

    def respond(method):
        def handler(url, params=None, timeout=None):
            assert timeout > 0
            state.calls.append((method, url, params))
            response = mock.Mock()
            response.json.return_value = state.created if url.endswith('/token') else state.tokens
            return response
        return handler

    session = mock.MagicMock()
    session.__enter__.return_value = session
    session.get.side_effect = respond('GET')
    session.post.side_effect = respond('POST')
    with mock.patch.object(cmr.requests, 'Session', return_value=session):
        state.session = session
        yield state


def in_days(days):
    """An expiration date the given number of days from now, in the MM/DD/YYYY format Earthdata Login uses"""
    return (datetime.now() + timedelta(days=days)).strftime('%m/%d/%Y')


def test_get_granule_is_unauthenticated_by_default(cmr_get):
    """Without a token the search goes to CMR UAT with no token"""
    granule = cmr.get_granule(MUR_CASE.short_name, MUR_CASE.provider, MUR_CASE.granule_ur, 'UAT')

    assert granule == granule_cmr(MUR_CASE)
    args, kwargs = cmr_get.call_args
    assert args == ('https://cmr.uat.earthdata.nasa.gov/search/granules.umm_json',)
    assert kwargs['params'] == {'short_name': MUR_CASE.short_name, 'provider': 'POCLOUD', 'granule_ur': MUR_CASE.granule_ur}
    assert 'headers' not in kwargs
    assert kwargs['timeout'] > 0
    cmr_get.return_value.raise_for_status.assert_called_once()


def test_get_granule_sends_token(cmr_get):
    """The token is sent as a bearer token in the token query parameter, which CMR needs to return ACL-protected
    collections and granules"""
    cmr.get_granule(MUR_CASE.short_name, MUR_CASE.provider, MUR_CASE.granule_ur, 'UAT', token='secret-token')

    assert cmr_get.call_args.kwargs['params']['token'] == 'Bearer secret-token'


@pytest.mark.parametrize('error', [
    requests.HTTPError('401 Client Error for url: https://cmr/search?token=Bearer+secret-token',
                       response=mock.Mock(status_code=401)),
    requests.ConnectionError('Max retries exceeded with url: /search?token=Bearer+secret-token'),
])
def test_get_granule_request_error_hides_token(cmr_get, error):
    """A failed request is reported without its URL, which would reveal the token"""
    cmr_get.side_effect = error

    with pytest.raises(cmr.CmrError) as raised:
        cmr.get_granule(MUR_CASE.short_name, MUR_CASE.provider, MUR_CASE.granule_ur, 'UAT', token='secret-token')

    assert 'secret-token' not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__
    assert type(error).__name__ in str(raised.value)


def test_get_granule_no_match_without_token_suggests_credentials(cmr_get):
    """With no token, an empty search may be an access controlled collection, so the error says how to fix that"""
    cmr_get.return_value.json.return_value = {'items': []}

    with pytest.raises(cmr.CmrError, match='set EDL_USER and EDL_PASS or add uat.urs.earthdata.nasa.gov to ~/.netrc'):
        cmr.get_granule(MUR_CASE.short_name, MUR_CASE.provider, MUR_CASE.granule_ur, 'UAT')


def test_credentials_from_environment(no_credentials, monkeypatch):
    """EDL_USER and EDL_PASS win over ~/.netrc"""
    write_netrc(no_credentials, 'machine uat.urs.earthdata.nasa.gov login netrc-user password netrc-pass\n')
    monkeypatch.setenv('EDL_USER', 'env-user')
    monkeypatch.setenv('EDL_PASS', 'env-pass')

    user, password, source = cmr.get_edl_credentials('UAT')

    assert (user, password) == ('env-user', 'env-pass')
    assert 'EDL_USER' in source


@pytest.mark.usefixtures('no_credentials')
@pytest.mark.parametrize('variable', ['EDL_USER', 'EDL_PASS'])
def test_credentials_from_environment_must_be_complete(monkeypatch, variable):
    """Setting only one of EDL_USER and EDL_PASS is an error rather than a silent fallback to ~/.netrc"""
    monkeypatch.setenv(variable, 'value')

    with pytest.raises(cmr.CmrError, match='Set both EDL_USER and EDL_PASS'):
        cmr.get_edl_credentials('UAT')


@pytest.mark.parametrize('cmr_env, user', [('UAT', 'uat-user'), ('OPS', 'ops-user')])
def test_credentials_from_netrc(no_credentials, cmr_env, user):
    """Without the environment variables, the ~/.netrc entry for the environment's Earthdata Login host is used"""
    write_netrc(no_credentials, 'machine uat.urs.earthdata.nasa.gov login uat-user password uat-pass\n'
                                'machine urs.earthdata.nasa.gov login ops-user password ops-pass\n')

    assert cmr.get_edl_credentials(cmr_env)[:2] == (user, user.replace('user', 'pass'))


@pytest.mark.parametrize('netrc_text', [None, 'machine urs.earthdata.nasa.gov login ops-user password ops-pass\n'])
def test_no_credentials(no_credentials, netrc_text):
    """No ~/.netrc, or one without the environment's host, means no credentials"""
    if netrc_text:
        write_netrc(no_credentials, netrc_text)

    assert cmr.get_edl_credentials('UAT') is None


def test_unparseable_netrc(no_credentials):
    """A broken ~/.netrc is reported without quoting its contents"""
    write_netrc(no_credentials, 'machine uat.urs.earthdata.nasa.gov login user password secret-pass extra\n')

    with pytest.raises(cmr.CmrError, match='Unable to parse') as raised:
        cmr.get_edl_credentials('UAT')
    assert 'secret-pass' not in str(raised.value)


def test_get_token_without_credentials_is_anonymous(caplog):
    """Without credentials there is no token and a warning says how to provide them"""
    assert cmr.get_token('UAT', None) is None
    assert 'set EDL_USER and EDL_PASS or add uat.urs.earthdata.nasa.gov to ~/.netrc' in caplog.text


def test_get_token_reuses_a_valid_token(urs, caplog):
    """An existing token that does not expire soon is reused, preferring the one that lasts longest"""
    caplog.set_level('INFO', logger='regression')
    urs.tokens = [{'access_token': 'later', 'expiration_date': in_days(50)},
                  {'access_token': 'sooner', 'expiration_date': in_days(10)}]

    assert cmr.get_token('UAT', ('user', 'pass', 'test')) == 'later'

    assert urs.session.auth == ('user', 'pass')
    assert urs.calls == [('GET', 'https://uat.urs.earthdata.nasa.gov/api/users/tokens', None)]
    assert 'as Earthdata Login user user (credentials from test)' in caplog.text
    assert 'pass' not in caplog.text.replace('pass)', '')


def test_get_token_creates_a_token_when_none_is_usable(urs):
    """A token expiring within the margin is not used; with room for another token a new one is created"""
    urs.tokens = [{'access_token': 'expiring', 'expiration_date': in_days(0)}]

    assert cmr.get_token('OPS', ('user', 'pass', 'test')) == 'new-token'

    assert urs.calls == [('GET', 'https://urs.earthdata.nasa.gov/api/users/tokens', None),
                         ('POST', 'https://urs.earthdata.nasa.gov/api/users/token', None)]


def test_get_token_revokes_the_oldest_token_when_full(urs):
    """With the maximum number of tokens and none usable, the one that expires first is revoked before creating one"""
    urs.tokens = [{'access_token': 'expiring', 'expiration_date': in_days(0)},
                  {'access_token': 'expired', 'expiration_date': '2020-01-01T00:00:00.000Z'}]

    assert cmr.get_token('UAT', ('user', 'pass', 'test')) == 'new-token'

    assert urs.calls[1:] == [('POST', 'https://uat.urs.earthdata.nasa.gov/api/users/revoke_token', {'token': 'expired'}),
                             ('POST', 'https://uat.urs.earthdata.nasa.gov/api/users/token', None)]


def test_get_token_error_hides_tokens(urs):
    """A failed Earthdata Login request is reported with its status, without the URL that may hold a token"""
    urs.session.get.side_effect = requests.HTTPError('401 Client Error for url: https://uat.urs/revoke?token=secret-token',
                                                     response=mock.Mock(status_code=401))

    with pytest.raises(cmr.CmrError, match='as user user: request failed with HTTP 401') as raised:
        cmr.get_token('UAT', ('user', 'pass', 'test'))

    assert 'secret-token' not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


def test_get_granule_ops_host(cmr_get):
    """The OPS CMR environment uses the production host"""
    cmr.get_granule(MUR_CASE.short_name, MUR_CASE.provider, MUR_CASE.granule_ur, 'OPS')

    assert cmr_get.call_args.args == ('https://cmr.earthdata.nasa.gov/search/granules.umm_json',)


@pytest.mark.parametrize('count', [0, 2])
def test_get_granule_requires_exactly_one_match(cmr_get, count):
    """No match or an ambiguous match is an error"""
    cmr_get.return_value.json.return_value = {'items': [granule_cmr(MUR_CASE)] * count}

    with pytest.raises(cmr.CmrError, match=f'found {count}'):
        cmr.get_granule(MUR_CASE.short_name, MUR_CASE.provider, MUR_CASE.granule_ur, 'UAT')


@pytest.mark.parametrize('lookup', [cmr.cmr_host, cmr.urs_host])
def test_unknown_cmr_environment(lookup):
    """Only UAT and OPS are known CMR environments"""
    with pytest.raises(cmr.CmrError, match='Unknown CMR environment'):
        lookup('SIT')


def test_build_input_message_standard():
    """A standard collection gets the Cumulus message the workflow expects, with the deployment's prefix as stack"""
    message = messages.build_input_message(MUR_CASE, granule_cmr(MUR_CASE), SANDBOX)

    assert message == {
        'cumulus_meta': {},
        'meta': {
            'buckets': {},
            'cmr': {'clientId': 'POCLOUD', 'cmrEnvironment': 'UAT', 'provider': 'POCLOUD'},
            'collection': {'name': 'MUR-JPL-L4-GLOB-v4.1'},
            'stack': 'gitc-sandbox',
            'provider': {}
        },
        'payload': {
            'granules': [
                {
                    'granuleId': '20260309090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1',
                    'provider': 'POCLOUD',
                    'cmrLink': 'https://cmr.uat.earthdata.nasa.gov/search/concepts/G1281443657-POCLOUD.umm_json',
                    'cmrConceptId': 'G1281443657-POCLOUD'
                }
            ]
        }
    }


def test_build_input_message_opera():
    """An OPERA collection also lists its browse GeoTIFF in the deployment's private bucket"""
    message = messages.build_input_message(OPERA_CASE, granule_cmr(OPERA_CASE, 'G1276618252-POCLOUD'), SANDBOX)

    granule_id = 'OPERA_L3_DSWx-HLS_T32VMJ_20250920T103741Z_20250922T082308Z_S2A_30_v1.0'
    assert message['meta']['stack'] == 'gitc-sandbox'
    assert message['meta']['cmr']['provider'] == 'POCLOUD'
    assert message['payload']['granules'] == [
        {
            'granuleId': granule_id,
            'provider': 'opera',
            'dataType': 'OPERA_L3_DSWX-HLS_V1',
            'cmrLink': 'https://cmr.uat.earthdata.nasa.gov/search/concepts/G1276618252-POCLOUD.umm_json',
            'cmrConceptId': 'G1276618252-POCLOUD',
            'files': [
                {
                    'bucket': 'gitc-sandbox-private',
                    'fileName': f'{granule_id}_BROWSE.tif',
                    'type': 'data',
                    'key': f'OPERA_L3_DSWX-HLS_V1/{granule_id}_BROWSE.tif',
                    'source': f'OPERA_L3_DSWX-HLS_V1/{granule_id}/{granule_id}_BROWSE.tif'
                }
            ]
        }
    ]


def test_build_input_message_uses_workspace_resource_prefix():
    """With a Terraform workspace, the stack and the OPERA bucket carry the workspace suffix"""
    deployment = Deployment(venue='sit', region='us-west-2', prefix='podaac-sit-svc', cmr_environment='UAT', workspace='issue-193')

    message = messages.build_input_message(OPERA_CASE, granule_cmr(OPERA_CASE), deployment)

    assert message['meta']['stack'] == 'podaac-sit-svc-issue-193'
    assert message['payload']['granules'][0]['files'][0]['bucket'] == 'podaac-sit-svc-issue-193-private'


def test_build_input_message_ops_cmr_link():
    """Cases for CMR OPS link to the production CMR host"""
    case = RegressionCase('MUR-JPL-L4-GLOB-v4.1', 'POCLOUD', 'granule-1', 'OPS')

    message = messages.build_input_message(case, granule_cmr(case, 'G1-POCLOUD'), SANDBOX)

    assert message['meta']['cmr']['cmrEnvironment'] == 'OPS'
    assert message['payload']['granules'][0]['cmrLink'] == 'https://cmr.earthdata.nasa.gov/search/concepts/G1-POCLOUD.umm_json'


def test_build_input_message_falls_back_to_the_test_case():
    """Identifiers missing from the CMR record are taken from the test case"""
    message = messages.build_input_message(MUR_CASE, {'meta': {'concept-id': 'G1-POCLOUD'}, 'umm': {}}, SANDBOX)

    assert message['meta']['collection']['name'] == MUR_CASE.short_name
    assert message['meta']['cmr']['provider'] == MUR_CASE.provider
    assert message['payload']['granules'][0]['granuleId'] == MUR_CASE.granule_ur
