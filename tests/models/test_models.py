"""Models: an exact ridge regression, a file named by its hash that is never overwritten, and a scorer."""
from __future__ import annotations

import dataclasses
import json
from fractions import Fraction

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hftlab.core import Side
from hftlab.features import FeatureRow
from hftlab.learn import validate
from hftlab.learn.train import flat_model, random_model, ridge_model
from hftlab.models import (
    BUY, SELL, Decision, FlatScorer, Model, ModelError, ModelMismatch, ModelRegistry, RandomScorer, Ridge, RidgeError,
    RidgeScorer, ScoreError, answer, ask, fit, heard, number, parse, scorer_for,
)  # fmt: skip
from tests.learn.kit import IMBALANCE, WIN, dataset, planted
from tests.venues.kit import T0, plain_costs

F = Fraction
SMALL = F(1, 1000)


# ---- the fit ---------------------------------------------------------------------------------------------------------


def test_a_line_is_recovered_exactly_and_a_penalty_of_the_row_count_halves_its_slope():
    rows, y = [[x] for x in range(5)], [3 + 2 * x for x in range(5)]
    plain = fit(rows, {"y": y}, F(0))
    assert (plain.intercepts["y"], plain.weights["y"]) == (F(3), (F(2),)) and plain.rows == 5
    assert (plain.means, plain.variances) == ((F(2),), (F(2),)), "the mean and the variance of 0, 1, 2, 3, 4"
    halved = fit(rows, {"y": y}, F(5))
    assert halved.weights["y"] == (F(1),) and halved.intercepts["y"] == F(5), "it still passes through the means: (2, 7)"
    assert [plain.predict("y", [x]) for x in (0, 10, -3)] == [3, 23, -3] and type(plain.predict("y", [1])) is Fraction


TABLES = st.integers(1, 3).flatmap(
    lambda width: st.lists(
        st.tuples(st.lists(st.integers(-50, 50), min_size=width, max_size=width), st.integers(-10**6, 10**6)),
        min_size=2, max_size=9,
    )
)  # fmt: skip
PENALTIES = st.fractions(min_value=F(1, 1000), max_value=10)


@given(TABLES, PENALTIES)
def test_the_fit_is_the_exact_minimum_of_the_penalised_squared_error(table, penalty):
    rows, y = [row for row, _ in table], [target for _, target in table]
    found = fit(rows, {"y": y}, penalty)
    errors = [found.predict("y", row) - target for row, target in zip(rows, y, strict=True)]
    assert sum(errors) == 0, "the errors sum to nothing: the constant is free"
    for j, (mean, variance, weight) in enumerate(zip(found.means, found.variances, found.weights["y"], strict=True)):
        slope = sum(error * (row[j] - mean) for error, row in zip(errors, rows, strict=True))
        assert slope + penalty * variance * weight == 0, "no change of this weight lowers the penalised error"
        assert mean == F(sum(row[j] for row in rows), len(rows)) and (weight == 0 if variance == 0 else True)


@given(TABLES, PENALTIES, st.integers(1, 9), st.integers(-100, 100), st.randoms(use_true_random=False))
def test_the_features_are_standardised_so_their_unit_and_their_origin_change_no_prediction(table, penalty, k, c, rng):
    rows, y = [row for row, _ in table], [target for _, target in table]
    found = fit(rows, {"y": y}, penalty)
    other = [[k * row[0] + c, *row[1:]] for row in rows]
    rescaled = fit(other, {"y": y}, penalty)
    assert [rescaled.predict("y", row) for row in other] == [found.predict("y", row) for row in rows]
    order = list(range(len(rows)))
    rng.shuffle(order)
    assert fit([rows[i] for i in order], {"y": [y[i] for i in order]}, penalty) == found, "the order of the rows is nothing"


def test_two_targets_are_fitted_to_the_same_rows_each_as_if_alone():
    rows = [[1, 5], [2, 3], [4, 4], [7, 1], [8, 9]]
    both = fit(rows, {"a": [1, 2, 3, 4, 5], "b": [9, 7, 5, 3, 0]}, SMALL)
    alone = fit(rows, {"b": [9, 7, 5, 3, 0]}, SMALL)
    assert both.weights["b"] == alone.weights["b"] and both.intercepts["b"] == alone.intercepts["b"]
    assert Ridge.from_params(both.params()) == both and json.loads(json.dumps(both.params())) == both.params()
    assert all(isinstance(text, str) for text in both.params()["means"]), "a fraction is kept as text, never a float"


def test_a_feature_that_never_varies_takes_no_part_and_features_that_repeat_need_a_penalty():
    rows = [[7, 1, 2], [7, 2, 4], [7, 4, 8], [7, 5, 10]]
    y = [10, 20, 40, 50]
    found = fit(rows, {"y": y}, SMALL)
    assert found.weights["y"][0] == 0 and found.variances[0] == 0
    assert found.weights["y"][1] == 2 * found.weights["y"][2], "the two share the weight, each by its own scale"
    assert all(abs(found.predict("y", row) - target) < F(1, 100) for row, target in zip(rows, y, strict=True))
    with pytest.raises(RidgeError, match="no single solution"):
        fit(rows, {"y": y}, F(0))
    assert fit([[3], [3]], {"y": [1, 5]}, F(0)).predict("y", [99]) == 3, "nothing varies: the mean of the target"


@pytest.mark.parametrize(
    ("rows", "targets", "penalty"),
    [([[1]], {"y": [1]}, SMALL), ([], {"y": []}, SMALL), ([[1], [2]], {}, SMALL), ([[1], [2]], {"y": [1]}, SMALL),
     ([[1], [2]], {"y": [1, 2]}, F(-1)), ([[1], [2, 3]], {"y": [1, 2]}, SMALL), ([[1.5], [2]], {"y": [1, 2]}, SMALL),
     ([[1], [2]], {"y": [1, 2.5]}, SMALL), ([[True], [2]], {"y": [1, 2]}, SMALL), ([[], []], {"y": [1, 2]}, SMALL)],
)  # fmt: skip
def test_rows_that_are_too_few_ragged_or_not_whole_numbers_are_refused(rows, targets, penalty):
    with pytest.raises(RidgeError):
        fit(rows, targets, penalty)
    with pytest.raises(RidgeError):
        fit([[1], [2]], {"y": [1, 2]}, SMALL).predict("y", [1, 2])
    with pytest.raises(RidgeError, match="not the parameters"):
        Ridge.from_params({"penalty": "1/0", "rows": 2, "means": [], "variances": [], "targets": {}})


# ---- the file and the registry ---------------------------------------------------------------------------------------


def a_model(seed: int = 3, **changes) -> Model:
    made = dataset(planted(seed, 6))
    return dataclasses.replace(ridge_model(made, range(len(made.rows)), SMALL), **changes)


def test_a_model_file_says_what_it_reads_what_it_predicts_and_what_it_was_fitted_to():
    made = dataset(planted(3, 6))
    model = ridge_model(made, range(len(made.rows)), SMALL)
    assert (model.kind, model.pair, model.size, model.features) == ("ridge", "EUR.USD", 20_000, made.features)
    assert model.label == made.label and model.cost_model == made.cost_model and model.placeholder is True
    assert model.dataset_hash == made.content_hash and set(model.params["targets"]) == {BUY, SELL}
    data = model.data()
    assert data.endswith(b"\n") and data.count(b"\n") == 1 and b'"hash":"' + model.content_hash.encode() in data
    assert parse(data) == model and parse(data).content_hash == model.content_hash
    assert b"." not in data.replace(b"EUR.USD", b""), "no decimal point anywhere: integers and text"
    measured = ridge_model(dataset(planted(3, 6)), range(10), SMALL)
    assert dataclasses.replace(measured, cost_model={**measured.cost_model, "placeholder": False}).placeholder is False


def test_the_hash_is_of_everything_in_the_file():
    model = a_model()
    changed = [
        a_model(pair="USD.JPY"), a_model(size=21_000), a_model(dataset_hash="0" * 64), a_model(kind="flat"),
        a_model(features=model.features[::-1]), a_model(label={**model.label, "horizon_ns": 5}),
        a_model(cost_model={**model.cost_model, "slippage_increments": 1}),
        a_model(params={**model.params, "rows": 1}), a_model(seed=4),
    ]  # fmt: skip
    assert len({model.content_hash, *(other.content_hash for other in changed)}) == 10


@pytest.mark.parametrize(
    "change",
    [{"params": {"weight": 0.5}}, {"kind": "forest"}, {"dataset_hash": "abc"}, {"features": ()},
     {"features": ("spread@v1", "spread@v1")}, {"pair": "EURUSD"}, {"size": 0}, {"cost_model": {"rate": 1.5}}],
)  # fmt: skip
def test_a_model_with_a_float_an_unknown_kind_or_no_dataset_is_refused(change):
    with pytest.raises((ModelError, ValueError)):
        a_model(**change)


def test_a_model_is_addressed_by_its_hash_and_is_never_overwritten(tmp_path):
    registry = ModelRegistry(tmp_path / "models")
    assert registry.hashes() == []
    model = a_model()
    digest = registry.put(model)
    path = tmp_path / "models" / f"{digest}.json"
    assert digest == model.content_hash and path.read_bytes() == model.data() and registry.hashes() == [digest]
    written = path.stat().st_mtime_ns, path.stat().st_ino
    assert registry.put(model) == digest and (path.stat().st_mtime_ns, path.stat().st_ino) == written, "put twice: nothing"
    assert sorted(p.name for p in path.parent.iterdir()) == [path.name], "and no half-written file is left beside it"
    other = a_model(seed=4)
    assert registry.put(other) != digest and registry.hashes() == sorted([digest, other.content_hash])
    path.write_bytes(other.data())  # someone put another model under this model's name
    with pytest.raises(ModelError, match="not written over"):
        registry.put(model)
    assert path.read_bytes() == other.data(), "what was there is still there"


def test_load_refuses_a_file_that_is_not_what_its_name_says_or_not_what_the_caller_expects(tmp_path):
    registry = ModelRegistry(tmp_path)
    model = a_model()
    digest = registry.put(model)
    expected = {"pair": "EUR.USD", "features": model.features, "label": model.label}
    assert registry.load(digest, **expected) == model
    for wrong in (
        {"features": model.features[:-1]}, {"features": model.features[::-1]}, {"pair": "USD.JPY"},
        {"label": {**model.label, "horizon_ns": 2}}, {"features": (*model.features[:-1], "to_week_close_s@v2")},
    ):  # fmt: skip
        with pytest.raises(ModelMismatch, match="another pair, other features or another label"):
            registry.load(digest, **{**expected, **wrong})
    path = tmp_path / f"{digest}.json"
    other = a_model(seed=4)
    path.write_bytes(other.data())
    with pytest.raises(ModelMismatch, match="not the one it is named for"):
        registry.load(digest, **expected)
    path.write_bytes(model.data().replace(b'"size":20000', b'"size":20001'))
    with pytest.raises(ModelError, match="does not match its own hash"):
        registry.load(digest, **expected)
    for damaged in (b"not json", b"[]", model.data()[:-1], model.data() + b"\n", b'{"hash": 1}',
                    json.dumps(json.loads(model.data())).encode()):  # fmt: skip
        path.write_bytes(damaged)
        with pytest.raises(ModelError):
            registry.load(digest, **expected)
    with pytest.raises(ModelError, match="there is no model"):
        registry.load("f" * 64, **expected)
    for name in ("../../etc/passwd", "abc", digest.upper(), ""):
        with pytest.raises(ModelError, match="addressed by its SHA-256"):
            registry.load(name, **expected)


# ---- the scorer ------------------------------------------------------------------------------------------------------


def shown(row) -> FeatureRow:
    return FeatureRow("EUR.USD", row.n, row.known_ns, row.features)


def test_the_ridge_scorer_takes_the_better_side_when_it_expects_more_than_nothing():
    made = dataset(planted(3, 10))
    model = ridge_model(made, range(len(made.rows)), SMALL)
    scorer = scorer_for(model)
    exact = Ridge.from_params(model.params)
    assert type(scorer) is RidgeScorer and scorer.model is model
    for row in made.rows:
        buy, sell = exact.predict(BUY, row.features), exact.predict(SELL, row.features)
        found = scorer.score(shown(row))
        if max(buy, sell) <= 0:
            assert found is None and not row.features[IMBALANCE]
        else:
            side = Side.BUY if buy > sell else Side.SELL
            assert found == Decision(side, max(buy, sell).numerator // max(buy, sell).denominator, model.content_hash)
            assert (found.side is Side.BUY) == (row.features[IMBALANCE] > 0) and abs(found.expected_micro - WIN) < 10_000
    assert {type(scorer.score(shown(row)).expected_micro) for row in made.rows if row.features[IMBALANCE]} == {int}


def test_the_scorer_abstains_when_it_cannot_tell_the_sides_apart():
    made = dataset(planted(3, 6))
    even = Ridge(F(0), 2, (F(0),) * 10, (F(1),) * 10, {BUY: F(5), SELL: F(5)}, {BUY: (F(0),) * 10, SELL: (F(0),) * 10})
    model = dataclasses.replace(ridge_model(made, range(20), SMALL), params=even.params())
    assert RidgeScorer(model).score(shown(made.rows[0])) is None
    lopsided = dataclasses.replace(even, intercepts={BUY: F(5), SELL: F(11, 2)})
    model = dataclasses.replace(model, params=lopsided.params())
    assert RidgeScorer(model).score(shown(made.rows[0])) == Decision(Side.SELL, 5, model.content_hash), "rounded down"


def test_the_two_baselines_are_models_like_any_other(tmp_path):
    made = dataset(planted(3, 6))
    flat, coin = flat_model(made), random_model(made, 7)
    registry = ModelRegistry(tmp_path)
    expected = {"pair": "EUR.USD", "features": made.features, "label": made.label}
    flat, coin = (registry.load(registry.put(model), **expected) for model in (flat, coin))
    assert type(scorer_for(flat)) is FlatScorer and type(scorer_for(coin)) is RandomScorer
    assert all(scorer_for(flat).score(shown(row)) is None for row in made.rows)
    tosses = [scorer_for(coin).score(shown(row)) for row in made.rows]
    assert tosses == [scorer_for(coin).score(shown(row)) for row in made.rows] and {t.expected_micro for t in tosses} == {0}
    assert {t.side for t in tosses} == {Side.BUY, Side.SELL}
    assert [t.side for t in tosses] != [scorer_for(random_model(made, 8)).score(shown(row)).side for row in made.rows]
    assert tosses[0].side is (Side.BUY if number(7, made.rows[0].n, made.rows[0].known_ns) & 1 else Side.SELL)
    with pytest.raises(ModelError, match="handed to the scorer of ridge"):
        RidgeScorer(flat)
    with pytest.raises(ModelError, match="its seed"):
        RandomScorer(dataclasses.replace(coin, params={}))
    with pytest.raises(ModelError, match="the outcome of each side"):
        RidgeScorer(dataclasses.replace(a_model(), features=a_model().features[:-1]))


def test_a_row_of_another_pair_or_another_width_is_not_scored():
    made = dataset(planted(3, 6))
    for model in (ridge_model(made, range(20), SMALL), flat_model(made), random_model(made, 1)):
        scorer = scorer_for(model)
        with pytest.raises(ScoreError):
            scorer.score(FeatureRow("USD.JPY", 1, T0, made.rows[0].features))
        with pytest.raises(ScoreError):
            scorer.score(FeatureRow("EUR.USD", 1, T0, made.rows[0].features[:-1]))


def test_the_same_question_and_answer_cross_a_process_boundary_as_lines_of_whole_numbers():
    made = dataset(planted(3, 10))
    for model in (ridge_model(made, range(len(made.rows)), SMALL), flat_model(made), random_model(made, 1)):
        scorer = scorer_for(model)
        for row in made.rows:
            request = ask(shown(row))
            reply = answer(scorer, request)
            assert heard(reply) == scorer.score(shown(row))
            assert "." not in request.replace("EUR.USD", "") and "." not in reply and "\n" not in request + reply
            assert json.loads(reply)["model"] == model.content_hash
    scorer = scorer_for(flat_model(made))
    for line in ("", "{}", '{"pair":"EUR.USD","n":1,"known_ns":1,"values":[1.5]}', '{"pair":"EUR.USD","n":"1","known_ns":1,"values":[]}'):
        with pytest.raises(ScoreError):
            answer(scorer, line)
    for line in ("", "{}", '{"model":"x","side":"UP","expected_micro":1}', '{"model":"x","side":"BUY","expected_micro":1.5}'):
        with pytest.raises(ScoreError):
            heard(line)


# ---- fitted on the training rows, applied unchanged ------------------------------------------------------------------


def test_the_standardisation_is_of_the_training_rows_alone_is_kept_with_the_model_and_is_applied_unchanged():
    made = dataset(planted(5, 12))
    cut = len(made.rows) // 2
    train, test = range(cut), range(cut, len(made.rows))
    model = ridge_model(made, train, SMALL)
    kept = Ridge.from_params(model.params)
    for j in range(len(made.features)):
        column = [made.rows[i].features[j] for i in train]
        mean = F(sum(column), len(column))
        assert kept.means[j] == mean and kept.variances[j] == F(sum((x - mean) ** 2 for x in column), len(column))
    assert kept.rows == cut and kept.means != Ridge.from_params(ridge_model(made, range(len(made.rows)), SMALL).params).means
    scorer = scorer_for(parse(model.data()))
    one_by_one = [scorer.score(shown(made.rows[i])) for i in test]
    assert [scorer.score(shown(made.rows[i])) for i in reversed(test)] == one_by_one[::-1], "a score depends on its row alone"
    assert [scorer_for(model).score(shown(made.rows[i])) for i in test] == one_by_one


def test_a_model_fitted_to_the_planted_effect_finds_it_on_rows_it_never_saw():
    made = dataset(planted(5, 30))
    cut = len(made.rows) * 2 // 3
    model = ridge_model(made, range(cut), SMALL)
    scorer = scorer_for(model)
    unseen = made.rows[cut + 20 :]  # well after the last training label ended

    def decide(row: FeatureRow) -> Side | None:
        found = scorer.score(row)
        return found.side if found else None

    report = validate.three_way(unseen, made.pair, decide, seed=1)
    signals = sum(1 for row in unseen if row.features[IMBALANCE])
    assert report.model.trades == signals > 50 and report.model.total_micro == signals * WIN
    assert report.random.total_micro < 0 < report.model.total_micro == report.oracle.total_micro
    flat = validate.three_way(unseen, made.pair, lambda row: None, seed=1)
    assert flat.model.trades == 0 and flat.oracle.total_micro == report.oracle.total_micro


@given(st.lists(st.integers(-10**9, 10**9), min_size=10, max_size=10), st.integers(0, 10**6))
def test_scoring_is_exact_whatever_the_row(values, n):
    model = a_model()
    exact = Ridge.from_params(model.params)
    buy, sell = exact.predict(BUY, values), exact.predict(SELL, values)
    found = scorer_for(model).score(FeatureRow("EUR.USD", n, T0, tuple(values)))
    if buy == sell or max(buy, sell) <= 0:
        assert found is None
    else:
        best = max(buy, sell)
        assert found == Decision(Side.BUY if buy > sell else Side.SELL, best.numerator // best.denominator, model.content_hash)


def test_the_costs_a_model_was_fitted_under_stay_with_it():
    measured = dataset(planted(3, 6))
    assert ridge_model(measured, range(20), SMALL).cost_model == plain_costs().as_dict()
