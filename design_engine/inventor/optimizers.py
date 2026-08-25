"""Optimisers: propose candidates, never judge them.

An Optimizer's entire contract is `ask()` -> candidates and `tell()` <- their
evaluated results. It never calls a solver, never decides feasibility, and
never overrides the engine. That separation is what keeps the deterministic
engineering layer authoritative.

Two implementations, in the order the brief asks for: a solid baseline first,
sophistication only where it earns its place.

  RandomSearch      dependency-aware uniform sampling. The honest baseline
                    every other optimiser must beat before it is worth having.
  EvolutionarySearch NSGA-II-style multi-objective GA with mixed-variable
                    operators, constraint-domination, and failure-informed
                    mutation.

Constraint handling uses **constrained domination** (Deb): a feasible
candidate always beats an infeasible one; two infeasible candidates are
compared on total normalised violation. A low mass never buys forgiveness for
a violated safety factor.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Sequence

from .candidate import Candidate, Fidelity
from .pareto import crowding_distance, non_dominated_sort
from .requirements import RequirementSet, Status
from .space import DesignSpace, VarType, values_digest


def total_violation(cand: Candidate) -> float:
    """Sum of normalised mandatory-constraint violations. 0.0 == feasible.

    UNKNOWN counts as a violation of 1.0 rather than 0: a design whose safety
    gate could not be evaluated must not out-rank one that demonstrably
    passes.
    """
    total = 0.0
    for r in cand.result.constraint_results:
        if r.constraint.severity != "mandatory":
            continue
        if r.status is Status.INVALID and r.normalized_margin is not None:
            total += abs(min(0.0, r.normalized_margin))
        elif r.status in (Status.UNKNOWN, Status.NOT_EVALUATED):
            total += 1.0
    return total


@dataclass
class OptimizationConfig:
    population: int = 32
    generations: int = 10
    seed: int = 0
    crossover_rate: float = 0.9
    mutation_rate: float | None = None      # default 1/n_vars
    mutation_sigma: float = 0.15            # as a fraction of each range
    elitism: int = 2
    screen_fidelity: Fidelity = Fidelity.L1_GEOMETRY
    promote_fidelity: Fidelity = Fidelity.L3_HIGH_FEA
    promote_top_k: int = 0                  # 0 disables promotion
    workers: int = 1
    max_evaluations: int | None = None
    failure_informed: bool = True

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["screen_fidelity"] = int(self.screen_fidelity)
        d["promote_fidelity"] = int(self.promote_fidelity)
        return d


class Optimizer:
    """Base contract. Subclasses implement ask/tell."""

    name = "base"

    def __init__(self, space: DesignSpace, requirements: RequirementSet,
                 config: OptimizationConfig | None = None):
        self.space = space
        self.requirements = requirements
        self.config = config or OptimizationConfig()
        self.rng = random.Random(self.config.seed)
        self.history: list[Candidate] = []
        self._seen: set[str] = set()
        self.generation = 0

    # -- helpers -------------------------------------------------------
    def _activate_missing(self, values: dict) -> dict:
        """Sample any variable that has just become active.

        `DesignSpace.resolve` fills these deterministically with a neutral
        midpoint, which is correct for reproducibility but useless for search:
        every design that switched a rib on would get the identical rib. The
        optimiser is allowed to be stochastic, so it samples them here, before
        resolve sees them.
        """
        out = dict(values)
        for var in self.space.variables:
            if var.type is VarType.DERIVED:
                continue
            if var.is_active(out) and var.name not in out:
                out[var.name] = var.sample(self.rng, out)
        return out

    def _fresh(self, values: dict, operator: str, reason: str,
               parent: Candidate | None = None) -> Candidate | None:
        """Build a candidate, skipping designs already seen.

        De-duplication is by content digest, so a design rediscovered by a
        different path is recognised and not re-evaluated.
        """
        values = self.space.resolve(self._activate_missing(values))
        digest = values_digest(values)
        if digest in self._seen:
            return None
        self._seen.add(digest)
        cand = Candidate(values=values, space_digest=self.space.digest(),
                         operator=operator, reason=reason,
                         parent_id=parent.candidate_id if parent else None,
                         generation=self.generation)
        return cand

    def ask(self, n: int) -> list[Candidate]:      # pragma: no cover - abstract
        raise NotImplementedError

    def tell(self, cands: Sequence[Candidate]) -> None:
        self.history.extend(cands)

    def state(self) -> dict:
        return {"optimizer": self.name, "generation": self.generation,
                "evaluated": len(self.history), "seen": len(self._seen)}


class RandomSearch(Optimizer):
    """Uniform dependency-aware sampling. The baseline to beat."""

    name = "random"

    def ask(self, n: int) -> list[Candidate]:
        out: list[Candidate] = []
        attempts = 0
        while len(out) < n and attempts < n * 50:
            attempts += 1
            cand = self._fresh(self.space.sample(self.rng), "random",
                               "uniform sample of the design space")
            if cand is not None:
                out.append(cand)
        return out


class EvolutionarySearch(Optimizer):
    """NSGA-II-style multi-objective GA over mixed variable types.

    Failure-informed mutation (enabled by default) biases which variable a
    mutation touches toward variables implicated in the parent's failure, using
    only TRUSTWORTHY failure records. A numerical artifact — a mesh refusal or
    a constraint-corner singularity — carries no information about the design
    and is explicitly not allowed to steer the search.
    """

    name = "evolutionary"

    def __init__(self, space, requirements, config=None,
                 failure_memory=None):
        super().__init__(space, requirements, config)
        self.population: list[Candidate] = []
        self.failure_memory = failure_memory

    # -- variation operators ------------------------------------------
    def _mutate_value(self, var, values: dict, current):
        if var.type is VarType.CATEGORICAL or var.type is VarType.DISCRETE:
            choices = [c for c in var.choices(values) if c != current]
            return self.rng.choice(choices) if choices else current
        lo, hi = var.bounds(values)
        span = hi - lo
        if span <= 0:
            return current
        nudged = float(current) + self.rng.gauss(0.0, self.config.mutation_sigma * span)
        return var.quantize(nudged, values)

    def _bias(self, parent: Candidate) -> list[str]:
        """Variables implicated in the parent's trustworthy failures."""
        if not self.config.failure_informed:
            return []
        names: list[str] = []
        for f in parent.result.failures:
            if not f.trustworthy:
                continue
            names.extend(f.contributing_variables)
        if self.failure_memory is not None:
            names.extend(self.failure_memory.suspects(parent))
        return [n for n in names if n in parent.values]

    def _mutate(self, parent: Candidate) -> Candidate | None:
        vals = dict(parent.values)
        searchable = [v for v in self.space.searchable if v.name in vals]
        if not searchable:
            return None
        rate = self.config.mutation_rate or (1.0 / max(1, len(searchable)))
        biased = set(self._bias(parent))
        touched = False
        for var in searchable:
            p = rate * (3.0 if var.name in biased else 1.0)
            if self.rng.random() < min(p, 0.9):
                vals[var.name] = self._mutate_value(var, vals, vals[var.name])
                touched = True
        if not touched:
            var = self.rng.choice(searchable)
            vals[var.name] = self._mutate_value(var, vals, vals[var.name])
        reason = ("failure-informed mutation targeting "
                  + ", ".join(sorted(biased))) if biased else "mutation"
        return self._fresh(vals, "mutate", reason, parent)

    def _crossover(self, a: Candidate, b: Candidate) -> Candidate | None:
        vals = {}
        for var in self.space.searchable:
            if var.name in a.values and var.name in b.values:
                vals[var.name] = (a.values if self.rng.random() < 0.5
                                  else b.values)[var.name]
            elif var.name in a.values:
                vals[var.name] = a.values[var.name]
            elif var.name in b.values:
                vals[var.name] = b.values[var.name]
        return self._fresh(vals, "crossover",
                           f"uniform crossover of {a.candidate_id} x {b.candidate_id}", a)

    # -- selection -----------------------------------------------------
    def _tournament(self, pool: Sequence[Candidate], key) -> Candidate:
        a, b = self.rng.choice(pool), self.rng.choice(pool)
        return a if self._better(a, b, key) else b

    def _better(self, a: Candidate, b: Candidate, key) -> bool:
        """Constrained domination (Deb): feasibility first, always."""
        va, vb = total_violation(a), total_violation(b)
        if (va > 0) != (vb > 0):
            return va == 0
        if va > 0 and vb > 0:
            return va < vb
        return key.get(id(a), (99, 0.0)) < key.get(id(b), (99, 0.0))

    def _rank(self, pool: Sequence[Candidate]) -> dict:
        feasible = [c for c in pool if total_violation(c) == 0]
        fronts = non_dominated_sort(feasible, self.requirements.objectives)
        key: dict[int, tuple[int, float]] = {}
        for fi, front in enumerate(fronts):
            cd = crowding_distance(front, self.requirements.objectives)
            for c in front:
                key[id(c)] = (fi, -cd.get(id(c), 0.0))
        return key

    # -- optimizer contract --------------------------------------------
    def ask(self, n: int) -> list[Candidate]:
        if not self.population:
            out, attempts = [], 0
            while len(out) < n and attempts < n * 50:
                attempts += 1
                c = self._fresh(self.space.sample(self.rng), "random",
                                "initial population")
                if c is not None:
                    out.append(c)
            return out

        self.generation += 1
        key = self._rank(self.population)
        children: list[Candidate] = []
        attempts = 0
        while len(children) < n and attempts < n * 60:
            attempts += 1
            p1 = self._tournament(self.population, key)
            child = None
            if self.rng.random() < self.config.crossover_rate and len(self.population) > 1:
                p2 = self._tournament(self.population, key)
                if p2 is not p1:
                    child = self._crossover(p1, p2)
            if child is None:
                child = self._mutate(p1)
            if child is not None:
                children.append(child)
        return children

    def tell(self, cands: Sequence[Candidate]) -> None:
        super().tell(cands)
        pool = self.population + list(cands)
        key = self._rank(pool)
        pool.sort(key=lambda c: (total_violation(c) > 0,
                                 total_violation(c),
                                 key.get(id(c), (99, 0.0))))
        self.population = pool[:self.config.population]


OPTIMIZERS = {"random": RandomSearch, "evolutionary": EvolutionarySearch}


def make_optimizer(name: str, space, requirements, config=None, **kw):
    if name not in OPTIMIZERS:
        raise ValueError(f"unknown optimizer {name!r}; have {sorted(OPTIMIZERS)}")
    return OPTIMIZERS[name](space, requirements, config, **kw)
