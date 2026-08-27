"""Engineering knowledge base (Phase 18).

Loaded by path rather than imported from the package, because the module is
deliberately stdlib-only and must keep working when the geometry kernel does
not. That is not a hypothetical: Smart App Control blocked an unsigned nlopt
DLL on this machine, taking CadQuery and the whole engine down, while the
accumulated engineering history stayed perfectly readable. These tests would
have run through that outage, and they must keep being able to.

The behaviours under test are the ones that stop a knowledge store becoming
a liability:
  - it never invents a correction from thin data
  - it is derived from the log, traceably, and ingest is idempotent
  - it says "marginal" when it genuinely cannot tell
"""

import importlib.util
import sqlite3
from pathlib import Path

import pytest

_MOD = (Path(__file__).parent.parent / "design_engine" / "inventor"
        / "knowledge.py")


def _load():
    spec = importlib.util.spec_from_file_location("kb_under_test", _MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


kbm = _load()


def test_module_is_stdlib_only():
    """No runtime dependency on the geometry kernel, by construction.

    Checked in a clean subprocess rather than against this process's
    `sys.modules`: once the full suite has run other files, cadquery is already
    imported, and an in-process check quietly becomes "has anyone imported
    cadquery" — passing in isolation, failing in the suite, and proving nothing
    in either case.
    """
    from test_memory import assert_loads_without_cad_kernel
    assert_loads_without_cad_kernel(_MOD)


class FakeLog:
    """Minimal stand-in with the ActionLog row shape."""

    def __init__(self, rows):
        self._rows = rows

    def rows(self, action=None):
        return [r for r in self._rows if action is None or r["action"] == action]


def _row(i, action="fea_static", result="pass", details=None, gid="P0001@v1",
         failure_mode=None):
    import json
    return {"id": i, "action": action, "result": result,
            "geometry_version": gid, "failure_mode": failure_mode,
            "reason": "test", "details_json": json.dumps(details or {})}


@pytest.fixture()
def kb(tmp_path):
    return kbm.KnowledgeBase(tmp_path / "k.sqlite")


# ------------------------------------------------------------------ ingest
def test_ingest_is_derived_and_traceable(kb):
    log = FakeLog([_row(11, details={
        "limit_state": "yield_von_mises", "safety_factor": 3.2,
        "material": {"name": "6061-T6511"}, "nodes": 50000,
        "solve_seconds": 12.0, "max_von_mises_MPa": 80.0})])
    out = kb.ingest_log(log)
    assert out["added"] == 1
    row = kb._conn.execute("SELECT * FROM observations").fetchone()
    # every record points back at the exact logged action that produced it
    assert row["source_action_id"] == 11
    assert row["limit_state"] == "yield_von_mises"
    assert row["material"] == "6061-T6511"


def test_ingest_is_idempotent(kb):
    log = FakeLog([_row(1, details={"safety_factor": 2.0}),
                   _row(2, details={"safety_factor": 3.0})])
    assert kb.ingest_log(log)["added"] == 2
    second = kb.ingest_log(log)
    assert second["added"] == 0 and second["already_known"] == 2
    assert kb.count("observations") == 2


def test_pending_rows_are_not_ingested(kb):
    """A pending action has no result yet; recording it as knowledge would be
    recording a question as an answer."""
    log = FakeLog([_row(1, result="pending", details={})])
    assert kb.ingest_log(log)["added"] == 0


# ------------------------------------------------------------- calibration
class _Stage:
    def __init__(self, fidelity, metrics):
        self.fidelity = fidelity
        self.metrics = metrics
        self.failures = []


class _Res:
    def __init__(self, stages, failures=()):
        self.stages = stages
        self.failures = list(failures)


class _Cand:
    def __init__(self, cid, stages, values=None, failures=()):
        self.candidate_id = cid
        self.geometry_id = f"P{cid}@v1"
        self.space_digest = "space1"
        self.values = values or {"t": 10.0}
        self.result = _Res(stages, failures)


def test_calibration_pair_is_harvested_from_a_promotion(kb):
    """The automated form of reading two FEA results and editing a constant."""
    c = _Cand("c1", [_Stage(0, {"sf.yield": 5.226}),
                     _Stage(3, {"sf.yield": 2.968})])
    assert kb.observe_candidate(c, problem="jetpack") == 1
    row = kb._conn.execute("SELECT * FROM calibrations").fetchone()
    assert row["predicted"] == pytest.approx(5.226)
    assert row["measured"] == pytest.approx(2.968)
    assert row["ratio"] == pytest.approx(5.226 / 2.968)
    assert row["low_fidelity"] == 0 and row["high_fidelity"] == 3


def test_correction_refuses_thin_data(kb):
    """Two samples is not a correction factor, it is a coincidence."""
    for i, (lo, hi) in enumerate([(5.2, 3.0), (20.5, 10.5)]):
        kb.observe_candidate(
            _Cand(f"c{i}", [_Stage(0, {"sf.yield": lo}), _Stage(3, {"sf.yield": hi})]),
            problem="jetpack")
    assert kb.correction("sf.yield", "jetpack") is None      # NOT 1.0


def test_correction_returns_factor_and_its_evidence(kb):
    for i, (lo, hi) in enumerate([(5.2, 3.0), (6.0, 3.5), (4.0, 2.3)]):
        kb.observe_candidate(
            _Cand(f"c{i}", [_Stage(0, {"sf.yield": lo}), _Stage(3, {"sf.yield": hi})]),
            problem="jetpack")
    est = kb.correction("sf.yield", "jetpack")
    assert est is not None and est.n == 3
    # geometric mean of the three ratios, all near 1.73
    assert est.factor == pytest.approx(1.73, abs=0.05)
    assert est.trustworthy is True
    assert len(est.to_dict()["evidence"]) == 3   # the answer carries its data


def test_wildly_disagreeing_ratios_are_flagged_untrustworthy(kb):
    for i, (lo, hi) in enumerate([(5.0, 5.0), (20.0, 4.0), (6.0, 5.5)]):
        kb.observe_candidate(
            _Cand(f"c{i}", [_Stage(0, {"sf.yield": lo}), _Stage(3, {"sf.yield": hi})]),
            problem="p")
    est = kb.correction("sf.yield", "p")
    assert est is not None and est.n == 3
    assert est.spread > 1.5
    assert est.trustworthy is False    # has a number, says don't trust it


def test_single_fidelity_metric_yields_no_calibration(kb):
    """Nothing to compare against is not a data point."""
    c = _Cand("c1", [_Stage(0, {"mass_kg": 3.9}), _Stage(0, {"mass_kg": 3.9})])
    assert kb.observe_candidate(c, problem="p") == 0


# ------------------------------------------------------------ failure memory
class _F:
    def __init__(self, cls, trustworthy=True, metric="", message="m"):
        self.failure_class = type("C", (), {"value": cls})()
        self.trustworthy = trustworthy
        self.metric = metric
        self.message = message


def test_untrustworthy_failures_are_stored_but_excluded_from_regions(kb):
    kb.observe_candidate(_Cand("c1", [], failures=[_F("numerical", False)]), "p")
    kb.observe_candidate(_Cand("c2", [], failures=[_F("yield", True)]), "p")
    assert len(kb.failure_regions(trustworthy_only=True)) == 1
    assert len(kb.failure_regions(trustworthy_only=False)) == 2


def test_warn_matches_nearby_prior_failures_and_shows_them(kb):
    class V:
        def __init__(self, name, lo, hi):
            self.name = name
            self._b = (lo, hi)
        def bounds(self, _):
            return self._b

    class Space:
        searchable = [V("t", 0.0, 100.0)]

    kb.observe_candidate(_Cand("bad", [], values={"t": 10.0},
                               failures=[_F("yield")]), "p")
    near = kb.warn({"t": 12.0}, Space(), problem="p", radius=0.12)
    far = kb.warn({"t": 90.0}, Space(), problem="p", radius=0.12)
    assert len(near) == 1 and far == []
    # the warning shows the prior design, not just a score
    assert near[0]["prior_values"] == {"t": 10.0}
    assert near[0]["failure_class"] == "yield"
    assert near[0]["distance"] == pytest.approx(0.02, abs=1e-6)


# --------------------------------------------------------------- solver cost
def _seed_cost(kb, pairs):
    import json
    rows = [_row(i, details={"nodes": n, "solve_seconds": s})
            for i, (n, s) in enumerate(pairs, start=1)]
    kb.ingest_log(FakeLog(rows))


def test_solver_cost_model_needs_data(kb):
    _seed_cost(kb, [(1000, 1.0), (2000, 2.0)])
    assert kb.solver_cost_model() is None        # refuses on 2 points
    assert kb.predict_solve(50000) is None


def test_solver_cost_model_recovers_a_known_power_law(kb):
    # exact t = 1e-5 * n^1.5, so the fit must recover exponent 1.5
    pairs = [(n, 1e-5 * n ** 1.5) for n in (10_000, 50_000, 100_000, 400_000, 800_000)]
    _seed_cost(kb, pairs)
    m = kb.solver_cost_model()
    assert m["exponent"] == pytest.approx(1.5, abs=0.02)
    assert m["n"] == 5
    assert m["band_multiplier"] == pytest.approx(1.0, abs=0.02)  # noiseless


def test_affordability_is_three_way_not_binary(kb):
    pairs = [(n, 1e-5 * n ** 1.5) for n in (10_000, 50_000, 100_000, 400_000, 800_000)]
    _seed_cost(kb, pairs)
    # 400k nodes -> 1e-5 * 400000^1.5 = 2530s
    assert kb.affordable(400_000, 6000)["verdict"] == "yes"
    assert kb.affordable(400_000, 100)["verdict"] == "no"
    # a budget sitting inside the band is honestly "marginal", never a guess
    verdicts = {kb.affordable(400_000, b)["verdict"] for b in (2400, 2600)}
    assert verdicts <= {"yes", "no", "marginal"}


def test_report_summarises_without_inventing(kb):
    log = FakeLog([_row(1, details={"limit_state": "yield_von_mises",
                                    "safety_factor": 4.0,
                                    "material": {"name": "6061-T6511"}})])
    kb.ingest_log(log)
    r = kb.report()
    assert r["observations"] == 1
    assert r["calibrations"] == 0
    assert r["solver_cost"] is None          # not enough data -> None, not a guess
