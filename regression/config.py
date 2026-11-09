"""
Resolves which bignbit deployment the regression suite targets from the example deployment's tfvars files
"""
import re
from dataclasses import dataclass
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
TFVARS_DIR = REPO_ROOT / 'examples' / 'cumulus-tf' / 'tfvars'

DEFAULT_TEST_CASES = PACKAGE_DIR / 'test_cases.csv'
DEFAULT_REFERENCE_DIR = PACKAGE_DIR / 'reference_data'
DEFAULT_WORKDIR = REPO_ROOT / 'build' / 'regression' / 'workdir'
DEFAULT_REPORT_DIR = REPO_ROOT / 'build' / 'regression' / 'report'

VENUES = ('sandbox', 'sit', 'uat')
DEFAULT_VENUE = 'sandbox'

# Matches the deploy job in .github/workflows/cicd-pipeline.yml: sandbox deploys to us-east-1, all other venues to us-west-2
SANDBOX_REGION = 'us-east-1'
DEFAULT_REGION = 'us-west-2'

# Names from examples/cumulus-tf/browse_image_workflow.tf and terraform/lambda_functions.tf
WORKFLOW_NAME = 'BrowseImageWorkflow'
APP_NAME = 'bignbit'
VERSION_LAMBDA = 'get_dataset_configuration'
LAMBDA_NAME_MAX_LENGTH = 64

_ASSIGNMENT = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_-]*)\s*=\s*(.*)$')
_COMMENT = re.compile(r'\s*(?:#|//)')


class ConfigError(Exception):
    """
    Exception for a deployment that cannot be resolved from the given arguments and tfvars files
    """


@dataclass(frozen=True)
class Deployment:
    """
    The deployment of the example cumulus-tf stack that the regression suite runs against
    """
    venue: str
    region: str
    prefix: str
    cmr_environment: str
    workspace: str = ''

    @property
    def resource_prefix(self) -> str:
        """Prefix shared by the deployment's resources, equivalent to local.ec2_resources_name in examples/cumulus-tf/main.tf."""
        return f'{self.prefix}-{self.workspace}' if self.workspace else self.prefix

    @property
    def state_machine_name(self) -> str:
        """Name of the state machine created by examples/cumulus-tf/browse_image_workflow.tf."""
        return f'{self.resource_prefix}-{WORKFLOW_NAME}'

    @property
    def private_bucket(self) -> str:
        """Name of the private bucket created by examples/cumulus-tf/s3.tf."""
        return f'{self.resource_prefix}-private'

    @property
    def version_lambda_name(self) -> str:
        """
        Name of a bignbit Lambda carrying the Version tag. Follows local.aws_resources_name in terraform/main.tf, where
        the workspace suffix comes after the app name, and the 64 character truncation in terraform/lambda_functions.tf.
        """
        resources_name = f'{self.prefix}-{APP_NAME}'
        if self.workspace:
            resources_name = f'{resources_name}-{self.workspace}'
        return f'{resources_name}-{VERSION_LAMBDA}'[:LAMBDA_NAME_MAX_LENGTH]


def parse_tfvars(text: str) -> dict:
    """
    Parse the simple ``name = value`` assignments of a tfvars file. Quoted values are unquoted and comments are
    dropped. Multi-line values (maps, lists, heredocs) are not supported; the suite only needs string settings.

    Parameters
    ----------
    text : str
        Content of a tfvars file

    Returns
    -------
    dict
        Variable names mapped to their string values
    """
    values = {}
    for line in text.splitlines():
        match = _ASSIGNMENT.match(line)
        if not match:
            continue
        name, raw_value = match.group(1), match.group(2).strip()
        if raw_value.startswith('"'):
            closing_quote = raw_value.find('"', 1)
            if closing_quote == -1:
                continue
            values[name] = raw_value[1:closing_quote]
        else:
            values[name] = _COMMENT.split(raw_value, maxsplit=1)[0].strip()
    return values


def default_region(venue: str) -> str:
    """Return the AWS region a venue is deployed to by the CI/CD pipeline."""
    return SANDBOX_REGION if venue == 'sandbox' else DEFAULT_REGION


def resolve_deployment(venue: str = DEFAULT_VENUE, region: str = None, prefix: str = None, workspace: str = None,
                       tfvars_dir: Path = None) -> Deployment:
    """
    Work out the target deployment for a venue. The resource prefix and CMR environment are read from the venue's
    tfvars file unless overridden.

    Parameters
    ----------
    venue : str
        Venue name (case-insensitive), one of VENUES
    region : str
        AWS region override; defaults by venue
    prefix : str
        Override for the ``prefix`` Terraform variable
    workspace : str
        Terraform workspace the stack was deployed in; empty or ``default`` means no suffix
    tfvars_dir : Path
        Directory holding ``<venue>.tfvars``; defaults to TFVARS_DIR

    Returns
    -------
    Deployment
        The resolved deployment

    Raises
    ------
    ConfigError
        If the venue is unknown, its tfvars file is missing, or a required setting is absent
    """
    venue = (venue or '').lower()
    if venue not in VENUES:
        raise ConfigError(f'Unknown venue {venue!r}; expected one of {", ".join(VENUES)}')

    tfvars_path = Path(tfvars_dir or TFVARS_DIR) / f'{venue}.tfvars'
    try:
        tfvars = parse_tfvars(tfvars_path.read_text(encoding='utf-8'))
    except OSError as ex:
        raise ConfigError(f'Unable to read tfvars file for venue {venue}: {tfvars_path} ({ex.strerror})') from ex

    prefix = prefix or tfvars.get('prefix')
    if not prefix:
        raise ConfigError(f'No "prefix" setting found in {tfvars_path} and no --prefix given')

    cmr_environment = tfvars.get('bignbit_cmr_environment', '').upper()
    if not cmr_environment:
        raise ConfigError(f'No "bignbit_cmr_environment" setting found in {tfvars_path}')

    if workspace == 'default':
        workspace = ''

    return Deployment(venue=venue,
                      region=region or default_region(venue),
                      prefix=prefix,
                      cmr_environment=cmr_environment,
                      workspace=workspace or '')
