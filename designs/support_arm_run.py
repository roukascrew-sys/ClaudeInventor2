"""Real design run: simply-supported structural arm, three-point bend.

Requirement (from Gideon, 2026-08-24): a structural arm/beam, simply
supported at both ends, moderate mid-span load, mild steel, cheapest usable
stock. Specific parameters assumed and stated below since not all were given
numerically -- flagged to Gideon rather than silently baked in.

Assumptions made (state these back to Gideon, do not bury them):
  - span L = 300 mm            (mid of his "bench-scale 150-500mm" range)
  - central point load F = 1000 N   (mid of his "500-2000 N" range)
  - square cross-section, steel bar stock (matches price book, his choice)
  - required safety factor = 1.67, AISC 360 ASD Omega_b for flexural
    yielding (sourced via web search, not invented)
  - prototype batch quantity = 5 units (not specified; stated as assumption)
  - no dollar budget given -> BOM run without a budget cap

This script runs Steps 1-7 of the design-engine-loop skill (predict, ripple,
execute, validate, gate, confidence). It stops BEFORE sign-off -- that is
Gideon's act, not this script's, per the skill's rule 4.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from design_engine import DesignEngine

ROOT = Path(__file__).parent.parent / "data"
eng = DesignEngine(ROOT)

S235 = {"name": "S235JR", "E_MPa": 210000, "nu": 0.3, "yield_MPa": 235,
        "source": "EN 10025-2 nominal values, t<=16mm"}
L = 300.0          # mm, span
F = 1000.0         # N, central point load
REQUIRED_SF = 1.67  # AISC 360 ASD, Omega_b for flexural yielding (I3.2a)


def predict(h):
    I = h * h ** 3 / 12.0
    c = h / 2.0
    M = F * L / 4.0
    return {"I": I, "c": c, "M": M, "sigma": M * c / I,
            "delta": F * L ** 3 / (48 * S235["E_MPa"] * I),
            "sf": S235["yield_MPa"] / (M * c / I)}


def case(h, mesh=1.5):
    mid = L / 2.0
    return {
        "material": dict(S235),
        "mesh": {"max_size_mm": mesh},
        "constraints": [
            {"where": {"axis": "z", "at": "min"}, "dof": [1, 2]},
            {"where": {"axis": "z", "at": "max"}, "dof": [1, 2]},
        ],
        "loads": [{
            "where": {"all": [
                {"axis": "y", "at": "max"},
                {"axis": "z", "at": mid, "tol": 10.0},
            ]},
            "force_total_N": [0, -F, 0],
        }],
        "limit_state": {"name": "yield_von_mises", "required_SF": REQUIRED_SF},
    }


# ---------------------------------------------------------------------------
# Step 1: log state (fresh part, nothing to review yet)
print("=== Step 1: log state ===")
print(f"pending actions: {len(eng.log.pending_actions())}")
print(f"existing failures: {len(eng.log.failures())}")

# ---------------------------------------------------------------------------
# Step 2/3: predictions + ripple analysis (first version -> nothing to ripple into)
print("\n=== Step 2: predictions ===")
p14, p16 = predict(14.0), predict(16.0)
print(f"h=14mm: sigma={p14['sigma']:.2f} MPa, SF={p14['sf']:.3f}, "
      f"delta={p14['delta']:.4f} mm -> {'FAIL' if p14['sf']<REQUIRED_SF else 'PASS'}")
print(f"h=16mm: sigma={p16['sigma']:.2f} MPa, SF={p16['sf']:.3f}, "
      f"delta={p16['delta']:.4f} mm -> {'FAIL' if p16['sf']<REQUIRED_SF else 'PASS'}")
print("\n=== Step 3: ripple analysis ===")
print("First version of a new part: no existing assemblies, stackups, BOMs, "
      "sign-offs, or artifacts reference it yet. Nothing to ripple into.")

# ---------------------------------------------------------------------------
# Step 4: create the initial design
print("\n=== Step 4: create_part (attempt 1, h=14mm) ===")
gid = eng.create_part({
    "name": "support-arm", "units": "mm", "density_kg_m3": 7850,
    "features": [{"op": "box", "x": 14.0, "y": 14.0, "z": L}],
}, reason=(
    f"structural arm, simply supported {L:.0f}mm span, central {F:.0f}N "
    f"load, S235JR square bar. Beam-theory prediction: sigma={p14['sigma']:.1f} "
    f"MPa, SF={p14['sf']:.3f} against required {REQUIRED_SF} (AISC 360 ASD "
    f"Omega_b flexural yielding) -- expected to FAIL the gate, first pass "
    f"at a conservative starting section"))["geometry_id"]
print(f"  {gid}")

# ---------------------------------------------------------------------------
# Step 5: validate
print("\n=== Step 5: run_fea_static (attempt 1) ===")
run1 = eng.run_fea_static(gid, case(14.0), reason=(
    f"three-point bend vs yield_von_mises, required SF {REQUIRED_SF} "
    f"(AISC 360 ASD Omega_b). Predicted sigma={p14['sigma']:.1f} MPa, "
    f"delta={p14['delta']:.4f} mm"))
print(f"  result={run1['result']}  SF={run1['safety_factor']:.3f}  "
      f"max_vM={run1['max_von_mises_MPa']:.1f} MPa  "
      f"delta={run1['max_displacement_mm']:.4f} mm")
print(f"  predicted sigma vs solver max_vM: {p14['sigma']:.1f} vs "
      f"{run1['max_von_mises_MPa']:.1f} MPa "
      f"({100*(run1['max_von_mises_MPa']/p14['sigma']-1):+.1f}%)")
print(f"  predicted delta vs solver: {p14['delta']:.4f} vs "
      f"{run1['max_displacement_mm']:.4f} mm "
      f"({100*(run1['max_displacement_mm']/p14['delta']-1):+.1f}%)")

# ---------------------------------------------------------------------------
# Step 6: gate outcome -> interpret -> fix, referencing the failure
print("\n=== Step 6: gate outcome ===")
assert run1["result"] == "fail", "expected the 14mm section to fail the gate"
print(f"  FAIL as predicted (log #{run1['failure_id']}). Interpretation: "
      f"section too shallow; moment of inertia scales h^3 so stress scales "
      f"1/h^2 -- targeting h=16mm for SF~{p16['sf']:.2f}.")

print("\n=== Step 3 (re-run for the edit) / Step 4: edit_part ===")
gid2 = eng.edit_part(
    gid, {"features.0.x": 16.0, "features.0.y": 16.0},
    reason=(
        f"deepen section 14->16mm both axes (square): attempt 1 SF="
        f"{run1['safety_factor']:.3f} < required {REQUIRED_SF}; stress "
        f"scales 1/h^2, predicted new sigma={p16['sigma']:.1f} MPa, "
        f"SF={p16['sf']:.3f}, delta={p16['delta']:.4f} mm"),
    addresses_failure_id=run1["failure_id"])["new_geometry_id"]
print(f"  {gid2}")

print("\n=== Step 5: run_fea_static (attempt 2) ===")
run2 = eng.run_fea_static(gid2, case(16.0), reason=(
    f"re-check after deepening to 16mm, addresses failure #{run1['failure_id']}. "
    f"Predicted sigma={p16['sigma']:.1f} MPa, delta={p16['delta']:.4f} mm"))
print(f"  result={run2['result']}  SF={run2['safety_factor']:.3f}  "
      f"max_vM={run2['max_von_mises_MPa']:.1f} MPa  "
      f"delta={run2['max_displacement_mm']:.4f} mm")
print(f"  predicted sigma vs solver max_vM: {p16['sigma']:.1f} vs "
      f"{run2['max_von_mises_MPa']:.1f} MPa "
      f"({100*(run2['max_von_mises_MPa']/p16['sigma']-1):+.1f}%)")
print(f"  predicted delta vs solver: {p16['delta']:.4f} vs "
      f"{run2['max_displacement_mm']:.4f} mm "
      f"({100*(run2['max_displacement_mm']/p16['delta']-1):+.1f}%)")

row = eng.log.rows(action="fea_static")[-1]
at = json.loads(row["details_json"])["max_von_mises_at_mm"]
print(f"  peak stress location: {at} mm (extreme fibre |y|={16/2}, "
      f"mid-span z={L/2})")

# ---------------------------------------------------------------------------
print("\n=== Step 7: confidence rubric ===")
margin_pct = 100 * (run2["safety_factor"] / REQUIRED_SF - 1)
print(f"  solver vs prediction (stress): "
      f"{100*(run2['max_von_mises_MPa']/p16['sigma']-1):+.1f}%")
print(f"  solver vs prediction (deflection): "
      f"{100*(run2['max_displacement_mm']/p16['delta']-1):+.1f}%")
print(f"  margin above required SF: {margin_pct:.1f}% "
      f"({'>20%, no forced mesh refinement needed' if margin_pct > 20 else 'within 20% of gate -> refine mesh and re-check'})")
print(f"  peak location at extreme fibre near mid-span: "
      f"{'YES' if abs(abs(at[1])-8.0) < 1.5 and abs(at[2]-L/2) < 20 else 'NO -- INVESTIGATE'}")
print("  ripple items (Step 3): none existed to check (first version, no "
      "downstream artifacts yet)")

report = eng.generate_report(ROOT / "report.html")
print(f"\nreport: {report}")
print(f"\nFinal geometry: {gid2}  SF={run2['safety_factor']:.3f} "
      f"(required {REQUIRED_SF})  -- READY FOR SIGN-OFF DECISION (Gideon's call)")
