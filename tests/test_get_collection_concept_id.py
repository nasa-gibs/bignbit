import pytest
from bignbit.get_collection_concept_id import CMA, get_collection_concept_id

CMA_CONFIG = {
    'collection_shortname': 'SHORT',
    'collection_version': '1',
    'cmr_provider': 'PROV',
    'cmr_environment': 'UAT'
}

@pytest.mark.vcr
@pytest.mark.parametrize("collection_shortname, collection_version, collection_provider, expected_concept_id", [
    ("PREFIRE_SAT2_2B-FLX_COG", "R01", "LARC_CLOUD", "C1273150419-LARC_CLOUD"),
    ("MUR-JPL-L4-GLOB-v4.1", "4.1", "POCLOUD", "C1238621141-POCLOUD")
], ids=["PREFIRE_SAT2_2B-FLX_COG", "MUR-JPL-L4-GLOB-v4.1"])
def test_get_collection_concept_id(collection_shortname, collection_version, collection_provider, expected_concept_id, monkeypatch):

    monkeypatch.setattr('bignbit.utils.get_edl_creds', lambda: (None, None))
    monkeypatch.setattr('bignbit.utils.get_cmr_user_token', lambda *args: None)

    concept_id = get_collection_concept_id(collection_shortname, collection_version, collection_provider, "UAT")

    assert expected_concept_id == concept_id

@pytest.mark.parametrize("dataset_config, expected_concept_id", [
    ({"concept_id": "C111-PROV"}, "C111-PROV"),
    ({"collectionId": "C222-PROV"}, "C222-PROV"),
    ({"concept_id": "C111-PROV", "collectionId": "C222-PROV"}, "C111-PROV"),
    ({"concept_id": "", "collectionId": "C222-PROV"}, "C222-PROV"),
], ids=["concept_id", "collectionId", "concept_id_precedence", "empty_concept_id"])
def test_process_uses_config_override(dataset_config, expected_concept_id, monkeypatch, tmp_path):
    def fail_lookup(*args):
        raise AssertionError("CMR lookup should not be called when an override is configured")

    monkeypatch.setattr('bignbit.get_collection_concept_id.get_collection_concept_id', fail_lookup)

    cma = CMA(input={"datasetConfigurationForBIG": {"config": dataset_config}},
              config=CMA_CONFIG, path=str(tmp_path))

    assert cma.process()["collection_concept_id"] == expected_concept_id


@pytest.mark.parametrize("dataset_config", [{}, {"concept_id": None, "collectionId": ""}],
                         ids=["no_keys", "empty_values"])
def test_process_falls_back_to_cmr_lookup(dataset_config, monkeypatch, tmp_path):
    monkeypatch.setattr('bignbit.get_collection_concept_id.get_collection_concept_id',
                        lambda *args: "C333-PROV")

    cma = CMA(input={"datasetConfigurationForBIG": {"config": dataset_config}},
              config=CMA_CONFIG, path=str(tmp_path))

    assert cma.process()["collection_concept_id"] == "C333-PROV"
