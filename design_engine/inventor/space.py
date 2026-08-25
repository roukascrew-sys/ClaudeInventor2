"""Design variables and the design space.

Deliberately NOT a flat parameter dict. Real engineering design spaces have
structure that a flat dict cannot express, and the structure is what lets the
optimiser avoid wasting expensive evaluations:

  * dependency        `min_internal_radius` only exists once a manufacturing
                      process is chosen, and its bounds depend on which one.
  * conditionality    a variable may be inactive for some configurations.
                      An inactive variable must not perturb the identity of a
                      candidate, or the cache will miss on designs that are
                      physically identical.
  * derived values    quantities computed from other variables. These are not
                      searched over; they are consequences.
  * feasibility rules relationships between variables ("web must be thinner
                      than flange") that can be checked in microseconds and
                      so should never reach a solver.

The space maps variable values onto a *parameter path* in the underlying
design spec (`features.0.z`, `material.name`, …) so the existing
`geometry.apply_changes` dot-path machinery is reused rather than duplicated.
"""

from __future__ import annotations

import itertools
import math
import random
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Sequence

from .requirements import ReqError, digest_of


class VarType(str, Enum):
    CONTINUOUS = "continuous"
    INTEGER = "integer"
    DISCRETE = "discrete"        # ordered numeric choices, e.g. stock sizes
    CATEGORICAL = "categorical"  # unordered, e.g. material
    DERIVED = "derived"          # computed, never sampled


@dataclass
class DesignVariable:
    name: str
    type: VarType
    units: str = ""
    lo: float | None = None
    hi: float | None = None
    step: float | None = None
    values: list | None = None
    default: Any = None
    path: str | None = None            # dot-path into the design spec
    description: str = ""
    # active_if(values) -> bool. When False the variable is not part of this
    # candidate at all and is excluded from its identity.
    active_if: Callable[[dict], bool] | None = None
    # bounds_from(values) -> (lo, hi) for dependent numeric ranges
    bounds_from: Callable[[dict], tuple[float, float]] | None = None
    # values_from(values) -> list for dependent choice sets
    values_from: Callable[[dict], list] | None = None
    # compute(values) -> Any, DERIVED only
    compute: Callable[[dict], Any] | None = None

    def __post_init__(self):
        if not self.name:
            raise ReqError("variable needs a name")
        t = self.type
        if t in (VarType.CONTINUOUS, VarType.INTEGER):
            if self.bounds_from is None and (self.lo is None or self.hi is None):
                raise ReqError(f"{self.name}: {t.value} needs lo/hi or bounds_from")
            if self.lo is not None and self.hi is not None and self.lo > self.hi:
                raise ReqError(f"{self.name}: lo > hi")
        elif t in (VarType.DISCRETE, VarType.CATEGORICAL):
            if self.values_from is None and not self.values:
                raise ReqError(f"{self.name}: {t.value} needs values or values_from")
        elif t is VarType.DERIVED and self.compute is None:
            raise ReqError(f"{self.name}: DERIVED needs compute()")

    # ---- dependency-aware accessors ---------------------------------
    def is_active(self, values: dict) -> bool:
        return True if self.active_if is None else bool(self.active_if(values))

    def bounds(self, values: dict) -> tuple[float, float]:
        if self.bounds_from is not None:
            lo, hi = self.bounds_from(values)
            return float(lo), float(hi)
        return float(self.lo), float(self.hi)

    def choices(self, values: dict) -> list:
        if self.values_from is not None:
            out = list(self.values_from(values))
            if not out:
                raise ReqError(f"{self.name}: values_from returned no choices")
            return out
        return list(self.values)

    def quantize(self, v: float, values: dict) -> float:
        """Snap to the variable's resolution. Manufacturing has resolution;
        pretending a wall thickness is 3.14159265 mm is fake precision."""
        lo, hi = self.bounds(values)
        v = min(max(float(v), lo), hi)
        if self.type is VarType.INTEGER:
            return int(round(v))
        if self.step:
            v = lo + round((v - lo) / self.step) * self.step
            v = min(max(v, lo), hi)
            # kill float dust so the digest is stable
            return round(v, 9)
        return round(v, 9)

    def sample(self, rng: random.Random, values: dict) -> Any:
        if self.type in (VarType.DISCRETE, VarType.CATEGORICAL):
            return rng.choice(self.choices(values))
        lo, hi = self.bounds(values)
        if self.type is VarType.INTEGER:
            return rng.randint(int(math.ceil(lo)), int(math.floor(hi)))
        return self.quantize(rng.uniform(lo, hi), values)

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type.value, "units": self.units,
                "lo": self.lo, "hi": self.hi, "step": self.step,
                "values": self.values, "path": self.path,
                "dependent": bool(self.bounds_from or self.values_from or self.active_if),
                "description": self.description}


@dataclass
class FeasibilityRule:
    """A microsecond-cost relationship between variables.

    This is the cheapest filter in the whole system and it runs before any
    geometry is built. `check(values) -> True` means acceptable.
    """
    name: str
    check: Callable[[dict], bool]
    message: str = ""

    def violated_by(self, values: dict) -> bool:
        try:
            return not bool(self.check(values))
        except Exception:
            # A rule that cannot be evaluated is not a pass. Same philosophy
            # as an unknown constraint: silence is the dangerous outcome.
            return True


@dataclass
class DesignSpace:
    """Ordered variables + dependency resolution + cheap feasibility rules."""
    name: str
    variables: list[DesignVariable] = field(default_factory=list)
    rules: list[FeasibilityRule] = field(default_factory=list)
    version: int = 1

    def __post_init__(self):
        names = [v.name for v in self.variables]
        if len(names) != len(set(names)):
            raise ReqError("duplicate design variable name")
        if not self.variables:
            raise ReqError("a DesignSpace needs at least one variable")

    def __len__(self) -> int:
        return len(self.variables)

    def by_name(self, name: str) -> DesignVariable:
        for v in self.variables:
            if v.name == name:
                return v
        raise KeyError(name)

    @property
    def searchable(self) -> list[DesignVariable]:
        return [v for v in self.variables if v.type is not VarType.DERIVED]

    def resolve(self, partial: dict) -> dict:
        """Complete a variable assignment: drop inactive variables, quantize
        numerics, then evaluate derived values in declaration order.

        Dropping inactive variables is what keeps the cache honest — two
        candidates that differ only in a variable neither of them uses are the
        same design and must produce the same digest.
        """
        values: dict = {}
        for var in self.variables:
            if var.type is VarType.DERIVED:
                continue
            if not var.is_active(values):
                continue
            if var.name in partial:
                v = partial[var.name]
                if var.type in (VarType.CONTINUOUS, VarType.INTEGER):
                    v = var.quantize(v, values)
                elif var.type in (VarType.DISCRETE, VarType.CATEGORICAL):
                    allowed = var.choices(values)
                    if v not in allowed:
                        # a dependency change can invalidate a previously legal
                        # choice (e.g. process changed); repair rather than crash
                        v = allowed[0]
                values[var.name] = v
            elif var.default is not None:
                values[var.name] = var.default
            else:
                raise ReqError(
                    f"no value supplied for active variable {var.name!r} and "
                    f"it has no default")
        for var in self.variables:
            if var.type is VarType.DERIVED and var.is_active(values):
                values[var.name] = var.compute(values)
        return values

    def sample(self, rng: random.Random) -> dict:
        """One dependency-respecting random assignment."""
        values: dict = {}
        for var in self.variables:
            if var.type is VarType.DERIVED:
                continue
            if not var.is_active(values):
                continue
            values[var.name] = var.sample(rng, values)
        for var in self.variables:
            if var.type is VarType.DERIVED and var.is_active(values):
                values[var.name] = var.compute(values)
        return values

    def violations(self, values: dict) -> list[str]:
        """Names of violated cheap feasibility rules. Empty == acceptable."""
        return [r.name for r in self.rules if r.violated_by(values)]

    def is_feasible(self, values: dict) -> bool:
        return not self.violations(values)

    def grid(self, per_dim: int = 3) -> Iterable[dict]:
        """Deterministic coarse grid, for baselines and reproducible tests.

        Only meaningful for spaces without active_if dependencies; dependent
        variables are resolved after the cartesian product, which may collapse
        some points onto each other. Callers should de-duplicate on digest.
        """
        axes = []
        for var in self.searchable:
            if var.type in (VarType.DISCRETE, VarType.CATEGORICAL):
                axes.append([(var.name, v) for v in var.choices({})])
            else:
                lo, hi = var.bounds({})
                if per_dim == 1:
                    pts = [(lo + hi) / 2.0]
                else:
                    pts = [lo + i * (hi - lo) / (per_dim - 1) for i in range(per_dim)]
                axes.append([(var.name, var.quantize(p, {})) for p in pts])
        for combo in itertools.product(*axes):
            yield dict(combo)

    def to_spec_changes(self, values: dict) -> dict:
        """Variable values -> dot-path changes for geometry.apply_changes.

        Only variables that declare a `path` participate; the rest are
        consumed by the evaluator (material choice, process choice, …).
        """
        out = {}
        for var in self.variables:
            if var.path and var.name in values:
                out[var.path] = values[var.name]
        return out

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version,
                "variables": [v.to_dict() for v in self.variables],
                "rules": [r.name for r in self.rules]}

    def digest(self) -> str:
        return digest_of(self.to_dict())


def values_digest(values: dict) -> str:
    """Identity of a variable assignment. Floats are rounded to kill dust so
    that arithmetically identical designs share a cache entry."""
    norm = {k: (round(v, 9) if isinstance(v, float) else v)
            for k, v in sorted(values.items())}
    return digest_of(norm)
