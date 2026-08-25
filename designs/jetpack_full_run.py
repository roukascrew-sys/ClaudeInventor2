"""Full jetpack: 4x JetCat P400-PRO side-pod turbojet rig -- Gideon 2026-08-25.

This supersedes designs/jetpack_frame_run.py (P0026@v3), which validated a
single frame member against room-temperature yield. Two capabilities added to
the engine for this build make that earlier result inadequate rather than
wrong, and both are exercised here:

  thermal_derated_yield   Gating a structure that sits beside four turbojets
                          on its 20 C yield overstates its strength. 6061-T6
                          retains only 55% of its proof strength at 250 C
                          (EN 1999-1-2 Table 1a).
  thrust_cg_alignment     A thrust resultant that misses the system centre of
                          mass applies a CONSTANT pitching moment for the
                          whole flight. No structural margin substitutes for
                          getting this right, and it is what forced the engine
                          layout below.

WHAT THIS IS AND IS NOT
----------------------------------------------------------------------------
This is a validated STRUCTURE plus a validated MASS/THRUST LAYOUT. It is not
an airworthy aircraft and this file makes no such claim. The engine has no
combustion, CFD, fatigue, or control-loop model, so the failure modes that
actually kill jetpack pilots are named as exclusions at the bottom of this
docstring, not quietly omitted.

ARCHITECTURE, AND WHY IT CHANGED FROM 5 ENGINES TO 4
----------------------------------------------------------------------------
Gideon selected 5x P400-PRO in a fixed-backpack layout. Five engines about a
symmetry plane forces one onto the centreline (x=0), and a 148.4 mm engine at
x=0 mounted behind the back both intersects the pilot's torso envelope and
exhausts 750 C gas straight down their back and legs. That is not a tuning
problem, so the count went to 4 in two side pods. Thrust drops 1985 -> 1588 N
and the mass budget had to follow; the resulting thrust/weight is reported
and gated rather than assumed.

The pods sit OUTBOARD (|x| = 350 and 540 mm) and nearly in the plane of the
pilot's back (y = +20 mm) rather than behind it. That is not styling: with
the engines slung 130 mm aft, the thrust line misses the system CG by ~170 mm
and needs a ~215 mm CG shift to trim, beyond any plausible pilot authority.
Outboard-and-in-plane brings the miss distance to ~77 mm. There is no body to
collide with at |x| > 275 mm, so y can be small there.

COORDINATES: origin at the pilot's back surface, waist height, on the body
centreline. +x = pilot's left, +y = aft (away from the back), +z = up.

SOURCED (verified, not invented)
----------------------------------------------------------------------------
  - JetCat P400-PRO: 397 N max thrust, 3.65 kg dry, 148.4 mm dia, EGT
    480-750 C, 1300 ml/min at max RPM. Cross-checked across JetCat's product
    page, Wikipedia and a retailer listing.
  - 6061-T6 derating: EN 1999-1-2:2007 Table 1a (k_0,2,theta, row EN AW-6061)
    and Table 2 (E_al,theta), extracted from the standard text.
  - Carbon steel derating: EN 1993-1-2:2005 Table 3.1 (k_y,theta and
    k_E,theta), extracted from the standard text.
  - 6061-T6511 and 1018 room-temperature properties: OnlineMetals product
    pages, reused from this project's ladder build.

STATED ASSUMPTIONS (mine, flagged as such, NOT code citations)
----------------------------------------------------------------------------
  - required_SF = 3.0. No OSHA/ANSI/FAA structural standard exists for a
    personal jet-propulsion frame. Carried over unchanged from the earlier
    frame run so the two are comparable.
  - Frame service temperature 150 C, cradle 400 C. NEITHER IS MEASURED. The
    script therefore also solves for the MAXIMUM temperature each part can
    tolerate at SF 3.0 and prints it as an acceptance criterion to be
    demonstrated by instrumented test before anything is built.
  - Pilot 90 kg (Gideon's choice) with CG at (0, -110, 0) mm: whole-body
    standing CG sits near waist height and near mid-torso-depth. Anthropometric
    estimate, not a measurement of any specific pilot.
  - Pilot trim authority 150 mm of system-CG shift by changing posture.
    Engineering assumption; needs validation with a real harnessed pilot.
  - Jet-A1 density 0.8 kg/L.

BOTH DERATING TABLES ARE FIRE-DESIGN TABLES. EN 1999-1-2 is stated for up to
2 hours thermal exposure and EN 1993-1-2 Table 3.1 gives an effective yield at
2% strain for short-duration fire. Neither covers CREEP or thermal-cycling
fatigue under repeated service, which is exactly what a jetpack does to its
structure. Using them here is a defensible and conservative-in-direction
choice for peak strength, and an acknowledged gap for endurance.

NOT BUILT, NOT VALIDATED -- named, not hidden
----------------------------------------------------------------------------
  - Propulsion entirely: combustion, thrust generation, FADEC, fuel delivery,
    starting, surge/flameout.
  - Fuel tank structure, baffling, venting, fire and crash-worthiness. The
    tank appears only as a sourced point mass.
  - The bracket joining each cradle to the crossbeam, and every bolted joint's
    local bearing / hole-edge stress. Same exclusion already standing on the
    ladder parts.
  - Harness webbing, buckles, and the pilot-side load spreading. The spine's
    lug BORES are validated; what attaches to them is not.
  - Fatigue and vibration. The turbines run to 98,000 RPM.
  - Exhaust plume mapping. Pod placement clears the torso envelope
    geometrically; whether the plume clears the pilot's ARMS and legs needs
    CFD or a tethered test.
  - Flight control, attitude stability, pilot workload, and any form of
    physical qualification testing.

Run with the project venv from the repo root:
    .venv\\Scripts\\python.exe designs\\jetpack_full_run.py
"""

import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from design_engine import DesignEngine
from design_engine.fea import derate_factor
from design_engine.geometry import SpecError

ROOT = Path(__file__).parent.parent / "data"
eng = DesignEngine(ROOT)

# --------------------------------------------------------------------------
# Sourced material data
# --------------------------------------------------------------------------
EC9_SRC = ("EN 1999-1-2:2007 Table 1a (0,2% proof strength ratios k_0,2,theta, "
           "row EN AW-6061 T6) and Table 2 (modulus E_al,theta / 70000), both "
           "stated for up to 2 hours thermal exposure. FIRE-DESIGN data: does "
           "not cover creep or thermal cycling under repeated service.")
K02_6061 = [[20, 1.00], [100, 0.95], [150, 0.91], [200, 0.79],
            [250, 0.55], [300, 0.31], [350, 0.10], [550, 0.0]]
KE_6XXX = [[20, 1.00], [50, 0.99], [100, 0.97], [150, 0.93], [200, 0.86],
           [250, 0.78], [300, 0.68], [350, 0.54], [400, 0.40], [550, 0.0]]

EC3_SRC = ("EN 1993-1-2:2005 Table 3.1 (k_y,theta effective yield and "
           "k_E,theta) for carbon steel. FIRE-DESIGN data at 2% total strain, "
           "short duration; does not cover creep or thermal cycling. Applied "
           "to 1018, a structural carbon steel.")
KY_STEEL = [[20, 1.000], [100, 1.000], [200, 1.000], [300, 1.000],
            [400, 1.000], [500, 0.780], [600, 0.470], [700, 0.230],
            [800, 0.110], [900, 0.060], [1000, 0.040], [1100, 0.020],
            [1200, 0.0]]
KE_STEEL = [[20, 1.000], [100, 1.000], [200, 0.900], [300, 0.800],
            [400, 0.700], [500, 0.600], [600, 0.310], [700, 0.130],
            [800, 0.090], [900, 0.0675], [1000, 0.045], [1100, 0.0225],
            [1200, 0.0]]

FRAME_TEMP_C = 150.0     # STATED ASSUMPTION - see docstring
CRADLE_TEMP_C = 400.0    # STATED ASSUMPTION - see docstring

AL = {"name": "6061-T6511", "E_MPa": 68900, "nu": 0.33, "yield_MPa": 276,
      "source": "OnlineMetals product pages (pid 1145, 1087), reused from "
                "this project's ladder build: yield 40 ksi, ultimate 42 ksi. "
                "E and nu are standard published values for wrought aluminium "
                "(E~69 GPa, nu~0.33).",
      "service_temp_C": FRAME_TEMP_C,
      "yield_derate_curve": K02_6061, "E_derate_curve": KE_6XXX,
      "derate_source": EC9_SRC}

STEEL = {"name": "1018-cold-finish", "E_MPa": 205000, "nu": 0.29,
         "yield_MPa": 372,
         "source": "OnlineMetals pid 4790 (ASTM-A108): yield 54 ksi, ultimate "
                   "64 ksi. E and nu are standard published values for carbon "
                   "steel (E~205 GPa, nu~0.29).",
         "service_temp_C": CRADLE_TEMP_C,
         "yield_derate_curve": KY_STEEL, "E_derate_curve": KE_STEEL,
         "derate_source": EC3_SRC}

REQUIRED_SF = 3.0

# --------------------------------------------------------------------------
# Propulsion (sourced input boundary condition, NOT computed by this engine)
# --------------------------------------------------------------------------
THRUST_N = 397.0
ENGINE_DRY_KG = 3.65
ENGINE_DIA = 148.4
ENGINE_FUEL_ML_MIN = 1300.0
N_ENGINES = 4
TOTAL_THRUST_N = THRUST_N * N_ENGINES

STATION_X = [-540.0, -350.0, 350.0, 540.0]
POD_Y = 20.35            # engine centreline, just aft of the back surface
ENGINE_Z = 40.0          # engine centre of mass height

# --------------------------------------------------------------------------
# Geometry (all axis-aligned: the geometry layer has no rotation op)
# --------------------------------------------------------------------------
SPINE_X, SPINE_Y, SPINE_Z = 50.8, 25.4, 450.0      # 2in x 1in bar, 450 tall
SPINE_AT = [0.0, POD_Y, -100.0]                    # global placement
LUG_D = 6.5                                        # M6 clearance
LUG_SHOULDER_LOCAL_Z = 400.0                       # -> global z = +300
LUG_WAIST_LOCAL_Z = 40.0                           # -> global z = -60

# 1280 not 1080: the outer engine sits AT x=540, and its 175mm cradle would
# overhang a beam that stopped there. Beam length does not change the root
# moment (that is set by the engine stations), only mass.
CB_LEN, CB_Y, CB_Z = 1280.0, 12.7, 50.8            # 1/2in x 2in flat bar
# Attempt 2 section, after the v1 gate failure (see the loop in Step 6):
# 3/4in x 2in bar with a local doubler pad over the spine joint.
CB_Y2 = 19.05                                      # 3/4in x 2in flat bar
PAD_X, PAD_Y, PAD_Z = 240.0, 38.1, 50.8            # bolted lap-joint pad
CB_AT = [0.0, POD_Y, 125.0]
CB_MID_Z = CB_AT[2] + CB_Z / 2.0                   # 150.4

CRADLE_SIDE, CRADLE_T = 175.0, 8.0
CRADLE_BORE = ENGINE_DIA
CRADLE_BOLT_XY = 72.0
CRADLE_Z = 100.0                                   # top of plate under beam

# fuel: sized to the mass budget, endurance reported not assumed
FUEL_KG = 12.0
TANK_KG = 2.5
FUEL_AT = [0.0, 140.0, 60.0]
PILOT_KG = 90.0
PILOT_AT = [0.0, -110.0, 0.0]
PILOT_TRIM_MM = 150.0

# MESH_SPINE 3.0 not 4.0: at 4.0mm the mesher produced 1 degenerate
# element of 44431 (Jacobian -12.48) at a 6.5mm lug hole and the quality
# gate refused the mesh - correctly, CalculiX would have aborted on it.
MESH_SPINE, MESH_CB, MESH_CB_BUCKLE, MESH_CRADLE = 3.0, 5.0, 6.0, 2.5


def max_service_temp(sigma_MPa, mat, curve):
    """Highest temperature at which this part still clears the gate.

    Solves yield_room * k(T) / sigma = required_SF for T by scanning the
    sourced curve. This converts an unmeasured input (how hot does it
    actually get?) into a stated acceptance criterion that a build must
    demonstrate by instrumented test.
    """
    need_k = REQUIRED_SF * sigma_MPa / mat["yield_MPa"]
    if need_k > 1.0:
        return None                      # fails even at room temperature
    best = None
    for i in range(len(curve) - 1):
        t0, k0 = curve[i]
        t1, k1 = curve[i + 1]
        if k0 >= need_k >= k1 and k0 != k1:
            best = t0 + (k0 - need_k) / (k0 - k1) * (t1 - t0)
    return best if best is not None else curve[-1][0]


print("=== Step 1: log state ===")
print(f"pending: {len(eng.log.pending_actions())}  "
      f"existing failures: {len(eng.log.failures())}")

# --------------------------------------------------------------------------
print("\n=== Step 2: predictions (hand calculations, before any solver run) ===")
k_frame = derate_factor(K02_6061, FRAME_TEMP_C, "frame")
k_cradle = derate_factor(KY_STEEL, CRADLE_TEMP_C, "cradle")
allow_frame = AL["yield_MPa"] * k_frame
allow_cradle = STEEL["yield_MPa"] * k_cradle
print(f"engines: {N_ENGINES}x JetCat P400-PRO @ {THRUST_N:.0f} N = "
      f"{TOTAL_THRUST_N:.0f} N ({TOTAL_THRUST_N/9.80665:.1f} kgf)")
print(f"6061-T6 @ {FRAME_TEMP_C:.0f} C: k={k_frame:.3f} -> allowable "
      f"{allow_frame:.1f} MPa (room {AL['yield_MPa']} MPa)")
print(f"1018 steel @ {CRADLE_TEMP_C:.0f} C: k={k_cradle:.3f} -> allowable "
      f"{allow_cradle:.1f} MPa (room {STEEL['yield_MPa']} MPa)")

# crossbeam: cantilever each side of the spine, 2 engines per side
cb_root_x = SPINE_X / 2.0
M_cb = sum(THRUST_N * (abs(x) - cb_root_x) for x in STATION_X if x > 0)
Z_cb = CB_Y * CB_Z ** 2 / 6.0
I_cb = CB_Y * CB_Z ** 3 / 12.0
sig_cb = M_cb / Z_cb
print(f"\ncrossbeam {CB_Y}x{CB_Z}mm, cantilever root at x={cb_root_x:.1f}mm:")
print(f"  M={M_cb:.0f} N*mm  Z={Z_cb:.0f} mm^3  sigma={sig_cb:.1f} MPa  "
      f"SF={allow_frame/sig_cb:.2f} @ {FRAME_TEMP_C:.0f} C "
      f"(room-temperature SF would be {AL['yield_MPa']/sig_cb:.2f})")
t_cb = max_service_temp(sig_cb, AL, K02_6061)
print(f"  MAX PERMISSIBLE TEMPERATURE at SF {REQUIRED_SF}: {t_cb:.0f} C")

# tip deflection tilts the thrust vector - reported, not gated
d_tip = sum(THRUST_N * a ** 2 * (3 * (STATION_X[-1] - cb_root_x) - a)
            / (6 * AL["E_MPa"] * k_frame * I_cb)
            for a in [x - cb_root_x for x in STATION_X if x > 0])
print(f"  tip deflection {d_tip:.2f} mm over "
      f"{STATION_X[-1]-cb_root_x:.0f} mm -> thrust vector tilt "
      f"~{math.degrees(math.atan(d_tip/(STATION_X[-1]-cb_root_x))):.2f} deg "
      f"(reported, not gated)")

# spine: symmetric flight is axial; the design case is one engine out
A_spine = SPINE_X * SPINE_Y
sig_spine_axial = TOTAL_THRUST_N / A_spine
M_oeo = THRUST_N * (abs(STATION_X[-1]) - cb_root_x)
Z_spine_weak = SPINE_Y * SPINE_X ** 2 / 6.0
sig_oeo = M_oeo / Z_spine_weak
print(f"\nspine {SPINE_X}x{SPINE_Y}mm:")
print(f"  symmetric flight: axial only, sigma={sig_spine_axial:.2f} MPa "
      f"(outrigger moments cancel by symmetry)")
print(f"  ONE ENGINE OUT: M={M_oeo:.0f} N*mm -> sigma={sig_oeo:.1f} MPa, "
      f"SF={allow_frame/sig_oeo:.2f} @ {FRAME_TEMP_C:.0f} C  [hand calc]")

# cradle: bore-to-bolt bending in the plate
bolt_r = math.hypot(CRADLE_BOLT_XY, CRADLE_BOLT_XY)
span = bolt_r - CRADLE_BORE / 2.0
M_cr = (THRUST_N / 4.0) * span
Z_cr = 50.0 * CRADLE_T ** 2 / 6.0          # ~50mm effective width per bolt
sig_cr = M_cr / Z_cr
print(f"\ncradle {CRADLE_SIDE}x{CRADLE_SIDE}x{CRADLE_T}mm steel, bore "
      f"{CRADLE_BORE}mm, bolts at r={bolt_r:.1f}mm:")
print(f"  span bore->bolt {span:.1f} mm, sigma={sig_cr:.1f} MPa, "
      f"SF={allow_cradle/sig_cr:.1f} @ {CRADLE_TEMP_C:.0f} C")

assert allow_frame / sig_cb >= REQUIRED_SF, "crossbeam must clear the gate by hand first"
assert allow_frame / sig_oeo >= REQUIRED_SF, "spine one-engine-out must clear by hand"
assert allow_cradle / sig_cr >= REQUIRED_SF, "cradle must clear the gate by hand"

# --------------------------------------------------------------------------
print("\n=== Step 3: mass budget and thrust/weight (context for the gate) ===")
vol_spine = SPINE_X * SPINE_Y * SPINE_Z
vol_cb = CB_LEN * CB_Y * CB_Z   # attempt-1 section; recomputed after the gate loop
vol_cradle = (CRADLE_SIDE ** 2 - math.pi * (CRADLE_BORE / 2) ** 2) * CRADLE_T
m_spine = vol_spine * 1e-9 * 2700
m_cb = vol_cb * 1e-9 * 2700
m_cradle = vol_cradle * 1e-9 * 7850
m_frame = m_spine + m_cb + 4 * m_cradle
m_total = PILOT_KG + N_ENGINES * ENGINE_DRY_KG + FUEL_KG + TANK_KG + m_frame
twr = TOTAL_THRUST_N / (m_total * 9.80665)
endurance = FUEL_KG / 0.8 / (N_ENGINES * ENGINE_FUEL_ML_MIN / 1000.0)
print(f"spine {m_spine:.2f} kg + crossbeam {m_cb:.2f} kg + 4 cradles "
      f"{4*m_cradle:.2f} kg = frame {m_frame:.2f} kg")
print(f"total system {m_total:.1f} kg -> T/W = {twr:.3f}")
print(f"endurance {endurance:.1f} min at CONTINUOUS MAX thrust. Hover needs "
      f"only ~{1/twr*100:.0f}% throttle so real endurance is longer, but the "
      f"engine's thrust-vs-fuel-flow curve is not in hand, so no hover "
      f"endurance is claimed.")

print("\n=== Step 4: ripple analysis ===")
print("Supersedes P0026@v3 (jetpack-spine-frame). That part was gated on "
      "room-temperature yield with no thermal limit state and no CG check, "
      "and its 5-engine layout puts an engine on the pilot's centreline. It "
      "is NOT reused here. Its sign-off stands only for what it claimed.")

# --------------------------------------------------------------------------
print("\n=== Step 5: create_part ===")


def add_hole_trying_signs(base_features, face, u, v, tag, d=LUG_D):
    """Place a hole, trying both signs of the in-plane u coordinate.

    Face-local (u, v) axes are face-relative and can be mirrored, and the
    hole guard REFUSES a placement that removes no material, so a wrong guess
    fails loud instead of producing a part with a hole that isn't there.
    """
    for uu in (u, -u):
        trial = base_features + [{"op": "hole", "d": d, "at": [uu, v],
                                  "face": face}]
        try:
            from design_engine.geometry import build
            build({"name": "probe", "units": "mm", "features": trial})
            print(f"  {tag}: face {face} at [{uu}, {v}] -> OK")
            return trial
        except SpecError as exc:
            print(f"  {tag}: face {face} at [{uu}, {v}] -> refused ({exc})")
    raise SpecError(f"could not place {tag}")


spine_feats = [{"op": "box", "x": SPINE_X, "y": SPINE_Y, "z": SPINE_Z,
                "at": [0, 0, 0]}]
spine_feats = add_hole_trying_signs(spine_feats, ">X", 0.0,
                                    LUG_SHOULDER_LOCAL_Z, "shoulder lug")
spine_feats = add_hole_trying_signs(spine_feats, ">X", 0.0,
                                    LUG_WAIST_LOCAL_Z, "waist lug")
spine = eng.create_part({
    "name": "jetpack-spine", "units": "mm", "density_kg_m3": 2700,
    "features": spine_feats,
}, reason=(f"jetpack backbone, carries {TOTAL_THRUST_N:.0f} N thrust into two "
           f"harness lugs; one-engine-out hand SF={allow_frame/sig_oeo:.2f} "
           f"at {FRAME_TEMP_C:.0f} C"))
SPINE_ID = spine["geometry_id"]
print(f"spine {SPINE_ID}: {spine['properties']['mass_kg_estimate']:.3f} kg")

crossbeam = eng.create_part({
    "name": "jetpack-crossbeam", "units": "mm", "density_kg_m3": 2700,
    "features": [{"op": "box", "x": CB_LEN, "y": CB_Y, "z": CB_Z,
                  "at": [0, 0, 0]}],
}, reason=(f"engine crossbeam, 2 engines per side at |x|=350/540mm; "
           f"predicted root sigma={sig_cb:.1f} MPa, "
           f"SF={allow_frame/sig_cb:.2f} at {FRAME_TEMP_C:.0f} C"))
CB_ID = crossbeam["geometry_id"]
print(f"crossbeam {CB_ID}: {crossbeam['properties']['mass_kg_estimate']:.3f} kg")

cradle_feats = [
    {"op": "box", "x": CRADLE_SIDE, "y": CRADLE_SIDE, "z": CRADLE_T,
     "at": [0, 0, 0]},
    {"op": "cylinder", "d": CRADLE_BORE, "h": CRADLE_T + 2.0,
     "at": [0, 0, -1.0], "mode": "cut"},
]
for sx in (-1, 1):
    for sy in (-1, 1):
        cradle_feats = cradle_feats + [
            {"op": "hole", "d": LUG_D,
             "at": [sx * CRADLE_BOLT_XY, sy * CRADLE_BOLT_XY], "face": ">Z"}]
cradle = eng.create_part({
    "name": "jetpack-engine-cradle", "units": "mm", "density_kg_m3": 7850,
    "features": cradle_feats,
}, reason=(f"engine cradle ring, 1018 steel for {CRADLE_TEMP_C:.0f} C service "
           f"beside a 480-750 C EGT turbine; carries {THRUST_N:.0f} N into 4 "
           f"M6 bolts"))
CRADLE_ID = cradle["geometry_id"]
print(f"cradle {CRADLE_ID}: {cradle['properties']['mass_kg_estimate']:.3f} kg")

# --------------------------------------------------------------------------
print("\n=== Step 6: FEA - crossbeam, thermal_derated_yield ===")
cb_case = {
    "material": dict(AL),
    "mesh": {"max_size_mm": MESH_CB},
    "constraints": [
        {"where": {"all": [{"axis": "y", "at": "max"},
                           {"axis": "x", "at": 0.0, "tol": 40.0}]},
         "dof": [1, 2, 3]},
    ],
    "loads": [
        {"where": {"all": [{"axis": "z", "at": "min"},
                           {"axis": "x", "at": x, "tol": 30.0}]},
         "force_total_N": [0.0, 0.0, THRUST_N]}
        for x in STATION_X
    ],
    "limit_state": {"name": "thermal_derated_yield",
                    "required_SF": REQUIRED_SF},
}
cb_out = eng.run_fea_static(CB_ID, cb_case, reason=(
    f"crossbeam bending under 4x{THRUST_N:.0f} N at |x|=350/540mm, bolted to "
    f"the spine over an 80mm footprint. Derated to {FRAME_TEMP_C:.0f} C "
    f"(k={k_frame:.3f}); predicted sigma={sig_cb:.1f} MPa"))


def report_static(tag, out):
    print(f"  [{tag}] result={out['result']}  SF={out['safety_factor']:.3f}  "
          f"max_vM={out['max_von_mises_MPa']:.1f} MPa  "
          f"allowable={out['allowable_MPa']:.1f} MPa")
    print(f"        peak at {out['max_von_mises_at_mm']} mm, median "
          f"{out['median_von_mises_MPa']:.1f}, p99.9 "
          f"{out['p99_9_von_mises_MPa']:.1f} MPa, outlier ratio "
          f"{out['stress_outlier_ratio']:.2f}")
    if out.get("stress_outlier_warning"):
        print(f"        ADVISORY: {out['stress_outlier_warning']}")


report_static("v1", cb_out)

if cb_out["result"] == "fail":
    print("\n  --- Attempt 1 failed the gate. Non-linear gate contract: the "
          "failure record is already in the log; this edit REFERENCES it "
          "rather than retrying blind. ---")
    print("  Diagnosis: the peak sits on the corner of the constraint patch "
          "with an outlier ratio well above the engine's 2.0 artifact "
          "threshold, while the median and p99.9 straddle the hand-calculated "
          "nominal. A rigid all-DOF patch on ONE face is an infinitely stiff "
          "corner - that is a boundary-condition defect, not a weak beam.")
    print("  Fix (a real joint, not a softer BC): thicken the bar to 3/4in "
          "and add a 240mm doubler pad so the spine clamps a genuine bolted "
          "lap joint across BOTH faces, moving the geometric step outboard "
          "to where the bending moment is lower.")
    cb_v2 = eng.edit_part(
        CB_ID,
        {"features": [
            {"op": "box", "x": CB_LEN, "y": CB_Y2, "z": CB_Z, "at": [0, 0, 0]},
            {"op": "box", "x": PAD_X, "y": PAD_Y, "z": PAD_Z,
             "at": [0, 0, 0], "mode": "union"},
        ]},
        reason=("crossbeam attempt 2: 3/4in bar + 240mm bolted lap-joint "
                "doubler pad, replacing the single-face rigid patch that "
                "produced a constraint-corner artifact"),
        addresses_failure_id=cb_out["failure_id"])
    CB_ID = cb_v2["new_geometry_id"]
    print(f"  -> {CB_ID}, {cb_v2['properties']['mass_kg_estimate']:.3f} kg")

    Z_cb2 = CB_Y2 * CB_Z ** 2 / 6.0
    print(f"  nominal root stress now {M_cb/Z_cb2:.1f} MPa "
          f"(SF {allow_frame/(M_cb/Z_cb2):.2f} at {FRAME_TEMP_C:.0f} C)")

    # clamp BOTH pad faces: y=max/min now exist only on the pad, so these
    # selectors pick the real 240x50.8 bolted footprint automatically
    cb_case = dict(cb_case)
    cb_case["constraints"] = [
        {"where": {"axis": "y", "at": "max"}, "dof": [1, 2, 3]},
        {"where": {"axis": "y", "at": "min"}, "dof": [1, 2, 3]},
    ]
    cb_out = eng.run_fea_static(CB_ID, cb_case, reason=(
        f"crossbeam attempt 2: 3/4in bar + doubler pad, clamped across both "
        f"pad faces over the real 240mm bolted footprint. Predicted nominal "
        f"root sigma={M_cb/Z_cb2:.1f} MPa at {FRAME_TEMP_C:.0f} C"))
    report_static("v2", cb_out)
    sig_cb = M_cb / Z_cb2
    t_cb = max_service_temp(sig_cb, AL, K02_6061)
    print(f"  MAX PERMISSIBLE TEMPERATURE at SF {REQUIRED_SF}: {t_cb:.0f} C")

    m_cb2 = CB_LEN * CB_Y2 * CB_Z * 1e-9 * 2700 + (
        PAD_X * (PAD_Y - CB_Y2) * PAD_Z * 1e-9 * 2700)
    m_total2 = (PILOT_KG + N_ENGINES * ENGINE_DRY_KG + FUEL_KG + TANK_KG
                + m_spine + m_cb2 + 4 * m_cradle)
    print(f"  mass budget after the fix: crossbeam {m_cb2:.2f} kg "
          f"(was {m_cb:.2f}), system {m_total2:.1f} kg, "
          f"T/W {TOTAL_THRUST_N/(m_total2*9.80665):.3f}")

print("\n=== Step 7: FEA - crossbeam, elastic_buckling ===")
print("  (a 1080mm beam only 12.7mm thick is a real lateral-torsional "
      "buckling candidate - this is the credible instability, not the "
      "stocky spine)")
cb_buckle_case = dict(cb_case)
cb_buckle_case["mesh"] = {"max_size_mm": MESH_CB_BUCKLE}
cb_buckle_case["limit_state"] = {"name": "elastic_buckling",
                                 "required_SF": REQUIRED_SF}
cb_buck = eng.run_fea_buckling(CB_ID, cb_buckle_case, reason=(
    f"crossbeam lateral-torsional buckling under the same 4-engine load "
    f"pattern, modulus derated to {FRAME_TEMP_C:.0f} C"))
print(f"  result={cb_buck['result']}  SF={cb_buck.get('safety_factor')}")

print("\n=== Step 8: FEA - spine, thermal_derated_yield ===")
spine_case = {
    "material": dict(AL),
    "mesh": {"max_size_mm": MESH_SPINE},
    "constraints": [
        {"where": {"cylinder": {"axis": "x", "center": [0.0, LUG_SHOULDER_LOCAL_Z],
                                "r": LUG_D / 2.0, "tol": 0.7}},
         "dof": [1, 2, 3]},
        {"where": {"cylinder": {"axis": "x", "center": [0.0, LUG_WAIST_LOCAL_Z],
                                "r": LUG_D / 2.0, "tol": 0.7}},
         "dof": [1, 2, 3]},
    ],
    "loads": [
        {"where": {"all": [{"axis": "y", "at": "min"},
                           {"axis": "z", "at": CB_MID_Z - SPINE_AT[2],
                            "tol": 26.0}]},
         "force_total_N": [0.0, 0.0, TOTAL_THRUST_N]},
    ],
    "limit_state": {"name": "thermal_derated_yield",
                    "required_SF": REQUIRED_SF},
}
spine_out = eng.run_fea_static(SPINE_ID, spine_case, reason=(
    f"spine carries {TOTAL_THRUST_N:.0f} N from the crossbeam joint into the "
    f"two harness lug bores. Derated to {FRAME_TEMP_C:.0f} C. This is the "
    f"symmetric case; one-engine-out is a hand calc "
    f"(sigma={sig_oeo:.1f} MPa)"))
report_static("spine", spine_out)

print("\n=== Step 9: FEA - cradle, thermal_derated_yield (steel, 400 C) ===")
cradle_case = {
    "material": dict(STEEL),
    "mesh": {"max_size_mm": MESH_CRADLE},
    "constraints": [
        {"where": {"cylinder": {"axis": "z",
                                "center": [sx * CRADLE_BOLT_XY,
                                           sy * CRADLE_BOLT_XY],
                                "r": LUG_D / 2.0, "tol": 0.7}},
         "dof": [1, 2, 3]}
        for sx in (-1, 1) for sy in (-1, 1)
    ],
    "loads": [
        {"where": {"cylinder": {"axis": "z", "center": [0.0, 0.0],
                                "r": CRADLE_BORE / 2.0, "tol": 0.7}},
         "force_total_N": [0.0, 0.0, THRUST_N]},
    ],
    "limit_state": {"name": "thermal_derated_yield",
                    "required_SF": REQUIRED_SF},
}
cradle_out = eng.run_fea_static(CRADLE_ID, cradle_case, reason=(
    f"cradle carries one engine's {THRUST_N:.0f} N from the bore into 4 M6 "
    f"bolts at {CRADLE_TEMP_C:.0f} C (k_y={k_cradle:.3f}). Bore traction is "
    f"the engine's clamp reaction, a documented simplification of contact"))
report_static("cradle", cradle_out)

# --------------------------------------------------------------------------
print("\n=== Step 10: assembly + tolerance stackup ===")
components = [
    {"geometry_id": SPINE_ID, "at": SPINE_AT, "ref": "spine"},
    {"geometry_id": CB_ID, "at": CB_AT, "ref": "crossbeam"},
]
for i, x in enumerate(STATION_X):
    components.append({"geometry_id": CRADLE_ID,
                       "at": [x, POD_Y, CRADLE_Z],
                       "ref": f"cradle{i+1}"})

asm = eng.create_assembly({
    "name": "jetpack-4x-p400", "units": "mm",
    "components": components,
    "chains": [{
        "name": "engine-casing-gap-inner-to-outer",
        # inner engine at |x|=350, outer at |x|=540: the casings must not
        # touch. 190mm centres minus one full diameter is the nominal gap.
        "requirement_mm": {"min": 10.0},
        "terms": [
            {"desc": "station spacing 350->540", "nominal": 190.0,
             "tol_plus": 1.0, "tol_minus": 1.0, "sense": 1},
            {"desc": "engine casing diameter", "nominal": ENGINE_DIA,
             "tol_plus": 0.5, "tol_minus": 0.5, "sense": -1},
        ],
    }, {
        "name": "inner-pod-to-shoulder-clearance",
        # pilot shoulder half-width taken as 240mm (stated assumption)
        "requirement_mm": {"min": 15.0},
        "terms": [
            {"desc": "inner engine station", "nominal": 350.0,
             "tol_plus": 1.0, "tol_minus": 1.0, "sense": 1},
            {"desc": "engine casing radius", "nominal": ENGINE_DIA / 2.0,
             "tol_plus": 0.25, "tol_minus": 0.25, "sense": -1},
            {"desc": "pilot shoulder half-width (stated assumption)",
             "nominal": 240.0, "tol_plus": 15.0, "tol_minus": 15.0,
             "sense": -1},
        ],
    }],
}, reason="full jetpack: spine + crossbeam + 4 engine cradles")
ASM_ID = asm["assembly_id"]
print(f"assembly {ASM_ID} with {len(components)} components")
stack = eng.check_tolerance_stackup(ASM_ID)
print(f"stackup worst-case margin {stack['worst_case_mm']:.2f} mm")
for c in stack["report"]["chains"]:
    print(f"  {c['name']}: {c['result']}, worst-case "
          f"[{c['worst_case_mm']['min']:.1f}, {c['worst_case_mm']['max']:.1f}] mm")

# --------------------------------------------------------------------------
print("\n=== Step 11: mass properties - thrust_cg_alignment + thrust_to_weight ===")
mp_case = {
    "point_masses": [
        {"name": "pilot + flight suit + helmet", "mass_kg": PILOT_KG,
         "at_mm": PILOT_AT,
         "source": "Gideon's stated design pilot mass (90 kg). CG placed at "
                   "waist height, mid-torso-depth from anthropometric "
                   "standing-CG data - an estimate, not a measurement."},
        {"name": "fuel (Jet-A1) + tank", "mass_kg": FUEL_KG + TANK_KG,
         "at_mm": FUEL_AT,
         "source": "fuel mass from the endurance budget at 0.8 kg/L; tank "
                   "mass is a stated allowance for a COTS aluminium cell, "
                   "not a designed part."},
    ] + [
        {"name": f"JetCat P400-PRO #{i+1}", "mass_kg": ENGINE_DRY_KG,
         "at_mm": [x, POD_Y, ENGINE_Z],
         "source": "JetCat P400-PRO published dry mass 3.65 kg"}
        for i, x in enumerate(STATION_X)
    ],
    "thrust": [
        {"name": f"engine {i+1}", "force_N": [0.0, 0.0, THRUST_N],
         "at_mm": [x, POD_Y, ENGINE_Z]}
        for i, x in enumerate(STATION_X)
    ],
    "pilot": {"mass_kg": PILOT_KG, "max_cg_shift_mm": PILOT_TRIM_MM},
    "limit_states": [
        {"name": "thrust_cg_alignment", "max_offset_mm": 100.0},
        {"name": "thrust_to_weight", "min_ratio": 1.15},
    ],
}
mp = eng.check_mass_properties(ASM_ID, mp_case, reason=(
    "does the thrust resultant pass close enough to the system CG for the "
    "pilot to trim it by leaning, and is there enough thrust to fly? "
    "max_offset_mm=100 chosen so the required CG shift stays inside the "
    "stated 150mm pilot trim authority at this T/W"))
print(f"  result={mp['result']}")
print(f"  total mass {mp['total_mass_kg']:.1f} kg, CG at "
      f"{mp['centre_of_mass_mm']} mm")
print(f"  thrust {mp['thrust_magnitude_N']:.0f} N, T/W {mp['thrust_to_weight']:.3f}")
print(f"  thrust line misses CG by {mp['thrust_cg_offset_mm']:.1f} mm")
if mp["pilot_trim"]:
    t = mp["pilot_trim"]
    print(f"  constant pitching moment {t['pitch_moment_Nm']:.1f} N*m -> pilot "
          f"must hold a {t['required_cg_shift_mm']:.0f} mm CG shift "
          f"(authority {t['available_cg_shift_mm']:.0f} mm, "
          f"within={t['within_authority']})")
for r in mp["limit_states"]:
    print(f"  {r['limit_state']}: {r['result']}")

# --------------------------------------------------------------------------
print("\n=== Step 12: summary ===")
results = {"crossbeam yield": cb_out["result"],
           "crossbeam buckling": cb_buck["result"],
           "spine yield": spine_out["result"],
           "cradle yield": cradle_out["result"],
           "mass properties": mp["result"],
           "tolerance stackup": "pass" if stack["worst_case_mm"] >= 0 else "fail"}
for k, v in results.items():
    print(f"  {k:24s} {v}")
all_pass = all(v == "pass" for v in results.values())
print(f"\nALL GATES PASS: {all_pass}")

print(f"\nMAXIMUM PERMISSIBLE STRUCTURE TEMPERATURE (acceptance criterion "
      f"for a build, to be demonstrated by instrumented test):")
print(f"  crossbeam: {t_cb:.0f} C   (assumed {FRAME_TEMP_C:.0f} C)")

summary = {
    "assembly_id": ASM_ID,
    "parts": {"spine": SPINE_ID, "crossbeam": CB_ID, "cradle": CRADLE_ID},
    "results": results, "all_pass": all_pass,
    "mass_properties": mp,
    "max_permissible_crossbeam_temp_C": t_cb,
}
out_path = ROOT / "jetpack_full_summary.json"
out_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
print(f"\nsummary written to {out_path}")
