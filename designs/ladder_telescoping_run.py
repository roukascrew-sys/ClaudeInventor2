"""Extension ladder, part 2: true telescoping channels, rung lock, buckling.

Builds the three items left open by designs/extension_ladder_run.py:

  1. TRUE NESTED-CHANNEL TELESCOPING. The base rail is a C-channel; the fly
     rail is a smaller C-channel that slides INSIDE it. Part 1 used the same
     I-beam for both, side by side, which is not a telescoping ladder.
  2. RUNG LOCK / DOG. The L-shaped pawl that hooks over a base-section rung
     and carries the fly section plus climber, checked in root bending.
  3. COLUMN BUCKLING of the base rail under axial climbing load, via the
     elastic_buckling limit state (verified against Euler to 0.16%).

Sourced basis, unchanged from part 1:
  - ANSI A14.2 Type IA = 300 lb duty.
  - OSHA 1926.1053: sustain 3.3x the rated load -> 990 lbf = 4404 N proof.
  - Required SF 1.67 = AISC 360 ASD Omega_b (flexural yielding).
  - 6061-T6511: yield 40 ksi = 276 MPa (OnlineMetals product pages).

SECTION PROPERTIES ARE COMPUTED, NOT GUESSED. An open C-channel is NOT
symmetric about its mid-height: the neutral axis sits below centre and the
extreme fibre is further away than h/2. A first attempt at this design used a
naive hollow-rectangle formula, under-predicted the base channel by 72%, and
the FEA gate caught it. channel_section() below does the real centroid and
parallel-axis work and agrees with FEA to ~2%.

Stated assumptions, not hidden:
  - Wall thicknesses and the lock's proportions are MY engineering choices
    sized to clear the gate, not a manufacturer's published design.
  - The sliding-fit band and the +/-0.30mm extrusion tolerance are
    engineering judgement, NOT cited standards. Real ladder section fits are
    deliberately loose so they still slide when dirty or slightly dented.
  - The lock is a solid pawl in bending. Its spring, pivot pin shear, and the
    wear/impact of repeated deployment are NOT modelled.
  - Buckling is checked for the BASE rail only.
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from design_engine import DesignEngine

ROOT = Path(__file__).parent.parent / "data"
eng = DesignEngine(ROOT)

AL = {"name": "6061-T6511", "E_MPa": 68900, "nu": 0.33, "yield_MPa": 276,
      "source": "OnlineMetals product pages (pid 1145, 1087): yield 40 ksi, "
                "ultimate 42 ksi. E and nu are standard published values for "
                "wrought aluminium (E~69 GPa, nu~0.33)."}
REQUIRED_SF = 1.67
DUTY_LB = 300.0
PROOF_N = 3.3 * DUTY_LB * 4.44822          # 4404 N
SECTION_LEN = 3505.2                        # mm (20ft extended, 3ft overlap)
M_PROOF = PROOF_N * SECTION_LEN / 4.0       # simply-supported midspan moment


def channel_section(w, h, t):
    """Centroid, second moment and extreme-fibre distance of a U-channel
    (open at the top): one w x t flange at the bottom, two t x (h-t) webs.

    Returns (I_mm4, c_mm, ybar_mm, area_mm2). The extreme fibre is the OPEN
    edge, which is further from the neutral axis than h/2 - that asymmetry is
    exactly what a hollow-rectangle formula gets wrong.
    """
    a_f, y_f = w * t, -h / 2.0 + t / 2.0
    a_w, y_w = 2.0 * t * (h - t), t / 2.0
    area = a_f + a_w
    ybar = (a_f * y_f + a_w * y_w) / area
    i_f = w * t ** 3 / 12.0 + a_f * (y_f - ybar) ** 2
    i_w = 2.0 * (t * (h - t) ** 3 / 12.0) + a_w * (y_w - ybar) ** 2
    return i_f + i_w, (h / 2.0 - ybar), ybar, area


# ---- geometry, sized against the computed section properties ---------------
BASE_W, BASE_H, BASE_T = 76.2, 101.6, 6.35     # 3in x 4in, 1/4in wall
SLIDE_CLEAR = 0.8                               # nominal per side
FLY_W = BASE_W - 2 * BASE_T - 2 * SLIDE_CLEAR   # nests inside the base
FLY_H, FLY_T = 88.9, 7.94                       # 3.5in deep, 5/16in wall

print("=== Step 1: log state ===")
print(f"pending: {len(eng.log.pending_actions())}  "
      f"failures so far: {len(eng.log.failures())}")

print("\n=== Step 2: predictions (computed section properties) ===")
I_b, c_b, yb_b, A_b = channel_section(BASE_W, BASE_H, BASE_T)
I_f, c_f, yb_f, A_f = channel_section(FLY_W, FLY_H, FLY_T)
sig_b, sig_f = M_PROOF * c_b / I_b, M_PROOF * c_f / I_f
print(f"base {BASE_W}x{BASE_H}x{BASE_T}: I={I_b:.0f} mm^4, ybar={yb_b:.2f}, "
      f"c={c_b:.2f} -> sigma={sig_b:.1f} MPa, SF={AL['yield_MPa']/sig_b:.3f}")
print(f"fly  {FLY_W:.2f}x{FLY_H}x{FLY_T}: I={I_f:.0f} mm^4, ybar={yb_f:.2f}, "
      f"c={c_f:.2f} -> sigma={sig_f:.1f} MPa, SF={AL['yield_MPa']/sig_f:.3f}")
assert AL["yield_MPa"] / sig_b >= REQUIRED_SF, "base hand-calc must clear the gate"
assert AL["yield_MPa"] / sig_f >= REQUIRED_SF, "fly hand-calc must clear the gate"

axial_N = DUTY_LB * 4.44822 * math.cos(math.atan(0.25)) / 2.0
P_cr_euler = math.pi ** 2 * AL["E_MPa"] * I_b / SECTION_LEN ** 2
print(f"base rail axial (per rail, 4:1 lean) = {axial_N:.1f} N; "
      f"Euler P_cr (pinned-pinned) = {P_cr_euler:.0f} N "
      f"-> factor ~{P_cr_euler/axial_N:.0f}")

# ---- lock / dog: L-shaped pawl ---------------------------------------------
# Root bending governs: the arm's full rectangular section carries
# load x reach. The hook lip at the tip does not carry that moment.
DOG_W, DOG_T, DOG_REACH = 38.1, 15.875, 44.45   # 1.5in x 5/8in, 1.75in reach
# 5/8in, not the 1/2in first tried: at 1/2in the FEA gave SF=1.435 against
# the 1.67 requirement. The simple cantilever formula under-predicted that
# case by 101% because it ignores the pivot-bore and L-corner stress
# concentrations, which are real (outlier ratio 1.02, not an artifact).
LIP_H = 25.4                                     # hooks down over the rung
dog_load = PROOF_N / 2.0                         # two dogs, one per rail
I_dog = DOG_W * DOG_T ** 3 / 12.0
sig_dog = (dog_load * DOG_REACH) * (DOG_T / 2.0) / I_dog
print(f"dog {DOG_W}x{DOG_T}mm, reach {DOG_REACH}mm, load {dog_load:.0f} N -> "
      f"root sigma={sig_dog:.1f} MPa, SF={AL['yield_MPa']/sig_dog:.3f}")

print("\n=== Step 3: ripple analysis ===")
print("New parts. The part-1 rail/rung are signed and untouched, and the "
      "part-1 sign-off explicitly does NOT extend to these channels.")

# ---- Step 4: build ----------------------------------------------------------
print("\n=== Step 4: create parts ===")
base_rail = eng.create_part({
    "name": "base-channel", "units": "mm", "density_kg_m3": 2700,
    "features": [
        {"op": "box", "x": BASE_W, "y": BASE_H, "z": SECTION_LEN},
        {"op": "box", "x": BASE_W - 2 * BASE_T, "y": BASE_H, "z": SECTION_LEN,
         "at": [0, BASE_T, 0], "mode": "cut"},
    ],
}, reason=(
    f"BASE channel, {BASE_W}x{BASE_H}mm C-section, {BASE_T}mm wall, "
    f"6061-T6511; the fly section slides inside it. Computed section: "
    f"I={I_b:.0f} mm^4, neutral axis {yb_b:.2f}mm below centre, c={c_b:.2f}mm "
    f"-> predicted sigma={sig_b:.1f} MPa, SF={AL['yield_MPa']/sig_b:.3f} "
    f"against the OSHA 3.3x proof load ({PROOF_N:.0f} N)"))["geometry_id"]
print(f"  base: {base_rail}")

fly_rail = eng.create_part({
    "name": "fly-channel", "units": "mm", "density_kg_m3": 2700,
    "features": [
        {"op": "box", "x": FLY_W, "y": FLY_H, "z": SECTION_LEN},
        {"op": "box", "x": FLY_W - 2 * FLY_T, "y": FLY_H, "z": SECTION_LEN,
         "at": [0, FLY_T, 0], "mode": "cut"},
    ],
}, reason=(
    f"FLY channel, {FLY_W:.2f}x{FLY_H}mm C-section, {FLY_T}mm wall, nesting "
    f"inside the base channel's {BASE_W-2*BASE_T:.2f}mm inner width with "
    f"{SLIDE_CLEAR}mm nominal clearance per side. Computed I={I_f:.0f} mm^4, "
    f"c={c_f:.2f}mm -> predicted sigma={sig_f:.1f} MPa, "
    f"SF={AL['yield_MPa']/sig_f:.3f}"))["geometry_id"]
print(f"  fly:  {fly_rail}")

dog = eng.create_part({
    "name": "rung-lock-dog", "units": "mm", "density_kg_m3": 2700,
    "features": [
        # arm, root at z=max (pivot end), tip at z=0
        {"op": "box", "x": DOG_W, "y": DOG_T, "z": DOG_REACH},
        # lip at the tip, hanging down to catch over a rung -> an L hook
        {"op": "box", "x": DOG_W, "y": LIP_H, "z": DOG_T,
         "at": [0, -(LIP_H - DOG_T) / 2.0, 0], "mode": "union"},
        # pivot bore near the root, drilled across the width (face ">X" maps
        # 'at' to (y, z) -- established empirically on the hinge leaf)
        {"op": "hole", "d": 10.0, "at": [0.0, DOG_REACH - 12.0], "face": ">X"},
    ],
}, reason=(
    f"rung lock (dog): L-shaped pawl, {DOG_W}x{DOG_T}mm arm, {DOG_REACH}mm "
    f"reach, {LIP_H}mm lip hooking over a rung, 10mm pivot bore near the "
    f"root. Carries half the proof load ({dog_load:.0f} N). Predicted root "
    f"sigma={sig_dog:.1f} MPa, SF={AL['yield_MPa']/sig_dog:.3f}"))["geometry_id"]
print(f"  dog:  {dog}")

# ---- Step 5: validate -------------------------------------------------------
def bend_case(length, mesh_mm, load_N, patch=15.0):
    return {
        "material": dict(AL), "mesh": {"max_size_mm": mesh_mm},
        "constraints": [
            {"where": {"axis": "z", "at": "min"}, "dof": [1, 2]},
            {"where": {"axis": "z", "at": "max"}, "dof": [1, 2]},
            {"where": {"axis": "z", "at": length / 2.0, "tol": 1.0}, "dof": [3]},
        ],
        "loads": [{
            "where": {"all": [{"axis": "y", "at": "max"},
                              {"axis": "z", "at": length / 2.0, "tol": patch}]},
            "force_total_N": [0, -load_N, 0]}],
        "limit_state": {"name": "yield_von_mises", "required_SF": REQUIRED_SF},
    }


print("\n=== Step 5: FEA ===")
results = {}
for gid, nm, pred in ((base_rail, "base channel", sig_b),
                      (fly_rail, "fly channel", sig_f)):
    r = eng.run_fea_static(gid, bend_case(SECTION_LEN, 15.0, PROOF_N),
        reason=(f"{nm} vs the OSHA 1926.1053 3.3x proof load ({PROOF_N:.0f} N), "
                f"simply supported, midspan patch. Predicted sigma="
                f"{pred:.1f} MPa from the computed channel section"))
    d = json.loads(eng.log.rows(action="fea_static")[-1]["details_json"])
    results[nm] = r
    print(f"  {nm}: {r['result']}  SF={r['safety_factor']:.3f}  "
          f"max_vM={r['max_von_mises_MPa']:.1f} MPa  "
          f"(pred {pred:.1f}, {100*(r['max_von_mises_MPa']/pred-1):+.1f}%)  "
          f"eq={d['equilibrium']['residual_rel']:.1e}")

dog_case = {
    "material": dict(AL), "mesh": {"max_size_mm": 2.2},
    "constraints": [{"where": {"axis": "z", "at": "max"}, "dof": [1, 2, 3]}],
    "loads": [{"where": {"axis": "y", "at": "min"},
               "force_total_N": [0, dog_load, 0]}],
    "limit_state": {"name": "yield_von_mises", "required_SF": REQUIRED_SF},
}
r = eng.run_fea_static(dog, dog_case, reason=(
    f"rung lock dog as a cantilever: built in at the pivot/root end, half the "
    f"proof load ({dog_load:.0f} N) bearing UP on the hook lip's underside "
    f"(the rung pushes up on the hook). Predicted root sigma={sig_dog:.1f} "
    f"MPa, SF={AL['yield_MPa']/sig_dog:.3f}"))
results["dog"] = r
d = json.loads(eng.log.rows(action="fea_static")[-1]["details_json"])
print(f"  dog: {r['result']}  SF={r['safety_factor']:.3f}  "
      f"max_vM={r['max_von_mises_MPa']:.1f} MPa  "
      f"eq={d['equilibrium']['residual_rel']:.1e}  "
      f"outlier={d['stress_outlier_ratio']}")

print("\n=== Step 5b: column buckling (base rail) ===")
buck_case = {
    "material": dict(AL), "mesh": {"max_size_mm": 15.0},
    "constraints": [
        {"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]},
        {"where": {"axis": "z", "at": "max"}, "dof": [1, 2]},
    ],
    "loads": [{"where": {"axis": "z", "at": "max"},
               "force_total_N": [0, 0, -axial_N]}],
    "limit_state": {"name": "elastic_buckling", "required_SF": REQUIRED_SF},
}
b = eng.run_fea_buckling(base_rail, buck_case, reason=(
    f"base rail column buckling under the axial climbing load "
    f"({axial_N:.1f} N per rail at the OSHA 4:1 lean), fixed base / pinned "
    f"top. Euler pinned-pinned (more conservative) predicts "
    f"P_cr={P_cr_euler:.0f} N"))
print(f"  buckling: {b['result']}  factor={b['safety_factor']:.1f}  "
      f"P_cr={b['safety_factor']*axial_N:.0f} N  "
      f"(scaling self-check {b['scaling_ratio']:.4f}, need 2.0)")

# ---- sliding fit stackup ----------------------------------------------------
print("\n=== tolerance stackup: telescoping sliding fit ===")
asm = eng.create_assembly({
    "name": "telescoping-pair", "units": "mm",
    "components": [
        {"ref": "base", "geometry_id": base_rail, "at": [0, 0, 0]},
        {"ref": "fly", "geometry_id": fly_rail,
         "at": [0, BASE_T, SECTION_LEN - 914.4]},   # 3ft overlap
    ],
    "chains": [{
        "name": "fly-in-base-sliding-fit",
        "requirement_mm": {"min": 0.3, "max": 2.5},
        "terms": [
            {"desc": "base channel inner width (extruded, ASSUMED +/-0.30mm)",
             "nominal": BASE_W - 2 * BASE_T, "tol_plus": 0.30,
             "tol_minus": 0.30, "sense": 1},
            {"desc": "fly channel outer width (extruded, ASSUMED +/-0.30mm)",
             "nominal": FLY_W, "tol_plus": 0.30, "tol_minus": 0.30,
             "sense": -1},
        ],
    }],
}, reason=(
    "telescoping pair at full 3ft overlap. The 0.3-2.5mm band is engineering "
    "judgement: the fly must still slide when the channels are dirty, painted "
    "or slightly dented, so a ladder section fit is deliberately loose. It is "
    "NOT a cited fit standard, and the +/-0.30mm extrusion tolerance is "
    "assumed rather than vendor-certified"))["assembly_id"]
stack = eng.check_tolerance_stackup(asm)
ch = stack["report"]["chains"][0]
print(f"  {ch['result']}  worst-case clearance "
      f"[{ch['worst_case_mm']['min']}, {ch['worst_case_mm']['max']}] mm "
      f"(requirement {ch['requirement_mm']})")

print("\n=== summary ===")
for k, r in results.items():
    print(f"  {k:14s} {r['result']:4s}  SF={r['safety_factor']:.3f}")
print(f"  {'base buckling':14s} {b['result']:4s}  factor={b['safety_factor']:.1f}")
print(f"  {'sliding fit':14s} {ch['result']}")
print(f"\nparts: base={base_rail}  fly={fly_rail}  dog={dog}")
print("NOT signed off -- Gideon's call.")
