#!/usr/bin/env python
import argparse
import boto3
import json
import netrc
import requests
import sys
import traceback
import warnings

from datetime import datetime
from typing import Any

from bignbit.utils import format_iso_expiration_date, get_edl_creds


class CollectionNotFoundError(Exception):
    """Exception raised when a collection is not found on CMR."""

    def __init__(self, message):
        super().__init__(message)

EDL_TOKENS: dict[str, str | None] = {
    'UAT': None,
    'OPS': None
}


def get_cmr_user_token(edl_user: str, edl_pass: str, cmr_env: str) -> str:
    """
    Reimplementation of get_cmr_user_token from bignbit.utils without global variable

    Parameters
    ----------
    edl_user
      EDL username
    edl_pass
      EDL password for user
    cmr_env
      CMR/URS environment to generate token in

    Returns
    -------
    str
      The token that can be used to query CMR
    """
    urs_get_tokens_url = f'https://{"uat." if cmr_env == "UAT" else ""}urs.earthdata.nasa.gov/api/users/tokens'
    urs_revoke_token_url = f'https://{"uat." if cmr_env == "UAT" else ""}urs.earthdata.nasa.gov/api/users/revoke_token'
    urs_create_token_url = f'https://{"uat." if cmr_env == "UAT" else ""}urs.earthdata.nasa.gov/api/users/token'

    with requests.Session() as session:
        session.auth = (edl_user, edl_pass)

        # Get existing user tokens
        get_tokens_request = session.request('get', urs_get_tokens_url)
        get_tokens_response = session.get(get_tokens_request.url, timeout=10)
        get_tokens_response.raise_for_status()
        tokens = get_tokens_response.json()

        # Filter expired tokens
        tokens = [{
            'access_token': t['access_token'],
            'expiration_date': datetime.strptime(format_iso_expiration_date(t['expiration_date']), '%m/%d/%Y')
        } for t in tokens]
        valid_tokens = list(filter(lambda t: datetime.now() < t['expiration_date'], tokens))
        expired_tokens = list(filter(lambda t: datetime.now() >= t['expiration_date'], tokens))

        # If there are no valid tokens and two expired tokens, need to revoke one of the expired tokens
        if len(valid_tokens) == 0 and len(expired_tokens) == 2:
            revoke_token_request = session.request('post', urs_revoke_token_url,
                                                   params={'token': next(iter(expired_tokens))['access_token']},
                                                   timeout=10)
            revoke_token_response = session.post(revoke_token_request.url)
            revoke_token_response.raise_for_status()

        # If there are no valid tokens, need to create one
        if len(valid_tokens) == 0:
            create_token_request = session.request('post', urs_create_token_url, timeout=10)
            create_token_response = session.post(create_token_request.url)
            create_token_response.raise_for_status()
            new_token = create_token_response.json()
            new_token['expiration_date'] = datetime.strptime(new_token['expiration_date'], '%m/%d/%Y')
            valid_tokens.insert(0, new_token)

    EDL_USER_TOKEN = next(iter(valid_tokens))
    return EDL_USER_TOKEN['access_token']


def get_edl_auth(venue: str) -> str:
    """
    Acquire Earthdata Login (EDL) authentication token using netrc first,
    if fails, tries to use SSM environment similar to bignbit module.

    Parameters
    ----------
    venue: str
        CMR environment/venue name

    Returns
    -------
    str
        CMR/Earthdata login token for the requested venue
    """
    nrc = netrc.netrc()
    edl_user = ''
    edl_pass = ''
    cred_source = '~/.netrc'
    for host_url in nrc.hosts.keys():
        if (venue == 'OPS' and host_url == 'urs.earthdata.nasa.gov') or (venue == 'UAT' and host_url == 'uat.urs.earthdata.nasa.gov'):
            edl_user, _, pss = nrc.hosts[host_url]
            edl_pass = pss or ''
            break
    if edl_user == '':
        edl_user, edl_pass = get_edl_creds()
        cred_source = 'SSM'
    print(f'{venue}: Authenticating to CMR as EDL user "{edl_user}" (credentials from {cred_source})')

    return get_cmr_user_token(edl_user, edl_pass, venue)


def find_best_collection(
        collection_list: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the 'best' version of a collection from a list of CMR results."""
    if len(collection_list) > 0:
        return collection_list[0]
    return {}


def query_cmr(
        venue: str,
        params: dict[str, Any],
        search_type: str
) -> list[dict[str, Any]]:
    cmr_url = f'https://cmr.{"uat." if venue == "UAT" else ""}earthdata.nasa.gov/search/{search_type}.umm_json'
    if EDL_TOKENS[venue] is None:
        EDL_TOKENS[venue] = get_edl_auth(venue)
    response = requests.get(
        cmr_url,
        headers={'Authorization': f'Bearer {EDL_TOKENS[venue]}'},
        params=params,
        timeout=10,
    )
    response.raise_for_status()
    umm_json = response.json()['items']
    return umm_json


def validate_collection(
        provider: str,
        short_name: str,
        collection_id: str | None,
        version: str | None = None,
) -> dict[str, Any]:
    """
    Search CMR database for the matching collection(s) on both production and uat
    venues, reporting if there are multiple collections (or none) that match the criteria
    and checking for mismatches in UMM-Var metadata between venues. Note that since version
    is not specified in dataset configs at this time, there may be multiple versions of
    a given collection and the ambiguity will be handled by the CMA input to bignbit.

    Parameters
    ----------
    provider: str
        CMR provider name (e.g., POCLOUD)
    short_name: str
        Short name of the collection
    collection_id: str | None
        Collection concept ID. Will only match one venue if specified.
    version: str | None
        Collection version (e.g., 003). If specified, only that version is matched and the
        concept ID found in each venue is reported.
    """
    search_params = {
        'provider': provider,
        'short_name': short_name,
    }
    if collection_id:
        search_params['concept_id'] = collection_id
    if version:
        search_params['version'] = version
    match_dict: dict[str, Any] = {}
    matches: list[int] = []
    for venue in ['UAT', 'OPS']:
        umm_json = query_cmr(venue, search_params, 'collections')
        matches.append(len(umm_json))
        match_dict[venue] = find_best_collection(umm_json)
        print(f'{venue}: Found {len(umm_json)} matching collections.')
        if version and match_dict[venue]:
            print(f'{venue}: Collection id for version {version} is {match_dict[venue]["meta"]["concept-id"]}')

    if not any(matches):
        version_msg = f' version {version}' if version else ''
        raise CollectionNotFoundError(
            f'Unable to find a collection matching {provider} and {short_name}{version_msg} in either CMR venue. Check that the collection exists and the EDL user has permission to read it'
        )

    return match_dict


def validate_img_variables(collections: dict[str, Any], img_variables: list[str] | None) -> list[str]:
    print('Verifying provided imgVariables are present in the collection...')
    verified_vars = []
    for venue, collection in collections.items():
        if collection == {}:
            print(f'Collection is unavailable on {venue}, skipping...')
            continue
        associations = collection['meta']['associations']
        variables = associations.get('variables')
        if not variables:
            warnings.warn(f'No variables found for the collection in {venue}')
            continue

        for variable in variables:
            search_params = {'concept_id': variable}
            umm_json = query_cmr(venue, search_params, 'variables')
            num_matches = len(umm_json)
            if num_matches < 1:
                warnings.warn(f'Unable to find variable {variable} referenced in collection umm_json')
                continue
            umm_var = umm_json[0]
            umm_fields = umm_var.get('umm', {})
            var_name = umm_fields.get('Name', '')
            if var_name not in verified_vars and img_variables and var_name in img_variables:
                verified_vars.append(var_name)

    if not verified_vars:
        if img_variables:
            warnings.warn(
                'No data variables found in the collection, please ensure that this is expected '
                'for the collection or bignbit will fail during its Harmony API call when using '
                'this dataset config.'
            )
            verified_vars.extend(img_variables)
        else:
            verified_vars = ['all']

    return verified_vars


def build_config_json(img_variables: list[str], **kwargs) -> dict[str, Any]:
    """Builds the config from the list of keyword arguments."""
    SPECIAL_KEYWORDS = ['no_sendToHarmony', 'operaHLSTreatment', 'imgVariables', 'dimensions', 'height', 'width']
    dimensions = kwargs.get('dimensions')
    if dimensions:
        big_height = dimensions[0]
        big_width = dimensions[1]
    else:
        big_height = None
        big_width = None
    config_template = {
        'sendToHarmony': not kwargs['no_sendToHarmony'],
        'operaHLSTreatment': kwargs['operaHLSTreatment'],
        'imgVariables': [{'id': var_name} for var_name in img_variables],
        'height': big_height,
        'width': big_width,
    }
    for k, v in kwargs.items():
        if k not in SPECIAL_KEYWORDS and v is not None:
            config_template[k] = v
    if config_template.get('singleDayNumber'):
        config_template['dataDayStrategy'] = 'single_day_of_year'

    return config_template


def create_dataset_config(
        provider: str,
        short_name: str,
        s3_destination: list[str] | None,
        **kwargs,
) -> str:
    """Main function to create a dataset config for a single collection."""
    version = kwargs.pop('version', None)
    collections = validate_collection(provider, short_name, kwargs.get('collectionId'), version)
    if version and not kwargs.get('collectionId'):
        # Concept ids are venue-specific, so only use the one found if it is unambiguous
        found_ids = {venue: c['meta']['concept-id'] for venue, c in collections.items() if c}
        if len(found_ids) == 1:
            venue, kwargs['collectionId'] = next(iter(found_ids.items()))
            print(f'Setting collectionId to {kwargs["collectionId"]} (found in {venue} only)')
        else:
            warnings.warn(
                f'Version {version} matched in multiple venues ({", ".join(found_ids)}) with different '
                'concept ids; collectionId was not set. Use --collectionId to choose one.'
            )
    img_variables = validate_img_variables(collections, kwargs.get('imgVariables'))
    dataset_config_json = build_config_json(img_variables, **kwargs)
    config_fname = f'{short_name}.cfg'
    if not s3_destination:
        with open(config_fname, 'w') as f:
            json.dump(dataset_config_json, f, indent=2)
        print(f'Wrote {config_fname} to file')
    else:
        config_key = f'{s3_destination[1]}/{config_fname}'
        config_bytes = json.dumps(dataset_config_json, indent=2).encode()
        print(f'Uploading dataset config to s3://{s3_destination[0]}/{config_key}...', end=None)
        s3_client = boto3.client('s3')
        s3_client.put_object(
            Bucket=s3_destination[0],
            Key=config_key,
            Body=config_bytes,
            ContentType='application/json',
        )
        print(' Success!')

    return f'{short_name}.cfg'


def cli() -> argparse.Namespace:
    """Initialize and parse CLI arguments"""
    parser = argparse.ArgumentParser(
        description='Create a dataset configuration file for bignbit using CMR metadata',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Create a dataset config for GHRSST MUR25 collection
  create_dataset_config.py POCLOUD MUR25-JPL-L4-GLOB-v04.2 --imgVariables analysed_sst sst_anomaly sea_ice_fraction
''',
    )

    parser.add_argument(
        'provider',
        type=str,
        help='CMR provider name for the collection (e.g., POCLOUD)',
    )
    parser.add_argument(
        'shortName',
        type=str,
        help='Short name of the collection'
    )
    parser.add_argument(
        '--collectionId',
        type=str,
        help=(
            'CMR collection id, use if the collection name is ambiguous. '
            'WARNING: since collection ids are venue-specific, the output config '
            'will need to be re-generated when deploying to a separate venue.'
        )
    )
    parser.add_argument(
        '--version',
        type=str,
        help=(
            'Collection version (e.g., 003). Narrows the CMR lookup to that version and prints the '
            'matching collection id for each venue. If the version matches in only one venue and '
            '--collectionId is not given, that collection id is written to the dataset config.'
        )
    )
    parser.add_argument(
        '--no-sendToHarmony',
        action='store_true',
        help=(
            'Specifies that the collection should not be processed by Harmony API. '
            'Default is `sendToHarmony = True`.'
        )
    )
    parser.add_argument(
        '--operaHLSTreatment',
        action='store_true',
        help=(
            'Specifies whether the collection should receive the "OPERA HLS Treatment". '
            'Default is `operaHLSTreatment = False`'
        )
    )
    parser.add_argument(
        '--imageFilenameRegex',
        type=str,
        help=(
            'Regular expression used to identify which file in a granule should be used as '
            'the image file. Uses first if multiple files match'
        )
    )
    parser.add_argument(
        '--nrtFilenameRegex',
        type=str,
        help=(
            'Regular expression used to identify whether a granule should be treated as NRT or '
            'standard. All files are assumed standard if not specified.'
        )
    )
    parser.add_argument(
        '--imgVariables',
        type=str,
        nargs='+',
        default=['all'],
        metavar='VAR_NAME',
        help=(
            'List of variable names (not UMM-Var concept ids) to use for generating browse '
            'image(s). If none are provided, the default of "all" is used'
        )
    )
    parser.add_argument(
        '--dimensions',
        type=int,
        nargs=2,
        metavar=('HEIGHT', 'WIDTH'),
        help=(
            'Optional override to specify the height and width that each browse image '
            'should have when processed by Harmony.'
        )
    )
    parser.add_argument(
        '--scaleExtentPolar',
        type=int,
        nargs=4,
        metavar=('MINX','MINY','MAXX','MAXY'),
        help=(
            'Controls the geographic extent of polar-projected browse image outputs. '
            'This keyword is ignored if `outputCrs` does not contain EPSG:3413 or EPSG:3031 '
            '(polar stereographic projections used by GIBS)'
        )
    )
    parser.add_argument(
        '--singleDayNumber',
        type=str,
        metavar='JJJ',
        help=(
            'All granules in this dataset will use the day of year specified in this keyword. '
            'ex: "001" for January 1st'
        )
    )
    parser.add_argument(
        '--subdaily',
        action='store_true',
        help=(
            'Set to true if granules contain subdaily data. This will send `DataDateTime` '
            'metadata to GIBS as described in the GIBS ICD'
        )
    )
    parser.add_argument(
        '--outputCrs',
        type=str,
        nargs='+',
        choices=['EPSG:4326', 'EPSG:3413', 'EPSG:3031', 'EPSG:3857'],
        default=['EPSG:4326'],
        help=(
            'List of output projections or coordinate reference systems for which to produce '
            'browse images. Applies to all variables in granule. GIBS-compatible values are '
            'EPSG:4326, EPSG:3413, or EPSG:3031'
        )
    )
    parser.add_argument(
        '--reprojection',
        action='store_true',
        help=(
            'Set to true to force reprojection for all granules. This will send the `outputCrs` '
            'parameter in Harmony API calls unconditionally. Warning: if the Harmony service used '
            'to generate browse images does not support reprojection, jobs will fail.'
        )
    )
    parser.add_argument(
        '--requesterPays',
        action='store_true',
        help=(
            'Set to true if the source data bucket is configured as requester pays, meaning '
            'the requester is charged for access. Default is `requesterPays = False`.'
        )
    )
    parser.add_argument(
        '--s3Destination',
        type=str,
        nargs=2,
        metavar=('BUCKET', 'KEY'),
        help=(
            'Specify an S3 URI (bucket key, ex: --s3Destination podaac-bignbit-sit-svc-internal big-config) '
            'to upload the config. Do not specify the filename, it is auto-generated.'
        )
    )

    args = parser.parse_args()
    return args


def main() -> int:
    args = cli()
    arg_dict = vars(args)
    try:
        create_dataset_config(
            args.provider,
            args.shortName,
            arg_dict.get('s3Destination'),
            **{k: v for k, v in arg_dict.items() if k not in ('provider', 'shortName', 's3Destination')}
        )

        return 0
    except Exception as e:
        print(f'Unexpected error creating dataset config: {e}', file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    main()