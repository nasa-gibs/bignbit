"""
Command line interface of the regression suite: ``python -m regression run`` and ``python -m regression update-reference``
"""
import argparse
import logging
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

from regression import aws, cmr, config, manifest, reference, report, runner
from regression.redact import RedactingFormatter, Redactor
from regression.results import EXIT_FAILURES, EXIT_NOT_STARTED, EXIT_OK, exit_code, summarize

LOGGER = logging.getLogger('regression')

COMMAND_RUN = 'run'
COMMAND_UPDATE_REFERENCE = 'update-reference'

_LOG_HANDLER_NAME = 'regression-cli'
_REPORT_DIR_OPTION = '--report-dir'

# Problems that mean the run cannot start at all, as opposed to a single case failing
_STARTUP_ERRORS = (config.ConfigError, manifest.ManifestError, cmr.CmrError, aws.StateMachineNotFound, BotoCoreError, ClientError,
                   OSError)


class UsageError(Exception):
    """
    Exception for invalid command line arguments
    """

    def __init__(self, message: str, usage: str = ''):
        super().__init__(message)
        self.usage = usage


class _ArgumentParser(argparse.ArgumentParser):
    """An argument parser that raises UsageError instead of exiting, so that ``run`` can still write an error report."""

    def error(self, message):
        raise UsageError(f'{self.prog}: error: {message}', self.format_usage())


def _positive_number(value: str) -> float:
    """Argparse type for a number of seconds greater than zero."""
    try:
        number = float(value)
    except ValueError as ex:
        raise argparse.ArgumentTypeError(f'{value!r} is not a number') from ex
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError(f'{value!r} is not greater than zero')
    return number


def _positive_integer(value: str) -> int:
    """Argparse type for a whole number greater than zero."""
    try:
        number = int(value)
    except ValueError as ex:
        raise argparse.ArgumentTypeError(f'{value!r} is not a whole number') from ex
    if number <= 0:
        raise argparse.ArgumentTypeError(f'{value!r} is not greater than zero')
    return number


def build_parser() -> argparse.ArgumentParser:
    """Build the command line parser."""
    parser = _ArgumentParser(prog='python -m regression',
                             description='Regression tests for a deployed bignbit BrowseImageWorkflow.')
    subparsers = parser.add_subparsers(dest='command', required=True, metavar=f'{{{COMMAND_RUN},{COMMAND_UPDATE_REFERENCE}}}')

    run = subparsers.add_parser(
        COMMAND_RUN, help='run the regression test cases and write a report',
        description='Run each test case through the deployed workflow and compare the browse image checksums in the CNM '
                    'messages it produces with the reference data. Exits 0 when every case that was not skipped passed, '
                    '1 when at least one did not, and 2 when the run could not start.')
    run.add_argument('--venue', type=str.lower, choices=config.VENUES, default=config.DEFAULT_VENUE,
                     help='venue of the deployment to test (default: %(default)s)')
    run.add_argument('--region', help=f'AWS region (default: {config.SANDBOX_REGION} for sandbox, {config.DEFAULT_REGION} otherwise)')
    run.add_argument('--prefix', help='resource prefix of the deployment (default: "prefix" in the venue tfvars file)')
    run.add_argument('--workspace', help='Terraform workspace the deployment was created in, appended to the prefix')
    run.add_argument('--state-machine-arn', help='ARN of the state machine to run, instead of looking it up by name')
    run.add_argument('--aws-profile', help='AWS profile to use instead of the default credential chain')
    run.add_argument('--test-cases', type=Path, default=config.DEFAULT_TEST_CASES, metavar='PATH',
                     help='test case manifest (default: %(default)s)')
    run.add_argument('--reference-dir', type=Path, default=config.DEFAULT_REFERENCE_DIR, metavar='PATH',
                     help='reference data directory (default: %(default)s)')
    run.add_argument('--workdir', type=Path, default=config.DEFAULT_WORKDIR, metavar='PATH',
                     help='directory for inputs and downloaded results (default: %(default)s)')
    run.add_argument(_REPORT_DIR_OPTION, type=Path, default=config.DEFAULT_REPORT_DIR, metavar='PATH',
                     help=f'directory to write {report.REPORT_FILENAME} and {report.RESULTS_FILENAME} to (default: %(default)s)')
    run.add_argument('--only', nargs='+', metavar='SHORT_NAME', help='run only these collections')
    run.add_argument('--poll-interval', type=_positive_number, default=10, metavar='SECONDS',
                     help='seconds between execution status checks (default: %(default)s)')
    run.add_argument('--execution-timeout', type=_positive_number, default=1800, metavar='SECONDS',
                     help='seconds to wait for an execution before reporting the case as TIMEOUT (default: %(default)s)')
    run.add_argument('--max-parallel', type=_positive_integer, default=runner.DEFAULT_MAX_PARALLEL, metavar='N',
                     help='number of test cases to run at the same time; 1 runs them one at a time (default: %(default)s)')
    run.add_argument('--download-browse', action='store_true',
                     help='also download the browse images to the workdir; they are not used by the comparison')

    update = subparsers.add_parser(
        COMMAND_UPDATE_REFERENCE, help='promote the latest results in the workdir to reference data',
        description='Copy the cnm directory of the latest workdir results of the named collections into the reference data.')
    update.add_argument('--workdir', type=Path, default=config.DEFAULT_WORKDIR, metavar='PATH',
                        help='workdir of a previous run (default: %(default)s)')
    update.add_argument('--reference-dir', type=Path, default=config.DEFAULT_REFERENCE_DIR, metavar='PATH',
                        help='reference data directory (default: %(default)s)')
    update.add_argument('--all', action='store_true', help='update every collection found in the workdir')
    update.add_argument('short_names', nargs='*', metavar='SHORT_NAME', help='collections to update')
    update.set_defaults(usage_parser=update)
    return parser


def parse_args(argv: list[str]) -> argparse.Namespace:
    """
    Parse and validate command line arguments.

    Raises
    ------
    UsageError
        If the arguments are invalid
    """
    args = build_parser().parse_args(argv)
    if args.command == COMMAND_UPDATE_REFERENCE and args.all == bool(args.short_names):
        args.usage_parser.error('give either one or more SHORT_NAMEs or --all')
    return args


def _configure_logging(redactor: Redactor) -> None:
    """Send the suite's progress messages to stderr with AWS account IDs redacted."""
    handler = logging.StreamHandler()
    handler.set_name(_LOG_HANDLER_NAME)
    handler.setFormatter(RedactingFormatter(redactor, '%(asctime)s %(levelname)s %(message)s'))

    for existing in list(LOGGER.handlers):
        if existing.get_name() == _LOG_HANDLER_NAME:
            LOGGER.removeHandler(existing)
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)


def _peek_report_dir(argv: list[str]) -> Path:
    """Find the report directory in arguments that failed to parse, falling back to the default."""
    for index, argument in enumerate(argv):
        if argument == _REPORT_DIR_OPTION and index + 1 < len(argv):
            return Path(argv[index + 1])
        if argument.startswith(f'{_REPORT_DIR_OPTION}='):
            return Path(argument.split('=', 1)[1])
    return config.DEFAULT_REPORT_DIR


def _write_error_report(report_dir: Path, run_info: report.RunInfo, message: str, redactor: Redactor) -> None:
    """Write a report explaining why the run could not start; a report directory that cannot be written is only logged."""
    try:
        report_path = report.write_report(report_dir, run_info, [], redactor, error=message)
    except OSError as ex:
        LOGGER.error('Unable to write the report to %s: %s', report_dir, ex)
        return
    LOGGER.info('Report written to %s', report_path)


def _prepare_run(args: argparse.Namespace, run_info: report.RunInfo, redactor: Redactor) -> tuple[list, runner.RunContext]:
    """
    Resolve the deployment, select the test cases, get a CMR token and connect to AWS, filling in the run metadata
    along the way.

    Raises
    ------
    ConfigError, ManifestError, CmrError, StateMachineNotFound, BotoCoreError, ClientError
        If the run cannot start
    """
    arn_region = arn_name = None
    if args.state_machine_arn:
        try:
            arn_region, account_id, arn_name = aws.parse_state_machine_arn(args.state_machine_arn)
        except ValueError as ex:
            raise config.ConfigError(f'--state-machine-arn: {ex}') from ex
        redactor.add_account_id(account_id)

    # A state machine given by ARN is run in the ARN's region unless --region says otherwise
    deployment = config.resolve_deployment(args.venue, args.region or arn_region, args.prefix, args.workspace)
    run_info.deployment = deployment
    run_info.state_machine_name = arn_name or deployment.state_machine_name

    all_cases = manifest.load_test_cases(args.test_cases)
    cases = manifest.select_cases(all_cases, deployment.cmr_environment, args.only)
    if not cases:
        raise manifest.ManifestError(f'No test cases to run: {args.test_cases} has no rows for CMR environment '
                                     f'{deployment.cmr_environment} matching the selection')
    other_environment = sum(1 for case in all_cases if case.cmr_env != deployment.cmr_environment)
    if other_environment:
        run_info.notes.append(f'{other_environment} manifest row(s) for another CMR environment were not run.')
    if args.only:
        run_info.notes.append('Limited to collection(s): ' + ', '.join(f'`{name}`' for name in sorted(set(args.only))) + '.')

    LOGGER.info('Target: venue %s, region %s, resource prefix %s, state machine %s, CMR %s; %d test case(s), up to %d at a time', deployment.venue,
                deployment.region, deployment.resource_prefix, run_info.state_machine_name, deployment.cmr_environment, len(cases),
                args.max_parallel)

    # Fetched once for the whole run, so that bad credentials stop the run instead of failing every case
    edl_credentials = cmr.get_edl_credentials(deployment.cmr_environment)
    if edl_credentials:
        redactor.add_secret(edl_credentials[1])
    cmr_token = cmr.get_token(deployment.cmr_environment, edl_credentials)
    redactor.add_secret(cmr_token)

    session = aws.create_session(args.aws_profile, deployment.region)
    redactor.add_account_id(aws.get_account_id(session))

    sfn_client = session.client('stepfunctions')
    state_machine_arn = args.state_machine_arn or aws.find_state_machine_arn(sfn_client, deployment.state_machine_name)
    run_info.deployed_version = aws.get_deployed_version(session.client('lambda'), deployment.version_lambda_name)
    LOGGER.info('Deployed bignbit version: %s', run_info.deployed_version)

    context = runner.RunContext(deployment=deployment,
                                state_machine_arn=state_machine_arn,
                                sfn_client=sfn_client,
                                s3_client=session.client('s3'),
                                redactor=redactor,
                                workdir=args.workdir,
                                reference_dir=args.reference_dir,
                                poll_interval=args.poll_interval,
                                execution_timeout=args.execution_timeout,
                                download_browse=args.download_browse,
                                max_parallel=args.max_parallel,
                                cmr_token=cmr_token)
    return cases, context


def run_command(args: argparse.Namespace, redactor: Redactor) -> int:
    """
    Run the regression test cases and write the report.

    Returns
    -------
    int
        0 when every case that was not skipped passed, 1 when at least one did not, 2 when the run could not start
    """
    run_info = report.RunInfo(venue=args.venue, started_at=datetime.now(timezone.utc), source=report.source_info(),
                              max_parallel=args.max_parallel)
    started = time.monotonic()

    try:
        cases, context = _prepare_run(args, run_info, redactor)
    except _STARTUP_ERRORS as ex:
        message = str(ex) if isinstance(ex, (config.ConfigError, manifest.ManifestError, cmr.CmrError)) else f'{type(ex).__name__}: {ex}'
        LOGGER.error('The regression run could not start: %s', message)
        run_info.duration_seconds = time.monotonic() - started
        _write_error_report(args.report_dir, run_info, message, redactor)
        return EXIT_NOT_STARTED

    results, run_info.interrupted = runner.run_cases(cases, context)
    run_info.duration_seconds = time.monotonic() - started
    if run_info.interrupted:
        run_info.notes.append(f'The run was interrupted after {len(results)} of {len(cases)} case(s).')

    report_path = report.write_report(args.report_dir, run_info, results, redactor)
    counts = summarize(results)
    LOGGER.info('Verdict: %s (%d total, %d passed, %d failed, %d mismatched, %d skipped)',
                report.get_verdict(results, run_info.interrupted), counts['total'], counts['passed'], counts['failed'],
                counts['mismatched'], counts['skipped'])
    LOGGER.info('Report written to %s', report_path)
    return exit_code(results, run_info.interrupted)


def update_reference_command(args: argparse.Namespace) -> int:
    """
    Promote workdir results to reference data.

    Returns
    -------
    int
        0 when every requested collection was updated, otherwise 1
    """
    short_names = reference.list_workdir_collections(args.workdir) if args.all else args.short_names
    if not short_names:
        LOGGER.error('No collections found in %s', args.workdir)
        return EXIT_FAILURES
    not_updated = reference.update_reference(args.workdir, args.reference_dir, short_names)
    return EXIT_FAILURES if not_updated else EXIT_OK


def main(argv: list[str] = None) -> int:
    """
    Entry point of the command line interface.

    Parameters
    ----------
    argv : list[str]
        Command line arguments; defaults to those of the process

    Returns
    -------
    int
        The process exit code
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    redactor = Redactor()
    _configure_logging(redactor)

    try:
        args = parse_args(argv)
    except UsageError as ex:
        sys.stderr.write(f'{ex.usage}{ex}\n')
        if argv[:1] == [COMMAND_RUN]:
            run_info = report.RunInfo(venue=report.UNKNOWN, started_at=datetime.now(timezone.utc), source=report.source_info())
            _write_error_report(_peek_report_dir(argv), run_info, str(ex), redactor)
        return EXIT_NOT_STARTED

    if args.command == COMMAND_RUN:
        return run_command(args, redactor)
    return update_reference_command(args)
