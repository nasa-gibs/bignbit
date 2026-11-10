"""
Removes AWS account IDs and secrets from text. The regression report is published publicly, so nothing the suite
writes or logs may contain them.
"""
import logging
import re

PLACEHOLDER = 'XXXXXXXXXXXX'
SECRET_PLACEHOLDER = '***'

_ACCOUNT_ID = re.compile(r'\d{12}')

# The account field of an ARN (arn:partition:service:region:account-id:resource), plus the other common places
# an account ID appears in AWS error messages: SQS queue URLs and ECR registry hostnames.
_PATTERNS = (
    re.compile(r'(arn:aws[a-z0-9-]*:[a-z0-9-]*:[a-z0-9-]*:)\d{12}(?!\d)', re.IGNORECASE),
    re.compile(r'(amazonaws\.com(?:\.cn)?/)\d{12}(?=/)', re.IGNORECASE),
    re.compile(r'((?<!\d))\d{12}(?=\.dkr\.ecr\.)', re.IGNORECASE),
)


class Redactor:
    """
    Replaces AWS account IDs with a placeholder: every occurrence of the account IDs it has been told about, and
    any 12 digit account field found inside an ARN, SQS queue URL or ECR hostname.
    """

    def __init__(self, account_ids=()):
        self._account_ids = set()
        self._secrets = set()
        for account_id in account_ids:
            self.add_account_id(account_id)

    def add_account_id(self, account_id: str) -> None:
        """Register a known account ID to be redacted wherever it appears. Values that are not 12 digits are ignored."""
        if account_id and _ACCOUNT_ID.fullmatch(str(account_id)):
            self._account_ids.add(str(account_id))

    def add_secret(self, secret: str) -> None:
        """Register a secret, such as a password or token, to be redacted wherever it appears. Empty values are ignored."""
        if secret:
            self._secrets.add(str(secret))

    def redact(self, text: str) -> str:
        """Return the text with secrets and account IDs replaced by placeholders."""
        text = str(text)
        for secret in self._secrets:
            text = text.replace(secret, SECRET_PLACEHOLDER)
        for account_id in self._account_ids:
            text = text.replace(account_id, PLACEHOLDER)
        for pattern in _PATTERNS:
            text = pattern.sub(rf'\g<1>{PLACEHOLDER}', text)
        return text


class RedactingFormatter(logging.Formatter):
    """
    A log formatter that redacts account IDs from the whole formatted record, including tracebacks
    """

    def __init__(self, redactor: Redactor, fmt: str = None):
        super().__init__(fmt)
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        """Format the record and redact the result."""
        return self._redactor.redact(super().format(record))
