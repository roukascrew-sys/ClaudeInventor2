"""Engineering knowledge base — reusable memory across designs (Phase 18).

Every FEA run this project has ever done already sits in the FRACAS log. What
it has never had is a way to ASK that history anything: has a design like this
been solved before, does the cheap model systematically lie in this regime,
which corner of the design space keeps producing buckling failures.

Three hard rules, and they are what separate this from a black box.

1. DERIVED, NEVER AUTHORED. The log is the source of truth (CLAUDE.md). This
   store ingests from `ActionLog` and every row keeps `source_action_id`, so
   any answer can be traced back to the exact logged action that produced it.
   Nothing is written here that was not observed there. Ingest is idempotent.

2. IT REFUSES WHEN THE DATA IS THIN. `correction()` returns None rather than
   1.0 when it has fewer than `min_observations` pairs. A correction factor
   invented from two samples is worse than no correction, because it looks
   like knowledge. Same principle as the derating curves refusing to
   extrapolate.

3. EVERY ANSWER CARRIES ITS EVIDENCE. `correction()` returns the ratios and
   the sample count; `warn()` returns the actual prior designs it matched and
   how far away they were. A human can check the reasoning, which is the
   explicit requirement in the brief: do not turn this into an opaque model.

The calibration table is the part that earns its keep immediately. During the
jetpack run I read two FEA results by hand and edited a Kt constant in a
design script. That is exactly the loop this automates — and unlike the hand
version, it records the spread, so it can say "I have three points and they
disagree by 40%, do not trust me yet".
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Sequence

if TYPE_CHECKING:                      # pragma: no cover
    from .candidate import Candidate

# NOTE: no runtime import of .candidate, .requirements or anything that
# reaches the geometry kernel. The knowledge layer is stdlib-only on purpose.
# It answers questions about history, which is a database problem, not a CAD
# problem - and keeping it independent means it still works when the kernel
# does not. That is not hypothetical: Smart App Control blocked an unsigned
# nlopt DLL on this machine, taking CadQuery (and therefore the whole engine)
# down, while the accumulated engineering history remained perfectly readable.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_action_id INTEGER UNIQUE,
    ingested_at REAL NOT NULL,
    project TEXT,
    geometry_id TEXT,
    action TEXT NOT NULL,
    limit_state TEXT,
    material TEXT,
    result TEXT NOT NULL,
    safety_factor REAL,
    required_sf REAL,
    max_von_mises_MPa REAL,
    allowable_MPa REAL,
    nodes INTEGER,
    elements INTEGER,
    mesh_mm REAL,
    solve_seconds REAL,
    peak_rss_mb REAL,
    outlier_ratio REAL,
    service_temp_C REAL,
    failure_mode TEXT,
    reason TEXT
);
CREATE TABLE IF NOT EXISTS calibrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at REAL NOT NULL,
    problem TEXT NOT NULL,
    metric TEXT NOT NULL,
    low_fidelity INTEGER NOT NULL,
    high_fidelity INTEGER NOT NULL,
    predicted REAL NOT NULL,
    measured REAL NOT NULL,
    ratio REAL NOT NULL,
    candidate_id TEXT,
    geometry_id TEXT,
    context_json TEXT,
    UNIQUE(problem, metric, candidate_id, low_fidelity, high_fidelity)
);
CREATE TABLE IF NOT EXISTS failure_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at REAL NOT NULL,
    problem TEXT NOT NULL,
    space_digest TEXT,
    candidate_id TEXT,
    failure_class TEXT NOT NULL,
    metric TEXT,
    trustworthy INTEGER NOT NULL,
    values_json TEXT NOT NULL,
    message TEXT,
    UNIQUE(problem, candidate_id, failure_class)
);
"""


def _num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class CorrectionEstimate:
    """A model-correction factor plus the evidence behind it."""

    def __init__(self, metric: str, problem: str, ratios: list[float],
                 rows: list[dict]):
        self.metric = metric
        self.problem = problem
        self.ratios = ratios
        self.rows = rows

    @property
    def n(self) -> int:
        return len(self.ratios)

    @property
    def factor(self) -> float:
        """Geometric mean — these are ratios, so the geometric mean is the
        right centre: over- and under-predictions of the same proportion
        cancel, which an arithmetic mean would not do."""
        return math.exp(statistics.fmean(math.log(r) for r in self.ratios))

    @property
    def spread(self) -> float:
        """Ratio of max to min. 1.0 is perfect agreement."""
        return max(self.ratios) / min(self.ratios) if self.ratios else float("nan")

    @property
    def trustworthy(self) -> bool:
        """Wide disagreement means the correction is not a correction, it is
        an average of unlike things."""
        return self.n >= 3 and self.spread <= 1.5

    def to_dict(self) -> dict:
        return {"metric": self.metric, "problem": self.problem,
                "factor": round(self.factor, 4), "n": self.n,
                "spread": round(self.spread, 4),
                "trustworthy": self.trustworthy,
                "ratios": [round(r, 4) for r in self.ratios],
                "evidence": self.rows}

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"<Correction {self.metric} x{self.factor:.3f} "
                f"n={self.n} spread={self.spread:.2f} "
                f"{'ok' if self.trustworthy else 'THIN'}>")


class KnowledgeBase:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # ---------------------------------------------------------------- ingest
    def ingest_log(self, log, project: str = "design-engine") -> dict:
        """Pull every solver run out of the FRACAS log.

        Idempotent: `source_action_id` is UNIQUE, so re-ingesting an unchanged
        log adds nothing. That matters because this is expected to be run
        after every design session.
        """
        added = skipped = 0
        for action in ("fea_static", "fea_buckling"):
            for row in log.rows(action=action):
                if row["result"] == "pending":
                    continue
                d = {}
                if row["details_json"]:
                    try:
                        d = json.loads(row["details_json"])
                    except (ValueError, TypeError):
                        d = {}
                mat = d.get("material") or {}
                derate = d.get("thermal_derating") or {}
                sf = d.get("safety_factor")
                sf = _num(sf) if not isinstance(sf, str) else None
                try:
                    self._conn.execute(
                        "INSERT INTO observations (source_action_id, ingested_at,"
                        " project, geometry_id, action, limit_state, material,"
                        " result, safety_factor, required_sf, max_von_mises_MPa,"
                        " allowable_MPa, nodes, elements, mesh_mm, solve_seconds,"
                        " peak_rss_mb, outlier_ratio, service_temp_C,"
                        " failure_mode, reason)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (row["id"], time.time(), project, row["geometry_version"],
                         action, d.get("limit_state"),
                         mat.get("name") if isinstance(mat, dict) else None,
                         row["result"], sf, _num(d.get("required_SF")),
                         _num(d.get("max_von_mises_MPa")),
                         _num(d.get("allowable_MPa")),
                         d.get("nodes"), d.get("elements"),
                         _num((d.get("mesh") or {}).get("max_size_mm")),
                         _num(d.get("solve_seconds")),
                         _num(d.get("peak_rss_mb")),
                         _num(d.get("stress_outlier_ratio")),
                         _num(derate.get("service_temp_C")),
                         row["failure_mode"], row["reason"]))
                    added += 1
                except sqlite3.IntegrityError:
                    skipped += 1
        self._conn.commit()
        return {"added": added, "already_known": skipped,
                "total": self.count("observations")}

    def observe_candidate(self, cand: Candidate, problem: str) -> int:
        """Harvest calibration pairs and failure points from one candidate.

        A calibration pair exists whenever the SAME metric was produced by two
        stages at different fidelities — which is exactly what happens when a
        screened candidate is promoted. This is the automated form of reading
        two FEA results and hand-editing a Kt constant.
        """
        pairs = 0
        by_metric: dict[str, list[tuple[int, float]]] = {}
        for st in cand.result.stages:
            for metric, value in st.metrics.items():
                v = _num(value)
                if v is None or v == 0:
                    continue
                by_metric.setdefault(metric, []).append((int(st.fidelity), v))

        for metric, seen in by_metric.items():
            if len(seen) < 2:
                continue
            seen.sort(key=lambda p: p[0])
            lo_fid, lo_val = seen[0]
            hi_fid, hi_val = seen[-1]
            if lo_fid == hi_fid or hi_val == 0:
                continue
            try:
                self._conn.execute(
                    "INSERT INTO calibrations (recorded_at, problem, metric,"
                    " low_fidelity, high_fidelity, predicted, measured, ratio,"
                    " candidate_id, geometry_id, context_json)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (time.time(), problem, metric, lo_fid, hi_fid, lo_val,
                     hi_val, lo_val / hi_val, cand.candidate_id,
                     cand.geometry_id, json.dumps(cand.values, default=str)))
                pairs += 1
            except sqlite3.IntegrityError:
                pass

        for f in cand.result.failures:
            try:
                self._conn.execute(
                    "INSERT INTO failure_points (recorded_at, problem,"
                    " space_digest, candidate_id, failure_class, metric,"
                    " trustworthy, values_json, message)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (time.time(), problem, cand.space_digest,
                     cand.candidate_id, f.failure_class.value, f.metric,
                     1 if f.trustworthy else 0,
                     json.dumps(cand.values, default=str), f.message[:400]))
            except sqlite3.IntegrityError:
                pass
        self._conn.commit()
        return pairs

    # ------------------------------------------------------------- questions
    def correction(self, metric: str, problem: str | None = None,
                   min_observations: int = 3) -> CorrectionEstimate | None:
        """How much the cheap model over-predicts this metric, or None.

        Returns None — never a neutral 1.0 — when the evidence is too thin. A
        caller that gets None must carry on uncorrected and know that it is
        uncorrected; a silent 1.0 would be indistinguishable from "measured
        and found to be fine".
        """
        sql = ("SELECT * FROM calibrations WHERE metric=?"
               + (" AND problem=?" if problem else ""))
        args = [metric] + ([problem] if problem else [])
        rows = self._conn.execute(sql, args).fetchall()
        ratios, evidence = [], []
        for r in rows:
            if r["ratio"] and r["ratio"] > 0:
                ratios.append(r["ratio"])
                evidence.append({
                    "candidate_id": r["candidate_id"],
                    "geometry_id": r["geometry_id"],
                    "predicted": round(r["predicted"], 4),
                    "measured": round(r["measured"], 4),
                    "ratio": round(r["ratio"], 4),
                    "low_fidelity": r["low_fidelity"],
                    "high_fidelity": r["high_fidelity"]})
        if len(ratios) < min_observations:
            return None
        return CorrectionEstimate(metric, problem or "*", ratios, evidence)

    def precedent(self, material: str | None = None,
                  limit_state: str | None = None,
                  passing_only: bool = True, limit: int = 10) -> list[dict]:
        """Has anything like this been solved before?"""
        sql = "SELECT * FROM observations WHERE 1=1"
        args: list = []
        if material:
            sql += " AND material=?"
            args.append(material)
        if limit_state:
            sql += " AND limit_state=?"
            args.append(limit_state)
        if passing_only:
            sql += " AND result='pass'"
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self._conn.execute(sql, args).fetchall()]

    def failure_regions(self, failure_class: str | None = None,
                        problem: str | None = None,
                        trustworthy_only: bool = True) -> list[dict]:
        """Known-bad points, so the search need not rediscover them."""
        sql = "SELECT * FROM failure_points WHERE 1=1"
        args: list = []
        if failure_class:
            sql += " AND failure_class=?"
            args.append(failure_class)
        if problem:
            sql += " AND problem=?"
            args.append(problem)
        if trustworthy_only:
            sql += " AND trustworthy=1"
        rows = self._conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["values"] = json.loads(r["values_json"])
            except (ValueError, TypeError):
                d["values"] = {}
            out.append(d)
        return out

    def warn(self, values: dict, space, problem: str | None = None,
             radius: float = 0.12, limit: int = 4) -> list[dict]:
        """Does this design sit near something that has failed before?

        Distance is normalised per variable by its declared range, so a 2 mm
        difference in a variable that spans 5 mm counts far more than in one
        that spans 500 mm. Categorical mismatches count as full distance.

        Returns the matched PRIOR DESIGNS with their distances, not a score,
        so the reasoning stays checkable.
        """
        out = []
        for rec in self.failure_regions(problem=problem):
            prior = rec.get("values") or {}
            shared = [v for v in space.searchable if v.name in prior and v.name in values]
            if not shared:
                continue
            total = 0.0
            for var in shared:
                a, b = values[var.name], prior[var.name]
                if isinstance(a, (int, float)) and isinstance(b, (int, float)) \
                        and not isinstance(a, bool) and not isinstance(b, bool):
                    try:
                        lo, hi = var.bounds(values)
                    except Exception:
                        continue
                    span = (hi - lo) or 1.0
                    total += ((float(a) - float(b)) / span) ** 2
                else:
                    total += 0.0 if a == b else 1.0
            dist = math.sqrt(total / len(shared))
            if dist <= radius:
                out.append({"distance": round(dist, 4),
                            "failure_class": rec["failure_class"],
                            "candidate_id": rec["candidate_id"],
                            "message": rec["message"],
                            "prior_values": prior})
        out.sort(key=lambda r: r["distance"])
        return out[:limit]

    # ---------------------------------------------------------------- report
    def count(self, table: str) -> int:
        if table not in ("observations", "calibrations", "failure_points"):
            raise ValueError(f"unknown table {table!r}")
        return self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def solver_cost_model(self) -> dict | None:
        """What a solve actually costs, learned from real runs.

        Lets a planner answer "can I afford to promote this?" before spending
        ten minutes finding out. Fitted on log(nodes) vs log(seconds), which is
        the right shape for a direct solver, and reported with its sample size.
        """
        rows = self._conn.execute(
            "SELECT nodes, solve_seconds FROM observations"
            " WHERE nodes > 0 AND solve_seconds > 0").fetchall()
        pts = [(math.log(r["nodes"]), math.log(r["solve_seconds"])) for r in rows]
        if len(pts) < 4:
            return None
        mx = statistics.fmean(p[0] for p in pts)
        my = statistics.fmean(p[1] for p in pts)
        denom = sum((p[0] - mx) ** 2 for p in pts)
        if denom == 0:
            return None
        slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / denom
        intercept = my - slope * mx
        # Residual spread in log space. Reported because a point estimate here
        # would be fake precision: measured against real runs this fit is ~6%
        # at 400k nodes but >100% at 96k, where a fixed meshing/IO overhead
        # the power law cannot represent dominates. It is an affordability
        # check at the expensive end, not a timing prediction.
        resid = [p[1] - (intercept + slope * p[0]) for p in pts]
        sigma = statistics.pstdev(resid) if len(resid) > 1 else 0.0
        return {"exponent": round(slope, 3),
                "seconds_at_100k_nodes": round(math.exp(intercept + slope * math.log(1e5)), 1),
                "n": len(pts),
                "log_residual_sigma": round(sigma, 3),
                "band_multiplier": round(math.exp(sigma), 2),
                "note": "fitted on log(nodes) vs log(seconds) from real runs, "
                        "single-threaded direct solve. Use predict_solve() and "
                        "treat the HIGH end as the budget - under-predicting a "
                        "solve wastes the whole timeout returning nothing."}

    def predict_solve(self, nodes: int) -> dict | None:
        """Affordability estimate for a solve of this size, with its band.

        Returns the high end alongside the estimate, and callers should budget
        against the high end. The asymmetry is deliberate: over-estimating
        costs a slightly conservative decision, under-estimating costs the
        entire solver timeout and yields no information at all - which is
        exactly what happened twice on the jetpack run.
        """
        m = self.solver_cost_model()
        if not m or nodes <= 0:
            return None
        est = m["seconds_at_100k_nodes"] * (nodes / 1e5) ** m["exponent"]
        band = m["band_multiplier"]
        return {"nodes": nodes, "estimate_s": round(est, 1),
                "low_s": round(est / band, 1), "high_s": round(est * band, 1),
                "n": m["n"], "band_multiplier": band}

    def solver_memory_model(self) -> dict | None:
        """What a solve costs in MEMORY, learned the same way as time.

        This exists because time was never the binding constraint. On
        2026-08-27 a 504k-node solve was predicted at 589 s, had a 3600 s
        budget, and died anyway — an access violation at a 6.1 GB working set
        on a machine with 1.3 GB free. `affordable()` had said yes, because it
        only ever modelled seconds. A plan that assumes "we can always refine
        further, it just costs hours" is wrong whenever RAM runs out first.

        Same log-log fit as the cost model: a direct sparse solve's fill-in
        grows as a power of the node count, so memory takes the same shape.
        """
        rows = self._conn.execute(
            "SELECT nodes, peak_rss_mb FROM observations"
            " WHERE nodes > 0 AND peak_rss_mb > 0").fetchall()
        pts = [(math.log(r["nodes"]), math.log(r["peak_rss_mb"])) for r in rows]
        if len(pts) < 4:
            return None
        mx = statistics.fmean(p[0] for p in pts)
        my = statistics.fmean(p[1] for p in pts)
        denom = sum((p[0] - mx) ** 2 for p in pts)
        if denom == 0:
            return None
        slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / denom
        intercept = my - slope * mx
        resid = [p[1] - (intercept + slope * p[0]) for p in pts]
        sigma = statistics.pstdev(resid) if len(resid) > 1 else 0.0
        return {"exponent": round(slope, 3),
                "mb_at_100k_nodes": round(
                    math.exp(intercept + slope * math.log(1e5)), 1),
                "n": len(pts),
                "log_residual_sigma": round(sigma, 3),
                "band_multiplier": round(math.exp(sigma), 2),
                "note": "peak working set vs node count, from real runs. Budget "
                        "against the HIGH end: exceeding available memory does "
                        "not slow the solve down, it kills it outright."}

    def predict_memory(self, nodes: int) -> dict | None:
        """Peak memory estimate for a solve of this size, with its band."""
        m = self.solver_memory_model()
        if not m or nodes <= 0:
            return None
        est = m["mb_at_100k_nodes"] * (nodes / 1e5) ** m["exponent"]
        band = m["band_multiplier"]
        return {"nodes": nodes, "estimate_mb": round(est, 1),
                "low_mb": round(est / band, 1), "high_mb": round(est * band, 1),
                "n": m["n"], "band_multiplier": band}

    @staticmethod
    def available_memory_mb() -> float | None:
        """Physical memory currently available, or None if unknowable.

        Deliberately reports AVAILABLE rather than total: a machine with 15.5
        GB installed and 1.3 GB free cannot run a 6 GB solve, and only the
        second number predicts that.
        """
        try:
            import ctypes

            class _MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_uint32),
                            ("dwMemoryLoad", ctypes.c_uint32),
                            ("ullTotalPhys", ctypes.c_uint64),
                            ("ullAvailPhys", ctypes.c_uint64),
                            ("ullTotalPageFile", ctypes.c_uint64),
                            ("ullAvailPageFile", ctypes.c_uint64),
                            ("ullTotalVirtual", ctypes.c_uint64),
                            ("ullAvailVirtual", ctypes.c_uint64),
                            ("ullAvailExtendedVirtual", ctypes.c_uint64)]

            if not hasattr(ctypes, "windll"):
                return None
            st = _MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None
            return round(st.ullAvailPhys / (1024 * 1024), 1)
        except (OSError, AttributeError, ImportError):
            return None

    def affordable(self, nodes: int, budget_s: float,
                   memory_mb: float | None = None) -> dict:
        """Should this candidate be promoted, given a solver budget?

        Checks memory FIRST when a model exists. Running out of memory does not
        make a solve slow, it makes it return nothing — so a time verdict on a
        solve that cannot fit is a confident answer to the wrong question.

        `memory_mb` defaults to what is actually free right now, not to the
        machine's installed total.
        """
        mem = self.predict_memory(nodes)
        if mem is not None:
            limit = memory_mb if memory_mb is not None else self.available_memory_mb()
            if limit is not None and mem["high_mb"] > limit:
                verdict = "no" if mem["estimate_mb"] > limit else "marginal"
                return {"verdict": verdict,
                        "reason": (f"predicted peak memory "
                                   f"{mem['estimate_mb']:.0f} MB (band to "
                                   f"{mem['high_mb']:.0f} MB) against "
                                   f"{limit:.0f} MB available; memory, not "
                                   f"time, is the binding constraint here"),
                        "prediction": self.predict_solve(nodes),
                        "memory": mem, "available_mb": limit}

        p = self.predict_solve(nodes)
        if p is None:
            return {"verdict": "unknown", "reason": "no cost model yet",
                    "prediction": None, "memory": mem}
        # Three verdicts, not two. Measured on this repo's own history the
        # band is x1.5-1.9 wide and restricting the fit does not tighten it
        # (the exponent goes physically implausible as n drops), so a binary
        # yes/no would be confidently wrong about half the borderline cases.
        # "no" is reserved for a clear refusal; the honest answer in between
        # is that we do not know yet.
        if p["estimate_s"] > budget_s:
            return {"verdict": "no",
                    "reason": (f"central estimate {p['estimate_s']:.0f}s already "
                               f"exceeds the {budget_s:.0f}s budget"),
                    "prediction": p, "memory": mem}
        if p["high_s"] > budget_s:
            return {"verdict": "marginal",
                    "reason": (f"estimate {p['estimate_s']:.0f}s fits but the band "
                               f"reaches {p['high_s']:.0f}s; on n={p['n']} runs "
                               f"this model cannot tell you which"),
                    "prediction": p, "memory": mem}
        return {"verdict": "yes",
                "reason": f"upper estimate {p['high_s']:.0f}s fits {budget_s:.0f}s",
                "prediction": p, "memory": mem}

    def report(self) -> dict:
        obs = self._conn.execute(
            "SELECT limit_state, material, result, COUNT(*) n,"
            " AVG(safety_factor) avg_sf FROM observations"
            " GROUP BY limit_state, material, result").fetchall()
        fails = self._conn.execute(
            "SELECT failure_class, COUNT(*) n FROM failure_points"
            " WHERE trustworthy=1 GROUP BY failure_class").fetchall()
        cals = self._conn.execute(
            "SELECT metric, COUNT(*) n FROM calibrations GROUP BY metric").fetchall()
        return {
            "observations": self.count("observations"),
            "calibrations": self.count("calibrations"),
            "failure_points": self.count("failure_points"),
            "by_limit_state": [dict(r) for r in obs],
            "trustworthy_failures": {r["failure_class"]: r["n"] for r in fails},
            "calibrated_metrics": {r["metric"]: r["n"] for r in cals},
            "solver_cost": self.solver_cost_model(),
        }
