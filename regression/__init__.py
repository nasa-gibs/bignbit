"""
Regression test suite for a deployed bignbit BrowseImageWorkflow.

For each test case in the manifest the suite looks the granule up in CMR, starts the workflow's
Step Functions state machine, downloads the CNM messages the workflow archived to S3 and compares
the browse image checksums in them against checked-in reference CNMs.

Run it from the repository root with ``python -m regression``. This package is not part of the
bignbit Lambda package and only needs boto3 and requests.
"""
