"""Does a fillet at the spine/pad junction make SF 3.844 convergeable?

The unfilleted frame put its peak von Mises at [-23.505, 4.014, 199.6] — on a
sharp 90-degree re-entrant corner produced by unioning the doubler pad onto the
spine with no blend. Linear elasticity has no finite stress there, so that peak
is a discretisation artefact that grows without bound as h -> 0 and cannot be
converged by any amount of solver time.

    M. L. Williams, "Stress Singularities Resulting from Various Boundary
    Conditions in Angular Corners of Plates in Extension", Journal of Applied
    Mechanics 19 (1952) 526-528.

`build_spec` now fillets those four junction edges at FILLET_R. This rebuilds
the lightest feasible design with the fillet and re-runs the same convergence
study, so the two are directly comparable: identical variables, identical
loads, constraints and material, identical mesh sizes. The ONLY difference is
the blend at the junction.

What distinguishes success from failure here:

    FIXED       peak von Mises settles as h falls — successive changes shrink
                toward zero, and the peak moves off the junction or stays on
                the fillet surface at a finite value
    NOT FIXED   peak keeps climbing at roughly h**-0.4555, meaning the fillet
                is present but under-resolved, or another sharp corner (the
                pad/crossbeam step at |x| = 120, or the lug holes) now governs

A falling safety factor is the expected and CORRECT outcome of removing a
fabricated number. The unfilleted SF was not conservative, it was meaningless;
whatever this study reports is the first converged value the design has had.

    .venv\\Scripts\\python.exe designs\\jetpack_fillet_convergence.py
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
from designs.jetpack_mesh_convergence import (observed_exponent,
                                              WILLIAMS_LAMBDA, run_one)

OUT = Path("data/convergence/jetpack_P0047_filleted.json")
SIZES = [3.2, 2.8]          # 2.4 added only if memory allows; see the report


def find_existing(spec):
    """Locate a stored part whose spec digest matches, so re-runs reuse it."""
    from design_engine.geometry import spec_digest
    want = spec_digest(spec)
    for props in sorted(Path("data/parts").glob("P*/v*/props.json")):
        try:
            if json.loads(props.read_text())["spec_digest"] == want:
                return f"{props.parent.parent.name}@{props.parent.name}"
        except (KeyError, json.JSONDecodeError):
            continue
    return None


def where_is(pt, v):
    """Name the feature the peak landed on, so the number is interpretable."""
    if pt is None:
        return "unknown"
    x, y, z = pt
    sx, cb_h = float(v["spine_x"]) / 2.0, float(v["cb_height"])
    cb_z = J.SPINE_Z / 2.0 - cb_h / 2.0
    r = J.FILLET_R
    for zj, name in ((cb_z, "lower"), (cb_z + cb_h, "upper")):
        if abs(abs(x) - sx) <= r + 1.5 and abs(z - zj) <= r + 1.5:
            return f"{name} spine/pad junction fillet (the filleted region)"
    if abs(abs(x) - float(v["pad_len"]) / 2.0) <= 3.0:
        return "pad/crossbeam step at |x|=pad_len/2 - STILL A SHARP CORNER"
    if abs(z - (J.SPINE_Z - 50.0)) <= 8.0 or abs(z - 40.0) <= 8.0:
        return "lug hole - constraint region"
    return "elsewhere"


def main() -> int:
    eng = DesignEngine(J.ROOT)
    eng.validation = ValidationTools(J.ROOT, eng.log, eng.parts,
                                     eng.validation.ccx_path,
                                     solve_timeout_s=3600)

    space = J.make_space()
    res = json.load(open('data/optimization/jetpack/result.json'))
    front = sorted(res['front'],
                   key=lambda c: c['result']['metrics']['frame_mass_kg'])
    v = space.resolve(front[0]['values'])

    class _C:
        values = v

    spec = J.build_spec(v, None)
    assert any(f["op"] == "fillet" for f in spec["features"]), \
        "build_spec produced no fillet; FILLET_R may be 0"

    print(f"FILLET CONVERGENCE — same design as P0047, r = {J.FILLET_R} mm")
    gid = find_existing(spec)
    if gid:
        # Re-running must not mint a new part number for identical geometry:
        # the spec digest is the identity, and a fresh P-number every run would
        # scatter the study across parts that are the same solid.
        print(f"  part: {gid} (existing, same spec digest)")
    else:
        part = eng.parts.create_part(spec, reason=(
            f"fillet the spine/pad re-entrant junction at r={J.FILLET_R} mm; the "
            f"unfilleted corner made peak stress a non-convergent mesh artefact"))
        gid = part["geometry_id"] if isinstance(part, dict) else part
        print(f"  part: {gid} (created)")
    props = eng.parts.get_part(gid)["properties"]
    print(f"  mass {props['mass_kg_estimate']:.4f} kg  "
          f"(unfilleted P0047@v1 was 3.9008 kg, "
          f"{(props['mass_kg_estimate']/3.900843379-1)*100:+.3f}%)")

    case = J.build_case(_C(), None)
    points = [run_one(eng, case, h, gid=gid) for h in SIZES]

    OUT.parent.mkdir(parents=True, exist_ok=True)
    summary = {"geometry_id": gid, "fillet_radius_mm": J.FILLET_R,
               "compare_to": "P0047@v1 (unfilleted)", "points": points,
               "peak_exponent": observed_exponent(points, "max_von_mises_MPa"),
               "p99_9_exponent": observed_exponent(points, "p99_9_von_mises_MPa"),
               "theoretical_singular_exponent": 1 - WILLIAMS_LAMBDA}
    OUT.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    ok = [p for p in points if not p.get("failed")]
    print("\n" + "=" * 78)
    print(f"{'h (mm)':>8} {'nodes':>10} {'max vM':>10} {'d%':>8} "
          f"{'p99.9':>9} {'d%':>8} {'SF':>8}")
    print("-" * 78)
    pm = pp = None
    for p in ok:
        dm = f"{(p['max_von_mises_MPa']/pm-1)*100:+.1f}" if pm else "  ---"
        dp = f"{(p['p99_9_von_mises_MPa']/pp-1)*100:+.1f}" if pp else "  ---"
        print(f"{p['mesh_mm']:>8.1f} {p['nodes']:>10,} "
              f"{p['max_von_mises_MPa']:>10.3f} {dm:>8} "
              f"{p['p99_9_von_mises_MPa']:>9.3f} {dp:>8} "
              f"{p['safety_factor']:>8.4f}")
        pm, pp = p['max_von_mises_MPa'], p['p99_9_von_mises_MPa']
    print("=" * 78)
    for p in ok:
        print(f"  h={p['mesh_mm']}: peak on {where_is(p.get('max_at_mm'), v)}")

    pe = summary["peak_exponent"]
    if pe is not None:
        print(f"\n  peak grows as h**-{pe:.3f}  "
              f"(sharp corner would be h**-{1-WILLIAMS_LAMBDA:.3f}, "
              f"converged would be ~0)")
    for p in [x for x in points if x.get("failed")]:
        print(f"\n  {p['mesh_mm']} mm DID NOT COMPLETE: {p['error'][:200]}")
    print(f"\n  written: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
