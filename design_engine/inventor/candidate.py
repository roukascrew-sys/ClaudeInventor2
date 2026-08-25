"""Candidates, evaluation results, and failure records.

A Candidate is a proposal. It is NOT an engineering fact until an evaluator
has run and the engine has spoken. The distinction is enforced by structure:
a Candidate holds `result`, and a result always carries the FIDELITY that
produced it. A mass from a closed-form beam formula and a mass from the
geometry kernel are both "mass", and confusing them is how a search convinces
itself of something untrue.

Lineage is first-class. Every candidate records its parent, the operator that
produced it, and the reason — so the system can always answer "where did this
design come from and why was it tried?", which is the same question the
FRACAS log answers for manual work.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .requirements import (ConstraintResult, Objective, RequirementSet,
                           Status, digest_of)
from .space import values_digest


class Fidelity(int, Enum):
    """Ordered so `>=` comparisons mean "at least this trustworthy"."""
    L0_ANALYTIC = 0        # closed-form, spec only, microseconds
    L1_GEOMETRY = 1        # real solid, exact mass properties, tens of ms
    L2_COARSE_FEA = 2      # meshed + solved, coarse. seconds
    L3_HIGH_FEA = 3        # converged mesh, full limit-state set. minutes
    L4_COUPLED = 4         # multi-physics / assembly-level coupling

    @property
    def label(self) -> str:
        return {0: "analytic", 1: "geometry", 2: "coarse-FEA",
                3: "high-fidelity-FEA", 4: "coupled"}[int(self)]


class FailureClass(str, Enum):
    """Failure taxonomy. `NUMERICAL` is separated from the physical modes on
    purpose: a constraint-corner singularity or a degenerate mesh is a defect
    in the MODEL, not evidence about the design, and feeding it to the
    optimiser as if it were physics poisons the search."""
    YIELD = "yield"
    BUCKLING = "buckling"
    FATIGUE = "fatigue"
    THERMAL = "thermal"
    STIFFNESS = "stiffness"
    KINEMATIC = "kinematic"
    GEOMETRIC = "geometric"          # spec built nothing / self-intersecting
    MANUFACTURING = "manufacturing"
    COST = "cost"
    SOURCING = "sourcing"
    INTERFACE = "interface"          # envelope, clearance, stackup
    MASS = "mass"
    BALANCE = "balance"              # CG / thrust-line / stability
    NUMERICAL = "numerical"          # mesh failure, singularity, solver abort
    UNKNOWN = "unknown"


@dataclass
class FailureRecord:
    """Why a candidate failed, in enough detail to steer the next attempt.

    `contributing_variables` is what turns a failure into information: it is
    the list of design variables the evaluator believes are implicated, which
    failure-informed search uses to bias mutation.
    """
    failure_class: FailureClass
    metric: str = ""
    message: str = ""
    location_mm: list[float] | None = None
    actual: float | None = None
    required: float | None = None
    normalized_margin: float | None = None
    contributing_variables: list[str] = field(default_factory=list)
    fidelity: Fidelity | None = None
    trustworthy: bool = True     # False for NUMERICAL / suspected artifacts

    def to_dict(self) -> dict:
        return {"class": self.failure_class.value, "metric": self.metric,
                "message": self.message, "location_mm": self.location_mm,
                "actual": self.actual, "required": self.required,
                "normalized_margin": self.normalized_margin,
                "contributing_variables": list(self.contributing_variables),
                "fidelity": int(self.fidelity) if self.fidelity is not None else None,
                "trustworthy": self.trustworthy}


@dataclass
class StageResult:
    """One evaluation stage's contribution."""
    stage: str
    fidelity: Fidelity
    status: Status
    metrics: dict = field(default_factory=dict)
    failures: list[FailureRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)
    seconds: float = 0.0
    cached: bool = False

    def to_dict(self) -> dict:
        return {"stage": self.stage, "fidelity": int(self.fidelity),
                "fidelity_label": self.fidelity.label,
                "status": self.status.value, "metrics": self.metrics,
                "failures": [f.to_dict() for f in self.failures],
                "warnings": list(self.warnings), "provenance": self.provenance,
                "seconds": round(self.seconds, 4), "cached": self.cached}


@dataclass
class EvaluationResult:
    """Everything known about a candidate, tagged with how well it is known."""
    status: Status = Status.NOT_EVALUATED
    metrics: dict = field(default_factory=dict)
    metric_fidelity: dict = field(default_factory=dict)   # metric -> Fidelity
    stages: list[StageResult] = field(default_factory=list)
    constraint_results: list[ConstraintResult] = field(default_factory=list)
    failures: list[FailureRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    seconds: float = 0.0

    @property
    def fidelity(self) -> Fidelity:
        """The LOWEST fidelity among stages that ran — a result is only as
        trustworthy as its weakest evaluated component."""
        if not self.stages:
            return Fidelity.L0_ANALYTIC
        return min(s.fidelity for s in self.stages)

    @property
    def max_fidelity(self) -> Fidelity:
        return max((s.fidelity for s in self.stages), default=Fidelity.L0_ANALYTIC)

    @property
    def feasible(self) -> bool:
        return self.status is Status.VALID

    @property
    def blocking_results(self) -> list[ConstraintResult]:
        return [c for c in self.constraint_results if c.blocking]

    def absorb(self, sr: StageResult) -> None:
        self.stages.append(sr)
        for k, v in sr.metrics.items():
            self.metrics[k] = v
            self.metric_fidelity[k] = sr.fidelity
        self.failures.extend(sr.failures)
        self.warnings.extend(sr.warnings)
        self.seconds += sr.seconds

    def apply_requirements(self, reqs: RequirementSet) -> None:
        """Run constraints and set the overall status.

        Order matters: a blocking constraint makes the candidate INVALID
        regardless of how good its objectives are. That is Principle 2 —
        constraints are not preferences — expressed in code rather than in a
        comment.
        """
        self.constraint_results = reqs.evaluate_constraints(self.metrics)
        hard_fail = [c for c in self.constraint_results
                     if c.constraint.severity == "mandatory"
                     and c.status is Status.INVALID]
        hard_unknown = [c for c in self.constraint_results
                        if c.constraint.severity == "mandatory"
                        and c.status in (Status.UNKNOWN, Status.NOT_EVALUATED)]
        # A stage that returned INVALID has DEFINITIVELY established
        # infeasibility (a design-space rule was violated, a spec would not
        # build, a solver said the safety factor is short). Evaluation then
        # stops, so no metrics exist and every constraint reads UNKNOWN --
        # which must NOT be allowed to downgrade a known refusal to "we could
        # not tell". UNKNOWN is for genuine ignorance only; conflating the two
        # would both mislabel the result and rob the optimiser of the
        # information that this region is actually bad.
        stage_refused = any(s.status is Status.INVALID for s in self.stages)
        # A stage that RAN and could not produce an answer degrades the whole
        # result to UNKNOWN, even when every constraint currently reads VALID.
        # Caught in a real jetpack promotion: the L3 solver stage errored out,
        # but `sf.thermal_derated_yield` was already present from the L0 beam
        # model, so the constraint passed AT L0 FIDELITY and the candidate
        # reported VALID with no solver run and no part ever materialised.
        # A failed promotion must never leave a design looking validated at a
        # fidelity that never executed. Note this only fires for stages that
        # actually ran - a stage skipped by a fidelity ceiling is
        # NOT_EVALUATED, so ordinary cheap screening is unaffected.
        stage_unknown = any(s.status is Status.UNKNOWN for s in self.stages)
        if hard_fail or stage_refused:
            self.status = Status.INVALID
        elif hard_unknown or stage_unknown:
            self.status = Status.UNKNOWN
        else:
            self.status = Status.VALID
        for c in hard_fail:
            if not any(f.metric == c.constraint.metric for f in self.failures):
                self.failures.append(FailureRecord(
                    failure_class=FailureClass.UNKNOWN,
                    metric=c.constraint.metric,
                    message=f"{c.constraint.name}: {c.actual} vs required "
                            f"{c.constraint.op.value} {c.constraint.bound}",
                    actual=c.actual, required=c.constraint.bound,
                    normalized_margin=c.normalized_margin,
                    fidelity=self.metric_fidelity.get(c.constraint.metric)))

    def objective_vector(self, objectives: list[Objective]) -> list[float] | None:
        """Losses (all minimised). None if any objective metric is missing —
        an incomplete vector must not be compared for dominance."""
        out = []
        for o in objectives:
            loss = o.loss(self.metrics.get(o.metric))
            if loss is None:
                return None
            out.append(loss)
        return out

    def to_dict(self) -> dict:
        return {"status": self.status.value,
                "fidelity": int(self.fidelity),
                "fidelity_label": self.fidelity.label,
                "max_fidelity": int(self.max_fidelity),
                "metrics": self.metrics,
                "metric_fidelity": {k: int(v) for k, v in self.metric_fidelity.items()},
                "constraints": [c.to_dict() for c in self.constraint_results],
                "failures": [f.to_dict() for f in self.failures],
                "warnings": list(self.warnings),
                "stages": [s.to_dict() for s in self.stages],
                "seconds": round(self.seconds, 4)}


@dataclass
class Candidate:
    """A design proposal plus its evaluation state and full lineage."""
    values: dict
    space_digest: str = ""
    candidate_id: str = ""
    parent_id: str | None = None
    generation: int = 0
    operator: str = "seed"           # how it was produced
    reason: str = ""
    spec: dict | None = None         # materialised design spec, if built
    spec_digest: str | None = None
    geometry_id: str | None = None   # set only if promoted into PartStore
    result: EvaluationResult = field(default_factory=EvaluationResult)
    created_at: float = field(default_factory=time.time)

    def __post_init__(self):
        if not self.candidate_id:
            self.candidate_id = self.identity()

    def identity(self) -> str:
        """Content-addressed id: same variable values + same space => same id.

        Deliberately content-based rather than a counter, so that a design
        rediscovered by a different search path is recognised as the same
        design and reuses its cached evaluation.
        """
        return f"c{values_digest(self.values)}"

    @property
    def status(self) -> Status:
        return self.result.status

    @property
    def feasible(self) -> bool:
        return self.result.feasible

    def child(self, values: dict, operator: str, reason: str) -> "Candidate":
        return Candidate(values=values, space_digest=self.space_digest,
                         parent_id=self.candidate_id,
                         generation=self.generation + 1,
                         operator=operator, reason=reason)

    def to_dict(self) -> dict:
        return {"candidate_id": self.candidate_id, "parent_id": self.parent_id,
                "generation": self.generation, "operator": self.operator,
                "reason": self.reason, "values": self.values,
                "space_digest": self.space_digest,
                "spec_digest": self.spec_digest, "geometry_id": self.geometry_id,
                "result": self.result.to_dict()}

    @property
    def provenance_digest(self) -> str:
        return digest_of({"values": self.values, "space": self.space_digest})
