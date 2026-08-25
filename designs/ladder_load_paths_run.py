"""Extension ladder, part 3: the remaining load paths.

Builds the four items flagged as unengineered after part 2:

  1. RUNG-TO-CHANNEL JOINT. The signed channels have NO rung holes, and a
     usable rail must have them. This adds them and re-verifies the channel
     in bending, plus a bearing/tear-out check on the hole itself. Because
     this changes the geometry, it produces a NEW version that the existing
     sign-off deliberately does not cover.
  2. LOCK PIVOT PIN, in shear and bending. This is the single most loaded
     small part in the ladder: everything above the lock hangs off it.
  3. FOOT / SHOE BRACKET, carrying the base reaction computed by the
     kinematics base-slip run (1483 N vertical + 352 N friction).
  4. FLY-TO-BASE GUIDE, the strap at the top of the base section that reacts
     the sections pushing apart at the overlap.

Sourced basis (unchanged):
  - ANSI A14.2 Type IA = 300 lb duty.
  - OSHA 1926.1053: sustain 3.3x the rated load -> 990 lbf = 4404 N proof.
  - Required SF 1.67 = AISC 360 ASD Omega_b (flexural yielding).
  - 6061-T6511 aluminium: yield 40 ksi = 276 MPa (OnlineMetals pid 1145/1087).
  - 1018 cold-finish steel: yield 54 ksi = 372 MPa (OnlineMetals pid 4790,
    already in the price book with its source URL). Used for the pivot pin,
    which is a steel part on any real ladder.

Stated assumptions, not hidden:
  - Bracket proportions are MY engineering choices sized to clear the gate.
  - The pin's clevis is assumed TIGHT (ears close either side of the dog).
    That is a real design requirement, not a convenience: pin bending scales
    with the unsupported span, and a sloppy clevis fails this part. The
    predicted numbers below show both cases.
  - The guide strap load is taken as the section shear (PROOF/2), a
    conservative stand-in for a proper composite-overlap transfer analysis.
  - Rung-to-channel bearing and tear-out are hand-calculated rather than
    FEA'd, because both come out one to two orders of magnitude below every
    other stress here; the arithmetic is shown so it can be checked.
  - BOLT HOLES ARE NOT MODELLED in the shoe or guide. Their bolted joints are
    represented by fixed boundary conditions, so local bolt bearing and
    hole-edge stress are NOT covered by these gates. Modelling a hole a few
    millimetres from a fully-fixed face would report a constraint artefact,
    not a real stress - a separate bolted-joint check is the honest way to
    cover that and is not done here.
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
STEEL = {"name": "1018-cold-finish", "E_MPa": 205000, "nu": 0.29,
         "yield_MPa": 372,
         "source": "OnlineMetals pid 4790 (ASTM-A108): yield 54 ksi, ultimate "
                   "64 ksi. E and nu are standard published values for "
                   "carbon steel (E~205 GPa, nu~0.29)."}
REQUIRED_SF = 1.67
PROOF_N = 3.3 * 300.0 * 4.44822          # 4404 N
SECTION_LEN = 3505.2
M_PROOF = PROOF_N * SECTION_LEN / 4.0

BASE_W, BASE_H, BASE_T = 76.2, 101.6, 6.35
RUNG_D, RUNG_HOLE_D = 25.4, 25.7
RUNG_SPACING, END_MARGIN = 304.8, 150.0

# reactions carried over from earlier verified runs
N_FLOOR, F_FLOOR = 1483.25, 352.21       # kinematics base-slip run
DOG_LOAD = PROOF_N / 2.0                  # 2202 N, one lock per rail


def channel_section(w, h, t):
    """Open U-channel: centroid, I, extreme-fibre distance. The neutral axis
    is NOT at mid-height - that asymmetry is what a hollow-rectangle formula
    gets wrong (it under-predicted this design by 72% on the first attempt)."""
    a_f, y_f = w * t, -h / 2.0 + t / 2.0
    a_w, y_w = 2.0 * t * (h - t), t / 2.0
    area = a_f + a_w
    ybar = (a_f * y_f + a_w * y_w) / area
    i_f = w * t ** 3 / 12.0 + a_f * (y_f - ybar) ** 2
    i_w = 2.0 * (t * (h - t) ** 3 / 12.0) + a_w * (y_w - ybar) ** 2
    return i_f + i_w, (h / 2.0 - ybar), ybar, area


print("=== Step 1: log state ===")
print(f"pending: {len(eng.log.pending_actions())}  "
      f"failures so far: {len(eng.log.failures())}")

print("\n=== Step 2: predictions ===")
I_b, c_b, yb_b, A_b = channel_section(BASE_W, BASE_H, BASE_T)
sig_b = M_PROOF * c_b / I_b
print(f"[1] base channel unholed: sigma={sig_b:.1f} MPa, "
      f"SF={AL['yield_MPa']/sig_b:.3f}  (signed result was SF 1.994)")

# Rung holes sit ON the neutral axis (y=0), where bending stress is ~zero.
# That is why real ladder rungs pass through there. What the hole does remove
# is shear area, and shear is maximum at the neutral axis - so that is the
# check that matters, not bending.
V_max = PROOF_N / 2.0
tau_web = V_max / (2 * BASE_T * (BASE_H - BASE_T))
tau_web_holed = V_max / (2 * BASE_T * (BASE_H - BASE_T - RUNG_HOLE_D))
print(f"    hole is at the neutral axis (y=0): bending stress there ~0.")
print(f"    web shear {tau_web:.2f} MPa -> {tau_web_holed:.2f} MPa at a holed "
      f"section (von Mises {math.sqrt(3)*tau_web_holed:.2f} MPa, "
      f"SF={AL['yield_MPa']/(math.sqrt(3)*tau_web_holed):.0f})")
bear = (DOG_LOAD / 2.0) / (RUNG_HOLE_D * BASE_T)
edge = BASE_H / 2.0
tear = (DOG_LOAD / 2.0) / (2 * edge * BASE_T)
print(f"    rung-to-hole bearing = {DOG_LOAD/2:.0f}N/({RUNG_HOLE_D}x{BASE_T}) "
      f"= {bear:.1f} MPa (SF {AL['yield_MPa']/bear:.0f})")
print(f"    tear-out to the open edge ({edge:.1f}mm) = {tear:.1f} MPa "
      f"(SF {AL['yield_MPa']/(math.sqrt(3)*tear):.0f})")

# [2] pivot pin
PIN_D, PIN_LEN = 10.0, 30.0
EAR_T = 6.0                                # clevis ear thickness each side
span_tight = PIN_LEN - EAR_T               # ear centre to ear centre
Z_pin = math.pi * PIN_D ** 3 / 32.0
A_pin = math.pi * PIN_D ** 2 / 4.0
# load spread over the dog's bearing width between the ears -> UDL, M = FL/8
M_pin = DOG_LOAD * span_tight / 8.0
sig_pin = M_pin / Z_pin
tau_pin = DOG_LOAD / (2 * A_pin)           # double shear
vm_pin = math.sqrt(sig_pin ** 2 + 3 * tau_pin ** 2)
sig_pin_sloppy = (DOG_LOAD * 44.0 / 4.0) / Z_pin
print(f"[2] pivot pin d{PIN_D} steel, TIGHT clevis (span {span_tight:.0f}mm): "
      f"bend {sig_pin:.1f} + shear {tau_pin:.1f} -> vM {vm_pin:.1f} MPa, "
      f"SF={STEEL['yield_MPa']/vm_pin:.2f}")
print(f"    if the clevis were SLOPPY (44mm span, point load): "
      f"{sig_pin_sloppy:.0f} MPa, SF={STEEL['yield_MPa']/sig_pin_sloppy:.2f} "
      f"-- a tight clevis is a design REQUIREMENT, not a detail")

# [3] shoe bracket
SHOE_W, SHOE_T, SHOE_DEPTH, SHOE_RISE = 76.2, 6.35, 60.0, 70.0
shoe_arm = SHOE_DEPTH / 2.0 - SHOE_T / 2.0
M_shoe = N_FLOOR * shoe_arm
Z_shoe = SHOE_W * SHOE_T ** 2 / 6.0
sig_shoe = M_shoe / Z_shoe
print(f"[3] shoe bracket {SHOE_W}x{SHOE_T}mm plate, arm {shoe_arm:.1f}mm, "
      f"N_floor={N_FLOOR:.0f}N -> sigma={sig_shoe:.1f} MPa, "
      f"SF={AL['yield_MPa']/sig_shoe:.2f}")

# [4] guide strap
GUIDE_W, GUIDE_T, GUIDE_SPAN = 50.0, 6.35, 90.0
guide_load = PROOF_N / 2.0
M_guide = guide_load * GUIDE_SPAN / 8.0     # fixed-fixed at the bolt lines
Z_guide = GUIDE_W * GUIDE_T ** 2 / 6.0
sig_guide = M_guide / Z_guide
print(f"[4] guide strap {GUIDE_W}x{GUIDE_T}mm, span {GUIDE_SPAN}mm, "
      f"load {guide_load:.0f}N -> sigma={sig_guide:.1f} MPa, "
      f"SF={AL['yield_MPa']/sig_guide:.2f}")

print("\n=== Step 3: ripple analysis ===")
print("Adding rung holes changes the base channel geometry -> a NEW version.")
print("P0015@v1's sign-off (#76) binds to its spec digest and does NOT carry")
print("over; the holed channel needs its own validation and sign-off.")

# ---------------------------------------------------------------------------
print("\n=== Step 4: create parts ===")
n_rungs = int((SECTION_LEN - 2 * END_MARGIN) // RUNG_SPACING) + 1
rung_zs = [END_MARGIN + i * RUNG_SPACING for i in range(n_rungs)]

holed = eng.edit_part("P0015@v1", {
    f"features.{2 + i}": {"op": "hole", "d": RUNG_HOLE_D,
                          "at": [0.0, z], "face": ">X"}
    for i, z in enumerate(rung_zs)
}, reason=(
    f"add {n_rungs} rung holes (d{RUNG_HOLE_D}mm at {RUNG_SPACING:.0f}mm "
    f"pitch) through both webs of the base channel. The signed v1 had none, "
    f"and a rail without rung holes is not a usable rail. Holes are on the "
    f"NEUTRAL AXIS (y=0) where bending stress is ~0, so the expected effect "
    f"on bending capacity is small; what they remove is shear area at the "
    f"point of maximum shear, hand-checked above at "
    f"{math.sqrt(3)*tau_web_holed:.1f} MPa von Mises"))["new_geometry_id"]
print(f"  holed base channel: {holed}")

pin = eng.create_part({
    "name": "lock-pivot-pin", "units": "mm", "density_kg_m3": 7850,
    "features": [{"op": "cylinder", "d": PIN_D, "h": PIN_LEN}],
}, reason=(
    f"lock pivot pin, d{PIN_D}mm x {PIN_LEN}mm, 1018 cold-finish steel. "
    f"Carries the whole fly section through the dog ({DOG_LOAD:.0f} N) in "
    f"combined bending and double shear. Predicted vM {vm_pin:.1f} MPa, "
    f"SF={STEEL['yield_MPa']/vm_pin:.2f} with a tight clevis"))["geometry_id"]
print(f"  pivot pin: {pin}")

shoe = eng.create_part({
    "name": "foot-shoe-bracket", "units": "mm", "density_kg_m3": 2700,
    "features": [
        # sole plate the rubber pad seats on
        {"op": "box", "x": SHOE_W, "y": SHOE_DEPTH, "z": SHOE_T},
        # upstand that bolts to the rail
        {"op": "box", "x": SHOE_W, "y": SHOE_T, "z": SHOE_RISE,
         "at": [0, (SHOE_DEPTH - SHOE_T) / 2.0, 0], "mode": "union"},
    ],
}, reason=(
    f"foot/shoe bracket, L-section {SHOE_W}mm wide, {SHOE_T}mm plate, "
    f"{SHOE_DEPTH}mm sole x {SHOE_RISE}mm upstand. Bolt holes are NOT "
    f"modelled: the bolted connection is represented by the fixed boundary "
    f"condition, and putting a hole a few mm from a fully-fixed face would "
    f"produce a constraint artefact rather than a real bearing stress. "
    f"Carries the base reaction COMPUTED by the kinematics base-slip run: "
    f"{N_FLOOR:.0f} N vertical and {F_FLOOR:.0f} N friction. Predicted "
    f"sigma={sig_shoe:.1f} MPa, SF={AL['yield_MPa']/sig_shoe:.2f}"))["geometry_id"]
print(f"  shoe bracket: {shoe}")

guide = eng.create_part({
    "name": "fly-guide-strap", "units": "mm", "density_kg_m3": 2700,
    "features": [
        {"op": "box", "x": GUIDE_SPAN, "y": GUIDE_T, "z": GUIDE_W},
    ],
}, reason=(
    f"fly-to-base guide strap, {GUIDE_SPAN}x{GUIDE_T}mm plate {GUIDE_W}mm "
    f"wide, bolted to both base webs (bolt holes not modelled - the fixed "
    f"end faces represent the bolted connection). Reacts the sections pushing apart at "
    f"the overlap; load taken as the section shear ({guide_load:.0f} N), a "
    f"conservative stand-in for a full composite-overlap transfer analysis. "
    f"Predicted sigma={sig_guide:.1f} MPa, "
    f"SF={AL['yield_MPa']/sig_guide:.2f}"))["geometry_id"]
print(f"  guide strap: {guide}")

# ---------------------------------------------------------------------------
print("\n=== Step 5: FEA ===")
results = {}


def report(name, r, pred=None):
    d = json.loads(eng.log.rows(action="fea_static")[-1]["details_json"])
    results[name] = r
    extra = ""
    if pred:
        extra = f"  (pred {pred:.1f}, {100*(r['max_von_mises_MPa']/pred-1):+.1f}%)"
    print(f"  {name}: {r['result']}  SF={r['safety_factor']:.3f}  "
          f"max_vM={r['max_von_mises_MPa']:.1f} MPa{extra}  "
          f"eq={d['equilibrium']['residual_rel']:.1e} "
          f"outlier={d['stress_outlier_ratio']}")
    return d


# [1] holed channel, same load case and mesh as the signed unholed run
r = eng.run_fea_static(holed, {
    "material": dict(AL), "mesh": {"max_size_mm": 15.0},
    "constraints": [
        {"where": {"axis": "z", "at": "min"}, "dof": [1, 2]},
        {"where": {"axis": "z", "at": "max"}, "dof": [1, 2]},
        {"where": {"axis": "z", "at": SECTION_LEN / 2.0, "tol": 1.0}, "dof": [3]}],
    "loads": [{"where": {"all": [{"axis": "y", "at": "max"},
                                 {"axis": "z", "at": SECTION_LEN / 2.0,
                                  "tol": 15.0}]},
               "force_total_N": [0, -PROOF_N, 0]}],
    "limit_state": {"name": "yield_von_mises", "required_SF": REQUIRED_SF},
}, reason=(
    f"holed base channel vs the OSHA 3.3x proof load, IDENTICAL load case and "
    f"15mm mesh to the signed unholed run (SF 1.994) so the two are directly "
    f"comparable. Question under test: do {n_rungs} neutral-axis rung holes "
    f"reduce bending capacity? NOTE this mesh does not resolve the local hole "
    f"concentration; the hole detail is covered by the hand calc above"))
report("holed channel", r, sig_b)

# [2] pivot pin: supported at the two clevis ears, dog bearing in the middle
r = eng.run_fea_static(pin, {
    "material": dict(STEEL), "mesh": {"max_size_mm": 0.9},
    "constraints": [
        {"where": {"axis": "z", "at": EAR_T / 2.0, "tol": EAR_T / 2.0},
         "dof": [1, 2]},
        {"where": {"axis": "z", "at": PIN_LEN - EAR_T / 2.0,
                   "tol": EAR_T / 2.0}, "dof": [1, 2]},
        {"where": {"axis": "z", "at": PIN_LEN / 2.0, "tol": 0.6}, "dof": [3]}],
    "loads": [{"where": {"all": [
        {"cylinder": {"axis": "z", "center": [0, 0], "r": PIN_D / 2.0,
                      "tol": 0.2, "half": [0, 1.0]}},
        {"axis": "z", "at": PIN_LEN / 2.0, "tol": (PIN_LEN - 2 * EAR_T) / 2.0}]},
        "force_total_N": [0, -DOG_LOAD, 0]}],
    "limit_state": {"name": "yield_von_mises", "required_SF": REQUIRED_SF},
}, reason=(
    f"lock pivot pin: supported at the two clevis ears ({EAR_T}mm each), dog "
    f"bearing on the upper half of the cylindrical surface between them "
    f"({DOG_LOAD:.0f} N). Predicted vM {vm_pin:.1f} MPa combining bending "
    f"{sig_pin:.1f} and double shear {tau_pin:.1f}"))
report("pivot pin", r, vm_pin)

# [3] shoe bracket: bolted at the upstand, load up through the sole
r = eng.run_fea_static(shoe, {
    "material": dict(AL), "mesh": {"max_size_mm": 2.5},
    "constraints": [{"where": {"axis": "z", "at": "max"}, "dof": [1, 2, 3]}],
    "loads": [{"where": {"all": [{"axis": "z", "at": "min"},
                                 {"axis": "y", "at": 0.0, "tol": 20.0}]},
               "force_total_N": [F_FLOOR, 0, N_FLOOR]}],
    "limit_state": {"name": "yield_von_mises", "required_SF": REQUIRED_SF},
}, reason=(
    f"foot/shoe bracket: bolted at the top of the upstand, ground reaction "
    f"applied to the sole underside - {N_FLOOR:.0f} N up AND {F_FLOOR:.0f} N "
    f"friction sideways, both taken from the kinematics base-slip run rather "
    f"than assumed. Predicted sigma={sig_shoe:.1f} MPa"))
report("shoe bracket", r, sig_shoe)

# [4] guide strap: bolted at both ends, fly pushing on the middle
r = eng.run_fea_static(guide, {
    "material": dict(AL), "mesh": {"max_size_mm": 2.2},
    "constraints": [
        {"where": {"axis": "x", "at": "min"}, "dof": [1, 2, 3]},
        {"where": {"axis": "x", "at": "max"}, "dof": [1, 2, 3]}],
    "loads": [{"where": {"all": [{"axis": "y", "at": "max"},
                                 {"axis": "x", "at": 0.0, "tol": 31.0}]},
               "force_total_N": [0, -guide_load, 0]}],
    "limit_state": {"name": "yield_von_mises", "required_SF": REQUIRED_SF},
}, reason=(
    f"fly guide strap: bolted (fixed) at both base webs, fly bearing on the "
    f"middle at the section shear ({guide_load:.0f} N). Fixed-fixed span "
    f"{GUIDE_SPAN}mm -> predicted sigma={sig_guide:.1f} MPa"))
report("guide strap", r, sig_guide)

print("\n=== summary ===")
for k, r in results.items():
    mark = "" if r["result"] == "pass" else "   <-- FAILS GATE"
    print(f"  {k:16s} {r['result']:4s}  SF={r['safety_factor']:.3f}{mark}")
print(f"\nparts: holed-base={holed}  pin={pin}  shoe={shoe}  guide={guide}")
print("NOT signed off -- Gideon's call.")
print(eng.generate_report())
