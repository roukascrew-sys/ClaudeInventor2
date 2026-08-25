"""Jetpack propulsion-mount frame: fixed 5-engine backpack architecture --
Gideon 2026-08-24, run as an explicit "experimental test of something not
made yet".

WHAT THIS ENGINE CAN AND CANNOT VALIDATE ABOUT A JETPACK
----------------------------------------------------------------------------
This engine has two validation tools: CalculiX static/buckling FEA (structural
stress and elastic instability) and Chrono rigid-body kinematics (joint
reaction forces). It has NO propulsion model -- no combustion, no thrust
generation, no fuel chemistry, no flight-control stability. A jetpack's
defining engineering problem IS its propulsion, and that problem is entirely
outside this tool. What follows is therefore NOT "a jetpack design" in the
full sense -- it is the structural frame that carries a real, sourced
turbine's published thrust into a wearable harness, using the exact same
yield/buckling gate discipline as every other part in this project. The
turbine's thrust and mass are an INPUT boundary condition, not something this
engine calculated or validated.

ARCHITECTURE (per Gideon's explicit choice)
----------------------------------------------------------------------------
Fixed backpack (Bell Rocket Belt style): all engines rigid-mounted, no
articulation, thrust vector fixed vertical. No active thrust vectoring and
no kinematics layer -- control would be by pilot body lean only, a real and
significant limitation relative to an articulated jet-suit, stated not
hidden.

SOURCED (verified this session, not invented)
----------------------------------------------------------------------------
  - Propulsion: JetCat P400-PRO turbojet (per Gideon's choice: 5x, jet-suit-
    class engine). Max thrust 397 N at 98,000 RPM; dry mass 3.65 kg; diameter
    148.4 mm; fuel Jet-A1 + 5% oil, consumption 1300 ml/min at max RPM.
    Cross-checked across JetCat's own product page, Wikipedia "JetCat P400",
    and a third retailer listing (397 N / 89 lbf agreed on all three; one
    forum post reported a field-measured 27 kgf on a specific used unit --
    noted, not used, since it is one anecdotal unit reading below the rated
    spec, not the manufacturer's rating).
  - Material: 6061-T6511 aluminum, E=68900 MPa, nu=0.33, yield=276 MPa --
    reused directly from this project's ladder build (OnlineMetals product
    pages, same alloy family already vetted here).
  - Pilot mass: 90 kg (Gideon's choice: typical fit adult + gear).

MY ENGINEERING JUDGMENT (flagged as estimates, not code citations)
----------------------------------------------------------------------------
  - Engine spacing 170 mm centerline (148.4 mm diameter + ~22 mm mount/heat-
    shield clearance) and crossbar span 800 mm: my sizing, not a cited
    standard.
  - required_SF = 3.0 against bare max-rated thrust (not a multiplied proof
    load). NO OSHA/ANSI/FAA-equivalent structural test standard exists for a
    personal jet-propulsion frame -- this is a real gap, not an oversight on
    my part. The ladder's 1.67 (AISC 360 ASD Omega_b) was applied to an
    OSHA-mandated 3.3x proof load, an effective 5.5x margin over duty load.
    With no such proof-load multiplier available here, I raised the bare
    factor to 3.0 as my own judgment call, reasoned from: this is a body-worn
    structure active during flight (comparably safety-critical to the
    ladder), moderated down from the ladder's *effective* 5.5x because a
    yielding frame here is a controlled-descent failure, not the sudden
    total collapse a mid-climb ladder failure would be. This is MY number,
    not a code requirement -- treat it as such.
  - Waist attachment (z=min) modeled fully fixed (dof 1,2,3); shoulder band
    (z~280mm) modeled as lateral-only (dof 1,2). Both are simplifications:
    a real webbing harness is compliant, not rigid, so this likely
    UNDER-predicts deflection and may under- or over-predict local stress
    depending on true harness stiffness -- flagged, not resolved here.

EXPLICITLY NOT BUILT OR VALIDATED IN THIS PASS -- stated, not hidden
----------------------------------------------------------------------------
  - Fuel system, fuel tank structure, and engine cradle/clamp hardware are
    NOT modeled. Each engine's thrust reaction is applied as a distributed
    face-patch load directly on the crossbar (this engine's documented
    consistent-nodal-load method), standing in for a clamp bracket that does
    not exist as geometry here.
  - Local bolted-joint bearing/hole-edge stress at the two harness lugs is
    OUT OF SCOPE, same exclusion already standing on every ladder part.
  - No fatigue, vibration, or repeated-start thermal cycling check --
    turbines vibrate and run hot; neither is modeled.
  - No flight-control, stability, or pilot-workload analysis of any kind.
  - Fuel mass is NOT included as a structural load in this pass (see the
    context note printed below) -- the frame is checked against thrust and
    its own self-weight is neglected as <5% of the thrust-driven design load,
    not against a fully fueled system's total weight in a fall or handling
    case.

Run with the project venv from the repo root:
    .venv\\Scripts\\python.exe designs\\jetpack_frame_run.py
"""

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from design_engine import DesignEngine
from design_engine.geometry import SpecError

ROOT = Path(__file__).parent.parent / "data"
eng = DesignEngine(ROOT)

AL = {"name": "6061-T6511", "E_MPa": 68900, "nu": 0.33, "yield_MPa": 276,
      "source": "OnlineMetals product pages (pid 1145, 1087), reused from "
                "this project's ladder build: yield 40 ksi, ultimate 42 ksi. "
                "E, nu are standard published values for wrought aluminum "
                "alloys (E~69 GPa, nu~0.33)."}
REQUIRED_SF = 3.0   # my judgment call -- see module docstring, not a code cite

# --- sourced propulsion (JetCat P400-PRO x5) --------------------------------
THRUST_N = 397.0
ENGINE_DRY_KG = 3.65
N_ENGINES = 5
TOTAL_THRUST_N = THRUST_N * N_ENGINES
ENGINE_SPACING = 170.0
POSITIONS_X = [-2 * ENGINE_SPACING, -ENGINE_SPACING, 0.0,
               ENGINE_SPACING, 2 * ENGINE_SPACING]

# --- frame geometry (solid bar members -- see docstring: hollow tube was
# sized first and would save ~50% mass, but was dropped in this pass for
# meshing robustness/time; flagged, not hidden) ------------------------------
SPINE_W, SPINE_D = 18.0, 24.0            # solid bar, x,y (y bumped to 24 to
                                          # flush-match the gusset/crossbar)
SPINE_H = 422.0                           # z, base to underside of gusset
GUSSET_W, GUSSET_D, GUSSET_H = 140.0, 24.0, 40.0   # widens the crossbar's
                                          # local support so it isn't a bare
                                          # point-cantilever off an 18mm spine
                                          # -- the real fix for the Attempt-1
                                          # yield failure, see run log below
CROSSBAR_SPAN = 800.0                     # x, engine-mount beam length
CROSSBAR_D, CROSSBAR_H = 24.0, 34.0       # y (depth), z (height) -- bumped
                                          # from 20x28 (Attempt 1) after FEA
                                          # showed the true junction moment
                                          # is well above simple-beam theory
TOTAL_H = SPINE_H + GUSSET_H + CROSSBAR_H  # 496 mm overall

WAIST_LUG_Z = 35.0
SHOULDER_LUG_Z = 280.0
LUG_D = 6.5                               # M6 clearance (Bolt Depot stock,
                                           # same fastener family this
                                           # project already sources)

MESH_MM = 3.0


def moment_at_center(span, positions, force_each):
    """Superposition: bending moment at midspan of a simply-supported beam
    (supports at +/- span/2) from discrete point loads at `positions`."""
    L = span
    M = 0.0
    for xi in positions:
        a = xi + L / 2.0
        x = L / 2.0            # point of interest = center
        b = L - a
        Mi = force_each * b * x / L if x <= a else force_each * a * (L - x) / L
        M += Mi
    return M


def predict_crossbar():
    """NOT a trustworthy prediction, kept only as a floor: Attempt 1 used
    this simply-supported-beam formula (crossbar 'supported' at its own
    ends) and got SF=3.71 by hand vs FEA's real SF=2.37 (max_vm 116.3 MPa
    vs predicted 74.4 MPa) -- because the crossbar is not actually
    supported at its ends at all, it is cantilevered off a single point
    attachment to the spine. The real load path is closer to a center-
    cantilever, which this formula does not represent. Left in as the
    optimistic floor; a pessimistic cantilever-moment bound is computed
    separately below and both are printed so neither is mistaken for a
    validated number -- only the FEA result is."""
    w, h = CROSSBAR_D, CROSSBAR_H
    I = w * h ** 3 / 12.0
    c = h / 2.0
    M = moment_at_center(CROSSBAR_SPAN, POSITIONS_X, THRUST_N)
    sigma = M * c / I
    M_cant = sum(THRUST_N * max(abs(x) - GUSSET_W / 2.0, 0.0)
                 for x in POSITIONS_X)
    sigma_cant = M_cant * c / I
    return {"I_mm4": I, "M_Nmm": M, "sigma_MPa": sigma,
            "SF": AL["yield_MPa"] / sigma,
            "M_cant_Nmm": M_cant, "sigma_cant_MPa": sigma_cant,
            "SF_cant": AL["yield_MPa"] / sigma_cant}


def predict_spine_buckling():
    w = SPINE_W
    I = w * w ** 3 / 12.0
    K = 1.0   # pinned-pinned: no rotational fixity credited to a webbing
              # harness at either end -- conservative vs. fixed-pinned
    L = TOTAL_H
    P_cr = math.pi ** 2 * AL["E_MPa"] * I / (K * L) ** 2
    return {"I_mm4": I, "P_cr_N": P_cr, "SF": P_cr / TOTAL_THRUST_N}


print("=== Step 1: log state ===")
print(f"pending: {len(eng.log.pending_actions())}  "
      f"existing failures: {len(eng.log.failures())}")

print("\n=== Step 2: predictions (beam theory, before any solver run) ===")
print(f"engines: {N_ENGINES}x JetCat P400-PRO, {THRUST_N:.0f} N each, "
      f"total thrust {TOTAL_THRUST_N:.0f} N ({TOTAL_THRUST_N/9.80665:.1f} kgf)")
pc = predict_crossbar()
print(f"crossbar {CROSSBAR_D:.0f}x{CROSSBAR_H:.0f}mm solid bar, span "
      f"{CROSSBAR_SPAN:.0f}mm, gusset width {GUSSET_W:.0f}mm:")
print(f"  optimistic (simple-beam) floor: M={pc['M_Nmm']:.0f} N*mm, "
      f"sigma={pc['sigma_MPa']:.1f} MPa, SF={pc['SF']:.2f}")
print(f"  pessimistic (point-cantilever off gusset edge) bound: "
      f"M={pc['M_cant_Nmm']:.0f} N*mm, sigma={pc['sigma_cant_MPa']:.1f} MPa, "
      f"SF={pc['SF_cant']:.2f}")
print("  neither is trusted after Attempt 1's miss -- FEA is the real check")
ps = predict_spine_buckling()
print(f"spine {SPINE_W:.0f}x{SPINE_D:.0f}mm solid column, L={TOTAL_H:.0f}mm "
      f"pinned-pinned: P_cr={ps['P_cr_N']:.0f} N vs applied "
      f"{TOTAL_THRUST_N:.0f} N, buckling SF={ps['SF']:.2f}")
assert pc["SF_cant"] >= REQUIRED_SF, (
    "crossbar must clear the gate even under the pessimistic bound before "
    "spending solver time")
assert ps["SF"] >= REQUIRED_SF, "spine hand-calc must already clear the gate"

print("\n--- context only (not a validated limit state, not part of the gate) ---")
FUEL_ENDURANCE_MIN = 5.0   # stated assumption, matches real jet-suit endurance
fuel_L = N_ENGINES * 1300.0 * FUEL_ENDURANCE_MIN / 1000.0
fuel_kg = fuel_L * 0.8   # Jet-A1 approx density
frame_kg_est = (CROSSBAR_D * CROSSBAR_H * CROSSBAR_SPAN
               + GUSSET_W * GUSSET_D * GUSSET_H
               + SPINE_W * SPINE_D * SPINE_H) * 1e-9 * 2700.0
engines_kg = N_ENGINES * ENGINE_DRY_KG
pilot_kg = 90.0
total_kg = pilot_kg + engines_kg + frame_kg_est + fuel_kg
print(f"est. frame mass {frame_kg_est:.2f} kg, engines {engines_kg:.2f} kg, "
      f"fuel for {FUEL_ENDURANCE_MIN:.0f} min @ max RPM {fuel_kg:.1f} kg "
      f"({fuel_L:.1f} L Jet-A1) -> total system ~{total_kg:.1f} kg")
print(f"thrust/weight = {TOTAL_THRUST_N / (total_kg * 9.80665):.2f} "
      f"(>1 required to leave the ground; fuel burns off in flight so this "
      f"is the worst-case, fully-fueled number)")

print("\n=== Step 3: ripple analysis ===")
print("New design, unrelated to the ladder project; nothing to ripple into.")

print("\n=== Step 4: create_part (spine + crossbar T-frame) ===")
features = [
    {"op": "box", "x": SPINE_W, "y": SPINE_D, "z": SPINE_H, "at": [0, 0, 0]},
    {"op": "box", "x": GUSSET_W, "y": GUSSET_D, "z": GUSSET_H,
     "at": [0, 0, SPINE_H], "mode": "union"},
    {"op": "box", "x": CROSSBAR_SPAN, "y": CROSSBAR_D, "z": CROSSBAR_H,
     "at": [0, 0, SPINE_H + GUSSET_H], "mode": "union"},
]
spec = {
    "name": "jetpack-spine-frame", "units": "mm", "density_kg_m3": 2700,
    "features": features,
}


def try_create(spec, reason):
    return eng.create_part(spec, reason=reason)


try:
    part = try_create(dict(spec), (
        f"experimental jetpack structural frame: 5x JetCat P400-PRO fixed "
        f"backpack mount, predicted crossbar SF={pc['SF']:.2f}, spine "
        f"buckling SF={ps['SF']:.2f}"))
except SpecError as e:
    print(f"create_part failed: {e}")
    raise

gid = part["geometry_id"]
print(f"created {gid}, volume={part['properties']['volume_mm3']:.0f} mm^3, "
      f"mass_estimate={part['properties']['mass_kg_estimate']:.3f} kg")

print("\n=== Step 5: harness lug holes (waist + shoulder) ===")
lug_features = list(features)


def add_hole(feats, z, tag):
    """Try +X face first; the hole guard refuses a silent miss, so a wrong
    (u, v) guess fails loud rather than building a bracket with a hole that
    doesn't exist -- exactly the bug this engine already caught once before
    on the ladder's shoe bracket."""
    for face, uv in ((">X", [0.0, z]), ("<X", [0.0, z])):
        trial = feats + [{"op": "hole", "d": LUG_D, "at": uv, "face": face}]
        try:
            out = eng.edit_part(
                gid, {"features": trial},
                reason=f"{tag} lug through-hole, z={z:.0f}mm, face {face}")
            print(f"  {tag} lug: face {face}, at {uv} -> OK")
            return out, trial
        except SpecError as e:
            print(f"  {tag} lug: face {face}, at {uv} -> refused ({e})")
    raise SpecError(f"could not place {tag} lug hole on either X face")


out, lug_features = add_hole(lug_features, WAIST_LUG_Z, "waist")
gid = out["new_geometry_id"]
out, lug_features = add_hole(lug_features, SHOULDER_LUG_Z, "shoulder")
gid = out["new_geometry_id"]
print(f"final geometry_id with both lugs: {gid}, volume="
      f"{out['properties']['volume_mm3']:.0f} mm^3")

print("\n=== Step 6: run_fea_static (yield_von_mises) ===")
common_constraints = [
    {"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]},
    {"where": {"axis": "z", "at": SHOULDER_LUG_Z, "tol": 2.0}, "dof": [1, 2]},
]
common_loads = [
    {"where": {"all": [{"axis": "z", "at": "max"},
                       {"axis": "x", "at": x, "tol": 20.0}]},
     "force_total_N": [0.0, 0.0, THRUST_N]}
    for x in POSITIONS_X
]

static_case = {
    "material": dict(AL),
    "mesh": {"max_size_mm": MESH_MM},
    "constraints": common_constraints,
    "loads": common_loads,
    "limit_state": {"name": "yield_von_mises", "required_SF": REQUIRED_SF},
}

static_out = eng.run_fea_static(
    gid, static_case,
    reason=(f"jetpack frame yield check: 5x397N thrust patches on crossbar "
            f"top, waist fixed + shoulder lateral restraint. Predicted "
            f"sigma={pc['sigma_MPa']:.1f} MPa, SF={pc['SF']:.2f}"))
print(f"result={static_out['result']}  SF={static_out.get('safety_factor')}  "
      f"max_vm={static_out.get('max_von_mises_MPa')} MPa")

print("\n=== Step 7: run_fea_buckling (elastic_buckling) ===")
# Coarser mesh than the yield case: buckling is a global stiffness/stability
# eigenvalue problem, not a local stress-concentration problem, and doesn't
# need the 3mm resolution the gusset/hole stress check required. Attempt 1
# of this step at MESH_MM=3.0 produced 271,531 nodes and the solver timed
# out at 600s (fea_buckling solves twice per attempt, up to 6 attempts) --
# a mesh-cost problem, not a structural one. 6mm matches this project's own
# buckling-verification precedent (test_buckling.py uses 6mm for a simple
# prismatic column) and should give ~8x fewer elements.
# 6.0mm was too coarse: check_element_quality rejected a degenerate element
# near the 6.5mm lug hole (Jacobian gate, not a timeout). 4.0mm is the
# middle ground -- still ~4x fewer elements than the 3mm static mesh, fine
# enough that the hole curvature shouldn't degenerate.
BUCKLING_MESH_MM = 4.0
buckling_case = {
    "material": dict(AL),
    "mesh": {"max_size_mm": BUCKLING_MESH_MM},
    "constraints": common_constraints,
    "loads": common_loads,
    "limit_state": {"name": "elastic_buckling", "required_SF": REQUIRED_SF},
}
buckling_out = eng.run_fea_buckling(
    gid, buckling_case,
    reason=(f"jetpack spine buckling check under total thrust "
            f"{TOTAL_THRUST_N:.0f} N. Predicted Euler SF={ps['SF']:.2f} "
            f"(K=1.0 pinned-pinned, L={TOTAL_H:.0f}mm)"))
print(f"result={buckling_out['result']}  "
      f"SF={buckling_out.get('safety_factor')}")

print("\n=== Step 8: sign-off ===")
if static_out["result"] == "pass" and buckling_out["result"] == "pass":
    signoff = eng.sign_off(
        gid, signed_off_by="Gideon",
        statement=(
            "Structural frame only, for an experimental 5x JetCat P400-PRO "
            "fixed backpack jetpack mount. Validated: yield_von_mises "
            f"(SF={static_out['safety_factor']}, required {REQUIRED_SF}) and "
            f"elastic_buckling (SF={buckling_out['safety_factor']}, "
            f"required {REQUIRED_SF}) against the engines' published max "
            "thrust (397 N each, JetCat data). required_SF=3.0 is my own "
            "engineering judgment, NOT a code citation -- no OSHA/ANSI/FAA "
            "structural standard exists for personal jet-propulsion frames. "
            "NOT covered by this sign-off: propulsion, fuel system, engine "
            "cradle/clamp hardware and its bolted-joint bearing stress, "
            "harness lug bolted-joint bearing/hole-edge stress, fatigue, "
            "vibration, thermal cycling, flight control/stability, and any "
            "physical qualification testing. Frame carries thrust as an "
            "input load; this sign-off makes no claim the assembly flies "
            "or is safe to wear."))
    print(f"signed off: {signoff}")
else:
    print("NOT SIGNED OFF -- a gate failed. Failure record is already in "
          "the log (non-linear gate contract); no blind retry.")

print("\n=== Step 9: viewer ===")
if static_out["result"] == "pass" and buckling_out["result"] == "pass":
    viewer_out = eng.generate_viewer(
        gid, reason="jetpack frame experimental validation pass -- viewer for Gideon")
    print(f"viewer: {viewer_out}")
