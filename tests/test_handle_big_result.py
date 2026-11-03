"""Unit tests for handle_big_result module"""
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ET
from unittest.mock import MagicMock, patch
import boto3
import pytest
import responses
from botocore.response import StreamingBody
from moto import mock_aws

import bignbit.utils
from bignbit.handle_big_result import (
    _resolve_static_data_day,
    construct_cnm,
    create_metadata_xml,
    generate_metadata,
    get_mdxml_cnm_file_meta,
    IncompleteImageSetError,
    process_harmony_results,
    write_cnm_message
)
from bignbit.image_set import ImageSet

@pytest.mark.vcr
@mock_aws
def test_process_harmony_results():
    """Test pulling results of a harmony job from s3."""
    bignbit.utils.AWS_ACCOUNT_ID = None
    bignbit.utils.ED_USER = 'test'
    bignbit.utils.ED_PASS = 'test'
    job_id = '3d276f84-56e2-4f0a-acb2-35b9fcaaa317'

    # Create mock S3 bucket and populate with test data
    s3_client = boto3.client('s3', region_name='us-west-2')
    bucket_name = 'harmony-uat-staging'
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={'LocationConstraint': 'us-west-2'}
    )

    # Dummy files for harmony request
    image_key = f'public/{job_id}/9202859/PREFIRE_SAT2_2B-FLX_S07_R00_20210721013413_03040.nc.G00.png'
    image_data = b'fake image data for testing'
    wld_key = f'public/{job_id}/9202859/PREFIRE_SAT2_2B-FLX_S07_R00_20210721013413_03040.nc.G00.pgw'
    wld_data = b'fake world file for testing'
    aux_key = f'public/{job_id}/9202859/PREFIRE_SAT2_2B-FLX_S07_R00_20210721013413_03040.nc.G00.png.aux.xml'
    aux_data = b'fake png aux xml metadata for testing'
    expected_checksums = {}
    for key, data in [(image_key, image_data), (wld_key, wld_data), (aux_key, aux_data)]:
        s3_client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=data
        )
        expected_checksums[key.split('/')[-1]] = hashlib.md5(data).hexdigest()

    harmony_job = {
        'job': job_id,
        'granule_id': 'PREFIRE_SAT2_2B-FLX_S07_R00_20210721013413_03040.nc.G00.tif',
        'granule_concept_id': 'G1263096192-EEDTEST',
        'variable': 'flx',
        'output_crs': 'EPSG:3413',
        'status': 'successful'
    }
    cmr_environment = 'UAT'
    file_dicts = process_harmony_results(harmony_job, cmr_environment)
    assert len(file_dicts) == 3
    for file in file_dicts:
        assert file['output_crs'] == 'EPSG:3413'
        assert file['variable'] == 'flx'
        # Checksum should come from the S3 ETag (MD5 of the object for single-part uploads)
        assert file['checksumType'] == 'md5'
        assert file['checksum'] == expected_checksums[file['fileName']]

@pytest.mark.vcr
def test_process_harmony_results_no_data():
    """Test case where a harmony job returned no data and was passed empty."""
    bignbit.utils.ED_USER = 'test'
    bignbit.utils.ED_PASS = 'test'

    harmony_job = {}
    cmr_environment = 'UAT'
    file_dicts = process_harmony_results(harmony_job, cmr_environment)
    assert file_dicts == []


@mock_aws
def test_process_harmony_results_fresh_harmony_session():
    """
    Regression test: on a fresh (unauthenticated) Harmony session, every request is first
    redirected through the EDL login. requests turns a redirected POST into a GET, so polling
    POST /jobs/status arrives as GET /jobs/status, which Harmony rejects ("Invalid format for
    Job ID 'status'"). Fetching results for an already-finished job must not poll that endpoint.
    """
    bignbit.utils.HARMONY_CLIENT = None
    bignbit.utils.ED_USER = 'test'
    bignbit.utils.ED_PASS = 'test'
    harmony = 'https://harmony.uat.earthdata.nasa.gov'
    edl = 'https://uat.urs.earthdata.nasa.gov/oauth/authorize'
    job_id = '0c8ce8fb-741b-4f73-9c05-2ebecc97c485'
    session = {'authenticated': False, 'return_to': None}

    def require_login(handler):
        def callback(request):
            if not session['authenticated']:
                session['return_to'] = request.url
                return 303, {'Location': edl}, 'See Other'
            return handler(request)
        return callback

    def oauth_redirect(_request):
        session['authenticated'] = True
        return 307, {'Location': session['return_to']}, ''

    def job_page(request):
        requested_id = request.url.split('/jobs/')[1].split('?')[0]
        if requested_id == 'status':
            body = {'code': 'harmony.RequestValidationError',
                    'description': "Error: Invalid format for Job ID 'status'. Job ID must be a UUID."}
            return 400, {'Content-Type': 'application/json'}, json.dumps(body)
        body = {'jobID': requested_id, 'status': 'successful', 'progress': 100, 'message': '', 'links': []}
        return 200, {'Content-Type': 'application/json'}, json.dumps(body)

    batch_status = {'jobStatuses': [{'jobID': job_id, 'status': 'successful', 'progress': 100}],
                    'notFoundJobIDs': []}
    with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
        rsps.add(responses.GET, edl, status=302, headers={'Location': f'{harmony}/oauth2/redirect?code=x'})
        rsps.add_callback(responses.GET, f'{harmony}/oauth2/redirect', callback=oauth_redirect)
        rsps.add_callback(responses.POST, f'{harmony}/jobs/status', callback=require_login(
            lambda _request: (200, {'Content-Type': 'application/json'}, json.dumps(batch_status))))
        rsps.add_callback(responses.GET, re.compile(f'{harmony}/jobs/.*'), callback=require_login(job_page))

        harmony_job = {'job': job_id, 'variable': 'analysed_sst', 'output_crs': 'EPSG:4326', 'status': 'successful'}
        assert process_harmony_results(harmony_job, 'UAT') == []

        assert not [c for c in rsps.calls if c.request.url.split('?')[0].endswith('/jobs/status')]
    bignbit.utils.HARMONY_CLIENT = None


@mock_aws
def test_generate_metadata():
    """Test generating image metadata xml end-to-end for a single image set."""
    bignbit.utils.AWS_ACCOUNT_ID = None
    # image metadata xml will be uploaded within the function, so the bucket needs to be mocked
    s3_client = boto3.client('s3', region_name='us-west-2')
    bucket_name = 'svc-bignbit-podaac-sit-svc-staging'
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={'LocationConstraint': 'us-west-2'}
    )

    image_set = ImageSet(
        name='test_2021202_EPSG:3413!C1263096190-EEDTEST',
        image={
            'fileName': 'PREFIRE_SAT2_2B-FLX_S07_R00_20210721013413_03040.nc.G00.png',
            'bucket': 'svc-bignbit-podaac-sit-svc-staging',
            'key': 'bignbit-harmony-output/prefire_sat2_2b-flx_eedtest/test/test/PREFIRE_SAT2_2B-FLX_S07_R00_20210721013413_03040.nc.G00.png',
            'type': 'browse',
            'subtype': 'png',
            'variable': 'flx',
            'output_crs': 'EPSG:3413',
            'dataday': '2021202'
        },
        world_file={
            'fileName': 'PREFIRE_SAT2_2B-FLX_S07_R00_20210721013413_03040.nc.G00.pgw',
            'bucket': 'svc-bignbit-podaac-sit-svc-staging',
            'key': 'bignbit-harmony-output/prefire_sat2_2b-flx_eedtest/test/test/PREFIRE_SAT2_2B-FLX_S07_R00_20210721013413_03040.nc.G00.pgw',
            'type': 'metadata',
            'subtype': 'world file',
            'variable': 'flx',
            'output_crs': 'EPSG:3413',
            'dataday': '2021202'
        }
    )
    begin = '2021-07-21T01:34:13.165Z'
    mid = '2021-07-21T02:21:51.985Z'
    end = '2021-07-21T03:09:30.805Z'
    data_day = '2021202'
    subdaily = True
    partial_id = None
    updated_image_set = generate_metadata(
        image_set,
        begin, mid, end, data_day,
        subdaily,
        partial_id
    )
    assert updated_image_set is not None
    assert updated_image_set.image_metadata['fileName'] == 'PREFIRE_SAT2_2B-FLX_S07_R00_20210721013413_03040.nc.G00.xml'
    assert updated_image_set.image_metadata['type'] == 'metadata'
    assert updated_image_set.image_metadata['subtype'] == 'ImageMetadata-v1.2'


def test_create_metadata_xml_subdaily():
    """Test creating metadata XML for subdaily products"""
    begin = '2021-07-21T01:34:13.165Z'
    mid = '2021-07-21T02:21:51.985Z'
    end = '2021-07-21T03:09:30.805Z'
    data_day = '2021202'
    subdaily = True
    partial_id = None

    xml_bytes = create_metadata_xml(begin, mid, end, data_day, subdaily, partial_id)

    # Parse the XML to verify structure
    root = ET.fromstring(xml_bytes)
    assert root.tag == 'ImageryMetadata'

    # Check required elements
    assert root.find('ProviderProductionDateTime') is not None
    data_start = root.find('DataStartDateTime')
    assert data_start is not None and data_start.text == begin
    data_mid = root.find('DataMidDateTime')
    assert data_mid is not None and data_mid.text == mid
    data_end = root.find('DataEndDateTime')
    assert data_end is not None and data_end.text == end

    # For subdaily, should have DataDateTime instead of DataDay
    data_datetime = root.find('DataDateTime')
    assert data_datetime is not None
    assert data_datetime.text == begin
    assert root.find('DataDay') is None

    # No partial ID for this test
    assert root.find('PartialId') is None


def test_create_metadata_xml_daily():
    """Test creating metadata XML for daily products"""
    begin = '2021-07-21T00:00:00.000Z'
    mid = '2021-07-21T12:00:00.000Z'
    end = '2021-07-21T23:59:59.999Z'
    data_day = '2021202'
    subdaily = False
    partial_id = None

    xml_bytes = create_metadata_xml(begin, mid, end, data_day, subdaily, partial_id)

    root = ET.fromstring(xml_bytes)

    # For daily products, should have DataDay instead of DataDateTime
    data_day_elem = root.find('DataDay')
    assert data_day_elem is not None
    assert data_day_elem.text == data_day
    assert root.find('DataDateTime') is None


def test_create_metadata_xml_with_partial_id():
    """Test creating metadata XML with partial ID (OPERA HLS)"""
    begin = '2021-07-21T00:00:00.000Z'
    mid = '2021-07-21T12:00:00.000Z'
    end = '2021-07-21T23:59:59.999Z'
    data_day = '2021202'
    subdaily = False
    partial_id = '11SKA'

    xml_bytes = create_metadata_xml(begin, mid, end, data_day, subdaily, partial_id)

    root = ET.fromstring(xml_bytes)

    # Should have PartialId element
    partial_id_elem = root.find('PartialId')
    assert partial_id_elem is not None
    assert partial_id_elem.text == partial_id


def test_get_mdxml_cnm_file_meta():
    """Test creating CNM file metadata for image metadata XML"""
    image_metadata_xml = b'<ImageryMetadata>test</ImageryMetadata>'

    cnm_file_meta = {
        'fileName': 'test_image.png',
        'bucket': 'test-bucket',
        'key': 'path/to/test_image.png',
        'checksum': 'abc123',
        'checksumType': 'md5',
        'variable': 'temperature',
        'output_crs': 'EPSG:4326'
    }

    result = get_mdxml_cnm_file_meta(image_metadata_xml, cnm_file_meta)

    # Check that basic properties are copied
    assert result['bucket'] == 'test-bucket'
    assert result['variable'] == 'temperature'
    assert result['output_crs'] == 'EPSG:4326'

    # Check that file-specific properties are updated
    assert result['fileName'] == 'test_image.xml'
    assert result['key'] == 'path/to/test_image.xml'
    assert result['type'] == 'metadata'
    assert result['subtype'] == 'ImageMetadata-v1.2'

    # Check that checksum is SHA512
    assert result['checksumType'] == 'SHA512'
    expected_checksum = hashlib.sha512(image_metadata_xml).hexdigest()
    assert result['checksum'] == expected_checksum

    # Check that size is set
    assert result['size'] == len(image_metadata_xml)


@mock_aws
def test_write_cnm_message():
    """Test writing CNM message to S3"""
    bignbit.utils.AWS_ACCOUNT_ID = None
    # Create mock S3 bucket
    s3_client = boto3.client('s3', region_name='us-west-2')
    bucket_name = 'test-audit-bucket'
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={'LocationConstraint': 'us-west-2'}
    )

    # Create test image set
    image_set = ImageSet(
        name='test_image_2021202_EPSG:4326!G1234567890-TESTPROV',
        image={
            'fileName': 'test_image.png',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.png',
            'variable': 'temperature',
            'output_crs': 'EPSG:4326'
        },
        world_file={
            'fileName': 'test_image.pgw',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.pgw'
        },
        image_metadata={
            'fileName': 'test_image.xml',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.xml',
            'type': 'metadata',
            'subtype': 'ImageMetadata-v1.2'
        }
    )

    cmr_provider = 'TESTPROV'
    collection_name = 'TEST_COLLECTION'
    granule_id = 'test_granule_id'
    isNrt = False
    bignbit_audit_path = 'bignbit-cnm-output'

    # Write CNM message (Standard)
    cnm_key = write_cnm_message(
        image_set,
        cmr_provider,
        collection_name,
        granule_id,
        isNrt,
        bucket_name,
        bignbit_audit_path
    )

    # Verify the key format
    assert cnm_key.startswith(f'{bignbit_audit_path}/')
    assert collection_name in cnm_key
    assert granule_id in cnm_key
    assert cnm_key.endswith('.cnm.json')
    # Colons in the submission timestamp should be replaced with underscores
    assert ':' not in cnm_key

    # Verify the file was uploaded to S3
    response = s3_client.get_object(Bucket=bucket_name, Key=cnm_key)
    cnm_content = response['Body'].read()

    # Verify it's valid JSON
    cnm_message = json.loads(cnm_content)
    assert cnm_message['provider'] == cmr_provider
    assert cnm_message['identifier'] == image_set.name


def test_construct_cnm_with_crs():
    """Test constructing CNM message with output CRS suffix"""
    image_set = ImageSet(
        name='test_image_2021202_EPSG:3413!G1234567890-TESTPROV',
        image={
            'fileName': 'test_image.png',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.png',
            'variable': 'temperature',
            'output_crs': 'EPSG:3413'
        },
        world_file={
            'fileName': 'test_image.pgw',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.pgw'
        },
        image_metadata={
            'fileName': 'test_image.xml',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.xml',
            'type': 'metadata',
            'subtype': 'ImageMetadata-v1.2'
        }
    )

    cmr_provider = 'TESTPROV'
    collection_name = 'TEST_COLLECTION'

    cnm = construct_cnm(image_set, cmr_provider, collection_name, False)

    # Verify CNM structure
    assert cnm['version'] == '1.5.1'
    assert cnm['duplicationid'] == image_set.name
    assert cnm['identifier'] == image_set.name
    assert cnm['provider'] == cmr_provider
    assert cnm['submissionTime'] is not None

    # Verify collection name includes CRS suffix (N for EPSG:3413) and no NRT string
    assert cnm['collection'] == 'TEST_COLLECTION_temperature_N'

    # Verify product structure
    assert 'product' in cnm
    assert 'files' in cnm['product']
    assert len(cnm['product']['files']) == 3  # image, world file, metadata


def test_construct_cnm_without_crs():
    """Test constructing CNM message without output CRS and no world file"""
    image_set = ImageSet(
        name='test_image_2021202!G1234567890-TESTPROV',
        image={
            'fileName': 'test_image.tif',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.tif',
            'variable': 'water_index'
        },
        image_metadata={
            'fileName': 'test_image.xml',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.xml',
            'type': 'metadata',
            'subtype': 'ImageMetadata-v1.2'
        }
    )

    cmr_provider = 'TESTPROV'
    collection_name = 'OPERA_L3_DSWX-HLS'

    cnm = construct_cnm(image_set, cmr_provider, collection_name, False)

    # Verify collection name doesn't have CRS suffix when output_crs is not present, and no NRT string
    assert cnm['collection'] == 'OPERA_L3_DSWX-HLS_water_index'


def test_construct_cnm_crs_suffixes():
    """Test that different CRS values produce correct suffixes"""
    test_cases = [
        ('EPSG:4326', 'LL'),  # Geographic
        ('EPSG:3413', 'N'),   # North Polar
        ('EPSG:3031', 'S'),   # South Polar
    ]

    for crs, expected_suffix in test_cases:
        image_set = ImageSet(
            name=f'test_{crs}',
            image={
                'fileName': 'test.png',
                'bucket': 'bucket',
                'key': 'key',
                'variable': 'var',
                'output_crs': crs
            },
            world_file={
                'fileName': 'test.pgw',
                'bucket': 'bucket',
                'key': 'key'
            },
            image_metadata={
                'fileName': 'test.xml',
                'bucket': 'bucket',
                'key': 'key',
                'type': 'metadata',
                'subtype': 'ImageMetadata-v1.2'
            }
        )

        cnm = construct_cnm(image_set, 'PROV', 'COLLECTION', False)
        assert cnm['collection'] == f'COLLECTION_var_{expected_suffix}'


def test_construct_cnm_nrt_with_crs():
    """Test that isNrt=True inserts _NRT before the CRS suffix in the collection name"""
    image_set = ImageSet(
        name='test_image_2021202_EPSG:3413!G1234567890-TESTPROV',
        image={
            'fileName': 'test_image.png',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.png',
            'variable': 'temperature',
            'output_crs': 'EPSG:3413'
        },
        world_file={
            'fileName': 'test_image.pgw',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.pgw'
        },
        image_metadata={
            'fileName': 'test_image.xml',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.xml',
            'type': 'metadata',
            'subtype': 'ImageMetadata-v1.2'
        }
    )

    cnm = construct_cnm(image_set, 'TESTPROV', 'TEST_COLLECTION', True)

    # _NRT should appear between the variable and the CRS suffix
    assert cnm['collection'] == 'TEST_COLLECTION_temperature_NRT_N'


def test_construct_cnm_nrt_without_crs():
    """Test that isNrt=True appends _NRT to the collection name when no CRS is present"""
    image_set = ImageSet(
        name='test_image_2021202!G1234567890-TESTPROV',
        image={
            'fileName': 'test_image.tif',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.tif',
            'variable': 'water_index'
        },
        image_metadata={
            'fileName': 'test_image.xml',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.xml',
            'type': 'metadata',
            'subtype': 'ImageMetadata-v1.2'
        }
    )

    cnm = construct_cnm(image_set, 'TESTPROV', 'OPERA_L3_DSWX-HLS', True)

    # _NRT should be appended after the variable when no output_crs is present
    assert cnm['collection'] == 'OPERA_L3_DSWX-HLS_water_index_NRT'


def test_construct_cnm_crs_suffixes_nrt():
    """Test that all CRS values produce correct suffixes when isNrt=True"""
    test_cases = [
        ('EPSG:4326', 'LL'),
        ('EPSG:3413', 'N'),
        ('EPSG:3031', 'S'),
    ]

    for crs, expected_suffix in test_cases:
        image_set = ImageSet(
            name=f'test_{crs}',
            image={
                'fileName': 'test.png',
                'bucket': 'bucket',
                'key': 'key',
                'variable': 'var',
                'output_crs': crs
            },
            world_file={
                'fileName': 'test.pgw',
                'bucket': 'bucket',
                'key': 'key'
            },
            image_metadata={
                'fileName': 'test.xml',
                'bucket': 'bucket',
                'key': 'key',
                'type': 'metadata',
                'subtype': 'ImageMetadata-v1.2'
            }
        )

        cnm = construct_cnm(image_set, 'PROV', 'COLLECTION', True)
        assert cnm['collection'] == f'COLLECTION_var_NRT_{expected_suffix}'


@mock_aws
def test_write_cnm_message_nrt():
    """Test that write_cnm_message with isNrt=True produces a collection name containing _NRT"""
    bignbit.utils.AWS_ACCOUNT_ID = None
    s3_client = boto3.client('s3', region_name='us-west-2')
    bucket_name = 'test-audit-bucket'
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={'LocationConstraint': 'us-west-2'}
    )

    image_set = ImageSet(
        name='test_image_2021202_EPSG:4326!G1234567890-TESTPROV',
        image={
            'fileName': 'test_image.png',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.png',
            'variable': 'temperature',
            'output_crs': 'EPSG:4326'
        },
        world_file={
            'fileName': 'test_image.pgw',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.pgw'
        },
        image_metadata={
            'fileName': 'test_image.xml',
            'bucket': 'test-bucket',
            'key': 'path/to/test_image.xml',
            'type': 'metadata',
            'subtype': 'ImageMetadata-v1.2'
        }
    )

    cnm_key = write_cnm_message(
        image_set,
        'TESTPROV',
        'TEST_COLLECTION',
        'test_granule_id',
        True,
        bucket_name,
        'bignbit-cnm-output'
    )

    # Verify the key has no colons and was uploaded to S3
    assert ':' not in cnm_key
    response = s3_client.get_object(Bucket=bucket_name, Key=cnm_key)
    cnm_message = json.loads(response['Body'].read())

    # Verify _NRT appears in the collection name
    assert '_NRT_' in cnm_message['collection'] or cnm_message['collection'].endswith('_NRT')


# --------------------------------------------------------------------------------------
# Tests for _resolve_static_data_day
# --------------------------------------------------------------------------------------

def test_resolve_static_data_day_no_strategy():
    """Returns None when dataDayStrategy is absent."""
    assert _resolve_static_data_day({}) is None


def test_resolve_static_data_day_different_strategy():
    """Returns None when dataDayStrategy is set to something other than 'single_day_of_year'."""
    config = {'dataDayStrategy': 'some_other_strategy', 'singleDayNumber': 100}
    assert _resolve_static_data_day(config) is None


def test_resolve_static_data_day_valid():
    """Returns the configured day number when it is within the valid range."""
    config = {'dataDayStrategy': 'single_day_of_year', 'singleDayNumber': 100}
    assert _resolve_static_data_day(config) == 100


def test_resolve_static_data_day_boundary_values():
    """Returns 1 and 366 as valid boundary day numbers."""
    assert _resolve_static_data_day({'dataDayStrategy': 'single_day_of_year', 'singleDayNumber': 1}) == 1
    assert _resolve_static_data_day({'dataDayStrategy': 'single_day_of_year', 'singleDayNumber': 366}) == 366


def test_resolve_static_data_day_too_low():
    """Returns 1 (fallback) when configured day is below the valid range."""
    config = {'dataDayStrategy': 'single_day_of_year', 'singleDayNumber': 0}
    assert _resolve_static_data_day(config) == 1


def test_resolve_static_data_day_too_high():
    """Returns 1 (fallback) when configured day exceeds 366."""
    config = {'dataDayStrategy': 'single_day_of_year', 'singleDayNumber': 367}
    assert _resolve_static_data_day(config) == 1


def test_resolve_static_data_day_default_number():
    """Returns 1 (default singleDayNumber) when the key is absent but strategy is set."""
    config = {'dataDayStrategy': 'single_day_of_year'}
    assert _resolve_static_data_day(config) == 1


# --------------------------------------------------------------------------------------
# Additional tests for generate_metadata and process_harmony_results
# --------------------------------------------------------------------------------------

def test_generate_metadata_raises_when_image_missing():
    """IncompleteImageSetError is raised when the image_set has no image."""
    image_set = ImageSet(name='empty_set', image={})
    with pytest.raises(IncompleteImageSetError):
        generate_metadata(image_set, 'begin', 'mid', 'end', 'day', False, None)


@mock_aws
@patch('bignbit.utils.get_harmony_client')
def test_process_harmony_results_empty_result_urls(mock_get_harmony_client):
    """Returns an empty list when a valid Harmony job produces no result files."""
    bignbit.utils.AWS_ACCOUNT_ID = None

    mock_harmony_client = MagicMock()
    mock_harmony_client.result_urls.return_value = iter([])
    mock_get_harmony_client.return_value = mock_harmony_client

    harmony_job = {'job': 'valid-job-id', 'variable': 'temperature', 'output_crs': 'EPSG:4326'}
    result = process_harmony_results(harmony_job, 'UAT')
    assert result == []


@mock_aws
@patch('bignbit.utils.get_harmony_client')
def test_process_harmony_results_all_variable_omitted(mock_get_harmony_client):
    """When variable is 'all', the 'variable' key is omitted from each file dict."""
    bignbit.utils.AWS_ACCOUNT_ID = None

    s3_client = boto3.client('s3', region_name='us-west-2')
    bucket_name = 'test-harmony-bucket'
    s3_client.create_bucket(
        Bucket=bucket_name,
        CreateBucketConfiguration={'LocationConstraint': 'us-west-2'}
    )
    image_key = 'path/to/result_image.png'
    s3_client.put_object(Bucket=bucket_name, Key=image_key, Body=b'fake image data')

    mock_harmony_client = MagicMock()
    mock_harmony_client.result_urls.return_value = iter([f's3://{bucket_name}/{image_key}'])
    mock_get_harmony_client.return_value = mock_harmony_client

    harmony_job = {'job': 'valid-job-id', 'variable': 'all', 'output_crs': 'EPSG:4326'}
    result = process_harmony_results(harmony_job, 'UAT')

    assert len(result) == 1
    assert 'variable' not in result[0]
    assert result[0]['output_crs'] == 'EPSG:4326'
    assert result[0]['fileName'] == 'result_image.png'


@patch('bignbit.utils.get_harmony_client')
@patch('bignbit.handle_big_result.boto3')
def test_process_harmony_results_multipart_etag_fallback(mock_boto3, mock_get_harmony_client):
    """When ETag has a multipart suffix, falls back to streaming the object to compute MD5."""
    bignbit.utils.AWS_ACCOUNT_ID = '123456789012'

    image_data = b'fake image data for multipart test'
    expected_checksum = hashlib.md5(image_data).hexdigest()

    bucket_name = 'test-harmony-bucket'
    image_key = 'path/to/result_image.png'

    mock_s3_instance = MagicMock()
    mock_s3_instance.head_object.return_value = {'ETag': '"abcdef1234567890abcdef1234567890-5"'}
    mock_s3_instance.get_object.return_value = {
        'Body': StreamingBody(io.BytesIO(image_data), len(image_data))
    }
    mock_boto3.client.return_value = mock_s3_instance

    mock_harmony_client = MagicMock()
    mock_harmony_client.result_urls.return_value = iter([f's3://{bucket_name}/{image_key}'])
    mock_get_harmony_client.return_value = mock_harmony_client

    harmony_job = {'job': 'valid-job-id', 'variable': 'temperature', 'output_crs': 'EPSG:4326'}
    result = process_harmony_results(harmony_job, 'UAT')

    assert len(result) == 1
    assert result[0]['checksumType'] == 'md5'
    assert result[0]['checksum'] == expected_checksum
    mock_s3_instance.get_object.assert_called_once()
