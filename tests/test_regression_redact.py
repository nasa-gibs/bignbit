"""Unit tests for the regression suite's AWS account ID redaction"""
import logging

import pytest

from regression.redact import PLACEHOLDER, RedactingFormatter, Redactor

ACCOUNT_ID = '123456789012'
OTHER_ACCOUNT_ID = '999988887777'


def test_known_account_id_is_redacted_everywhere():
    """A known account ID is removed wherever it appears, not only inside ARNs"""
    redactor = Redactor([ACCOUNT_ID])

    text = f'account {ACCOUNT_ID} owns s3://bucket-{ACCOUNT_ID}/key and "{ACCOUNT_ID}"'

    assert redactor.redact(text) == f'account {PLACEHOLDER} owns s3://bucket-{PLACEHOLDER}/key and "{PLACEHOLDER}"'


@pytest.mark.parametrize('text, expected', [
    (f'arn:aws:states:us-west-2:{OTHER_ACCOUNT_ID}:execution:machine:name',
     f'arn:aws:states:us-west-2:{PLACEHOLDER}:execution:machine:name'),
    (f'arn:aws:iam::{OTHER_ACCOUNT_ID}:role/some-role', f'arn:aws:iam::{PLACEHOLDER}:role/some-role'),
    (f'arn:aws-us-gov:lambda:us-gov-west-1:{OTHER_ACCOUNT_ID}:function:f', f'arn:aws-us-gov:lambda:us-gov-west-1:{PLACEHOLDER}:function:f'),
    (f'{{"arn": "arn:aws:sqs:us-east-1:{OTHER_ACCOUNT_ID}:queue.fifo"}}', f'{{"arn": "arn:aws:sqs:us-east-1:{PLACEHOLDER}:queue.fifo"}}'),
    (f'https://sqs.us-east-1.amazonaws.com/{OTHER_ACCOUNT_ID}/gitc-PODAAC-IN.fifo', f'https://sqs.us-east-1.amazonaws.com/{PLACEHOLDER}/gitc-PODAAC-IN.fifo'),
    (f'{OTHER_ACCOUNT_ID}.dkr.ecr.us-west-2.amazonaws.com/bignbit:1.0', f'{PLACEHOLDER}.dkr.ecr.us-west-2.amazonaws.com/bignbit:1.0'),
])
def test_unknown_account_ids_are_redacted_by_pattern(text, expected):
    """Account IDs the redactor was never told about are still removed from ARNs, SQS URLs and ECR hostnames"""
    assert Redactor().redact(text) == expected


def test_multiple_arns_in_one_text():
    """Every ARN in a text is redacted"""
    text = f'arn:aws:lambda:us-east-1:{ACCOUNT_ID}:function:a failed calling arn:aws:sqs:us-east-1:{OTHER_ACCOUNT_ID}:q'

    redacted = Redactor().redact(text)

    assert ACCOUNT_ID not in redacted
    assert OTHER_ACCOUNT_ID not in redacted
    assert redacted.count(PLACEHOLDER) == 2


def test_other_numbers_are_left_alone():
    """Timestamps, checksums and S3 ARNs without an account are not mangled"""
    text = ('20260309090000-JPL-L4_GHRSST-SSTfnd-MUR granule, checksum 4cccda6d8745ee05ad11ae90ec5c6490, '
            'job 999988887777 and arn:aws:s3:::some-bucket/key')

    assert Redactor().redact(text) == text


def test_invalid_account_ids_are_ignored():
    """Empty or malformed account IDs are not registered, so they cannot blank out unrelated text"""
    redactor = Redactor(['', None, '1234', 'not-an-account'])
    redactor.add_account_id('')

    assert redactor.redact('1234 not-an-account None') == '1234 not-an-account None'


def test_add_account_id():
    """Account IDs can be registered after construction"""
    redactor = Redactor()
    redactor.add_account_id(ACCOUNT_ID)

    assert redactor.redact(f'id={ACCOUNT_ID}') == f'id={PLACEHOLDER}'


def test_placeholder_is_markdown_and_json_safe():
    """The placeholder must survive Markdown rendering and JSON encoding unchanged"""
    assert PLACEHOLDER.isalnum()
    assert len(PLACEHOLDER) == 12


def test_redacting_formatter_covers_messages_and_tracebacks():
    """Log output is redacted, including exception text"""
    formatter = RedactingFormatter(Redactor([ACCOUNT_ID]), '%(levelname)s %(message)s')
    error = RuntimeError(f'cannot assume arn:aws:iam::{OTHER_ACCOUNT_ID}:role/r')
    record = logging.LogRecord('test', logging.ERROR, __file__, 1, 'account %s failed', (ACCOUNT_ID,),
                               (RuntimeError, error, None))

    formatted = formatter.format(record)

    assert formatted.startswith(f'ERROR account {PLACEHOLDER} failed')
    assert 'RuntimeError' in formatted
    assert ACCOUNT_ID not in formatted
    assert OTHER_ACCOUNT_ID not in formatted
