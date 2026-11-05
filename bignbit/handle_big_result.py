"""
Deals with metadata generation and uploading after Browse Image Generation (BIG)
Retrieves a list of files from the browse image processing, separates into image sets,
  creates image metadata, and uploads CNM summary to S3
"""
import hashlib
import json
import logging
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlparse

import boto3
from cumulus_logger import CumulusLogger
from cumulus_process import Process
from harmony import LinkType

from bignbit import utils
from bignbit.image_set import build_image_sets, to_cnm_product_dict, ImageSet

CUMULUS_LOGGER = CumulusLogger('handle_big_result')

GIBS_CRS_NAME_TO_SUFFIX = {
    'EPSG:4326': 'LL',
    'EPSG:3413': 'N',
    'EPSG:3031': 'S'
}


class IncompleteImageSetError(Exception):
    """
    Exception thrown if Pobit can not find a complete image set while processing the input
    """

    def __init__(self, message):
        super().__init__(message)


class CMA(Process):
    """
    A cumulus message adapter
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = CUMULUS_LOGGER

    def process(self) -> dict[str, Any]:
        """
        Takes a list of files resulting from Browse Image Generation (BIG) and generates metadata
        XMLs, then builds a list of image sets and saves those as CNM messages on S3, which are sent
        to GIBS.

        Returns
        -------
        dict
          A dict with the "pobit" field containing a list of S3 URLs of CNM messages that will be
            transferred to GIBS
        """

        # ------------------------------------------------------------------------------------------
        # Get relevant information from state input and task config
        # ------------------------------------------------------------------------------------------
        # If the result list comes from the Harmony branch path, this will be a nested list of
        # harmony job dictionaries, and we need to retrieve the file information from harmony job
        # status page. If the result list comes from Apply OPERA HLS Treatment, it will be a list
        # of files.
        result_list = self.input.get('big')
        granule_umm_json = self.input.get('granule_umm_json')
        # Throw KeyError if the state input doesn't contain the dataset config
        dataset_config = self.input['datasetConfigurationForBIG']['config']

        static_data_day = _resolve_static_data_day(dataset_config)

        subdaily = dataset_config.get('subdaily', False)
        try:
            cmr_concept_id = self.input['granules'][0]['cmrConceptId']
        except KeyError:
            cmr_concept_id = urlparse(
                self.input['granules'][0]['cmrLink']
            ).path.rstrip('/').split('/')[-1].split('.')[0]
        cmr_environment = self.config.get('cmr_environment', 'UAT')
        cmr_provider = self.config.get('cmr_provider')
        collection_name = self.config.get('collection')
        bignbit_audit_bucket = self.config.get('bignbit_audit_bucket')
        bignbit_audit_path = self.config.get('bignbit_audit_path')
        granule_id = self.input['granules'][0]['granuleId']

        nrt_filename_regex = dataset_config.get('nrtFilenameRegex')
        is_nrt = False
        if nrt_filename_regex:
            if re.search(nrt_filename_regex, granule_id):
                is_nrt = True

        try:
            partial_id = utils.extract_mgrs_grid_code(granule_umm_json)
        except KeyError:
            # No partial id for this granule
            partial_id = None

        # ------------------------------------------------------------------------------------------
        # Retrieve list of files resulting from BIG process
        # ------------------------------------------------------------------------------------------
        if result_list and any(isinstance(el, list) for el in result_list):
            # flatten list of lists from Harmony map state (per variable, per output crs)
            harmony_job_refs = [item for sublist in result_list for item in sublist]
            cma_file_list = []
            for ref in harmony_job_refs:
                if ref == {} or ref is None:
                    # Skip harmony jobs that failed with HarmonyJobNoDataError, the pass state
                    continue
                file_sublist = process_harmony_results(ref, cmr_environment)
                cma_file_list.extend(file_sublist)
            # If there is truly no data, return no CNM URLs and finish the workflow.
            if not cma_file_list:
                return {
                    'granules': self.input.get('granules', []),
                    'pobit': [],
                }
        else:
            cma_file_list = result_list
            partial_id = utils.extract_mgrs_grid_code(granule_umm_json)

        # Get date information from umm-g, if static_data_day is None it will be ignored
        begin, mid, end, data_day = utils.extract_granule_dates(granule_umm_json, static_data_day)

        # ------------------------------------------------------------------------------------------
        # Separate list of files into image sets
        # ------------------------------------------------------------------------------------------
        pobit_image_sets = build_image_sets(
            cma_file_list,
            cmr_concept_id,
            data_day
        )

        # ------------------------------------------------------------------------------------------
        # Generate a metadata XML and upload a CNM message for each image set
        # ------------------------------------------------------------------------------------------
        pobit_cnm_urls: list[dict[str, str]] = []
        for image_set in pobit_image_sets:
            # Generates and uploads metadata XML, then returns the updated image_set
            # that is, image_set.image_metadata field gets populated with XML
            updated_image_set = generate_metadata(
                image_set,
                begin, mid, end, data_day,
                subdaily,
                partial_id
            )
            CUMULUS_LOGGER.info(f'Finished generating metadata for {image_set.name}')
            # Convert image_set into CNM JSON object and upload to S3
            cnm_key = write_cnm_message(
                updated_image_set,
                cmr_provider,
                collection_name,
                granule_id,
                is_nrt,
                bignbit_audit_bucket,
                bignbit_audit_path
            )
            pobit_cnm_urls.append({
                'cmr_provider': cmr_provider,
                'collection_name': collection_name,
                'cnm_bucket': bignbit_audit_bucket,
                'cnm_key': cnm_key
            })
            CUMULUS_LOGGER.info(f'Finished writing CNM message for {image_set.name}')

        response_payload = {
            'granules': self.input.get('granules', []),
            'pobit': pobit_cnm_urls,
        }

        return response_payload


def _resolve_static_data_day(dataset_config: dict) -> int | None:
    """
    Determine the static data day override from dataset config, if configured.

    Parameters
    ----------
    dataset_config : dict
      Dataset configuration dictionary

    Returns
    -------
    int | None
      The static day-of-year override (1–366), or None if not configured.
      Falls back to 1 if the configured value is outside the valid range.
    """
    if dataset_config.get('dataDayStrategy') != 'single_day_of_year':
        return None
    # Throw TypeError on bad configuration
    static_data_day = int(dataset_config.get('singleDayNumber', 1))
    if static_data_day < 1 or static_data_day > 366:
        CUMULUS_LOGGER.warning(
            f'Specified data day override {static_data_day} is not logical '
            'as a day of year. Defaulting to doy 001.'
        )
        return 1
    return static_data_day


def process_harmony_results(harmony_job: dict[str, str], cmr_env: str) -> list[dict[str, Any]]:
    """
    Process the results of a Harmony job

    Parameters
    ----------
    harmony_job : Dict[str, str]
       The result dictionary from a successful Harmony job. Contains job id, variable, and output crs
    cmr_env : str
       The CMR environment to use

    Returns
    ----------
        List[Dict[str, Any]]
            A list of CMA file dictionaries pointing to the transformed image(s)
    """
    job_id = harmony_job.get('job', '')
    if job_id == '':
        return []
    variable = harmony_job.get('variable', 'all')
    current_crs = harmony_job.get('output_crs', 'EPSG:4326')

    s3_client = boto3.client('s3')
    harmony_client = utils.get_harmony_client(cmr_env)
    # The Get Harmony Job Status step has already confirmed this job finished, so skip
    # harmony-py's wait-for-completion poll (allow_incomplete=True). That poll uses
    # POST /jobs/status, which breaks on a fresh session: the EDL login redirect turns
    # the POST into a GET, and Harmony rejects 'status' as an invalid job ID.
    result_urls = list(harmony_client.result_urls(job_id, link_type=LinkType.s3, allow_incomplete=True))

    CUMULUS_LOGGER.info('Processing {} result files for {}', len(result_urls), variable)
    CUMULUS_LOGGER.debug('Results: {}', result_urls)

    # Check if Harmony returned no data
    if not result_urls:
        return []

    file_dicts = []
    for url in result_urls:
        bucket, key = urlparse(url).netloc, urlparse(url).path.lstrip('/')

        response = s3_client.head_object(Bucket=bucket, Key=key,
                                         ExpectedBucketOwner=utils.get_aws_account_id())
        etag = response['ETag'].strip('"')

        # For single-part uploads, the S3 ETag is the MD5 hex digest of the object.
        # For multipart uploads, the ETag has a '-<partcount>' suffix and is not an MD5.
        if re.fullmatch(r'[0-9a-fA-F]{32}', etag):
            checksum = etag
        else:
            CUMULUS_LOGGER.warning(
                'ETag {} for s3://{}/{} is not a valid MD5 digest; '
                'falling back to streaming MD5 computation', etag, bucket, key
            )
            get_response = s3_client.get_object(Bucket=bucket, Key=key,
                                                ExpectedBucketOwner=utils.get_aws_account_id())
            checksum = hashlib.md5(get_response['Body'].read()).hexdigest()

        filename = key.split('/')[-1]
        file_dict = {
            'fileName': filename,
            'bucket': bucket,
            'key': key,
            'checksum': checksum,
            'checksumType': 'md5'
        }
        # Weird quirk where if we are working with a collection that doesn't define variables, the Harmony request
        # should specify 'all' as the variable value but the GIBS message should be sent with the variable set to 'none'
        if variable.lower() != 'all':
            file_dict['variable'] = variable
        file_dict['output_crs'] = current_crs.upper()
        file_dicts.append(file_dict)

    return file_dicts


def generate_metadata(
        image_set: ImageSet,
        begin_time: str,
        mid_time: str,
        end_time: str,
        data_day: str,
        subdaily: bool,
        partial_id: str | None
) -> ImageSet:
    """
    For each image set, create an ImageMetadata-v1.2 xml file and upload it to s3 in the same
    bucket and path as the image file. Throw an error if any image set is incomplete at this
    stage.

    Parameters
    ----------
    image_set
      An ImageSet with image and world_file populated
    begin_time
      range beginning date time as string formatted "%Y-%m-%dT%H:%M:%S.%fZ"
    mid_time
      date time halfway between begin and end as string formatted "%Y-%m-%dT%H:%M:%S.%fZ"
    end_time
      range ending date time as string formatted "%Y-%m-%dT%H:%M:%S.%fZ"
    data_day
      Data day associated with the midpoint of the data timerange or a static override
    subdaily
      boolean flag if product is subdaily (if True, add DataDateTime to metadata)
    partial_id
      MGRS grid code used for OPERA-HLS (None for other datasets)

    Returns
    -------
    ImageSet
      The ImageSet object from the input with the metadata xml field populated
    """
    # World file is allowed to be excluded as it is not present when performing OPERA HLS Treatment
    if image_set.image == {}:
        raise IncompleteImageSetError(f'Missing one or more components of GIBS image set: {image_set.name}')
    image_mdxml = create_metadata_xml(
        begin_time,
        mid_time,
        end_time,
        data_day,
        subdaily,
        partial_id
    )

    image_file_meta = image_set.image
    mdxml_file_meta = get_mdxml_cnm_file_meta(image_mdxml, image_file_meta)
    image_set.image_metadata = mdxml_file_meta
    staging_bucket = str(mdxml_file_meta.get('bucket', ''))
    xml_key = str(mdxml_file_meta.get('key', ''))
    s3_uri = utils.upload_object(
        image_mdxml,
        staging_bucket,
        xml_key,
        'application/xml'
    )
    CUMULUS_LOGGER.info(f'Uploaded file {s3_uri}')
    return image_set


def create_metadata_xml(
        begin_time: str,
        mid_time: str,
        end_time: str,
        data_day: str,
        subdaily: bool,
        partial_id: str | None,
) -> bytes:
    """
    Create an ImageMetadata-v1.2 XML Element tree

    Parameters
    ----------
    beginning_time
      formatted datetime string for data begin date time
    middle_time
      formatted datetime string for data midpoint date time
    ending_time
      formatted datetime string for data end date time
    dataday
      string if format %Y%j for day of year the data represents
    subdaily
      boolean flag if product is subdaily (if True, add DataDateTime to metadata)
    partial_id
      partial id associated with data

    Returns
    -------
    str
      New XML document encoded as a utf-8 string
    """
    time_now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ')

    imagery_metadata = ET.Element('ImageryMetadata')
    ET.SubElement(imagery_metadata, 'ProviderProductionDateTime').text = time_now
    ET.SubElement(imagery_metadata, 'DataStartDateTime').text = begin_time
    ET.SubElement(imagery_metadata, 'DataMidDateTime').text = mid_time
    ET.SubElement(imagery_metadata, 'DataEndDateTime').text = end_time
    if subdaily:
        ET.SubElement(imagery_metadata, 'DataDateTime').text = begin_time
    else:
        ET.SubElement(imagery_metadata, 'DataDay').text = data_day
    if partial_id:
        ET.SubElement(imagery_metadata, 'PartialId').text = partial_id

    # This should have an XML header, but we've been sending it to GIBS without
    # one, so don't break compatibility
    return ET.tostring(imagery_metadata, encoding='utf-8')  # , xml_declaration=True)


def get_mdxml_cnm_file_meta(
        image_metadata_xml: bytes,
        cnm_file_meta: dict
) -> dict:
    """
    Construct a CNM-compatible file dict for an image metadata xml file

    Parameters
    ----------
    image_metadata_xml
      bytestring of xml data
    cnm_file_meta
      file metadata of the image file that the image metadata xml is describing

    Returns
    -------
    dict
      CNM-compatible file dict for an image metadata xml file
    """
    image_metadata_file_metadata = cnm_file_meta.copy()

    image_metadata_xml_key = Path(cnm_file_meta['key']).with_suffix('.xml')

    image_metadata_file_metadata['fileName'] = image_metadata_xml_key.name
    image_metadata_file_metadata['key'] = str(image_metadata_xml_key)
    image_metadata_file_metadata['type'] = 'metadata'
    image_metadata_file_metadata['subtype'] = 'ImageMetadata-v1.2'
    image_metadata_file_metadata['checksumType'] = 'SHA512'
    image_metadata_file_metadata['checksum'] = hashlib.sha512(image_metadata_xml).hexdigest()
    image_metadata_file_metadata['size'] = len(image_metadata_xml)

    return image_metadata_file_metadata


def write_cnm_message(
        image_set: ImageSet,
        cmr_provider: str,
        collection_name: str,
        granule_id: str,
        is_nrt: bool,
        bignbit_audit_bucket: str,
        bignbit_audit_path: str,
) -> str:
    """
    Generate and upload the CNM message using the ImageSet

    Parameters
    ----------
    image_set: ImageSet
        ImageSet for one image to be sent to gibs
    cmr_provider: str
      The provider sent in the CNM message
    collection_name: str
      Collection that this image set belongs to
    granule_id: str
      Granule id (used to determine CNM filename)
    is_nrt: bool
      True if the filename of the image_set is considered NRT
    bignbit_audit_bucket: str
      staging bucket where CNM is uploaded (default is *-internal)
    bignbit_audit_path: str
      configured key within the bucket to store the CNM (default is bignbit-cnm-output)

    Returns
    ----------
    s3_key: str
      s3 key pointing to the uploaded CNM message
    """
    cnm_message = construct_cnm(image_set, cmr_provider, collection_name, is_nrt)
    cnm_bytes = json.dumps(cnm_message).encode()
    submission_time = cnm_message.get('submissionTime')
    collection_fullname = cnm_message.get('collection')
    cnm_key = f'{bignbit_audit_path}/{collection_fullname}/{granule_id}.{submission_time}.cnm.json'.replace(':', '_')
    utils.upload_object(
        cnm_bytes,
        bignbit_audit_bucket,
        cnm_key,
        'application/json'
    )
    return cnm_key


def construct_cnm(
        image_set: ImageSet,
        cmr_provider: str,
        collection_name: str,
        is_nrt: bool,
) -> dict[str, Any]:
    """
    Construct the CNM message for GITC

    Parameters
    ----------
    image_set: ImageSet
      ImageSet for one image to be sent to gibs
    cmr_provider: str
      The provider sent in the CNM message
    collection_name: str
      Collection that this image set belongs to
    is_nrt: bool
      True if the filename of the image_set is considered NRT

    Returns
    ----------
    cnm: dict
        CNM message
    """
    product = to_cnm_product_dict(image_set)
    submission_time = datetime.now(timezone.utc).isoformat()[:-9] + 'Z'
    CUMULUS_LOGGER.debug(image_set.image['variable'])
    nrt_string = '_NRT' if is_nrt else ''
    if 'output_crs' in image_set.image:
        crs_suffix = GIBS_CRS_NAME_TO_SUFFIX.get(image_set.image.get('output_crs', 'EPSG:4326'), 'LL')
        new_collection = f"{collection_name}_{image_set.image['variable']}{nrt_string}_{crs_suffix}".replace('/', '_')
    else:
        new_collection = f"{collection_name}_{image_set.image['variable']}{nrt_string}".replace('/', '_')

    return {
        'version': '1.5.1',
        'duplicationid': image_set.name,
        'collection': new_collection,
        'submissionTime': submission_time,
        'identifier': image_set.name,
        'product': product,
        'provider': cmr_provider
    }


def lambda_handler(event, context):
    """handler that gets called by aws lambda
    Parameters
    ----------
    event: dict
        event from a lambda call
    context: dict
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
