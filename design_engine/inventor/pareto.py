"""Pareto dominance, frontier extraction, and archetype selection.

Dominance is computed on the objective LOSS vector (every objective already
flipped to "smaller is better" by `Objective.loss`). Nothing here collapses
objectives into a scalar; the frontier is the output, not a winner.

Feasibility is handled before dominance, not inside it. An infeasible
candidate never dominates a feasible one regardless of its objective values —
that is Principle 2 (constraints are not preferences) again. Candidates whose
objective vector is incomplete are excluded entirely rather than being
compared on partial information.
"""

from __future__ import annotations

from typing import Sequence

from .candidate import Candidate
from .requirements import Objective, RequirementSet, Sense, Status


def dominates(a: Sequence[float], b: Sequence[float]) -> bool:
    """True if loss-vector `a` Pareto-dominates `b`: no worse in every
    objective and strictly better in at least one."""
    if len(a) != len(b):
        raise ValueError("objective vectors must have equal length")
    no_worse = all(x <= y for x, y in zip(a, b))
    better = any(x < y for x, y in zip(a, b))
    return no_worse and better


def _vectors(cands: Sequence[Candidate], objectives: list[Objective]):
    out = []
    for c in cands:
        v = c.result.objective_vector(objectives)
        if v is not None:
            out.append((c, v))
    return out


def pareto_front(cands: Sequence[Candidate], objectives: list[Objective],
                 feasible_only: bool = True) -> list[Candidate]:
    """Non-dominated set. O(n^2), which is fine at the scale that survives to
    this point (hundreds, not hundreds of thousands)."""
    pool = [c for c in cands if c.feasible] if feasible_only else list(cands)
    pairs = _vectors(pool, objectives)
    front = []
    for cand, vec in pairs:
        if not any(dominates(other, vec) for o, other in pairs if o is not cand):
            front.append(cand)
    return front


def non_dominated_sort(cands: Sequence[Candidate],
                       objectives: list[Objective]) -> list[list[Candidate]]:
    """Rank into successive fronts (NSGA-II style), used for selection."""
    remaining = list(cands)
    fronts = []
    while remaining:
        f = pareto_front(remaining, objectives, feasible_only=False)
        if not f:                      # every remaining vector is incomplete
            fronts.append(list(remaining))
            break
        fronts.append(f)
        ids = {id(c) for c in f}
        remaining = [c for c in remaining if id(c) not in ids]
    return fronts


def crowding_distance(front: Sequence[Candidate],
                      objectives: list[Objective]) -> dict[int, float]:
    """NSGA-II crowding distance — preserves spread along the frontier so the
    search does not collapse onto one corner of it."""
    pairs = _vectors(front, objectives)
    dist = {id(c): 0.0 for c, _ in pairs}
    n = len(pairs)
    if n == 0:
        return dist
    for m in range(len(objectives)):
        pairs.sort(key=lambda p: p[1][m])
        dist[id(pairs[0][0])] = float("inf")
        dist[id(pairs[-1][0])] = float("inf")
        lo, hi = pairs[0][1][m], pairs[-1][1][m]
        span = hi - lo
        if span <= 0:
            continue
        for i in range(1, n - 1):
            prev_v, next_v = pairs[i - 1][1][m], pairs[i + 1][1][m]
            dist[id(pairs[i][0])] += (next_v - prev_v) / span
    return dist


def rank_key(cands: Sequence[Candidate], objectives: list[Objective]):
    """(front_index, -crowding) sort key for selection."""
    fronts = non_dominated_sort(cands, objectives)
    key: dict[int, tuple[int, float]] = {}
    for fi, front in enumerate(fronts):
        cd = crowding_distance(front, objectives)
        for c in front:
            key[id(c)] = (fi, -cd.get(id(c), 0.0))
    return key


def _pref_score(cand: Candidate, reqs: RequirementSet) -> float:
    """Soft preference score, used ONLY to break ties among equals."""
    total = 0.0
    for p in reqs.preferences:
        v = cand.result.metrics.get(p.metric)
        if v is None:
            continue
        total += p.weight * (-float(v) if p.sense is Sense.MIN else float(v))
    return total


def archetypes(front: Sequence[Candidate], reqs: RequirementSet) -> dict:
    """Name meaningful alternatives DERIVED from the actual frontier.

    Deliberately not a fixed menu of hard-coded categories: each archetype is
    only emitted when the frontier actually contains a candidate that earns
    it. A single-objective problem has no "best balance", and pretending
    otherwise would be theatre.
    """
    front = [c for c in front if c.feasible]
    if not front:
        return {}
    objectives = reqs.objectives
    out: dict[str, Candidate] = {}

    # extreme of each objective, named for the objective itself
    for o in objectives:
        vals = [(c, c.result.metrics.get(o.metric)) for c in front]
        vals = [(c, v) for c, v in vals if v is not None]
        if not vals:
            continue
        best = min(vals, key=lambda p: o.loss(p[1]))[0]
        out[f"best_{o.name}"] = best

    # balanced: minimum normalised distance to the ideal point
    if len(objectives) > 1:
        vecs = _vectors(front, objectives)
        if vecs:
            cols = list(zip(*[v for _, v in vecs]))
            lo = [min(c) for c in cols]
            hi = [max(c) for c in cols]
            def norm_dist(v):
                s = 0.0
                for i, x in enumerate(v):
                    span = hi[i] - lo[i]
                    s += 0.0 if span <= 0 else ((x - lo[i]) / span) ** 2
                return s ** 0.5
            out["balanced"] = min(vecs, key=lambda p: norm_dist(p[1]))[0]

    # most robust / most constrained-safe, only when the evidence exists
    robust = [c for c in front if c.result.metrics.get("robust_failure_rate") is not None]
    if robust:
        out["most_robust"] = min(
            robust, key=lambda c: c.result.metrics["robust_failure_rate"])

    safest = []
    for c in front:
        margins = [r.normalized_margin for r in c.result.constraint_results
                   if r.constraint.severity == "mandatory"
                   and r.normalized_margin is not None]
        if margins:
            safest.append((c, min(margins)))
    if safest:
        out["largest_margin"] = max(safest, key=lambda p: p[1])[0]

    if reqs.preferences:
        out["most_preferred"] = max(front, key=lambda c: _pref_score(c, reqs))

    return out


def hypervolume(front: Sequence[Candidate], objectives: list[Objective],
                reference: Sequence[float] | None = None) -> float | None:
    """Dominated hypervolume in LOSS space (larger is better).

    Exact for two objectives via a sweep. For three or more this returns None
    rather than an approximation, because a Monte-Carlo estimate reported
    without its error bars is precisely the kind of fake precision this
    project refuses; use `compare_fronts` instead, which works in any
    dimension.

    `reference` defaults to the componentwise worst point of the front plus a
    10% pad, so the number is only meaningful when comparing two fronts
    against the SAME reference — pass one explicitly to compare runs.
    """
    if len(objectives) != 2:
        return None
    pairs = [c.result.objective_vector(objectives) for c in front]
    pairs = [p for p in pairs if p is not None]
    if not pairs:
        return 0.0
    if reference is None:
        ref = [max(p[i] for p in pairs) for i in range(2)]
        ref = [r + 0.1 * abs(r) + 1e-9 for r in ref]
    else:
        ref = list(reference)
    pts = sorted({(p[0], p[1]) for p in pairs})
    hv, prev_y = 0.0, ref[1]
    for x, y in pts:
        if x >= ref[0] or y >= ref[1]:
            continue
        if y < prev_y:
            hv += (ref[0] - x) * (prev_y - y)
            prev_y = y
    return hv


def compare_fronts(a: Sequence[Candidate], b: Sequence[Candidate],
                   objectives: list[Objective]) -> dict:
    """How two Pareto fronts relate, in any number of dimensions.

    Comparing multi-objective searches on a single objective is a category
    error: an NSGA-II run deliberately spends budget spreading along the
    frontier rather than driving one axis to its extreme, so it can lose on
    "best mass" while producing a strictly better set of trade-offs. This
    counts, for each front, how many of the other's points it dominates.
    """
    va = [(c, c.result.objective_vector(objectives)) for c in a]
    vb = [(c, c.result.objective_vector(objectives)) for c in b]
    va = [(c, v) for c, v in va if v is not None]
    vb = [(c, v) for c, v in vb if v is not None]
    a_dom_b = sum(1 for _, y in vb if any(dominates(x, y) for _, x in va))
    b_dom_a = sum(1 for _, y in va if any(dominates(x, y) for _, x in vb))
    return {"a_size": len(va), "b_size": len(vb),
            "a_points_dominated_by_b": b_dom_a,
            "b_points_dominated_by_a": a_dom_b,
            "a_dominated_fraction": (b_dom_a / len(va)) if va else None,
            "b_dominated_fraction": (a_dom_b / len(vb)) if vb else None}
