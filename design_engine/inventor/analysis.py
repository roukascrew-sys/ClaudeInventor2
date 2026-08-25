"""Failure memory, sensitivity analysis, and robustness.

Three jobs that all turn evaluation history into decision-relevant knowledge,
kept inspectable rather than hidden in a fitted model:

  FailureMemory   which regions of the design space produce which failure
                  modes, learned empirically from evaluated candidates rather
                  than hard-coded per part type.
  sensitivity     which variables actually move which metrics, so effort is
                  not spent optimising parameters that do not matter.
  robustness      whether a design survives perturbation, so a fragile
                  nominal optimum is distinguishable from a genuinely good one.

Everything here refuses to over-claim. `FailureMemory.suspects` returns a
ranked list of variables, not a probability. `robustness` reports an observed
failure fraction over a stated, finite number of samples and says how many —
it does not manufacture a reliability figure the evidence cannot support.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Sequence

from .candidate import Candidate, FailureClass, Fidelity
from .requirements import RequirementSet, Status
from .space import DesignSpace, VarType


class FailureMemory:
    """Empirical association between variable values and failure classes.

    Only TRUSTWORTHY failures are learned from. A mesh refusal or a
    constraint-corner artifact says something about the model, not about the
    design, and letting it shape the search would carve real designs out of
    the space for no reason.

    Kept deliberately simple and inspectable: for each (failure class,
    variable) it stores the mean value among failing candidates and among
    non-failing ones. The gap between those, normalised by the variable's
    spread, is the evidence that a variable is implicated. That is a
    statement a human can check, unlike a black-box importance score.
    """

    def __init__(self, space: DesignSpace, min_observations: int = 6):
        self.space = space
        self.min_observations = min_observations
        self._fail: dict[FailureClass, dict[str, list[float]]] = defaultdict(
            lambda: defaultdict(list))
        self._ok: dict[str, list[float]] = defaultdict(list)
        self.counts: dict[FailureClass, int] = defaultdict(int)
        self.discarded_untrustworthy = 0

    @staticmethod
    def _numeric(values: dict, name: str) -> float | None:
        v = values.get(name)
        return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

    def observe(self, cand: Candidate) -> None:
        classes = set()
        for f in cand.result.failures:
            if not f.trustworthy:
                self.discarded_untrustworthy += 1
                continue
            classes.add(f.failure_class)
        for cls in classes:
            self.counts[cls] += 1
            for var in self.space.searchable:
                v = self._numeric(cand.values, var.name)
                if v is not None:
                    self._fail[cls][var.name].append(v)
        if not classes:
            for var in self.space.searchable:
                v = self._numeric(cand.values, var.name)
                if v is not None:
                    self._ok[var.name].append(v)

    def evidence(self, cls: FailureClass) -> list[tuple[str, float, dict]]:
        """Ranked (variable, effect_size, detail) for one failure class."""
        out = []
        for name, fails in self._fail.get(cls, {}).items():
            oks = self._ok.get(name, [])
            if len(fails) < self.min_observations or len(oks) < self.min_observations:
                continue
            mf, mo = statistics.fmean(fails), statistics.fmean(oks)
            pooled = statistics.pstdev(fails + oks)
            if pooled <= 0:
                continue
            effect = (mf - mo) / pooled          # signed standardised difference
            out.append((name, effect, {
                "mean_when_failing": round(mf, 6),
                "mean_when_ok": round(mo, 6),
                "n_failing": len(fails), "n_ok": len(oks),
                "direction": "lower is safer" if effect > 0 else "higher is safer"}))
        out.sort(key=lambda t: -abs(t[1]))
        return out

    def suspects(self, cand: Candidate, top_k: int = 2) -> list[str]:
        """Variables most implicated in this candidate's failure classes."""
        names: list[str] = []
        for f in cand.result.failures:
            if not f.trustworthy:
                continue
            for name, effect, _ in self.evidence(f.failure_class)[:top_k]:
                if abs(effect) >= 0.4:           # ignore negligible associations
                    names.append(name)
        return names

    def report(self) -> dict:
        return {
            "observations_by_class": {k.value: v for k, v in self.counts.items()},
            "discarded_untrustworthy": self.discarded_untrustworthy,
            "evidence": {
                cls.value: [{"variable": n, "effect_size": round(e, 3), **d}
                            for n, e, d in self.evidence(cls)]
                for cls in self.counts},
        }


def sensitivity(history: Sequence[Candidate], space: DesignSpace,
                metrics: Sequence[str]) -> dict:
    """Rank-correlation of each variable against each metric.

    Spearman rather than Pearson because engineering responses are routinely
    monotonic but not linear (section modulus goes as h^2, buckling as h^3),
    and a linear coefficient would understate a strong monotone effect.

    Reported with `n` so the reader can judge it. Correlation over 12 samples
    is a hint; over 400 it is a finding.
    """
    def rank(xs):
        order = sorted(range(len(xs)), key=lambda i: xs[i])
        r = [0.0] * len(xs)
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
                j += 1
            avg = (i + j) / 2.0 + 1.0
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    def spearman(xs, ys):
        if len(xs) < 3:
            return None
        rx, ry = rank(xs), rank(ys)
        mx, my = statistics.fmean(rx), statistics.fmean(ry)
        num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
        den = math.sqrt(sum((a - mx) ** 2 for a in rx)
                        * sum((b - my) ** 2 for b in ry))
        return None if den == 0 else num / den

    out: dict[str, list[dict]] = {}
    for metric in metrics:
        rows = []
        for var in space.searchable:
            if var.type is VarType.CATEGORICAL:
                continue                      # no meaningful ordering to rank
            xs, ys = [], []
            for c in history:
                v = c.values.get(var.name)
                m = c.result.metrics.get(metric)
                if isinstance(v, (int, float)) and isinstance(m, (int, float)):
                    xs.append(float(v))
                    ys.append(float(m))
            rho = spearman(xs, ys)
            if rho is not None:
                rows.append({"variable": var.name, "spearman": round(rho, 4),
                             "n": len(xs), "units": var.units})
        rows.sort(key=lambda r: -abs(r["spearman"]))
        out[metric] = rows
    return out


@dataclass
class Perturbation:
    """One source of real-world variation.

    `apply(values, rng) -> values` so a perturbation can act on any variable
    type; nothing here assumes the design is a beam or that variation is
    Gaussian in a single dimension.
    """
    name: str
    apply: Callable[[dict, random.Random], dict]
    description: str = ""


def tolerance_perturbation(variable: str, sigma: float,
                           description: str = "") -> Perturbation:
    """Normal perturbation on one variable, sigma in the variable's units."""
    def _apply(values: dict, rng: random.Random) -> dict:
        out = dict(values)
        if isinstance(out.get(variable), (int, float)):
            out[variable] = float(out[variable]) + rng.gauss(0.0, sigma)
        return out
    return Perturbation(f"{variable}±{sigma}", _apply,
                        description or f"manufacturing variation on {variable}")


@dataclass
class RobustnessResult:
    samples: int
    nominal_feasible: bool
    failure_rate: float
    failing_classes: dict
    metric_stats: dict
    worst_case: dict
    perturbations: list[str]
    fidelity: int

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["failure_rate"] = round(self.failure_rate, 4)
        return d


def robustness(cand: Candidate, evaluator, perturbations: Sequence[Perturbation],
               samples: int = 24, seed: int = 0,
               max_fidelity: Fidelity | None = None,
               metrics_of_interest: Sequence[str] = ()) -> RobustnessResult:
    """Perturb a design and re-evaluate; report what actually happened.

    Runs at the evaluator's cheap fidelity by default. Perturbing a design 24
    times through a 290-second FEA is not analysis, it is a way to never
    finish; the caller raises the fidelity deliberately for finalists.

    The reported `failure_rate` is an OBSERVED FRACTION over `samples` draws,
    and `samples` is returned alongside it precisely so it is not mistaken for
    a reliability figure. No distribution is fitted and no confidence interval
    is invented.
    """
    from .candidate import Candidate as _C
    rng = random.Random(seed)
    fid = max_fidelity if max_fidelity is not None else Fidelity.L1_GEOMETRY

    collected: dict[str, list[float]] = defaultdict(list)
    classes: dict[str, int] = defaultdict(int)
    failures = 0
    evaluated = 0
    for i in range(samples):
        vals = dict(cand.values)
        for p in perturbations:
            vals = p.apply(vals, rng)
        try:
            vals = evaluator.ctx.space.resolve(vals)
        except Exception:
            continue
        probe = _C(values=vals, space_digest=cand.space_digest,
                   parent_id=cand.candidate_id, generation=cand.generation,
                   operator="perturb", reason=f"robustness sample {i}")
        evaluator.evaluate(probe, max_fidelity=fid)
        evaluated += 1
        if not probe.feasible:
            failures += 1
            for f in probe.result.failures:
                classes[f.failure_class.value] += 1
        for m in metrics_of_interest:
            v = probe.result.metrics.get(m)
            if isinstance(v, (int, float)):
                collected[m].append(float(v))

    stats, worst = {}, {}
    for m, xs in collected.items():
        if not xs:
            continue
        stats[m] = {"mean": round(statistics.fmean(xs), 6),
                    "stdev": round(statistics.pstdev(xs), 6) if len(xs) > 1 else 0.0,
                    "min": round(min(xs), 6), "max": round(max(xs), 6),
                    "n": len(xs)}
        worst[m] = round(min(xs), 6)

    return RobustnessResult(
        samples=evaluated,
        nominal_feasible=cand.feasible,
        failure_rate=(failures / evaluated) if evaluated else float("nan"),
        failing_classes=dict(classes), metric_stats=stats, worst_case=worst,
        perturbations=[p.name for p in perturbations], fidelity=int(fid))
