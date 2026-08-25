"""Explainability — why this design, and what it costs you.

A recommendation that says "candidate #4821, score 94.7" is useless to an
engineer. This module renders the actual reasons: which constraints it
satisfies and by how much, which objectives it dominates, what it trades away
against the alternatives on the frontier, what evidence backs each number,
and what remains unknown.

The rule enforced here is that **every number is reported with the fidelity
that produced it**. A safety factor from beam theory and one from a converged
FEA both print as "SF", so the fidelity tag is the only thing preventing a
reader from over-trusting a screening estimate. Nothing in this module
invents a confidence value; it reports evidence and its limits.
"""

from __future__ import annotations

from typing import Sequence

from .analysis import RobustnessResult
from .candidate import Candidate, Fidelity
from .pareto import pareto_front
from .requirements import Objective, RequirementSet, Sense, Status


def _fmt(v, unit: str = "") -> str:
    if v is None:
        return "UNKNOWN"
    if isinstance(v, float):
        if v != v:                       # NaN
            return "UNKNOWN"
        if v == float("inf"):
            return "inf"
        s = f"{v:.4g}"
    else:
        s = str(v)
    return f"{s} {unit}".strip()


def explain_candidate(cand: Candidate, reqs: RequirementSet,
                      front: Sequence[Candidate] = (),
                      role: str = "", robustness: RobustnessResult | None = None
                      ) -> dict:
    """Structured explanation. `render_text` turns this into prose."""
    res = cand.result
    objectives = []
    for o in reqs.objectives:
        v = res.metrics.get(o.metric)
        fid = res.metric_fidelity.get(o.metric)
        better = worse = 0
        for other in front:
            ov = other.result.metrics.get(o.metric)
            if ov is None or v is None or other is cand:
                continue
            if o.loss(ov) < o.loss(v):
                better += 1
            elif o.loss(ov) > o.loss(v):
                worse += 1
        objectives.append({
            "name": o.name, "metric": o.metric, "value": v, "units": o.units,
            "sense": o.sense.value,
            "fidelity": fid.label if fid is not None else "not evaluated",
            "beaten_by_on_front": better, "beats_on_front": worse,
        })

    constraints = []
    for r in res.constraint_results:
        constraints.append({
            "name": r.constraint.name, "status": r.status.value,
            "actual": r.actual, "required": r.constraint.bound,
            "op": r.constraint.op.value, "units": r.constraint.units,
            "normalized_margin": r.normalized_margin,
            "severity": r.constraint.severity,
            "source": r.constraint.source,
            "fidelity": (res.metric_fidelity.get(r.constraint.metric).label
                         if res.metric_fidelity.get(r.constraint.metric) is not None
                         else "not evaluated"),
            "note": r.note,
        })

    binding = sorted(
        [c for c in constraints
         if c["status"] == Status.VALID.value and c["normalized_margin"] is not None],
        key=lambda c: c["normalized_margin"])[:3]

    unknowns = [c["name"] for c in constraints
                if c["status"] in (Status.UNKNOWN.value, Status.NOT_EVALUATED.value)]

    return {
        "candidate_id": cand.candidate_id, "role": role,
        "generation": cand.generation, "operator": cand.operator,
        "parent_id": cand.parent_id, "reason": cand.reason,
        "geometry_id": cand.geometry_id, "spec_digest": cand.spec_digest,
        "status": res.status.value,
        "evidence_fidelity": {"lowest": res.fidelity.label,
                              "highest": res.max_fidelity.label},
        "variables": cand.values,
        "objectives": objectives,
        "constraints": constraints,
        "binding_constraints": binding,
        "unknown_constraints": unknowns,
        "failures": [f.to_dict() for f in res.failures],
        "warnings": res.warnings,
        "validations_performed": [
            {"stage": s.stage, "fidelity": s.fidelity.label,
             "status": s.status.value, "seconds": round(s.seconds, 3),
             "cached": s.cached} for s in res.stages],
        "robustness": robustness.to_dict() if robustness else None,
    }


def compare(a: Candidate, b: Candidate, reqs: RequirementSet) -> list[dict]:
    """Objective-by-objective trade between two candidates."""
    out = []
    for o in reqs.objectives:
        va, vb = a.result.metrics.get(o.metric), b.result.metrics.get(o.metric)
        if va is None or vb is None:
            out.append({"objective": o.name, "delta_pct": None,
                        "note": "not comparable, a value is missing"})
            continue
        better = o.loss(va) < o.loss(vb)
        delta = (va - vb)
        pct = (delta / vb * 100.0) if vb not in (0, None) else None
        out.append({"objective": o.name, "a": va, "b": vb, "units": o.units,
                    "a_is_better": better,
                    "delta": delta,
                    "delta_pct": round(pct, 2) if pct is not None else None})
    return out


def render_text(exp: dict, reqs: RequirementSet, alternatives: dict | None = None,
                sensitivity: dict | None = None) -> str:
    """Human-readable explanation."""
    L: list[str] = []
    head = f"RECOMMENDED DESIGN - {exp['candidate_id']}"
    if exp.get("role"):
        head += f"  [{exp['role']}]"
    L.append(head)
    L.append("=" * len(head))
    L.append(f"status: {exp['status'].upper()}   evidence: "
             f"{exp['evidence_fidelity']['lowest']} -> "
             f"{exp['evidence_fidelity']['highest']}")
    if exp.get("geometry_id"):
        L.append(f"materialised as {exp['geometry_id']} "
                 f"(spec digest {exp['spec_digest']})")

    L.append("\nDESIGN VARIABLES")
    for k, v in exp["variables"].items():
        L.append(f"  {k:28s} {_fmt(v)}")

    L.append("\nOBJECTIVES  (every value tagged with the model that produced it)")
    for o in exp["objectives"]:
        L.append(f"  {o['name']:20s} {_fmt(o['value'], o['units']):>18s}   "
                 f"[{o['fidelity']}]")
        if o["beaten_by_on_front"] or o["beats_on_front"]:
            L.append(f"    {' ':20s} beats {o['beats_on_front']} and is beaten by "
                     f"{o['beaten_by_on_front']} others on the frontier")

    L.append("\nCONSTRAINTS")
    for c in exp["constraints"]:
        mark = {"valid": "PASS", "invalid": "FAIL",
                "unknown": "UNKNOWN", "not_evaluated": "NOT RUN"}[c["status"]]
        req = f"{c['op']} {_fmt(c['required'], c['units'])}"
        L.append(f"  [{mark:7s}] {c['name']:26s} {_fmt(c['actual'], c['units']):>14s} "
                 f"vs {req:>16s}  [{c['fidelity']}]")
        if c["source"]:
            L.append(f"            source: {c['source']}")

    if exp["binding_constraints"]:
        L.append("\nWHAT IS ACTUALLY LIMITING THIS DESIGN")
        for c in exp["binding_constraints"]:
            L.append(f"  {c['name']} - normalised margin "
                     f"{c['normalized_margin']:+.3f} (tightest first)")

    if alternatives:
        L.append("\nTRADEOFFS AGAINST THE ALTERNATIVES")
        for role, rows in alternatives.items():
            parts = []
            for r in rows:
                if r.get("delta_pct") is None:
                    continue
                direction = "better" if r["a_is_better"] else "worse"
                parts.append(f"{r['objective']} {abs(r['delta_pct']):.1f}% {direction}")
            if parts:
                L.append(f"  vs {role}: " + ", ".join(parts))

    if exp.get("robustness"):
        rb = exp["robustness"]
        L.append("\nROBUSTNESS")
        L.append(f"  perturbations: {', '.join(rb['perturbations'])}")
        L.append(f"  {rb['samples']} perturbed samples at "
                 f"fidelity L{rb['fidelity']}; observed failure fraction "
                 f"{rb['failure_rate']:.3f}")
        L.append("  (an observed fraction over a finite sample - NOT a "
                 "reliability figure; no distribution was fitted)")
        for m, s in rb.get("metric_stats", {}).items():
            L.append(f"    {m}: mean {s['mean']:.4g}, worst {s['min']:.4g}, "
                     f"sd {s['stdev']:.4g} (n={s['n']})")

    if sensitivity:
        L.append("\nWHICH VARIABLES ACTUALLY MATTER  (Spearman rank correlation)")
        for metric, rows in sensitivity.items():
            top = [r for r in rows if abs(r["spearman"]) >= 0.2][:4]
            if top:
                L.append(f"  {metric}:")
                for r in top:
                    L.append(f"    {r['variable']:24s} rho={r['spearman']:+.2f} "
                             f"(n={r['n']})")

    L.append("\nVALIDATION PERFORMED")
    for v in exp["validations_performed"]:
        c = " (cached)" if v["cached"] else ""
        L.append(f"  {v['stage']:22s} {v['fidelity']:20s} {v['status']:8s} "
                 f"{v['seconds']:.3f}s{c}")

    weak = []
    if exp["unknown_constraints"]:
        weak.append(f"constraints not established: {', '.join(exp['unknown_constraints'])}")
    if exp["evidence_fidelity"]["highest"] in ("analytic", "geometry"):
        weak.append("no solver-grade validation was run on this candidate; "
                    "the safety numbers are model estimates")
    for w in exp.get("warnings", []):
        weak.append(w)
    L.append("\nKNOWN WEAKNESSES AND REMAINING UNCERTAINTY")
    if weak:
        for w in weak:
            L.append(f"  - {w}")
    else:
        L.append("  - none recorded beyond the stated model assumptions")

    return "\n".join(L)


def render_run(run, top: int = 3, sensitivity_metrics=None) -> str:
    """Full report for an OptimizationRun."""
    reqs = run.requirements
    front = run.front()
    arch = run.archetypes()
    sens = run.sensitivity(sensitivity_metrics)
    s = run.summary()

    L = ["OPTIMISATION RUN SUMMARY", "=" * 24,
         f"optimizer          {s['optimizer']}",
         f"generations        {s['generations']}",
         f"evaluations        {s['evaluations']}",
         f"unique candidates  {s['unique_candidates']}",
         f"feasible           {s['feasible']}",
         f"infeasible         {s['infeasible']}",
         f"unknown            {s['unknown']}",
         f"pareto frontier    {s['pareto_size']}",
         f"promoted to FEA    {s['promoted']}",
         f"wall time          {s['wall_seconds']} s",
         f"cache hit rate     {s['cost']['cache']['hit_rate']:.1%} "
         f"({s['cost']['cache']['hits']} hits / {s['cost']['cache']['misses']} misses)",
         f"requirements       {s['requirements_digest']}",
         f"design space       {s['space_digest']}"]

    L.append("\nEVALUATION COST BY STAGE")
    for name, secs in s["cost"]["stage_seconds"].items():
        runs = s["cost"]["stage_runs"].get(name, 0)
        per = (secs / runs) if runs else 0.0
        L.append(f"  {name:22s} {runs:6d} runs  {secs:9.2f} s  "
                 f"({per*1000:.2f} ms each)")

    if arch:
        L.append("\nFRONTIER ARCHETYPES")
        for role, cand in arch.items():
            bits = []
            for o in reqs.objectives:
                bits.append(f"{o.name}={_fmt(cand.result.metrics.get(o.metric), o.units)}")
            L.append(f"  {role:22s} {cand.candidate_id}  " + "  ".join(bits))

    for role, cand in list(arch.items())[:top]:
        alts = {r: compare(cand, other, reqs)
                for r, other in arch.items() if other is not cand}
        exp = explain_candidate(cand, reqs, front, role=role)
        L.append("\n" + "-" * 70 + "\n")
        L.append(render_text(exp, reqs, alternatives=alts, sensitivity=sens))

    return "\n".join(L)
