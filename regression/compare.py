"""
Compares the browse images listed in a run's CNM messages against reference CNM messages by checksum
"""
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

MATCH = 'MATCH'
MISMATCH = 'MISMATCH'

TILE_PATTERN = re.compile(r'\.(r\d+c\d+)\.')
SUBTILE_PATTERN = re.compile(r'_(\d{6})_')


@dataclass
class BrowseImageResult:
    """
    A browse image file listed in a CNM message
    """
    file_name: str
    checksum: str
    checksum_type: str
    collection: str
    tile_id: str = ''

    @property
    def key(self) -> str:
        """Identity of the image within a granule's results: its GIBS collection plus tile id, when tiled."""
        if self.tile_id:
            return f'{self.collection}|{self.tile_id}'
        return self.collection


@dataclass
class CnmResults:
    """
    The browse images found in a directory of CNM messages for one granule
    """
    short_name: str
    granule_ur: str
    cmr_env: str
    cnm_file_count: int = 0
    browse_images: list[BrowseImageResult] = field(default_factory=list)


@dataclass
class Comparison:
    """
    Outcome of comparing a run's CNM results with the reference: MATCH, or MISMATCH with the differences found
    """
    status: str
    issues: list[str] = field(default_factory=list)


def extract_tile_id(filename: str) -> str:
    """Return the tile id (for example ``r00c01`` or a six digit subtile) embedded in a browse image filename, if any."""
    match = TILE_PATTERN.search(filename) or SUBTILE_PATTERN.search(filename)
    return match.group(1) if match else ''


def load_cnm_files(short_name: str, granule_ur: str, cmr_env: str, cnm_dir) -> CnmResults:
    """
    Collect the browse images from every CNM JSON file in a directory.

    Parameters
    ----------
    short_name : str
        Collection short name the CNMs belong to
    granule_ur : str
        Granule UR the CNMs belong to
    cmr_env : str
        CMR environment of the run
    cnm_dir : str or Path
        Directory of ``*.json`` CNM messages; a missing directory yields empty results

    Returns
    -------
    CnmResults
        The number of CNM files and the browse images they list
    """
    results = CnmResults(short_name=short_name, granule_ur=granule_ur, cmr_env=cmr_env)
    cnm_dir = Path(cnm_dir)

    if not cnm_dir.is_dir():
        return results

    for cnm_path in sorted(cnm_dir.iterdir()):
        if not cnm_path.name.endswith('.json'):
            continue

        results.cnm_file_count += 1
        cnm_data = json.loads(cnm_path.read_text(encoding='utf-8'))
        collection = cnm_data.get('collection', '')

        for product_file in cnm_data.get('product', {}).get('files', []):
            if product_file.get('type') == 'browse':
                file_name = product_file.get('fileName', '')
                results.browse_images.append(BrowseImageResult(
                    file_name=file_name,
                    checksum=product_file.get('checksum', ''),
                    checksum_type=product_file.get('checksumType', ''),
                    collection=collection,
                    tile_id=extract_tile_id(file_name),
                ))

    return results


def _identity_issues(reference: CnmResults, current: CnmResults) -> list[str]:
    """Report results that do not describe the same granule or the same number of CNM messages."""
    issues = []
    if reference.short_name != current.short_name:
        issues.append(f'short_name mismatch: reference={reference.short_name}, current={current.short_name}')
    if reference.granule_ur != current.granule_ur:
        issues.append(f'granule_ur mismatch: reference={reference.granule_ur}, current={current.granule_ur}')
    if reference.cmr_env != current.cmr_env:
        issues.append(f'cmr_env mismatch: reference={reference.cmr_env}, current={current.cmr_env}')
    if reference.cnm_file_count != current.cnm_file_count:
        issues.append(f'CNM file count: reference={reference.cnm_file_count}, current={current.cnm_file_count}')
    return issues


def compare(reference: CnmResults, current: CnmResults) -> Comparison:
    """
    Compare a run's browse images with the reference by GIBS collection, tile id, checksum type and checksum.

    Parameters
    ----------
    reference : CnmResults
        Results loaded from the reference data
    current : CnmResults
        Results loaded from the run being tested

    Returns
    -------
    Comparison
        MATCH when the two sets of browse images are identical, otherwise MISMATCH with one issue per difference
    """
    if reference.cnm_file_count == 0:
        return Comparison(MISMATCH, ['No reference data found for comparison'])

    issues = _identity_issues(reference, current)

    ref_keys = [img.key for img in reference.browse_images]
    cur_keys = [img.key for img in current.browse_images]

    issues.extend(f'Duplicate in reference: {key}' for key, count in Counter(ref_keys).items() if count > 1)
    issues.extend(f'Duplicate in current: {key}' for key, count in Counter(cur_keys).items() if count > 1)

    ref_map = {img.key: img for img in reference.browse_images}
    cur_map = {img.key: img for img in current.browse_images}

    for key in sorted(set(ref_keys) - set(cur_keys)):
        issues.append(f'Missing from current: {ref_map[key].collection} [{ref_map[key].file_name}]')

    for key in sorted(set(cur_keys) - set(ref_keys)):
        issues.append(f'Extra in current: {cur_map[key].collection} [{cur_map[key].file_name}]')

    for key in sorted(set(ref_keys) & set(cur_keys)):
        if ref_map[key].checksum_type != cur_map[key].checksum_type:
            issues.append(f'ChecksumType mismatch: {ref_map[key].collection} [{ref_map[key].file_name}]')
        if ref_map[key].checksum != cur_map[key].checksum:
            issues.append(f'Checksum mismatch: {ref_map[key].collection} [{ref_map[key].file_name}]')

    return Comparison(MISMATCH, issues) if issues else Comparison(MATCH)
