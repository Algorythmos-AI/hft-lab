"""The kind of model is a setting: ridge unless the file says gbm, each a trial of its own, and gbm small by rule."""
from __future__ import annotations

import ast

import pytest
import yaml

from hftlab.learn.config import MOST_DEPTH, MOST_TREES, Boosting, LearnConfigError, config_hash, load
from hftlab.models import trees
from tests.learn.kit import learn_settings
from tests.support import ROOT, sources

COSTS = {"placeholder": True}


def frozen(folder, **changes) -> dict:
    return load(learn_settings(folder, **changes)).frozen("EUR.USD", 10**9, COSTS)


def test_ridge_is_the_shipped_kind_and_the_shipped_boosting_numbers_are_small_placeholders():
    shipped = load(ROOT / "config/learn.yaml")
    assert shipped.model == "ridge" and shipped.placeholder
    boosting = shipped.boosting
    assert boosting.trees <= 100 and boosting.max_depth <= 4 and boosting.leaves <= 16, "small, until a record says"
    assert boosting.leaves <= 2**boosting.max_depth and boosting.leaf_scale == 1000
    text = (ROOT / "config/learn.yaml").read_text()
    block = text.split("boosting:")[1].split("validation:")[0]
    assert "PLACEHOLDER" in block and "no run searches them" in block and "make ml" in block


def test_each_kind_is_its_own_trial_and_a_number_a_run_does_not_read_makes_no_new_one(tmp_path):
    ridge, gbm = frozen(tmp_path / "a"), frozen(tmp_path / "b", model="gbm")
    assert ridge["kind"] == "ridge" and "penalty" in ridge and "boosting" not in ridge
    assert gbm["kind"] == "gbm" and "boosting" in gbm and "penalty" not in gbm
    assert config_hash(ridge) != config_hash(gbm)
    assert frozen(tmp_path / "c", boosting={"trees": 7}) == ridge, "ridge does not read the boosting numbers"
    assert frozen(tmp_path / "d", model="gbm", ridge_penalty="1/2") == gbm, "and gbm does not read the penalty"
    others = {"trees": 21, "max_depth": 4, "leaves": 7, "learning_rate_ppm": 100_000, "min_leaf_rows": 6, "bins": 31,
              "l2_ppm": 0, "leaf_scale": 100, "seed": 2}  # fmt: skip
    for key, value in others.items():
        other = frozen(tmp_path / key, model="gbm", boosting={key: value})
        assert config_hash(other) != config_hash(gbm), f"another {key} is another configuration, counted again"
    assert gbm["boosting"] == load(learn_settings(tmp_path / "e")).boosting.as_dict()


@pytest.mark.parametrize(
    "wrong",
    [
        {"model": "forest"}, {"model": None}, {"boosting": {"trees": MOST_TREES + 1}}, {"boosting": {"trees": 0}},
        {"boosting": {"max_depth": MOST_DEPTH + 1}}, {"boosting": {"leaves": 9}}, {"boosting": {"leaves": 1}},
        {"boosting": {"bins": 256}}, {"boosting": {"learning_rate_ppm": 0}}, {"boosting": {"learning_rate_ppm": 1.5}},
        {"boosting": {"leaf_scale": 0}}, {"boosting": {"min_leaf_rows": None}}, {"boosting": {"search": 3}},
    ],
)  # fmt: skip
def test_an_unknown_kind_a_model_too_large_or_a_number_that_is_not_whole_is_refused(tmp_path, wrong):
    with pytest.raises(LearnConfigError):
        load(learn_settings(tmp_path, **wrong))


def test_boosting_is_a_required_block_with_exactly_its_keys_and_nothing_to_search(tmp_path):
    path = learn_settings(tmp_path)
    loaded = yaml.safe_load(path.read_text())
    del loaded["boosting"]["seed"]
    path.write_text(yaml.safe_dump(loaded))
    with pytest.raises(LearnConfigError, match="boosting must have exactly the keys"):
        load(path)
    assert not [key for key in Boosting.__dataclass_fields__ if "search" in key or "grid" in key or "candidates" in key]
    assert MOST_TREES <= trees.MOST_TREES and 2**MOST_DEPTH <= trees.MOST_LEAVES, "a file holds what they allow"


def test_nothing_that_serves_weighs_or_watches_a_model_asks_what_kind_it_is():
    """No special case: the lineup, the decision and the drift look never read a model's kind."""
    for rel, tree in sources():
        if rel.startswith("src/hftlab/lineup/") or rel in ("src/hftlab/learn/promote.py", "src/hftlab/learn/drift.py"):
            words = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant)}
            assert not words & {"gbm", "ridge"}, rel
            assert not [node for node in ast.walk(tree) if isinstance(node, ast.Attribute) and node.attr == "kind"
                        and isinstance(node.value, ast.Name) and node.value.id == "model"], rel  # fmt: skip
