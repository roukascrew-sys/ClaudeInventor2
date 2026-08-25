"""Evaluator: staged, cached, fidelity-aware candidate evaluation.

The optimiser must not know that CalculiX exists. It hands a Candidate to an
Evaluator and gets back an EvaluationResult. Everything about meshes, decks,
solvers, price books and geometry kernels lives behind this boundary.

Staging is the whole point. Measured on this repo:

    L0 analytic   ~4 us      ~250,000 / s
    L1 geometry   ~24 ms     ~40 / s
    L2 coarse FEA ~8 s       ~0.1 / s
    L3 high FEA   8-290 s    ~0.01 / s

so a population is filtered cheaply and only survivors are allowed to consume
solver time. `Evaluator.evaluate` stops at the first stage that produces a
blocking failure, because there is no reason to mesh a design that already
violates its envelope.

Two rules this module enforces structurally:

1. A metric is always tagged with the fidelity that produced it. An analytic
   mass and a kernel-computed mass are different claims.
2. A stage that could not produce an answer returns UNKNOWN, never VALID.
   The audit found meshing to be non-monotonic (a part that meshes at 3 mm
   and 8 mm can fail at 5 mm), so treating a mesh failure as "infeasible"
   would silently delete valid regions of the design space.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, Protocol, Sequence

from .. import fea as _fea_mod
from .. import geometry as _geom_mod
from .. import mesh as _mesh_mod
from .candidate import (Candidate, EvaluationResult, FailureClass,
                        FailureRecord, Fidelity, StageResult)
from .requirements import RequirementSet, Status, digest_of
from .space import DesignSpace


def _code_digest() -> str:
    """Digest of the engineering modules whose behaviour affects results.

    This is part of every cache key. An explicit hand-maintained version
    constant would go stale the moment someone edits fea.py and forgets to
    bump it, and a stale cache that returns a pre-bugfix safety factor is
    exactly the failure this project refuses elsewhere. Hashing the source is
    automatic and conservative: any edit invalidates the cache.
    """
    h = hashlib.sha256()
    for mod in (_geom_mod, _mesh_mod, _fea_mod):
        try:
            h.update(Path(mod.__file__).read_bytes())
        except OSError:                       # pragma: no cover - defensive
            h.update(b"unreadable")
    return h.hexdigest()[:12]


CODE_DIGEST = _code_digest()


class Stage(Protocol):
    """One rung of the fidelity ladder."""
    name: str
    fidelity: Fidelity

    def config_digest(self) -> str: ...
    def run(self, cand: Candidate, ctx: "EvalContext") -> StageResult: ...


class EvalContext:
    """Shared, read-mostly state handed to every stage."""

    def __init__(self, space: DesignSpace, requirements: RequirementSet,
                 base_spec: dict | None = None, engine=None,
                 extras: dict | None = None):
        self.space = space
        self.requirements = requirements
        self.base_spec = base_spec
        self.engine = engine           # design_engine.DesignEngine, or None
        self.extras = extras or {}


class EvaluationCache:
    """Content-addressed cache of StageResults.

    The key includes the candidate's variable values, the design space, the
    stage and its configuration, AND the digest of the engineering source
    code. Anything that could change the answer changes the key. Nothing is
    ever returned that was produced by different code.
    """

    def __init__(self, db_path: str | Path | None = None):
        self._mem: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.db_path = Path(db_path) if db_path else None
        self._conn: sqlite3.Connection | None = None
        if self.db_path:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS stage_cache ("
                " key TEXT PRIMARY KEY, created_at REAL, payload TEXT)")
            self._conn.commit()

    @staticmethod
    def key(cand: Candidate, stage: Stage, ctx: EvalContext) -> str:
        return digest_of({
            "values": {k: (round(v, 9) if isinstance(v, float) else v)
                       for k, v in sorted(cand.values.items())},
            "space": ctx.space.digest(),
            "stage": stage.name,
            "stage_config": stage.config_digest(),
            "base_spec": _geom_mod.spec_digest(ctx.base_spec) if ctx.base_spec else None,
            "code": CODE_DIGEST,
        })

    def get(self, key: str) -> dict | None:
        with self._lock:
            if key in self._mem:
                self.hits += 1
                return self._mem[key]
            if self._conn is not None:
                row = self._conn.execute(
                    "SELECT payload FROM stage_cache WHERE key=?", (key,)).fetchone()
                if row:
                    payload = json.loads(row[0])
                    self._mem[key] = payload
                    self.hits += 1
                    return payload
            self.misses += 1
            return None

    def put(self, key: str, payload: dict) -> None:
        with self._lock:
            self._mem[key] = payload
            if self._conn is not None:
                self._conn.execute(
                    "INSERT OR REPLACE INTO stage_cache VALUES (?,?,?)",
                    (key, time.time(), json.dumps(payload, default=str)))
                self._conn.commit()

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def stats(self) -> dict:
        return {"hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hit_rate, 4),
                "entries": len(self._mem), "code_digest": CODE_DIGEST}


def _stage_from_payload(payload: dict) -> StageResult:
    sr = StageResult(
        stage=payload["stage"], fidelity=Fidelity(payload["fidelity"]),
        status=Status(payload["status"]), metrics=payload.get("metrics", {}),
        warnings=list(payload.get("warnings", [])),
        provenance=payload.get("provenance", {}),
        seconds=payload.get("seconds", 0.0), cached=True)
    for f in payload.get("failures", []):
        sr.failures.append(FailureRecord(
            failure_class=FailureClass(f["class"]), metric=f.get("metric", ""),
            message=f.get("message", ""), location_mm=f.get("location_mm"),
            actual=f.get("actual"), required=f.get("required"),
            normalized_margin=f.get("normalized_margin"),
            contributing_variables=list(f.get("contributing_variables", [])),
            fidelity=Fidelity(f["fidelity"]) if f.get("fidelity") is not None else None,
            trustworthy=f.get("trustworthy", True)))
    return sr


class Evaluator:
    """Runs stages in fidelity order, caching each one independently.

    Per-stage caching (rather than caching a whole evaluation) means a
    candidate promoted from L1 to L3 does not re-run its L1 work, and a
    change to the FEA case does not invalidate its geometry.
    """

    def __init__(self, stages: Sequence[Stage], ctx: EvalContext,
                 cache: EvaluationCache | None = None,
                 max_fidelity: Fidelity = Fidelity.L4_COUPLED):
        self.stages = list(stages)
        self.ctx = ctx
        self.cache = cache if cache is not None else EvaluationCache()
        self.max_fidelity = max_fidelity
        self.stage_seconds: dict[str, float] = {}
        self.stage_runs: dict[str, int] = {}

    def stages_upto(self, max_fidelity: Fidelity) -> list[Stage]:
        return [s for s in self.stages if int(s.fidelity) <= int(max_fidelity)]

    def evaluate(self, cand: Candidate,
                 max_fidelity: Fidelity | None = None,
                 stop_on_block: bool = True) -> Candidate:
        ceiling = max_fidelity if max_fidelity is not None else self.max_fidelity
        result = EvaluationResult()
        # Bind the in-progress result to the candidate BEFORE running stages.
        # Later stages legitimately depend on earlier ones — a cost model
        # needs the volume the geometry stage just computed — and with the
        # result held only in a local they would each see an empty metrics
        # dict and silently return nothing.
        cand.result = result
        for stage in self.stages_upto(ceiling):
            key = EvaluationCache.key(cand, stage, self.ctx)
            payload = self.cache.get(key)
            if payload is not None:
                sr = _stage_from_payload(payload)
            else:
                t0 = time.perf_counter()
                try:
                    sr = stage.run(cand, self.ctx)
                except Exception as exc:
                    # A stage that raises is UNKNOWN, not INVALID. We do not
                    # know the design is bad; we know we failed to find out.
                    sr = StageResult(
                        stage=stage.name, fidelity=stage.fidelity,
                        status=Status.UNKNOWN,
                        failures=[FailureRecord(
                            failure_class=FailureClass.NUMERICAL,
                            message=f"{type(exc).__name__}: {exc}",
                            fidelity=stage.fidelity, trustworthy=False)])
                sr.seconds = sr.seconds or (time.perf_counter() - t0)
                self.cache.put(key, sr.to_dict())
                self.stage_seconds[stage.name] = (
                    self.stage_seconds.get(stage.name, 0.0) + sr.seconds)
                self.stage_runs[stage.name] = self.stage_runs.get(stage.name, 0) + 1
            result.absorb(sr)
            if stop_on_block and sr.status is Status.INVALID:
                break
        result.apply_requirements(self.ctx.requirements)
        cand.result = result
        if cand.spec_digest is None and cand.spec is not None:
            cand.spec_digest = _geom_mod.spec_digest(cand.spec)
        return cand

    def evaluate_many(self, cands: Sequence[Candidate],
                      max_fidelity: Fidelity | None = None,
                      workers: int = 1) -> list[Candidate]:
        """Evaluate a population.

        `workers > 1` uses threads. That is genuinely useful for the stages
        that matter: the FEA stage spends its time blocked on the CalculiX
        subprocess with the GIL released. Pure-python L0 work will not scale
        with threads, but it is already ~250,000/s and is not the bottleneck.
        Stages that mutate shared engine state declare `thread_safe = False`
        and force serial execution (see adapters.FeaStage).
        """
        if workers <= 1 or len(cands) <= 1:
            return [self.evaluate(c, max_fidelity) for c in cands]

        ceiling = max_fidelity if max_fidelity is not None else self.max_fidelity
        unsafe = [s for s in self.stages_upto(ceiling)
                  if not getattr(s, "thread_safe", True)]
        if unsafe:
            return [self.evaluate(c, max_fidelity) for c in cands]

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda c: self.evaluate(c, ceiling), cands))

    def cost_report(self) -> dict:
        return {"stage_seconds": {k: round(v, 3) for k, v in self.stage_seconds.items()},
                "stage_runs": dict(self.stage_runs),
                "cache": self.cache.stats()}
