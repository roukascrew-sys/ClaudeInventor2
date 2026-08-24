"""Closed loop: Chrono computes the hinge load, CalculiX sizes the hinge.

This is the point of the kinematics layer. Previously the hinge leaf was
validated against an *assumed* 200 N prying load and the pin against an
*assumed* 50 N bearing load - numbers I picked, flagged as assumptions. Here
the load is COMPUTED from the door it actually carries, and the FEA case is
built from that computed reaction.

Assumptions still present, stated rather than buried:
  - door 800 x 40 x 2000 mm at 300 kg/m^3 (hollow-core interior door,
    ~19.2 kg). Density is a representative value, not a measured one.
  - two hinges, 1500 mm apart, idealised as SPHERICAL (force-only) joints.
    That is deliberate: revolute joints would carry the tipping moment
    internally and report no horizontal force, hiding the pull the leaf
    actually sees. Both idealisations are verified against closed form in
    tests/test_kinematics.py.
  - the whole door load is taken by one leaf pair; no third hinge.

Run with the project venv from the repo root:
    .venv\\Scripts\\python.exe designs\\hinge_loop_run.py
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
REQUIRED_SF = 1.67          # AISC 360 ASD Omega_b, flexural yielding
DOOR_W, DOOR_T, DOOR_H = 800.0, 40.0, 2000.0
HINGE_SEP = 1500.0

print("=== 1. parts ===")
leaf = eng.create_part({
    "name": "hinge-leaf", "units": "mm", "density_kg_m3": 7850,
    "features": [
        {"op": "box", "x": 2.5, "y": 32, "z": 76.2, "at": [0, 16, 0]},
        {"op": "cylinder", "d": 7.0, "h": 76.2, "at": [0, 0, 0], "mode": "union"},
        {"op": "cylinder", "d": 3.7, "h": 76.2, "at": [0, 0, 0], "mode": "cut"},
        {"op": "hole", "d": 4.5, "at": [20, 15], "face": ">X"},
        {"op": "hole", "d": 4.5, "at": [20, 61.2], "face": ">X"},
    ]}, reason=(
    "3in butt hinge leaf, S235JR: 2.5mm gauge leaf, 7mm knuckle, 3.7mm pin "
    "bore, two M4 clearance holes"))["geometry_id"]
door = eng.create_part({
    "name": "door-slab", "units": "mm", "density_kg_m3": 300,
    "features": [{"op": "box", "x": DOOR_W, "y": DOOR_T, "z": DOOR_H}],
}, reason=("hollow-core interior door slab, 300 kg/m^3 representative "
           "effective density (assumption, not measured)"))["geometry_id"]
frame = eng.create_part({
    "name": "door-frame", "units": "mm", "density_kg_m3": 500,
    "features": [{"op": "box", "x": 60, "y": 60, "z": DOOR_H}],
}, reason="frame post the hinges mount to; held fixed in the solve"
   )["geometry_id"]
mass = eng.get_part(door)["properties"]["mass_kg_estimate"]
print(f"  door mass {mass:.2f} kg  (weight {mass * 9.81:.1f} N)")

print("\n=== 2. mechanism: door hung on two hinges ===")
asm = eng.create_assembly({
    "name": "hung-door", "units": "mm",
    "components": [
        {"ref": "frame", "geometry_id": frame, "at": [0, 0, 0]},
        {"ref": "leaf", "geometry_id": door, "at": [DOOR_W / 2.0, 0, 0]},
    ],
    "joints": [
        {"id": "lower-hinge", "type": "spherical", "between": ["leaf", "frame"],
         "at": [0, 0, DOOR_H / 2.0 - HINGE_SEP / 2.0], "axis": [0, 0, 1]},
        {"id": "upper-hinge", "type": "spherical", "between": ["leaf", "frame"],
         "at": [0, 0, DOOR_H / 2.0 + HINGE_SEP / 2.0], "axis": [0, 0, 1]},
    ],
    "chains": [{"name": "leaf-to-frame-gap", "requirement_mm": {"min": 1.0},
                "terms": [{"desc": "gap", "nominal": 3.0, "tol_plus": 0.5,
                           "tol_minus": 0.5, "sense": 1}]}],
}, reason=("door hung on two hinges, force-only (spherical) idealisation so "
           "the horizontal pull on the leaf is reported rather than hidden "
           "as an internal joint moment"))["assembly_id"]

# Closed form for the moment-free idealisation, stated before solving.
W = mass * 9.81
H_pred = W * (DOOR_W / 1000.0) / (2 * (HINGE_SEP / 1000.0))
print(f"  predicted horizontal couple W*w/(2d) = {H_pred:.2f} N")

kin = eng.run_kinematics(asm, {
    "gravity_mm_s2": [0, 0, -9810],
    "analysis": "static",
    "fixed": ["frame"],
    "limit_state": {
        "name": "joint_reaction_force",
        "allowable": 1000.0,
        "source": "provisional screening allowable, 1 kN - well above the "
                  "expected reaction; the purpose of this run is to COMPUTE "
                  "the load for the FEA case, not to gate on it",
    },
}, reason=(f"compute hinge reactions for a {mass:.1f} kg door, "
           f"{DOOR_W:.0f}mm wide, hinges {HINGE_SEP:.0f}mm apart. "
           f"Predicted horizontal couple = {H_pred:.1f} N"))

for r in kin["reactions"]:
    fx, fy, fz = r["force_N"]
    print(f"  {r['joint_id']:>13}: F=({fx:7.2f}, {fy:6.2f}, {fz:8.2f}) N  "
          f"|F|={r['force_magnitude_N']:7.2f} N")
peak_N = kin["peak_value"]
print(f"  peak reaction {peak_N:.2f} N at {kin['peak_joint']!r} "
      f"(solver vs closed form on the horizontal term: "
      f"{abs(kin['reactions'][0]['force_N'][0]):.2f} vs {H_pred:.2f} N)")

print("\n=== 3. size the leaf against the COMPUTED load ===")
case = {
    "material": dict(S235),
    "mesh": {"max_size_mm": 1.6},
    "constraints": [
        {"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]},
        {"where": {"axis": "z", "at": "max"}, "dof": [1, 2, 3]},
    ],
    "loads": [{
        "where": {"all": [{"axis": "x", "at": 1.25, "tol": 0.01},
                          {"axis": "y", "at": 6, "tol": 2}]},
        "force_total_N": [round(peak_N, 3), 0, 0],
    }],
    "limit_state": {"name": "yield_von_mises", "required_SF": REQUIRED_SF},
}
fea = eng.run_fea_static(leaf, case, reason=(
    f"hinge leaf against the reaction COMPUTED by run_kinematics "
    f"(log #{kin['action_id']}): {peak_N:.2f} N at joint "
    f"{kin['peak_joint']!r}, not an assumed load. Gate SF {REQUIRED_SF} "
    f"(AISC 360 ASD Omega_b)"))
det = json.loads(eng.log.rows(action="fea_static")[-1]["details_json"])
print(f"  leaf: {fea['result']}  SF={fea['safety_factor']:.2f}  "
      f"max vM={fea['max_von_mises_MPa']:.1f} MPa")
print(f"  stress outlier ratio {det['stress_outlier_ratio']} "
      f"(warning: {det['stress_outlier_warning']})")

report = eng.generate_report()
print(f"\nreport: {report}")
print(f"Loop closed: kinematics #{kin['action_id']} -> FEA #{fea['action_id']}")
