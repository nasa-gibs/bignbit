"""Unit tests for the regression suite's CNM checksum comparison"""
from regression_helpers import make_cnm, write_cnm
from regression import compare

SHORT_NAME = 'MUR-JPL-L4-GLOB-v4.1'
GRANULE_UR = 'granule-1'


def load(cnm_dir):
    """Load CNM results for the test granule from a directory"""
    return compare.load_cnm_files(SHORT_NAME, GRANULE_UR, 'UAT', cnm_dir)


def write_tiles(cnm_dir, checksums, collection='MUR_sst_LL'):
    """Write one CNM per tile, with the given checksum for each tile id"""
    for index, (tile_id, checksum) in enumerate(checksums.items()):
        write_cnm(cnm_dir, f'tile{index}', make_cnm(collection, f'granule-1_sst.{tile_id}.png', checksum))


def test_extract_tile_id():
    """Tile ids are taken from rNNcNN tiles first, then six digit subtiles, else empty"""
    assert compare.extract_tile_id('granule_sst_reformatted.r00c01.png') == 'r00c01'
    assert compare.extract_tile_id('OPERA_L3_DSWx_BROWSE_003002_reprojected.png') == '003002'
    assert compare.extract_tile_id('granule_003002_sst.r01c02.png') == 'r01c02'
    assert compare.extract_tile_id('granule_sst.png') == ''


def test_load_cnm_files_collects_browse_images_only(tmp_path):
    """Only browse files are collected and only .json files are counted"""
    write_cnm(tmp_path, 'a', make_cnm('MUR_sst_LL', 'granule-1_sst.r00c00.png', 'abc'))
    (tmp_path / 'notes.txt').write_text('not a cnm', encoding='utf-8')

    results = load(tmp_path)

    assert results.cnm_file_count == 1
    assert len(results.browse_images) == 1
    image = results.browse_images[0]
    assert (image.file_name, image.checksum, image.checksum_type) == ('granule-1_sst.r00c00.png', 'abc', 'MD5')
    assert image.key == 'MUR_sst_LL|r00c00'


def test_load_cnm_files_missing_directory(tmp_path):
    """A missing directory gives empty results rather than an error"""
    results = load(tmp_path / 'does-not-exist')
    assert results.cnm_file_count == 0
    assert results.browse_images == []


def test_untiled_image_key_is_the_collection(tmp_path):
    """An image without a tile id is identified by its collection alone"""
    write_cnm(tmp_path, 'a', make_cnm('MODIS_sst', 'AQUA_MODIS.sst.png', 'abc'))
    assert load(tmp_path).browse_images[0].key == 'MODIS_sst'


def test_compare_match_ignores_cnm_filenames(tmp_path):
    """Identical checksums match even though the CNM filenames (which embed a timestamp) differ"""
    write_cnm(tmp_path / 'ref', 'granule.2026-06-10T19_24_23.277Z', make_cnm('MUR_sst_LL', 'g.r00c00.png', 'abc'))
    write_cnm(tmp_path / 'cur', 'granule.2026-11-09T01_02_03.004Z', make_cnm('MUR_sst_LL', 'g.r00c00.png', 'abc'))

    comparison = compare.compare(load(tmp_path / 'ref'), load(tmp_path / 'cur'))

    assert comparison.status == compare.MATCH
    assert comparison.issues == []


def test_compare_checksum_mismatch(tmp_path):
    """A changed checksum is reported for the tile it belongs to"""
    write_tiles(tmp_path / 'ref', {'r00c00': 'abc', 'r00c01': 'def'})
    write_tiles(tmp_path / 'cur', {'r00c00': 'abc', 'r00c01': 'XXX'})

    comparison = compare.compare(load(tmp_path / 'ref'), load(tmp_path / 'cur'))

    assert comparison.status == compare.MISMATCH
    assert comparison.issues == ['Checksum mismatch: MUR_sst_LL [granule-1_sst.r00c01.png]']


def test_compare_checksum_type_mismatch(tmp_path):
    """A changed checksum type is reported separately from the checksum"""
    write_cnm(tmp_path / 'ref', 'a', make_cnm('MUR_sst_LL', 'g.r00c00.png', 'abc', 'MD5'))
    write_cnm(tmp_path / 'cur', 'a', make_cnm('MUR_sst_LL', 'g.r00c00.png', 'abc', 'SHA512'))

    comparison = compare.compare(load(tmp_path / 'ref'), load(tmp_path / 'cur'))

    assert comparison.issues == ['ChecksumType mismatch: MUR_sst_LL [g.r00c00.png]']


def test_compare_missing_and_extra_images(tmp_path):
    """Images present on only one side are reported, along with the differing CNM file count"""
    write_tiles(tmp_path / 'ref', {'r00c00': 'abc', 'r00c01': 'def'})
    write_tiles(tmp_path / 'cur', {'r00c00': 'abc'})
    write_cnm(tmp_path / 'cur', 'other', make_cnm('MUR_ice_LL', 'granule-1_ice.r00c00.png', 'abc'))
    write_cnm(tmp_path / 'cur', 'another', make_cnm('MUR_ice_LL', 'granule-1_ice.r00c01.png', 'abc'))

    comparison = compare.compare(load(tmp_path / 'ref'), load(tmp_path / 'cur'))

    assert comparison.status == compare.MISMATCH
    assert comparison.issues == [
        'CNM file count: reference=2, current=3',
        'Missing from current: MUR_sst_LL [granule-1_sst.r00c01.png]',
        'Extra in current: MUR_ice_LL [granule-1_ice.r00c00.png]',
        'Extra in current: MUR_ice_LL [granule-1_ice.r00c01.png]'
    ]


def test_compare_duplicates(tmp_path):
    """The same collection and tile appearing twice is reported for whichever side it is on"""
    write_cnm(tmp_path / 'ref', 'a', make_cnm('MUR_sst_LL', 'g.r00c00.png', 'abc'))
    write_cnm(tmp_path / 'ref', 'b', make_cnm('MUR_sst_LL', 'g.r00c00.png', 'abc'))
    write_cnm(tmp_path / 'cur', 'a', make_cnm('MUR_sst_LL', 'g.r00c00.png', 'abc'))
    write_cnm(tmp_path / 'cur', 'b', make_cnm('MUR_sst_LL', 'g.r00c00.png', 'abc'))

    comparison = compare.compare(load(tmp_path / 'ref'), load(tmp_path / 'cur'))

    assert comparison.issues == ['Duplicate in reference: MUR_sst_LL|r00c00', 'Duplicate in current: MUR_sst_LL|r00c00']


def test_compare_without_reference_data(tmp_path):
    """A case with no reference data can never match"""
    write_tiles(tmp_path / 'cur', {'r00c00': 'abc'})

    comparison = compare.compare(load(tmp_path / 'ref'), load(tmp_path / 'cur'))

    assert comparison.status == compare.MISMATCH
    assert comparison.issues == ['No reference data found for comparison']


def test_compare_no_current_results(tmp_path):
    """A run that produced no CNMs is a mismatch listing everything that is missing"""
    write_tiles(tmp_path / 'ref', {'r00c00': 'abc'})

    comparison = compare.compare(load(tmp_path / 'ref'), load(tmp_path / 'cur'))

    assert comparison.issues == ['CNM file count: reference=1, current=0',
                                 'Missing from current: MUR_sst_LL [granule-1_sst.r00c00.png]']


def test_compare_identity_mismatch(tmp_path):
    """Results describing a different granule are reported as such"""
    write_tiles(tmp_path, {'r00c00': 'abc'})
    reference = compare.load_cnm_files(SHORT_NAME, GRANULE_UR, 'UAT', tmp_path)
    current = compare.load_cnm_files('OTHER', 'granule-2', 'OPS', tmp_path)

    comparison = compare.compare(reference, current)

    assert comparison.issues == ['short_name mismatch: reference=MUR-JPL-L4-GLOB-v4.1, current=OTHER',
                                 'granule_ur mismatch: reference=granule-1, current=granule-2',
                                 'cmr_env mismatch: reference=UAT, current=OPS']
