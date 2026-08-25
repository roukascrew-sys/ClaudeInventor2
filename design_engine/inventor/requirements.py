"""Formal requirement model — the machine-readable statement of the problem.

Three distinct kinds, deliberately NOT collapsed into one weighted score:

  Constraint  a hard feasibility gate. Violating it makes a candidate
              infeasible no matter how good it is elsewhere. A cheap, light
              design that fails its safety margin is not a trade-off, it is
              a rejected design.
  Objective   a quantity to minimise, maximise, hit a target, or keep inside
              a preferred band. Objectives stay a VECTOR through the whole
              pipeline; scalarisation, if it happens at all, happens at the
              very end for display.
  Preference  a soft tie-breaker. Influences ranking among already-feasible,
              already-Pareto-optimal candidates. Never gates anything.

Every one of them names a METRIC, which is a key into
EvaluationResult.metrics. If the metric is absent the constraint evaluates to
UNKNOWN — never to "pass". That mirrors the engine's existing refusal
philosophy: missing information is not success.

Requirements carry units and a source string. `source` is required on hard
constraints for the same reason `fea.validate_case` requires a material
source: a mandatory safety gate whose provenance nobody recorded is not a
gate, it is a number somebody typed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class ReqError(ValueError):
    pass


class Sense(str, Enum):
    """Which direction is better for an objective."""
    MIN = "minimize"
    MAX = "maximize"
    TARGET = "target"          # closer to `target` is better
    RANGE = "range"            # anywhere inside [lo, hi] is equally good


class Op(str, Enum):
    """Comparison for a constraint: actual <op> bound."""
    LE = "<="
    GE = ">="
    LT = "<"
    GT = ">"
    BETWEEN = "between"


class Status(str, Enum):
    """Four states, not two. UNKNOWN and NOT_EVALUATED must never be
    silently folded into VALID — that is how an unevaluated safety gate
    becomes an apparent pass."""
    VALID = "valid"
    INVALID = "invalid"
    UNKNOWN = "unknown"              # evaluated, but the answer is not trustworthy
    NOT_EVALUATED = "not_evaluated"  # this stage never ran


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def digest_of(obj: Any) -> str:
    """Short stable digest, same convention as geometry.spec_digest."""
    return hashlib.sha256(_canon(obj).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class Constraint:
    """A hard feasibility gate on a named metric.

    `scale` normalises the margin so the optimiser can compare "how badly
    did this miss" across constraints with different units. Without it a
    0.1 mm violation and a 10 MPa violation are incomparable numbers.
    """
    name: str
    metric: str
    op: Op
    bound: float | None = None
    lo: float | None = None
    hi: float | None = None
    units: str = ""
    source: str = ""
    scale: float | None = None
    severity: str = "mandatory"      # "mandatory" | "advisory"

    def __post_init__(self):
        if not self.name or not self.metric:
            raise ReqError("constraint needs a name and a metric")
        if self.severity not in ("mandatory", "advisory"):
            raise ReqError(f"{self.name}: severity must be mandatory|advisory")
        if self.op is Op.BETWEEN:
            if self.lo is None or self.hi is None:
                raise ReqError(f"{self.name}: BETWEEN needs lo and hi")
            if self.lo >= self.hi:
                raise ReqError(f"{self.name}: lo must be < hi")
        elif self.bound is None:
            raise ReqError(f"{self.name}: op {self.op.value} needs a bound")
        if self.severity == "mandatory" and not self.source.strip():
            raise ReqError(
                f"{self.name}: a mandatory constraint requires a 'source' — "
                f"cite the standard, datasheet, or stated assumption behind "
                f"it. An unsourced hard gate is just a number someone typed.")
        if self.scale is not None and self.scale <= 0:
            raise ReqError(f"{self.name}: scale must be positive")

    # ---- evaluation -------------------------------------------------
    def _scale(self) -> float:
        if self.scale is not None:
            return self.scale
        ref = self.bound if self.bound is not None else max(abs(self.lo), abs(self.hi))
        return abs(ref) if ref else 1.0

    def evaluate(self, metrics: dict) -> "ConstraintResult":
        if self.metric not in metrics:
            return ConstraintResult(
                constraint=self, actual=None, status=Status.UNKNOWN,
                margin=None, normalized_margin=None,
                note=f"metric {self.metric!r} not present in this result")
        actual = metrics[self.metric]
        if actual is None:
            return ConstraintResult(
                constraint=self, actual=None, status=Status.UNKNOWN,
                margin=None, normalized_margin=None,
                note=f"metric {self.metric!r} is None")
        actual = float(actual)

        # margin > 0 means feasible, in the metric's own units
        if self.op is Op.LE:
            margin = self.bound - actual
            ok = actual <= self.bound
        elif self.op is Op.LT:
            margin = self.bound - actual
            ok = actual < self.bound
        elif self.op is Op.GE:
            margin = actual - self.bound
            ok = actual >= self.bound
        elif self.op is Op.GT:
            margin = actual - self.bound
            ok = actual > self.bound
        else:  # BETWEEN
            margin = min(actual - self.lo, self.hi - actual)
            ok = self.lo <= actual <= self.hi

        return ConstraintResult(
            constraint=self, actual=actual,
            status=Status.VALID if ok else Status.INVALID,
            margin=margin, normalized_margin=margin / self._scale())

    def to_dict(self) -> dict:
        d = asdict(self)
        d["op"] = self.op.value
        return d


@dataclass(frozen=True)
class ConstraintResult:
    constraint: Constraint
    actual: float | None
    status: Status
    margin: float | None
    normalized_margin: float | None
    note: str = ""

    @property
    def blocking(self) -> bool:
        """A mandatory constraint that is INVALID or UNKNOWN blocks the
        candidate. UNKNOWN blocks deliberately: we do not ship a design whose
        safety gate could not be evaluated."""
        if self.constraint.severity != "mandatory":
            return False
        return self.status in (Status.INVALID, Status.UNKNOWN)

    def to_dict(self) -> dict:
        return {"name": self.constraint.name,
                "metric": self.constraint.metric,
                "op": self.constraint.op.value,
                "required": (self.constraint.bound if self.constraint.op is not Op.BETWEEN
                             else [self.constraint.lo, self.constraint.hi]),
                "actual": self.actual,
                "units": self.constraint.units,
                "margin": self.margin,
                "normalized_margin": self.normalized_margin,
                "status": self.status.value,
                "severity": self.constraint.severity,
                "source": self.constraint.source,
                "note": self.note}


@dataclass(frozen=True)
class Objective:
    """A quantity to optimise. Units and physical meaning are preserved;
    nothing here converts to a dimensionless score."""
    name: str
    metric: str
    sense: Sense
    units: str = ""
    target: float | None = None
    lo: float | None = None
    hi: float | None = None
    weight: float = 1.0          # used ONLY for optional display scalarisation
    description: str = ""

    def __post_init__(self):
        if self.sense is Sense.TARGET and self.target is None:
            raise ReqError(f"{self.name}: TARGET objective needs a target")
        if self.sense is Sense.RANGE and (self.lo is None or self.hi is None):
            raise ReqError(f"{self.name}: RANGE objective needs lo and hi")

    def loss(self, value: float | None) -> float | None:
        """Convert to a MINIMISED quantity for dominance testing.

        Dominance needs a common direction; this is a direction flip, not a
        scalarisation across objectives — each objective keeps its own axis.
        """
        if value is None:
            return None
        v = float(value)
        if self.sense is Sense.MIN:
            return v
        if self.sense is Sense.MAX:
            return -v
        if self.sense is Sense.TARGET:
            return abs(v - self.target)
        lo, hi = self.lo, self.hi          # RANGE
        return 0.0 if lo <= v <= hi else (lo - v if v < lo else v - hi)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sense"] = self.sense.value
        return d


@dataclass(frozen=True)
class Preference:
    """Soft tie-breaker among feasible, non-dominated candidates."""
    name: str
    metric: str
    sense: Sense
    weight: float = 1.0
    description: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sense"] = self.sense.value
        return d


@dataclass
class RequirementSet:
    """The whole problem statement, versionable and digestible."""
    name: str
    constraints: list[Constraint] = field(default_factory=list)
    objectives: list[Objective] = field(default_factory=list)
    preferences: list[Preference] = field(default_factory=list)
    notes: str = ""
    version: int = 1

    def __post_init__(self):
        seen = set()
        for item in list(self.constraints) + list(self.objectives) + list(self.preferences):
            if item.name in seen:
                raise ReqError(f"duplicate requirement name {item.name!r}")
            seen.add(item.name)
        if not self.objectives:
            raise ReqError(
                "a RequirementSet needs at least one objective — otherwise "
                "there is nothing to optimise and this is just a checker")

    @property
    def mandatory(self) -> list[Constraint]:
        return [c for c in self.constraints if c.severity == "mandatory"]

    def required_metrics(self) -> set[str]:
        return ({c.metric for c in self.constraints}
                | {o.metric for o in self.objectives}
                | {p.metric for p in self.preferences})

    def evaluate_constraints(self, metrics: dict) -> list[ConstraintResult]:
        return [c.evaluate(metrics) for c in self.constraints]

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "notes": self.notes,
                "constraints": [c.to_dict() for c in self.constraints],
                "objectives": [o.to_dict() for o in self.objectives],
                "preferences": [p.to_dict() for p in self.preferences]}

    def digest(self) -> str:
        return digest_of(self.to_dict())
