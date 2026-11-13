"""
Loads the regression test case manifest (a CSV file) and selects the cases to run
"""
import csv
from dataclasses import dataclass
from pathlib import Path

FIELDNAMES = ('short_name', 'provider', 'granule_ur', 'cmr_env', 'skip')
REQUIRED_VALUES = ('short_name', 'provider', 'granule_ur', 'cmr_env')
CMR_ENVIRONMENTS = ('UAT', 'OPS')


class ManifestError(Exception):
    """
    Exception for a test case manifest that is missing or malformed
    """


@dataclass(frozen=True)
class RegressionCase:
    """
    One row of the manifest: a granule to run through the browse image workflow
    """
    short_name: str
    provider: str
    granule_ur: str
    cmr_env: str
    skip: str = ''

    @property
    def skipped(self) -> bool:
        """True when the row is marked to be skipped (any non-empty value in the skip column)."""
        return bool(self.skip)


def is_safe_path_component(name: str) -> bool:
    """
    Check that a name can be used as a single directory name. Short names and granule URs become directories under
    the workdir and reference data trees, so anything that could escape them is rejected.
    """
    path = Path(name)
    return bool(name) and not path.is_absolute() and '..' not in path.parts and path.name == name


def _parse_row(row: dict, line_number: int, path: Path) -> RegressionCase:
    """Build a RegressionCase from a CSV row, raising ManifestError when the row is invalid."""
    values = {name: (row.get(name) or '').strip() for name in FIELDNAMES}

    missing = [name for name in REQUIRED_VALUES if not values[name]]
    if missing:
        raise ManifestError(f'{path} line {line_number}: missing value for {", ".join(missing)}')

    for name in ('short_name', 'granule_ur'):
        if not is_safe_path_component(values[name]):
            raise ManifestError(f'{path} line {line_number}: invalid {name} {values[name]!r}')

    values['cmr_env'] = values['cmr_env'].upper()
    if values['cmr_env'] not in CMR_ENVIRONMENTS:
        raise ManifestError(f'{path} line {line_number}: cmr_env must be one of {", ".join(CMR_ENVIRONMENTS)}')

    return RegressionCase(**values)


def load_test_cases(path) -> list[RegressionCase]:
    """
    Read the test case manifest.

    Parameters
    ----------
    path : str or Path
        CSV file with the header ``short_name,provider,granule_ur,cmr_env,skip``

    Returns
    -------
    list[RegressionCase]
        The test cases in file order, including rows marked to be skipped

    Raises
    ------
    ManifestError
        If the file cannot be read, lacks a required column, or contains an invalid or duplicated row
    """
    path = Path(path)
    cases = []
    seen = set()
    try:
        with open(path, 'r', encoding='utf-8', newline='') as handle:
            reader = csv.DictReader(handle)
            missing_columns = [name for name in FIELDNAMES if name not in (reader.fieldnames or [])]
            if missing_columns:
                raise ManifestError(f'{path}: missing column(s) {", ".join(missing_columns)}; expected header {",".join(FIELDNAMES)}')

            for row in reader:
                if not any((value or '').strip() for value in row.values() if isinstance(value, str)):
                    continue
                case = _parse_row(row, reader.line_num, path)
                key = (case.short_name, case.granule_ur, case.cmr_env)
                if key in seen:
                    raise ManifestError(f'{path} line {reader.line_num}: duplicate test case {case.short_name} / {case.granule_ur}')
                seen.add(key)
                cases.append(case)
    except (OSError, csv.Error) as ex:
        raise ManifestError(f'Unable to read test case manifest {path}: {ex}') from ex
    return cases


def select_cases(cases: list[RegressionCase], cmr_env: str, only: list[str] = None) -> list[RegressionCase]:
    """
    Select the test cases that apply to a deployment.

    Parameters
    ----------
    cases : list[RegressionCase]
        All cases from the manifest
    cmr_env : str
        CMR environment of the target deployment; rows for another environment are left out
    only : list[str]
        Optional collection short names to restrict the run to

    Returns
    -------
    list[RegressionCase]
        The selected cases in manifest order. Rows marked to be skipped are kept so they can be reported.

    Raises
    ------
    ManifestError
        If ``only`` names a collection that is not in the manifest
    """
    if only:
        unknown = sorted(set(only) - {case.short_name for case in cases})
        if unknown:
            raise ManifestError(f'Collection(s) not found in the test case manifest: {", ".join(unknown)}')

    return [case for case in cases
            if case.cmr_env == cmr_env.upper() and (not only or case.short_name in only)]
