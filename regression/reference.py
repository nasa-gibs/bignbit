"""
Promotes the CNM messages of the latest regression run to reference data for future comparisons.

Only the ``cnm`` directory of each granule is copied, from ``<workdir>/<short_name>/<granule_ur>/<cmr_env>/cnm`` to
``<reference_dir>/<short_name>/<granule_ur>/cnm``. Inputs, execution outputs and browse images stay in the workdir.
"""
import logging
import shutil
from pathlib import Path

from regression.manifest import is_safe_path_component

LOGGER = logging.getLogger(__name__)

CNM_DIRNAME = 'cnm'


def list_workdir_collections(workdir) -> list[str]:
    """Return the short names of the collections that have a directory in the workdir."""
    workdir = Path(workdir)
    if not workdir.is_dir():
        return []
    return sorted(path.name for path in workdir.iterdir() if path.is_dir())


def _latest_cnm_dir(granule_dir: Path) -> Path:
    """Return the most recently written cnm directory holding CNM JSON files among a granule's CMR environments, if any."""
    candidates = [env_dir / CNM_DIRNAME for env_dir in granule_dir.iterdir()
                  if env_dir.is_dir() and any((env_dir / CNM_DIRNAME).glob('*.json'))]
    return max(candidates, key=lambda path: path.stat().st_mtime, default=None)


def update_collection(workdir, reference_dir, short_name: str) -> int:
    """
    Replace a collection's reference CNMs with those of its latest workdir results.

    Parameters
    ----------
    workdir : str or Path
        Workdir of a previous ``run``
    reference_dir : str or Path
        Root of the reference data tree
    short_name : str
        Collection to update

    Returns
    -------
    int
        Number of granules whose reference CNMs were replaced; 0 when the name is invalid or the workdir holds
        no CNMs for it, in which case the existing reference data is left untouched
    """
    if not is_safe_path_component(short_name):
        LOGGER.warning('SKIP: invalid short_name %r', short_name)
        return 0

    source = Path(workdir) / short_name
    if not source.is_dir():
        LOGGER.warning('SKIP: %s does not exist', source)
        return 0

    updated = 0
    for granule_dir in sorted(path for path in source.iterdir() if path.is_dir()):
        cnm_dir = _latest_cnm_dir(granule_dir)
        if cnm_dir is None:
            LOGGER.warning('SKIP: no CNM files found for %s / %s', short_name, granule_dir.name)
            continue

        destination = Path(reference_dir) / short_name / granule_dir.name / CNM_DIRNAME
        if destination.exists():
            shutil.rmtree(destination)
        LOGGER.info('Copying: %s -> %s', cnm_dir, destination)
        shutil.copytree(cnm_dir, destination)
        updated += 1
    return updated


def update_reference(workdir, reference_dir, short_names: list[str]) -> list[str]:
    """
    Update the reference data of several collections.

    Returns
    -------
    list[str]
        The short names for which no reference data could be updated
    """
    not_updated = [short_name for short_name in short_names
                   if not update_collection(workdir, reference_dir, short_name)]
    LOGGER.info('Done: %d/%d collection(s) copied', len(short_names) - len(not_updated), len(short_names))
    return not_updated
