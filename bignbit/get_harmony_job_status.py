"""Cumulus lambda class to check harmony job status"""
import logging
import os

import requests
from cumulus_logger import CumulusLogger
from cumulus_process import Process
from harmony import LinkType

from bignbit import utils
from bignbit.utils import json_dumps_with_datetime

CUMULUS_LOGGER = CumulusLogger('get_harmony_job_status')


class HarmonyTransientError(Exception):
    """Exception raised when the Harmony API returns a transient 5xx error"""

    def __init__(self, message):
        super().__init__(message)


class HarmonyJobIncompleteError(Exception):
    """Exception raised when a harmony job is not complete"""

    def __init__(self, message):
        super().__init__(message)


class HarmonyJobFailedError(Exception):
    """Exception raised when a harmony job has failed"""

    def __init__(self, message):
        super().__init__(message)


class HarmonyJobNoDataError(Exception):
    """Exception raised when a harmony job completes successfully but returns no data"""

    def __init__(self, message):
        super().__init__(message)


class CMA(Process):
    """Cumulus class to check harmony job status"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logger = CUMULUS_LOGGER

    def process(self):
        """
        Checks the job status of a harmony call

        Returns
        ----------
        CMA message
        """

        harmony_job_id = self.config.get("harmony_job")
        cmr_environment = self.config.get("cmr_environment", "UAT")
        current_variable = self.config.get("current_variable", "")
        current_crs = self.config.get("current_crs", "")

        job_status = check_harmony_job(
            harmony_job_id,
            cmr_environment,
            current_variable,
            current_crs
        )
        harmony_job_info = self.input.get("harmony_job", {})
        harmony_job_info["status"] = job_status
        self.logger.info(f"Harmony job {harmony_job_id} was {job_status}")
        return harmony_job_info


def check_harmony_job(
        harmony_job_id: str,
        cmr_env: str,
        variable: str,
        crs: str
) -> str:
    """
    Function to check a harmony job id status and returns the status

    Parameters
    ----------
    harmony_job_id: str
        The harmony job id to check
    cmr_env: str
        The CMR environment to use, defaults to None which uses the default environment
    Returns
    ----------
    str
        The status of the harmony job if 'successful' or
        raises an exception if the job has failed or is incomplete.

    Raises
    ----------
    HarmonyJobNoDataError
        When the Harmony job completes successfully but returns no data, moves to a pass state
    HarmonyJobIncompleteError
        When the Harmony job is incomplete, triggers a retry
    HarmonyJobFailedError
        When the Harmony job fails, fails the workflow run
    """

    harmony_client = utils.get_harmony_client(cmr_env)
    try:
        job_status = harmony_client.status(harmony_job_id)
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        raise HarmonyTransientError(
            f'Harmony API returned a transient error checking status of job {harmony_job_id}: {exc}'
        ) from exc
    except requests.exceptions.HTTPError as exc:
        if exc.response is not None and exc.response.status_code >= 500:
            raise HarmonyTransientError(
                f'Harmony API returned a transient error checking status of job {harmony_job_id}: {exc}'
            ) from exc
        raise

    # For a successful job, return the status; for all other states, raise an exception.
    if job_status.get('status') == 'successful':
        # Check that the harmony job returned data to confirm that the job was successful
        try:
            # The job is already known to be successful, so skip harmony-py's
            # wait-for-completion poll (see process_harmony_results in handle_big_result)
            result_urls = list(harmony_client.result_urls(harmony_job_id, link_type=LinkType.s3,
                                                          allow_incomplete=True))
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            raise HarmonyTransientError(
                f'Harmony API returned a transient error fetching result URLs for job {harmony_job_id}: {exc}'
            ) from exc
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code >= 500:
                raise HarmonyTransientError(
                    f'Harmony API returned a transient error fetching result URLs for job {harmony_job_id}: {exc}'
                ) from exc
            raise
        if not result_urls:
            error_msg = (
                f'Harmony job {harmony_job_id} completed successfully but returned no data for {variable} and {crs}'
            )
            CUMULUS_LOGGER.warning(error_msg)
            raise HarmonyJobNoDataError(error_msg)
        return job_status.get('status', 'successful')

    # If the job is still running or accepted, raise an exception that will be retried by the step function workflow.
    if job_status.get('status') in ['accepted', 'running']:
        raise HarmonyJobIncompleteError(
            f'Harmony job {harmony_job_id} is not complete. Status: {json_dumps_with_datetime(job_status)}')

    # If the job has failed, raise an exception that will cause the step function workflow to fail.
    raise HarmonyJobFailedError(
        f'Harmony job {harmony_job_id} has failed. Status: {json_dumps_with_datetime(job_status)}')


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
