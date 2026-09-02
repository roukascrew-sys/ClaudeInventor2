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

    Reported with `n` so the reader can judge it, and since 2026-09-02 with
    `p_value` as well, so the reader does not have to. "Correlation over 12
    samples is a hint; over 400 it is a finding" was the right instinct but it
    left the judgement to the eye; the two-sided p-value against the null of
    no monotone association makes the same call arithmetically. It is NOT a
    licence to read a small p as a large effect — `spearman` is the effect
    size, `p_value` only says whether this many samples could plausibly have
    produced it by chance. Rows are still ordered by |rho|, not by p.

    The coefficient itself comes from `scipy.stats.spearmanr` rather than the
    hand-rolled tie-averaging rank correlation that lived here until
    2026-09-02. Same algorithm, same numbers (`test_sensitivity_*` covers it),
    but it is somebody else's job to keep correct, and it carries the p-value
    for free.
    """
    # scipy.stats costs ~2 s to import and this runs once at the end of a run,
    # so it is paid for here rather than by every `import design_engine`.
    import warnings

    from scipy import stats
    from scipy.stats import ConstantInputWarning

    out: dict[str, list[dict]] = {}
    # A constant column is the ordinary case for a variable the search never
    # moved, not an anomaly, and it is handled below by the isfinite check.
    # Scoped with catch_warnings rather than simplefilter: this is our noise
    # to swallow, not the whole process's.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConstantInputWarning)
        for metric in metrics:
            rows = []
            for var in space.searchable:
                if var.type is VarType.CATEGORICAL:
                    continue                  # no meaningful ordering to rank
                xs, ys = [], []
                for c in history:
                    v = c.values.get(var.name)
                    m = c.result.metrics.get(metric)
                    if isinstance(v, (int, float)) and isinstance(m, (int, float)):
                        xs.append(float(v))
                        ys.append(float(m))
                if len(xs) < 3:
                    continue
                res = stats.spearmanr(xs, ys)
                rho, p = float(res.statistic), float(res.pvalue)
                # nan when either input is constant: no ranking, hence no
                # correlation to report. Silence beats reporting 0.0 as if
                # the variable had been shown not to matter.
                if not math.isfinite(rho):
                    continue
                rows.append({
                    "variable": var.name, "spearman": round(rho, 4),
                    "p_value": (round(p, 6) if math.isfinite(p) else None),
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

    `quantile(values, u) -> values` is the OPTIONAL inverse-CDF form of the
    same variation, where u is a uniform draw in (0, 1). It exists so
    `robustness` can stratify (see there). A perturbation that cannot be
    written as a one-dimensional inverse CDF — a discrete topology flip, a
    correlated pair — simply leaves it None and keeps working exactly as
    before. The two forms must describe the SAME distribution; nothing here
    can check that, so a perturbation that supplies both owns the obligation.
    """
    name: str
    apply: Callable[[dict, random.Random], dict]
    description: str = ""
    quantile: Callable[[dict, float], dict] | None = None


def tolerance_perturbation(variable: str, sigma: float,
                           description: str = "") -> Perturbation:
    """Normal perturbation on one variable, sigma in the variable's units."""
    def _apply(values: dict, rng: random.Random) -> dict:
        out = dict(values)
        if isinstance(out.get(variable), (int, float)):
            out[variable] = float(out[variable]) + rng.gauss(0.0, sigma)
        return out

    def _quantile(values: dict, u: float) -> dict:
        out = dict(values)
        if isinstance(out.get(variable), (int, float)):
            # Clamped off the open interval's ends: inv_cdf(0) is -inf, and an
            # infinite thickness is not a tolerance, it is a crash.
            u = min(max(float(u), 1e-12), 1.0 - 1e-12)
            out[variable] = (float(out[variable])
                             + sigma * statistics.NormalDist().inv_cdf(u))
        return out

    return Perturbation(f"{variable}+/-{sigma}", _apply,
                        description or f"manufacturing variation on {variable}",
                        quantile=_quantile)


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
    sampling: str = "independent_random"

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["failure_rate"] = round(self.failure_rate, 4)
        return d


def _lhs_design(d: int, n: int, seed: int):
    """An n x d Latin hypercube in (0, 1), or None if scipy cannot supply one.

    Returning None rather than raising is deliberate: stratification is an
    improvement to the sampling, not a precondition for doing the analysis at
    all, and a robustness study that refuses to run because an optional
    dependency moved would be a worse outcome than one that runs unstratified
    and says so.
    """
    try:
        from scipy.stats import qmc
    except Exception:                          # pragma: no cover - env-dependent
        return None
    return qmc.LatinHypercube(d=d, seed=seed).random(n=n)


def robustness(cand: Candidate, evaluator, perturbations: Sequence[Perturbation],
               samples: int = 24, seed: int = 0,
               max_fidelity: Fidelity | None = None,
               metrics_of_interest: Sequence[str] = (),
               stratified: bool = True) -> RobustnessResult:
    """Perturb a design and re-evaluate; report what actually happened.

    Runs at the evaluator's cheap fidelity by default. Perturbing a design 24
    times through a 290-second FEA is not analysis, it is a way to never
    finish; the caller raises the fidelity deliberately for finalists.

    The reported `failure_rate` is an OBSERVED FRACTION over `samples` draws,
    and `samples` is returned alongside it precisely so it is not mistaken for
    a reliability figure. No distribution is fitted and no confidence interval
    is invented.

    SAMPLING. When `stratified` and every perturbation exposes a `quantile`,
    the draws come from a Latin hypercube (`scipy.stats.qmc`) instead of
    independent per-variable draws, and `sampling` says which was used.

    Measured 2026-09-02 on the marginal two-tolerance test design, 60 seeds
    per point: the two agree on the mean failure fraction to within 0.01 (so
    LHS is estimating the same quantity, not a different one), while the
    standard deviation of the estimate falls from 0.1013 to 0.0445 at n=24 —
    a factor of 0.44, holding at 0.43-0.50 across n=16, 24 and 40. Matching
    LHS's precision with independent draws would take about 5x the
    evaluations, which at FEA fidelity is the difference between a study and
    an afternoon.

    The catch, stated because it is easy to miss: LHS draws are NOT
    independent. The failure fraction it returns is a better estimate of the
    same quantity, but a binomial confidence interval — already not computed
    here — would be doubly wrong on it.

    If any perturbation lacks a
    `quantile`, or scipy is unavailable, this silently falls back to the
    independent draws and records `sampling="independent_random"`; the caller
    does not have to care, but can check.
    """
    from .candidate import Candidate as _C
    rng = random.Random(seed)
    fid = max_fidelity if max_fidelity is not None else Fidelity.L1_GEOMETRY

    design = None
    if stratified and perturbations and all(p.quantile for p in perturbations):
        design = _lhs_design(len(perturbations), samples, seed)
    sampling = "latin_hypercube" if design is not None else "independent_random"

    collected: dict[str, list[float]] = defaultdict(list)
    classes: dict[str, int] = defaultdict(int)
    failures = 0
    evaluated = 0
    for i in range(samples):
        vals = dict(cand.values)
        if design is not None:
            for j, p in enumerate(perturbations):
                vals = p.quantile(vals, float(design[i][j]))
        else:
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
        perturbations=[p.name for p in perturbations], fidelity=int(fid),
        sampling=sampling)
