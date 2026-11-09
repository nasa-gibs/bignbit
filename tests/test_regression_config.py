"""Unit tests for the regression suite's tfvars parsing and venue resolution"""
import subprocess

import pytest

from regression import config

TFVARS = '''
# stage: dev, sandbox, sit, uat, prod
bignbit_stage = "sit"
bignbit_cmr_environment = "UAT"   # trailing comment
prefix="my-prefix"
quoted_hash = "a#b"
force_delete_ecr             = false // another comment
some_number = 3
not an assignment
unterminated = "oops
'''


def write_tfvars(tmp_path, venue='sandbox', text='prefix = "tmp-prefix"\nbignbit_cmr_environment = "uat"\n'):
    """Write a tfvars file for a venue into a temporary tfvars directory"""
    (tmp_path / f'{venue}.tfvars').write_text(text, encoding='utf-8')
    return tmp_path


def test_parse_tfvars():
    """Assignments are parsed with quotes and comments removed; anything else is ignored"""
    assert config.parse_tfvars(TFVARS) == {
        'bignbit_stage': 'sit',
        'bignbit_cmr_environment': 'UAT',
        'prefix': 'my-prefix',
        'quoted_hash': 'a#b',
        'force_delete_ecr': 'false',
        'some_number': '3'
    }


@pytest.mark.parametrize('venue, prefix, region', [
    ('sandbox', 'gitc-sandbox', 'us-east-1'),
    ('sit', 'podaac-sit-svc', 'us-west-2'),
    ('uat', 'podaac-uat-svc', 'us-west-2'),
])
def test_resolve_deployment_from_checked_in_tfvars(venue, prefix, region):
    """Each venue resolves from the example deployment's real tfvars file"""
    deployment = config.resolve_deployment(venue)

    assert deployment.venue == venue
    assert deployment.prefix == prefix
    assert deployment.resource_prefix == prefix
    assert deployment.region == region
    assert deployment.cmr_environment == 'UAT'
    assert deployment.state_machine_name == f'{prefix}-BrowseImageWorkflow'
    assert deployment.private_bucket == f'{prefix}-private'
    assert deployment.version_lambda_name == f'{prefix}-bignbit-get_dataset_configuration'


def test_default_venue_is_sandbox():
    """With no arguments the sandbox deployment is targeted"""
    assert config.resolve_deployment().venue == 'sandbox'
    assert config.resolve_deployment().prefix == 'gitc-sandbox'


def test_venue_is_case_insensitive():
    """Venue names are accepted in any case, as the CI/CD pipeline uses upper case"""
    assert config.resolve_deployment('SANDBOX') == config.resolve_deployment('sandbox')


def test_overrides(tmp_path):
    """Region and prefix can be overridden; the CMR environment still comes from the tfvars file"""
    deployment = config.resolve_deployment('sit', region='eu-west-1', prefix='custom', tfvars_dir=write_tfvars(tmp_path, 'sit'))

    assert deployment.region == 'eu-west-1'
    assert deployment.prefix == 'custom'
    assert deployment.cmr_environment == 'UAT'
    assert deployment.state_machine_name == 'custom-BrowseImageWorkflow'


def test_workspace_suffix():
    """
    A workspace is appended to the resource prefix as in examples/cumulus-tf/main.tf, but follows the app name
    in Lambda names as in terraform/main.tf
    """
    deployment = config.resolve_deployment('sandbox', workspace='issue-193')

    assert deployment.prefix == 'gitc-sandbox'
    assert deployment.resource_prefix == 'gitc-sandbox-issue-193'
    assert deployment.state_machine_name == 'gitc-sandbox-issue-193-BrowseImageWorkflow'
    assert deployment.private_bucket == 'gitc-sandbox-issue-193-private'
    assert deployment.version_lambda_name == 'gitc-sandbox-bignbit-issue-193-get_dataset_configuration'


def test_default_workspace_has_no_suffix():
    """Terraform's default workspace adds no suffix"""
    assert config.resolve_deployment('sandbox', workspace='default').resource_prefix == 'gitc-sandbox'


def test_lambda_name_is_truncated_like_terraform():
    """Lambda names are cut to 64 characters, as terraform/lambda_functions.tf does"""
    deployment = config.resolve_deployment('uat', workspace='a-rather-long-terraform-workspace-name')

    expected = 'podaac-uat-svc-bignbit-a-rather-long-terraform-workspace-name-get_dataset_configuration'[:64]
    assert deployment.version_lambda_name == expected
    assert len(deployment.version_lambda_name) == 64


def test_unknown_venue():
    """A venue without a tfvars file is rejected"""
    with pytest.raises(config.ConfigError, match='Unknown venue'):
        config.resolve_deployment('ops')


def test_missing_tfvars_file(tmp_path):
    """A missing tfvars file is a configuration error, not a crash"""
    with pytest.raises(config.ConfigError, match='Unable to read tfvars file'):
        config.resolve_deployment('sandbox', tfvars_dir=tmp_path)


def test_missing_prefix(tmp_path):
    """A tfvars file without a prefix needs --prefix"""
    tfvars_dir = write_tfvars(tmp_path, text='bignbit_cmr_environment = "UAT"\n')

    with pytest.raises(config.ConfigError, match='prefix'):
        config.resolve_deployment('sandbox', tfvars_dir=tfvars_dir)
    assert config.resolve_deployment('sandbox', prefix='given', tfvars_dir=tfvars_dir).prefix == 'given'


def test_missing_cmr_environment(tmp_path):
    """A tfvars file without a CMR environment is rejected"""
    with pytest.raises(config.ConfigError, match='bignbit_cmr_environment'):
        config.resolve_deployment('sandbox', tfvars_dir=write_tfvars(tmp_path, text='prefix = "p"\n'))


def test_default_output_paths_are_gitignored():
    """The default workdir and report directory sit under build/, which git ignores"""
    build_dir = config.REPO_ROOT / 'build'
    assert build_dir in config.DEFAULT_WORKDIR.parents
    assert build_dir in config.DEFAULT_REPORT_DIR.parents

    paths = [str(config.DEFAULT_WORKDIR / 'x'), str(config.DEFAULT_REPORT_DIR / 'report.md')]
    try:
        completed = subprocess.run(['git', 'check-ignore', *paths], cwd=config.REPO_ROOT, capture_output=True, text=True,
                                   check=False, timeout=30)
    except OSError:
        pytest.skip('git is not available')
    if completed.returncode not in (0, 1):
        pytest.skip('not running inside a git checkout')
    assert completed.stdout.split() == paths
