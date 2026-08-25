"""OptimizationRun — the orchestration loop, with checkpointing and staged
promotion to high fidelity.

The loop is deliberately boring:

    for each generation:
        ask the optimiser for candidates
        SCREEN them at cheap fidelity
        tell the optimiser what happened
    then:
        PROMOTE the best few to high fidelity and re-judge them

The promotion step is where the multi-fidelity story becomes real. Screening
happens at L0/L1 (microseconds to ~24 ms). Only a handful of finalists are
ever allowed to spend solver time, and — critically — a candidate that looked
good at L1 and then FAILS at L3 is demoted, because the higher fidelity is
authoritative. The optimiser proposed; the engine decided.

`promote()` intentionally re-runs `apply_requirements`, so a promoted
candidate's status reflects its highest-fidelity evidence, and the result
carries `max_fidelity` so no consumer can mistake a screened design for a
solved one.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .analysis import FailureMemory, sensitivity
from .candidate import Candidate, Fidelity
from .evaluate import Evaluator
from .optimizers import OptimizationConfig, Optimizer, total_violation
from .pareto import archetypes, pareto_front
from .requirements import RequirementSet, Status


@dataclass
class GenerationRecord:
    generation: int
    asked: int
    evaluated: int
    feasible: int
    unknown: int
    seconds: float
    best: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class OptimizationRun:
    def __init__(self, optimizer: Optimizer, evaluator: Evaluator,
                 requirements: RequirementSet,
                 config: OptimizationConfig | None = None,
                 workdir: str | Path | None = None,
                 failure_memory: FailureMemory | None = None):
        self.optimizer = optimizer
        self.evaluator = evaluator
        self.requirements = requirements
        self.config = config or optimizer.config
        self.workdir = Path(workdir) if workdir else None
        if self.workdir:
            self.workdir.mkdir(parents=True, exist_ok=True)
        self.failure_memory = failure_memory
        if (self.failure_memory is not None
                and getattr(optimizer, "failure_memory", None) is None):
            try:
                optimizer.failure_memory = self.failure_memory
            except AttributeError:               # pragma: no cover
                pass
        self.all_candidates: list[Candidate] = []
        self.generations: list[GenerationRecord] = []
        self.promoted: list[Candidate] = []
        self.started_at = time.time()
        self.evaluations = 0

    # -- main loop -----------------------------------------------------
    def screen(self, cands: Sequence[Candidate]) -> list[Candidate]:
        out = self.evaluator.evaluate_many(
            cands, max_fidelity=self.config.screen_fidelity,
            workers=self.config.workers)
        self.evaluations += len(out)
        if self.failure_memory is not None:
            for c in out:
                self.failure_memory.observe(c)
        return out

    def run(self, generations: int | None = None) -> "OptimizationRun":
        gens = generations if generations is not None else self.config.generations
        for _ in range(gens):
            if (self.config.max_evaluations is not None
                    and self.evaluations >= self.config.max_evaluations):
                break
            t0 = time.perf_counter()
            asked = self.optimizer.ask(self.config.population)
            if not asked:
                break
            evaluated = self.screen(asked)
            self.optimizer.tell(evaluated)
            self.all_candidates.extend(evaluated)

            feasible = [c for c in evaluated if c.feasible]
            unknown = [c for c in evaluated if c.status is Status.UNKNOWN]
            best = {}
            front = pareto_front(self.all_candidates, self.requirements.objectives)
            if front:
                for o in self.requirements.objectives:
                    vals = [(c, c.result.metrics.get(o.metric)) for c in front]
                    vals = [(c, v) for c, v in vals if v is not None]
                    if vals:
                        b = min(vals, key=lambda p: o.loss(p[1]))
                        best[o.name] = b[1]
            rec = GenerationRecord(
                generation=self.optimizer.generation, asked=len(asked),
                evaluated=len(evaluated), feasible=len(feasible),
                unknown=len(unknown), seconds=time.perf_counter() - t0, best=best)
            self.generations.append(rec)
            if self.workdir:
                self.checkpoint()
        return self

    # -- promotion to high fidelity ------------------------------------
    def promote(self, top_k: int | None = None,
                fidelity: Fidelity | None = None) -> list[Candidate]:
        """Re-evaluate the best screened candidates at high fidelity.

        This is the only place the expensive solver is used, and the result it
        returns overrides the cheap estimate. A candidate that fails here is
        genuinely rejected no matter how well it screened.
        """
        k = top_k if top_k is not None else self.config.promote_top_k
        fid = fidelity if fidelity is not None else self.config.promote_fidelity
        if k <= 0:
            return []
        front = pareto_front(self.all_candidates, self.requirements.objectives)
        pool = front or [c for c in self.all_candidates if c.feasible]
        pool = sorted(pool, key=lambda c: total_violation(c))[:k]
        promoted = []
        for cand in pool:
            self.evaluator.evaluate(cand, max_fidelity=fid)
            self.evaluations += 1
            if self.failure_memory is not None:
                self.failure_memory.observe(cand)
            promoted.append(cand)
        self.promoted = promoted
        return promoted

    # -- outputs -------------------------------------------------------
    def front(self, feasible_only: bool = True) -> list[Candidate]:
        return pareto_front(self.all_candidates, self.requirements.objectives,
                            feasible_only=feasible_only)

    def archetypes(self) -> dict:
        return archetypes(self.front(), self.requirements)

    def sensitivity(self, metrics: Sequence[str] | None = None) -> dict:
        ms = list(metrics) if metrics else [o.metric for o in self.requirements.objectives]
        return sensitivity(self.all_candidates, self.evaluator.ctx.space, ms)

    def summary(self) -> dict:
        feasible = [c for c in self.all_candidates if c.feasible]
        unknown = [c for c in self.all_candidates if c.status is Status.UNKNOWN]
        invalid = [c for c in self.all_candidates if c.status is Status.INVALID]
        by_fid: dict[str, int] = {}
        for c in self.all_candidates:
            key = c.result.max_fidelity.label
            by_fid[key] = by_fid.get(key, 0) + 1
        return {
            "optimizer": self.optimizer.name,
            "config": self.config.to_dict(),
            "requirements_digest": self.requirements.digest(),
            "space_digest": self.evaluator.ctx.space.digest(),
            "generations": len(self.generations),
            "evaluations": self.evaluations,
            "unique_candidates": len(self.all_candidates),
            "feasible": len(feasible), "unknown": len(unknown),
            "infeasible": len(invalid),
            "evaluated_at_fidelity": by_fid,
            "pareto_size": len(self.front()),
            "promoted": len(self.promoted),
            "wall_seconds": round(time.time() - self.started_at, 2),
            "cost": self.evaluator.cost_report(),
        }

    # -- checkpoint / resume -------------------------------------------
    def checkpoint(self, path: str | Path | None = None) -> Path:
        p = Path(path) if path else (self.workdir / "checkpoint.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "summary": self.summary(),
            "generations": [g.to_dict() for g in self.generations],
            "optimizer_state": self.optimizer.state(),
            "candidates": [c.to_dict() for c in self.all_candidates],
        }
        p.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return p

    @staticmethod
    def load_checkpoint(path: str | Path) -> dict:
        """Read a checkpoint back.

        Returns the raw payload rather than a reconstructed live run: the
        evaluator, engine handles and callables in a DesignSpace are not
        serialisable, so a resumed run rebuilds those from the same code and
        replays the candidate history. The evaluation CACHE is what makes that
        cheap — replayed candidates hit cache rather than re-solving.
        """
        return json.loads(Path(path).read_text(encoding="utf-8"))

    def resume_from(self, path: str | Path) -> int:
        """Re-seed the optimiser's de-duplication set and history from a
        checkpoint so a continued run does not repeat work. Returns how many
        candidate records were replayed."""
        payload = self.load_checkpoint(path)
        n = 0
        for row in payload.get("candidates", []):
            cid = row.get("candidate_id", "")
            # cid is "c" + values_digest; slice, never lstrip("c") — lstrip
            # removes EVERY leading 'c', silently corrupting any digest that
            # begins with one (about 1 in 16 of them).
            if cid.startswith("c") and len(cid) > 1:
                self.optimizer._seen.add(cid[1:])
                n += 1
        return n
