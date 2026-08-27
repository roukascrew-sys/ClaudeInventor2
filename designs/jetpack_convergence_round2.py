"""Second refinement round: sharp and filleted, both at 2.8 mm.

The first attempt at 2.8 mm crashed the solver — `0xC0000005` at a 6.1 GB
working set on a machine with 1.3 GB available. Closing Chrome and the WebView
host raised that to ~5.9 GB, so this retries the point that was lost and adds
the matching filleted run.

Two questions, one solve each:

    SHARP     P0047@v1 at 2.8 mm. Does the peak on the unfilleted re-entrant
              corner GROW as predicted? Williams gives sigma ~ h**-0.4555, so
              65.340 MPa at 3.2 mm should become about 69.5 MPa. A prediction
              made before the run and recorded in the module docstring, so it
              can be wrong in public.

    FILLETED  P0048@v1 at 2.8 mm. Does the peak on the fillet arc HOLD? At
              3.2 mm it was 54.207 MPa with the peak sitting 0.0002 mm off the
              10 mm arc. If the fillet has genuinely removed the singularity,
              this should move very little.

Run sequentially, never concurrently: two ~500k-node direct solves at once on
this machine would starve each other and reproduce the original crash.

    .venv\\Scripts\\python.exe designs\\jetpack_convergence_round2.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, '.')
from design_engine import DesignEngine
from design_engine.fea import ValidationTools
import designs.jetpack_optimization_run as J
from designs.jetpack_mesh_convergence import run_one, observed_exponent

OUT = Path("data/convergence/round2.json")
MESH = 2.8

# Measured at 3.2 mm, from the FRACAS log. Not re-solved.
KNOWN = {
    "P0047@v1": {"label": "sharp corner", "mesh_mm": 3.2, "nodes": 337727,
                 "max_von_mises_MPa": 65.339842, "p99_9_von_mises_MPa": 40.003796,
                 "safety_factor": 3.843903, "stress_outlier_ratio": 1.6333,
                 "max_at_mm": [-23.505, 4.014, 199.6], "source": "log action 213"},
    "P0048@v1": {"label": f"filleted r={J.FILLET_R} mm", "mesh_mm": 3.2,
                 "nodes": 338446, "max_von_mises_MPa": 54.206986,
                 "p99_9_von_mises_MPa": 44.488023, "safety_factor": 4.633351,
                 "stress_outlier_ratio": 1.2185,
                 "max_at_mm": [29.513, -0.024, 199.225], "source": "log action 217"},
}

# sigma ~ h**-(1-lambda) for a traction-free 270-degree corner
PREDICTED_SHARP = 65.339842 * (3.2 / MESH) ** 0.4555


def main() -> int:
    eng = DesignEngine(J.ROOT)
    eng.validation = ValidationTools(J.ROOT, eng.log, eng.parts,
                                     eng.validation.ccx_path,
                                     solve_timeout_s=5400)

    space = J.make_space()
    res = json.load(open('data/optimization/jetpack/result.json'))
    front = sorted(res['front'],
                   key=lambda c: c['result']['metrics']['frame_mass_kg'])
    v = space.resolve(front[0]['values'])

    class _C:
        values = v

    case = J.build_case(_C(), None)

    print(f"CONVERGENCE ROUND 2 — both geometries at {MESH} mm")
    print(f"  prediction for the sharp corner: {PREDICTED_SHARP:.2f} MPa "
          f"(from 65.34 at 3.2 mm, Williams h**-0.4555)")
    print(f"  filleted at 3.2 mm was 54.207 MPa; if the fillet worked this "
          f"should barely move\n")

    results = {}
    for gid, known in KNOWN.items():
        print(f"\n{'='*70}\n{gid} — {known['label']}\n{'='*70}", flush=True)
        pt = run_one(eng, case, MESH, gid=gid)
        results[gid] = {"known_3_2mm": known, "refined": pt}
        if not pt.get("failed"):
            pair = [known, pt]
            results[gid]["peak_exponent"] = observed_exponent(
                pair, "max_von_mises_MPa")
            results[gid]["p99_9_exponent"] = observed_exponent(
                pair, "p99_9_von_mises_MPa")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"mesh_mm": MESH, "predicted_sharp_MPa": PREDICTED_SHARP,
         "results": results}, indent=2), encoding="utf-8")

    print("\n" + "=" * 78)
    print(f"{'geometry':>12} {'h':>5} {'nodes':>9} {'max vM':>9} {'d%':>7} "
          f"{'p99.9':>8} {'SF':>8} {'ratio':>7}")
    print("-" * 78)
    for gid, r in results.items():
        k = r["known_3_2mm"]
        print(f"{gid:>12} {k['mesh_mm']:>5.1f} {k['nodes']:>9,} "
              f"{k['max_von_mises_MPa']:>9.3f} {'---':>7} "
              f"{k['p99_9_von_mises_MPa']:>8.3f} {k['safety_factor']:>8.4f} "
              f"{k['stress_outlier_ratio']:>7.3f}")
        p = r["refined"]
        if p.get("failed"):
            print(f"{'':>12} {MESH:>5.1f}   FAILED: {p['error'][:80]}")
            continue
        d = (p['max_von_mises_MPa'] / k['max_von_mises_MPa'] - 1) * 100
        print(f"{'':>12} {p['mesh_mm']:>5.1f} {p['nodes']:>9,} "
              f"{p['max_von_mises_MPa']:>9.3f} {d:>+7.1f} "
              f"{p['p99_9_von_mises_MPa']:>8.3f} {p['safety_factor']:>8.4f} "
              f"{p['stress_outlier_ratio']:>7.3f}")
        e = r.get("peak_exponent")
        if e is not None:
            print(f"{'':>12}        peak ~ h**-{e:.3f}   "
                  f"(singular 0.456 | converged ~0)")
        print()
    print("=" * 78)

    sharp = results.get("P0047@v1", {}).get("refined", {})
    if not sharp.get("failed") and sharp.get("max_von_mises_MPa"):
        got = sharp["max_von_mises_MPa"]
        print(f"\n  sharp prediction {PREDICTED_SHARP:.2f} MPa vs measured "
              f"{got:.2f} MPa  ({(got/PREDICTED_SHARP-1)*100:+.1f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
