"""Unit tests for the regression suite's test case manifest, including the checked-in manifest and reference data"""
import json
import re

import pytest

from regression import config, manifest
from regression.manifest import RegressionCase

HEADER = 'short_name,provider,granule_ur,cmr_env,skip\n'


def write_manifest(tmp_path, body, header=HEADER):
    """Write a manifest CSV and return its path"""
    path = tmp_path / 'test_cases.csv'
    path.write_text(header + body, encoding='utf-8')
    return path


def test_load_test_cases(tmp_path):
    """Rows are read in order, values are stripped, cmr_env is upper-cased and blank lines are ignored"""
    path = write_manifest(tmp_path, 'B_COLLECTION,POCLOUD,granule-b,UAT,\n'
                                    '\n'
                                    ' A_COLLECTION , LARC_CLOUD , granule-a , uat , X \n'
                                    'C_COLLECTION,POCLOUD,granule-c,OPS,\n')

    assert manifest.load_test_cases(path) == [
        RegressionCase('B_COLLECTION', 'POCLOUD', 'granule-b', 'UAT', ''),
        RegressionCase('A_COLLECTION', 'LARC_CLOUD', 'granule-a', 'UAT', 'X'),
        RegressionCase('C_COLLECTION', 'POCLOUD', 'granule-c', 'OPS', '')
    ]


def test_skip_column(tmp_path):
    """Any non-empty skip value marks the row to be skipped"""
    path = write_manifest(tmp_path, 'A,POCLOUD,granule-a,UAT,\nB,POCLOUD,granule-b,UAT,X\nC,POCLOUD,granule-c,UAT,broken upstream\n')

    assert [case.skipped for case in manifest.load_test_cases(path)] == [False, True, True]


def test_missing_file(tmp_path):
    """A missing manifest is a manifest error"""
    with pytest.raises(manifest.ManifestError, match='Unable to read test case manifest'):
        manifest.load_test_cases(tmp_path / 'missing.csv')


def test_missing_column(tmp_path):
    """The header must name every column"""
    path = write_manifest(tmp_path, 'A,POCLOUD,granule-a\n', header='short_name,provider,granule_ur\n')

    with pytest.raises(manifest.ManifestError, match=r'missing column\(s\) cmr_env, skip'):
        manifest.load_test_cases(path)


@pytest.mark.parametrize('row, message', [
    ('A,,granule-a,UAT,', 'line 2: missing value for provider'),
    ('A,POCLOUD,granule-a,SIT,', 'line 2: cmr_env must be one of UAT, OPS'),
    ('../A,POCLOUD,granule-a,UAT,', "line 2: invalid short_name '../A'"),
    ('A,POCLOUD,some/granule,UAT,', "line 2: invalid granule_ur 'some/granule'"),
    ('A,POCLOUD,..,UAT,', "line 2: invalid granule_ur '..'"),
])
def test_invalid_rows(tmp_path, row, message):
    """Rows with missing or unsafe values are rejected with their line number"""
    with pytest.raises(manifest.ManifestError, match=re.escape(message)):
        manifest.load_test_cases(write_manifest(tmp_path, row + '\n'))


def test_duplicate_rows(tmp_path):
    """The same granule listed twice would overwrite its own results, so it is rejected"""
    path = write_manifest(tmp_path, 'A,POCLOUD,granule-a,UAT,\nA,POCLOUD,granule-a,UAT,X\n')

    with pytest.raises(manifest.ManifestError, match='line 3: duplicate test case A / granule-a'):
        manifest.load_test_cases(path)


@pytest.mark.parametrize('name, safe', [
    ('MUR-JPL-L4-GLOB-v4.1', True),
    ('PREFIRE_SAT2_2B-FLX_R01_P00_20260407203733_10344.nc.G00.tif', True),
    ('', False),
    ('.', False),
    ('..', False),
    ('../other', False),
    ('a/b', False),
    ('/absolute', False),
])
def test_is_safe_path_component(name, safe):
    """Only single, relative path components are safe to use as directory names"""
    assert manifest.is_safe_path_component(name) is safe


def test_select_cases_by_cmr_environment():
    """Only rows for the deployment's CMR environment are selected; skipped rows stay so they can be reported"""
    cases = [
        RegressionCase('A', 'POCLOUD', 'granule-a', 'UAT'),
        RegressionCase('B', 'POCLOUD', 'granule-b', 'OPS'),
        RegressionCase('C', 'POCLOUD', 'granule-c', 'UAT', 'X')
    ]

    assert [case.short_name for case in manifest.select_cases(cases, 'UAT')] == ['A', 'C']
    assert [case.short_name for case in manifest.select_cases(cases, 'ops')] == ['B']


def test_select_cases_only():
    """--only restricts the run to the named collections"""
    cases = [RegressionCase(name, 'POCLOUD', f'granule-{name}', 'UAT') for name in ('A', 'B', 'C')]

    assert [case.short_name for case in manifest.select_cases(cases, 'UAT', only=['C', 'A'])] == ['A', 'C']


def test_select_cases_only_unknown_collection():
    """Naming a collection that is not in the manifest is an error rather than an empty run"""
    cases = [RegressionCase('A', 'POCLOUD', 'granule-a', 'UAT')]

    with pytest.raises(manifest.ManifestError, match='not found in the test case manifest: NOPE, ZZZ'):
        manifest.select_cases(cases, 'UAT', only=['ZZZ', 'A', 'NOPE'])


def test_checked_in_manifest():
    """The checked-in manifest loads, is sorted by short name and has cases for the UAT CMR environment"""
    cases = manifest.load_test_cases(config.DEFAULT_TEST_CASES)

    assert cases
    assert [case.short_name for case in cases] == sorted(case.short_name for case in cases)
    assert manifest.select_cases(cases, 'UAT')


def test_checked_in_manifest_matches_reference_data():
    """Every test case has reference CNMs, and the reference tree holds nothing but those cases' per-environment cnm directories"""
    cases = manifest.load_test_cases(config.DEFAULT_TEST_CASES)
    expected_dirs = {config.DEFAULT_REFERENCE_DIR / case.short_name / case.granule_ur / case.cmr_env / 'cnm' for case in cases}

    for cnm_dir in expected_dirs:
        assert any(cnm_dir.glob('*.cnm.json')), f'no reference CNMs in {cnm_dir}'

    files = [path for path in config.DEFAULT_REFERENCE_DIR.rglob('*') if path.is_file()]
    assert files
    for path in files:
        assert path.parent in expected_dirs, f'unexpected file in reference data: {path}'
        assert path.name.endswith('.cnm.json'), f'unexpected file in reference data: {path}'


def test_checked_in_reference_data_has_no_account_ids():
    """
    This repository is public: reference CNMs must not contain ARNs or AWS account IDs. Checksums are skipped when
    looking for 12 digit numbers because hex digests contain digit runs by chance.
    """
    account_id = re.compile(r'(?<!\d)\d{12}(?!\d)')

    def strings(value, key=''):
        if isinstance(value, dict):
            for child_key, child in value.items():
                yield from strings(child, child_key)
        elif isinstance(value, list):
            for child in value:
                yield from strings(child, key)
        elif isinstance(value, str) and key != 'checksum':
            yield value

    for path in config.DEFAULT_REFERENCE_DIR.rglob('*.json'):
        text = path.read_text(encoding='utf-8')
        assert 'arn:aws' not in text, f'ARN found in {path}'
        assert not account_id.search(path.name), f'12 digit number in the name of {path}'
        for value in strings(json.loads(text)):
            assert not account_id.search(value), f'12 digit number found in {path}: {value}'
