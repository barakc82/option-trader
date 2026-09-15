import re

from rmax_model.features import FEATURES, feature_list

LEAKAGE_PATTERN = re.compile(r"stop|k_|barrier_actual")


def test_no_stop_leaking_feature_names_in_default_list():
    for name in FEATURES:
        assert not LEAKAGE_PATTERN.search(name), f"feature name '{name}' looks stop-dependent"


def test_no_stop_leaking_feature_names_for_arbitrary_reference_multiples():
    names = feature_list((1.5, 2.0, 3.0, 4.0, 5.0, 7.5))
    for name in names:
        assert not LEAKAGE_PATTERN.search(name), f"feature name '{name}' looks stop-dependent"


def test_feature_list_has_no_duplicates():
    assert len(FEATURES) == len(set(FEATURES))
