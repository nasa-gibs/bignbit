"""Unit tests for the regression suite's CMR lookup and workflow input message generation"""
# pylint: disable=redefined-outer-name
from unittest import mock

import pytest

from regression_helpers import MUR_CASE, OPERA_CASE, granule_cmr
from regression import cmr, messages
from regression.config import Deployment
from regression.manifest import RegressionCase

SANDBOX = Deployment(venue='sandbox', region='us-east-1', prefix='gitc-sandbox', cmr_environment='UAT')


@pytest.fixture
def cmr_get(monkeypatch):
    """Replace requests.get in the CMR module with a mock returning one granule, so no request leaves the test"""
    monkeypatch.delenv('EDL_TOKEN', raising=False)
    response = mock.Mock()
    response.json.return_value = {'hits': 1, 'items': [granule_cmr(MUR_CASE)]}
    with mock.patch.object(cmr.requests, 'get', return_value=response) as patched:
        yield patched


def test_get_granule_is_unauthenticated_by_default(cmr_get):
    """Without EDL_TOKEN the search goes to CMR UAT with no Authorization header"""
    granule = cmr.get_granule(MUR_CASE.short_name, MUR_CASE.provider, MUR_CASE.granule_ur, 'UAT')

    assert granule == granule_cmr(MUR_CASE)
    args, kwargs = cmr_get.call_args
    assert args == ('https://cmr.uat.earthdata.nasa.gov/search/granules.umm_json',)
    assert kwargs['params'] == {'short_name': MUR_CASE.short_name, 'provider': 'POCLOUD', 'granule_ur': MUR_CASE.granule_ur}
    assert kwargs['headers'] == {}
    assert kwargs['timeout'] > 0
    cmr_get.return_value.raise_for_status.assert_called_once()


def test_get_granule_sends_edl_token_when_set(cmr_get, monkeypatch):
    """EDL_TOKEN is sent as a bearer token"""
    monkeypatch.setenv('EDL_TOKEN', 'secret-token')

    cmr.get_granule(MUR_CASE.short_name, MUR_CASE.provider, MUR_CASE.granule_ur, 'UAT')

    assert cmr_get.call_args.kwargs['headers'] == {'Authorization': 'Bearer secret-token'}


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


def test_unknown_cmr_environment():
    """Only UAT and OPS are known CMR environments"""
    with pytest.raises(cmr.CmrError, match='Unknown CMR environment'):
        cmr.cmr_host('SIT')


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
