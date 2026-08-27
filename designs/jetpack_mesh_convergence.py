"""Mesh convergence for the headline SF 3.844 on P0047@v1.

Roadmap #1. The reported safety factor was computed at a single mesh size —
3.2 mm — and that size was never chosen on accuracy grounds. It was simply the
first rung of the ladder [5.0, 4.0, 3.2] that survived the Jacobian gate; 5.0
and 4.0 both produced non-positive Jacobians and were refused. So the headline
number has an unquantified discretisation error, and every comparison that used
it inherits that error.

This re-solves the SAME stored STEP at progressively finer meshes. Only the
discretisation changes: identical geometry, identical loads, identical
constraints, identical material.

WHAT TO EXPECT, AND WHY IT MATTERS
The peak stress in the 3.2 mm run sits at [-23.505, 4.014, 199.6]. That is not
a load patch (those are at |x| = 330 and 550) and not a constraint (the lugs
are at z = 400 and z = 40). It is the underside of the doubler pad, 1.28 mm
outboard of the spine wall at x = -22.225 — the spine/pad T-junction.

`build_spec` unions three boxes and applies no fillet anywhere, so that
junction is a sharp 90-degree re-entrant corner. Linear elasticity has no
finite stress there: for a re-entrant corner the Williams eigenvalue expansion
gives sigma ~ r**(lambda-1) with lambda ~ 0.5445 for a 270-degree material
angle, so the peak grows without bound as h -> 0 rather than converging to
anything.

    M. L. Williams, "Stress Singularities Resulting from Various Boundary
    Conditions in Angular Corners of Plates in Extension", Journal of Applied
    Mechanics 19 (1952) 526-528.

If that is what is happening here, refining the mesh cannot validate SF 3.844
and no amount of solver time will. The study is still the right thing to run,
because it is what tells the two cases apart:

    CONVERGING   successive changes in peak stress shrink toward zero
    SINGULAR     peak stress keeps climbing at a rate set by the corner, while
                 a far-field measure (p99.9) settles down

p99.9 von Mises is recorded precisely for that reason. A singularity is local
to a handful of elements; if the bulk field converges while the peak does not,
the model is sound and only the peak METRIC is unusable.

MEMORY NOTE: this machine had ~1.3 GB available and 20.9 GB committed of a
29 GB limit when this was written. The predicted 1.38M-node solve at 2.0 mm was
NOT attempted for that reason — a direct solve that pages is not a measurement,
it is a way to lose an hour. The study is capped at 2.4 mm and reports that.

    .venv\\Scripts\\python.exe designs\\jetpack_mesh_convergence.py
"""
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, '.')
from design_engine import DesignEngine
from design_engine.fea import ValidationTools
import designs.jetpack_optimization_run as J

GEOMETRY_ID = "P0047@v1"
OUT = Path("data/convergence/jetpack_P0047.json")

# 3.2 mm is the coarsest available: the ladder proved 4.0 mm degenerate on this
# geometry, so the study can only refine downward. That makes this a one-sided
# study and it is reported as one.
BASELINE_MM = 3.2
SIZES = [2.8, 2.4]

# Williams exponent for a 90-degree re-entrant corner (270-degree material
# angle) in plane elasticity. Used ONLY as a comparison curve for the observed
# growth rate, never as a source of stress values.
WILLIAMS_LAMBDA = 0.5445


def baseline_from_log(eng):
    """Reuse the logged 3.2 mm result rather than re-solving it.

    It is in the FRACAS log, which is the source of truth. Re-running it would
    cost 318 s to reproduce a number the project already has, and would tell us
    about run-to-run variance rather than about mesh size.
    """
    rows = [r for r in eng.log.rows(action="fea_static")
            if r["geometry_version"] == GEOMETRY_ID and r["result"] == "pass"]
    if not rows:
        return None
    d = json.loads(rows[-1]["details_json"])
    return {"mesh_mm": BASELINE_MM, "source": f"log action {rows[-1]['id']}",
            "nodes": d["nodes"], "elements": d["elements"],
            "max_von_mises_MPa": d["max_von_mises_MPa"],
            "p99_9_von_mises_MPa": d["p99_9_von_mises_MPa"],
            "safety_factor": d["safety_factor"],
            "allowable_MPa": d["allowable_MPa"],
            "stress_outlier_ratio": d["stress_outlier_ratio"],
            "max_at_mm": d["max_von_mises_at_mm"],
            "solve_seconds": d["solve_seconds"]}


def log_details(eng, gid):
    """Pull the full result payload for the most recent solve from the log.

    `fea_static` RETURNS the engineering answer (safety factor, stresses) but
    writes the discretisation facts — node and element counts — only to the
    FRACAS log. The log is the source of truth, so read them from there rather
    than assuming the return dict carries every field.
    """
    rows = [r for r in eng.log.rows(action="fea_static")
            if r["geometry_version"] == gid]
    return json.loads(rows[-1]["details_json"] or "{}") if rows else {}


def run_one(eng, case, mesh_mm, gid=GEOMETRY_ID):
    case = json.loads(json.dumps(case))          # deep copy; never mutate
    case["mesh"]["max_size_mm"] = mesh_mm
    print(f"\n  --- {mesh_mm} mm ---", flush=True)
    t0 = time.time()
    try:
        r = eng.validation.fea_static(
            gid, case,
            reason=f"mesh convergence study for SF 3.844, h = {mesh_mm} mm")
    except Exception as e:                        # noqa: BLE001 - report, don't hide
        print(f"      REFUSED after {time.time()-t0:.0f}s: "
              f"{type(e).__name__}: {str(e)[:220]}", flush=True)
        return {"mesh_mm": mesh_mm, "failed": True,
                "error": f"{type(e).__name__}: {str(e)[:400]}",
                "wall_seconds": round(time.time() - t0, 1)}
    d = log_details(eng, gid)
    out = {"mesh_mm": mesh_mm, "failed": False, "source": "solved now",
           "nodes": d.get("nodes"), "elements": d.get("elements"),
           "max_von_mises_MPa": r.get("max_von_mises_MPa"),
           "p99_9_von_mises_MPa": r.get("p99_9_von_mises_MPa"),
           "safety_factor": r.get("safety_factor"),
           "allowable_MPa": r.get("allowable_MPa"),
           "stress_outlier_ratio": d.get("stress_outlier_ratio"),
           "max_at_mm": r.get("max_von_mises_at_mm"),
           "solve_seconds": d.get("solve_seconds"),
           "wall_seconds": round(time.time() - t0, 1)}
    print(f"      nodes {out['nodes']:,}  maxVM {out['max_von_mises_MPa']:.3f} MPa"
          f"  p99.9 {out['p99_9_von_mises_MPa']:.3f}  SF {out['safety_factor']:.4f}"
          f"  ({out['wall_seconds']:.0f}s)", flush=True)
    print(f"      peak at {out['max_at_mm']}", flush=True)
    return out


def observed_exponent(points, key):
    """Fit p in  sigma ~ h**(-p)  by least squares on log-log.

    A converging quantity gives p ~ 0. A singular one gives p > 0, and for a
    sharp 90-degree re-entrant corner the theoretical value is 1 - lambda.
    """
    pts = [(p["mesh_mm"], p[key]) for p in points
           if not p.get("failed") and p.get(key)]
    if len(pts) < 2:
        return None
    xs = [math.log(h) for h, _ in pts]
    ys = [math.log(s) for _, s in pts]
    n = len(pts)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den
    return -slope        # positive => grows as the mesh refines


def main() -> int:
    eng = DesignEngine(J.ROOT)
    # The engine default is 600 s. The 2.4 mm solve is predicted at ~1270 s
    # (high end 2247 s) by the learned cost model, and an under-set timeout
    # spends the whole budget and returns nothing.
    eng.validation = ValidationTools(J.ROOT, eng.log, eng.parts,
                                     eng.validation.ccx_path,
                                     solve_timeout_s=3600)

    space = J.make_space()
    res = json.load(open('data/optimization/jetpack/result.json'))
    front = sorted(res['front'],
                   key=lambda c: c['result']['metrics']['frame_mass_kg'])
    v = space.resolve(front[0]['values'])

    class _C:                       # build_case only reads .values
        values = v
    case = J.build_case(_C(), None)

    print("MESH CONVERGENCE — P0047@v1, thermal_derated_yield")
    print(f"  geometry: stored STEP, unchanged across runs")
    print(f"  coarsest available: {BASELINE_MM} mm (4.0 mm is degenerate here)")
    print(f"  refining to: {SIZES}")

    points = []
    base = baseline_from_log(eng)
    if base:
        points.append(base)
        print(f"\n  --- {BASELINE_MM} mm (reused from {base['source']}) ---")
        print(f"      nodes {base['nodes']:,}  maxVM {base['max_von_mises_MPa']:.3f} MPa"
              f"  p99.9 {base['p99_9_von_mises_MPa']:.3f}"
              f"  SF {base['safety_factor']:.4f}")
        print(f"      peak at {base['max_at_mm']}")
    else:
        print("\n  no logged 3.2 mm baseline found; it will be solved")
        points.append(run_one(eng, case, BASELINE_MM))

    for h in SIZES:
        points.append(run_one(eng, case, h))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    summary = {"geometry_id": GEOMETRY_ID, "limit_state": "thermal_derated_yield",
               "points": points,
               "peak_exponent": observed_exponent(points, "max_von_mises_MPa"),
               "p99_9_exponent": observed_exponent(points, "p99_9_von_mises_MPa"),
               "williams_lambda": WILLIAMS_LAMBDA,
               "theoretical_singular_exponent": 1 - WILLIAMS_LAMBDA}
    OUT.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    # ------------------------------------------------------------- report
    ok = [p for p in points if not p.get("failed")]
    print("\n" + "=" * 74)
    print(f"{'h (mm)':>8} {'nodes':>10} {'max vM':>10} {'d%':>8} "
          f"{'p99.9':>9} {'d%':>8} {'SF':>8}")
    print("-" * 74)
    prev_max = prev_p99 = None
    for p in ok:
        dm = f"{(p['max_von_mises_MPa']/prev_max-1)*100:+.1f}" if prev_max else "  ---"
        dp = f"{(p['p99_9_von_mises_MPa']/prev_p99-1)*100:+.1f}" if prev_p99 else "  ---"
        print(f"{p['mesh_mm']:>8.1f} {p['nodes']:>10,} "
              f"{p['max_von_mises_MPa']:>10.3f} {dm:>8} "
              f"{p['p99_9_von_mises_MPa']:>9.3f} {dp:>8} "
              f"{p['safety_factor']:>8.4f}")
        prev_max, prev_p99 = p['max_von_mises_MPa'], p['p99_9_von_mises_MPa']
    print("=" * 74)

    pe, p9 = summary["peak_exponent"], summary["p99_9_exponent"]
    if pe is not None:
        print(f"\n  peak grows as h**-{pe:.3f}   "
              f"(sharp 90-deg re-entrant corner predicts h**-{1-WILLIAMS_LAMBDA:.3f})")
    if p9 is not None:
        print(f"  p99.9 grows as h**-{p9:.3f}")

    for p in [x for x in points if x.get("failed")]:
        print(f"\n  {p['mesh_mm']} mm DID NOT COMPLETE: {p['error'][:200]}")

    print(f"\n  written: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
