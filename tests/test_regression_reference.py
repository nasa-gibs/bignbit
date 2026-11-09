"""Unit tests for promoting regression workdir results to reference data"""
import os

from regression_helpers import make_cnm, write_cnm
from regression import reference

SHORT_NAME = 'MUR-JPL-L4-GLOB-v4.1'
GRANULE_UR = 'granule-1'


def populate_workdir(workdir, short_name=SHORT_NAME, granule_ur=GRANULE_UR, cmr_env='UAT', checksum='new'):
    """Create the files a run leaves behind for one granule and return its results directory"""
    results_dir = workdir / short_name / granule_ur / cmr_env
    write_cnm(results_dir / 'cnm', 'a', make_cnm('MUR_sst_LL', 'g.r00c00.png', checksum))
    (results_dir / 'input.json').write_text('{}', encoding='utf-8')
    (results_dir / 'output.json').write_text('{"arn": "arn:aws:lambda:us-east-1:123456789012:function:x"}', encoding='utf-8')
    (results_dir / 'files').mkdir()
    (results_dir / 'files' / 'g.r00c00.png').write_bytes(b'png')
    return results_dir


def reference_files(reference_dir):
    """Return every file in the reference tree, relative to its root"""
    return sorted(str(path.relative_to(reference_dir)) for path in reference_dir.rglob('*') if path.is_file())


def test_update_copies_only_the_cnm_directory(tmp_path):
    """Only cnm/ is promoted: inputs, outputs (which contain ARNs) and browse images stay in the workdir"""
    workdir, reference_dir = tmp_path / 'workdir', tmp_path / 'reference'
    populate_workdir(workdir)

    assert reference.update_reference(workdir, reference_dir, [SHORT_NAME]) == []

    assert reference_files(reference_dir) == [os.path.join(SHORT_NAME, GRANULE_UR, 'cnm', 'a.cnm.json')]


def test_update_replaces_existing_reference_cnms(tmp_path):
    """Old reference CNMs of the granule are removed; other granules of the collection are left alone"""
    workdir, reference_dir = tmp_path / 'workdir', tmp_path / 'reference'
    populate_workdir(workdir)
    write_cnm(reference_dir / SHORT_NAME / GRANULE_UR / 'cnm', 'stale', make_cnm('MUR_sst_LL', 'g.r00c00.png', 'old'))
    write_cnm(reference_dir / SHORT_NAME / 'granule-2' / 'cnm', 'kept', make_cnm('MUR_sst_LL', 'g.r00c00.png', 'old'))

    reference.update_reference(workdir, reference_dir, [SHORT_NAME])

    assert reference_files(reference_dir) == [os.path.join(SHORT_NAME, GRANULE_UR, 'cnm', 'a.cnm.json'),
                                              os.path.join(SHORT_NAME, 'granule-2', 'cnm', 'kept.cnm.json')]
    assert '"new"' in (reference_dir / SHORT_NAME / GRANULE_UR / 'cnm' / 'a.cnm.json').read_text(encoding='utf-8')


def test_update_uses_the_latest_results(tmp_path):
    """When a granule has results for more than one CMR environment, the most recently written ones win"""
    workdir, reference_dir = tmp_path / 'workdir', tmp_path / 'reference'
    older = populate_workdir(workdir, cmr_env='UAT', checksum='older')
    newer = populate_workdir(workdir, cmr_env='OPS', checksum='newer')
    os.utime(older / 'cnm', (1_000_000, 1_000_000))
    os.utime(newer / 'cnm', (2_000_000, 2_000_000))

    reference.update_reference(workdir, reference_dir, [SHORT_NAME])

    assert '"newer"' in (reference_dir / SHORT_NAME / GRANULE_UR / 'cnm' / 'a.cnm.json').read_text(encoding='utf-8')


def test_update_keeps_reference_when_the_run_produced_no_cnms(tmp_path):
    """A failed run (no CNMs in the workdir) must not wipe out the existing reference data"""
    workdir, reference_dir = tmp_path / 'workdir', tmp_path / 'reference'
    (workdir / SHORT_NAME / GRANULE_UR / 'UAT').mkdir(parents=True)
    (workdir / SHORT_NAME / GRANULE_UR / 'UAT' / 'error.json').write_text('{}', encoding='utf-8')
    write_cnm(reference_dir / SHORT_NAME / GRANULE_UR / 'cnm', 'existing', make_cnm('MUR_sst_LL', 'g.r00c00.png', 'old'))

    assert reference.update_reference(workdir, reference_dir, [SHORT_NAME]) == [SHORT_NAME]

    assert reference_files(reference_dir) == [os.path.join(SHORT_NAME, GRANULE_UR, 'cnm', 'existing.cnm.json')]


def test_update_rejects_path_traversal(tmp_path):
    """Short names that would escape the workdir or reference tree are skipped"""
    workdir, reference_dir = tmp_path / 'root' / 'workdir', tmp_path / 'root' / 'reference'
    populate_workdir(tmp_path / 'root', short_name='outside')
    populate_workdir(workdir)
    names = ['../outside', '..', str(tmp_path / 'root' / 'outside'), 'a/b', '', 'MISSING', SHORT_NAME]

    not_updated = reference.update_reference(workdir, reference_dir, names)

    assert not_updated == names[:-1]
    assert reference_files(reference_dir) == [os.path.join(SHORT_NAME, GRANULE_UR, 'cnm', 'a.cnm.json')]
    assert sorted(path.name for path in (tmp_path / 'root').iterdir()) == ['outside', 'reference', 'workdir']


def test_list_workdir_collections(tmp_path):
    """--all covers every collection directory in the workdir; a missing workdir has none"""
    populate_workdir(tmp_path, short_name='B')
    populate_workdir(tmp_path, short_name='A')
    (tmp_path / 'stray-file.txt').write_text('x', encoding='utf-8')

    assert reference.list_workdir_collections(tmp_path) == ['A', 'B']
    assert reference.list_workdir_collections(tmp_path / 'missing') == []
