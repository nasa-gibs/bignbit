import sys
from pathlib import Path

import pytest

# The regression suite (regression/) is a top-level directory that is not installed as a package, unlike bignbit.
# Put the repository root on the import path so its unit tests can import it however pytest is invoked.
REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


@pytest.fixture(scope="session")
def vcr_config():
    return {
        "filter_headers": ["authorization"],
        "decode_compressed_response": True,
        "record_mode": "once"
    }
