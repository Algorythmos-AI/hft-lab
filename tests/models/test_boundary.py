"""The optional ML set stays optional: nothing but training a gbm model imports it, and scoring one never does.

Every test here runs in a process of its own in which the set cannot be imported at all, whether or not it is
installed. So the same thing is shown on a developer's machine with `make ml` and in the base `test` job
without it: the recorder, the trader and the scorer of a gradient-boosted model need none of it (ADR 0025).
"""
from __future__ import annotations

import json
import subprocess
import sys

from hftlab.models import BUY, SELL, ModelRegistry, Tree, Trees
from tests.learn.kit import SATURDAY, learn_settings, record, weeks
from tests.models.test_trees import a_model
from tests.support import ROOT

ML_SET = ("lightgbm", "scipy", "narwhals")  # what requirements-ml.in adds to an environment
ARRAYS = "numpy"  # in the base lock already, for the broker's client library; the adapter alone may import it
# Run before anything else in the child: an import of the set fails as it would on a host that never installed it.
BLOCKED = f"""
import sys
class Absent:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {(*ML_SET, ARRAYS)!r}:
            raise ImportError(f"{{name}} is not installed here")
sys.meta_path.insert(0, Absent())
"""
LOADED = f"sorted({{name.split('.')[0] for name in sys.modules}} & set({(*ML_SET, ARRAYS)!r}))"


def child(script: str, *, blocked: bool = True) -> subprocess.CompletedProcess[str]:
    code = (BLOCKED if blocked else "import sys\n") + script
    environment = {"PYTHONPATH": f"{ROOT / 'src'}:{ROOT}", "PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=environment, check=False)


def test_the_trader_the_recorder_and_everything_but_the_broker_adapter_import_without_the_set():
    script = """
import importlib, pkgutil, hftlab
for name in ("hftlab.trader", "hftlab.trader.__main__", "hftlab.record", "hftlab.record.__main__"):
    importlib.import_module(name)
names = [m.name for m in pkgutil.walk_packages(hftlab.__path__, "hftlab.")]
skipped = [n for n in names if n.startswith("hftlab.venues.ibkr") or n.endswith("__main__")]
for name in names:
    if name not in skipped:
        importlib.import_module(name)
assert "hftlab.learn.boost" in sys.modules and "hftlab.learn.train" in sys.modules and "hftlab.models.trees" in sys.modules
print(len(names) - len(skipped), LOADED)
""".replace("LOADED", LOADED)
    done = child(script)
    assert done.returncode == 0, done.stderr
    count, loaded = done.stdout.split(maxsplit=1)
    assert int(count) > 100 and loaded.strip() == "[]", "over a hundred modules, and none of them reached the set"


def test_with_the_set_installed_or_not_importing_the_platform_loads_none_of_it():
    """Nothing is blocked here: if the set is installed it could be imported, and it is not."""
    script = """
import importlib
for name in ("hftlab.trader", "hftlab.trader.__main__", "hftlab.record", "hftlab.record.__main__", "hftlab.lineup",
             "hftlab.models", "hftlab.learn.train", "hftlab.learn.boost", "hftlab.learn.promote", "hftlab.strategy"):
    importlib.import_module(name)
print(LOADED)
""".replace("LOADED", LOADED)
    done = child(script, blocked=False)
    assert done.returncode == 0 and done.stdout.strip() == "[]", done.stdout + done.stderr


def test_a_gbm_model_file_is_loaded_and_scored_where_the_library_cannot_be_imported(tmp_path):
    split = Tree(feature=(4,), threshold=(0,), left=(~0,), right=(~1,), leaves=(-6_000_000_000, 14_000_000_000))
    other = Tree(feature=(4,), threshold=(-1,), left=(~0,), right=(~1,), leaves=(14_000_000_000, -6_000_000_000))
    model = a_model(Trees(1000, 10, 10, {BUY: (split,), SELL: (other,)}, {"trees": 1}, "by hand"))
    digest = ModelRegistry(tmp_path).put(model)
    script = f"""
import json
from pathlib import Path
from hftlab.features import FeatureRow
from hftlab.models import ModelRegistry, scorer_for, answer, ask
model = ModelRegistry(Path({str(tmp_path)!r})).load({digest!r}, pair="EUR.USD", features={model.features!r},
                                                   label={dict(model.label)!r})
scorer = scorer_for(model)
out = []
for imbalance in (-500_000, 0, 500_000):
    row = FeatureRow("EUR.USD", 1, 2, (10, 0, 0, 0, imbalance, 0, 0, 0, 0, 0))
    found = scorer.score(row)
    assert json.loads(answer(scorer, ask(row)))["side"] == (found.side.value if found else None)
    out.append([found.side.value, found.expected_micro] if found else None)
print(json.dumps([type(scorer).__name__, out, {LOADED}]))
"""
    done = child(script)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == ["TreeScorer", [["SELL", 14_000_000], None, ["BUY", 14_000_000]], []]


def test_the_scorers_own_process_serves_a_gbm_model_where_the_library_cannot_be_imported(tmp_path):
    """The child of ADR 0023 is what scores for the trader once a place says `scorer: process`: it needs none of it."""
    split = Tree(feature=(4,), threshold=(0,), left=(~0,), right=(~1,), leaves=(-6_000_000_000, 14_000_000_000))
    other = Tree(feature=(4,), threshold=(-1,), left=(~0,), right=(~1,), leaves=(14_000_000_000, -6_000_000_000))
    model = a_model(Trees(1000, 10, 10, {BUY: (split,), SELL: (other,)}, {"trees": 1}, "by hand"))
    digest = ModelRegistry(tmp_path).put(model)
    script = f"""
import io, json
from pathlib import Path
from hftlab.features import FeatureRow
from hftlab.models import ask
from hftlab.scoring import child, wire
asked = wire.frame(wire.load({digest!r}))
for imbalance in (-500_000, 0, 500_000):
    asked += wire.frame(ask(FeatureRow("EUR.USD", 1, 2, (10, 0, 0, 0, imbalance, 0, 0, 0, 0, 0))))
source, sink = io.BytesIO(asked), io.BytesIO()
code = child.serve(Path({str(tmp_path)!r}), source, sink)
sink.seek(0)
replies = [json.loads(wire.read_frame(sink.read)) for _ in range(4)]
print(json.dumps([code, replies[0], [[r["side"], r["expected_micro"]] for r in replies[1:]], {LOADED}]))
"""
    done = child(script)
    assert done.returncode == 0, done.stderr
    decided = [["SELL", 14_000_000], [None, None], ["BUY", 14_000_000]]
    assert json.loads(done.stdout) == [0, {"loaded": digest}, decided, []]


def run_training(folder, model: str) -> subprocess.CompletedProcess[str]:
    config = learn_settings(folder, model=model)
    (folder / "registry.yaml").write_text("history: []\n")
    script = f"""
from hftlab.core import SimClock
from hftlab.learn import train
argv = ["--config", {str(config)!r}, "--cost-model", {str(ROOT / "config/cost_model.yaml")!r},
        "--calendar", {str(ROOT / "config/calendar")!r}, "--feasibility", {str(ROOT / "config/feasibility.yaml")!r},
        "--store", {str(folder / "rec")!r}, "--trial-registry", {str(folder / "registry.yaml")!r}]
code = train.main(argv, SimClock({SATURDAY}))
print({LOADED})
sys.exit(code)
"""
    return child(script)


def test_without_the_set_a_ridge_run_trains_as_before_and_a_gbm_run_is_refused_before_anything_is_counted(tmp_path):
    record(tmp_path / "rec", weeks(1))
    ridge = run_training(tmp_path, "ridge")
    assert ridge.returncode == 0 and ridge.stdout.strip().endswith("[]"), ridge.stderr
    assert len(list((tmp_path / "learn" / "models").glob("*.json"))) == 1
    other = tmp_path / "other"
    other.mkdir()
    (other / "rec").symlink_to(tmp_path / "rec")
    gbm = run_training(other, "gbm")
    assert gbm.returncode == 2 and "refused" in gbm.stderr and "make ml" in gbm.stderr and "optional ML set" in gbm.stderr
    assert not (other / "learn").exists(), "no trial in the ledger, no model, no report, no run file"
    assert not (other / "trader").exists(), "and no candidate in the lineup"
