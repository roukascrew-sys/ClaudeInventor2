"""Jetpack frame, re-run as a SEARCH problem through the inventor layer.

The 2026-08-25 jetpack (P0030 / P0031@v2 / P0032) was hand-designed: I chose a
3/4in x 2in bar, added a doubler pad after an FEA failure, and checked it.
This re-runs the same engineering problem as an optimisation, so the sections,
the engine stations and the material are SEARCHED rather than chosen.

WHY THIS PROBLEM IS WORTH SEARCHING
----------------------------------------------------------------------------
It is genuinely coupled, and the couplings pull against each other:

  engines outboard  -> more roll authority, but the root moment grows, so the
                       beam gets heavier, so thrust/weight falls
  engines aft       -> easy to package, but the thrust line leaves the CG and
                       the pilot fights a constant pitching moment for the
                       whole flight
  thicker beam      -> lower stress -> survives a HOTTER structure, but heavier
  steel not alu     -> full yield to 400 C (EN 1993-1-2 Table 3.1) instead of
                       derating to 55% at 250 C, at 2.9x the density

So the two objectives are frame mass and THERMAL HEADROOM, and they genuinely
trade. The hand build assumed 150 C and reported 273 C as the acceptance
criterion that any real build must demonstrate; here "how hot can this frame
get and still clear SF 3.0" is a first-class thing to maximise rather than a
number that fell out at the end.

Controllability is NOT an objective. Thrust/weight and thrust-CG offset are
hard constraints, because a jetpack the pilot cannot trim is not a worse
design, it is not a design.

FIDELITY LADDER
----------------------------------------------------------------------------
  L0  beam theory + rigid-body CG/thrust statics        microseconds
  L1  real CadQuery frame solid, exact OCC mass, then
      system CG and T/W recomputed from the REAL mass   ~30 ms
  L3  CalculiX thermal_derated_yield on the whole
      welded frame - spine + pad + crossbeam in ONE
      model, which is better than the hand build got:
      that FEA'd spine and crossbeam separately with
      idealised boundary conditions at their joint      ~20-60 s

L0 CALIBRATION, STATED PLAINLY: pure beam theory under-predicted the real FEA
stress at the doubler step by 1.51x on the one part where both numbers exist
(P0031@v2: beam theory 31.5 MPa, FEA 47.6 MPa). A screening model that is
optimistic in the unsafe direction is worse than no model, so Kt = 1.5 is
applied at the pad step and labelled as calibrated against that SINGLE run.
It is a screening aid. FEA still decides.

Run:  .venv\\Scripts\\python.exe designs\\jetpack_optimization_run.py [--selftest]
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from design_engine import DesignEngine
from design_engine.fea import derate_factor
from design_engine.geometry import GeometryError, SpecError
from design_engine.uncertainty import (SourcedRange, SourcedValue,
                                       required_value, verdict_across)
from design_engine.inventor import (AnalyticStage, CallableStage, Candidate,
                                    Constraint, DesignSpace, DesignVariable,
                                    EvalContext, EvaluationCache, Evaluator,
                                    EvolutionarySearch, FailureMemory,
                                    FeaStage, FeasibilityRule, Fidelity,
                                    GeometryStage, Objective, Op,
                                    OptimizationConfig, OptimizationRun,
                                    Preference, RandomSearch, RequirementSet,
                                    RuleStage, Sense, StageResult, Status,
                                    VarType, compare_fronts, hypervolume,
                                    render_run, robustness,
                                    tolerance_perturbation)

ROOT = Path(__file__).parent.parent / "data"

# ---------------------------------------------------------------- sourced
THRUST_N = 397.0          # JetCat P400-PRO, cross-checked across 3 sources
ENGINE_DRY_KG = 3.65
ENGINE_DIA = 148.4
N_ENGINES = 4
TOTAL_THRUST_N = THRUST_N * N_ENGINES

EC9_SRC = ("EN 1999-1-2:2007 Table 1a (k_0,2,theta, row EN AW-6061 T6) and "
           "Table 2 (E_al,theta), up to 2 hours thermal exposure. FIRE-DESIGN "
           "data: does not cover creep or thermal cycling.")
K02_6061 = [[20, 1.00], [100, 0.95], [150, 0.91], [200, 0.79],
            [250, 0.55], [300, 0.31], [350, 0.10], [550, 0.0]]
KE_6XXX = [[20, 1.00], [50, 0.99], [100, 0.97], [150, 0.93], [200, 0.86],
           [250, 0.78], [300, 0.68], [350, 0.54], [400, 0.40], [550, 0.0]]
EC3_SRC = ("EN 1993-1-2:2005 Table 3.1 (k_y,theta, k_E,theta) for carbon "
           "steel. FIRE-DESIGN data at 2% total strain, short duration.")
KY_STEEL = [[20, 1.0], [100, 1.0], [200, 1.0], [300, 1.0], [400, 1.0],
            [500, 0.78], [600, 0.47], [700, 0.23], [800, 0.11], [900, 0.06],
            [1000, 0.04], [1100, 0.02], [1200, 0.0]]
KE_STEEL = [[20, 1.0], [100, 1.0], [200, 0.90], [300, 0.80], [400, 0.70],
            [500, 0.60], [600, 0.31], [700, 0.13], [800, 0.09], [900, 0.0675],
            [1000, 0.045], [1100, 0.0225], [1200, 0.0]]

FRAME_TEMP_C = 150.0      # stated design assumption, same as the hand build
REQUIRED_SF = 3.0         # engineer's judgment, NOT a code citation

MATERIALS = {
    "6061-T6511": {
        "name": "6061-T6511", "E_MPa": 68900, "nu": 0.33, "yield_MPa": 276,
        "density_kg_m3": 2700,
        "source": "OnlineMetals product pages (pid 1145, 1087): yield 40 ksi. "
                  "E and nu standard published values for wrought aluminium.",
        "service_temp_C": FRAME_TEMP_C, "yield_derate_curve": K02_6061,
        "E_derate_curve": KE_6XXX, "derate_source": EC9_SRC},
    "1018-cold-finish": {
        "name": "1018-cold-finish", "E_MPa": 205000, "nu": 0.29,
        "yield_MPa": 372, "density_kg_m3": 7850,
        "source": "OnlineMetals pid 4790 (ASTM-A108): yield 54 ksi. E and nu "
                  "standard published values for carbon steel.",
        "service_temp_C": FRAME_TEMP_C, "yield_derate_curve": KY_STEEL,
        "E_derate_curve": KE_STEEL, "derate_source": EC3_SRC},
}

# --- L0 calibration against real FEA. THREE data points, all named. -------
# Kt at the doubler step, from the hand build:
#   P0031@v2  beam theory 31.5 MPa -> FEA 47.6 MPa   ratio 1.51
KT_PAD_STEP = 1.5
# Kt at the crossbeam/spine T-junction. The first version of this model
# applied NO concentration factor when there was no doubler, treating the
# junction as a clean cantilever root. It is not - it is a re-entrant corner
# where a 1280mm beam meets the spine, and the solver said so:
#   c6357ea1badbd  L0 SF 5.226 -> FEA SF 2.968   ratio 1.76  (alu, no doubler)
#   c9773b1e66055  L0 SF 20.53 -> FEA SF 10.45   ratio 1.96  (steel, no doubler)
# Both had outlier ratios of 1.65/1.76, below the 1.9 artifact threshold, so
# these are not CONSTRAINT singularities. 1.85 is the mean.
#
# CAUTION (2026-08-27): "not a constraint singularity" was read at the time as
# "a real, convergeable stress". That does not follow. The outlier-ratio test
# compares the peak against the bulk field and catches a hot spot pinned to a
# constraint patch; it says nothing about a GEOMETRIC singularity at a sharp
# re-entrant corner, which is what the unfilleted spine/pad junction was. Both
# runs above were unfilleted, so both peaks sat on that corner and neither
# number was converged. Kt values fitted to them inherit that. See FILLET_R.
KT_ROOT_JUNCTION = 1.85

# --------------------------------------------------------------- HAZ
# Welding a 6xxx alloy in T6 destroys the temper locally. The frame is welded,
# and until 2026-08-28 every safety factor it ever produced used the PARENT
# proof strength (276 MPa) - a strength that does not exist at the joints, and
# the peak stress is at a joint.
#
# rho_o,haz multiplies the 0.2% PROOF strength, which is what this engine gates
# on. Do not confuse it with rho_u,haz (ultimate), quoted as 0.61 in Eurocode
# 9's own worked example - using the ultimate factor on a yield gate would
# overstate the joint by ~30%.
#
# SOURCED RANGE, least severe first. No source supports a value above 0.50 for
# 6xxx-T6, and the frame needs 0.647 to reach its 3.0 gate.
#   0.500  EN 1999-1-1 / Eurocode 9: "the 0,2% proof strength in HAZ is half
#          the strength in the base material for EN-AW 6082-T6"; "6xxx alloys
#          in T6 temper lose roughly half of their strength in the HAZ"
#          (European Aluminium, Design of Aluminium Structures: Introduction
#          to Eurocode 9 with Worked Examples)
#   0.475  6061-T6 MIG with 5356 filler: 19 ksi vs 40 ksi parent
#   0.450  6061-T6 MIG with 4043 filler: 18 ksi vs 40 ksi parent
#   0.375  6061-T6 as-welded HAZ: 15 ksi vs 40 ksi parent
#
# 0.50 is used as the DESIGN value because it is the least severe defensible
# one and is the code-based figure; the lower numbers are recorded so the
# sensitivity is visible rather than buried. AWS D1.2 guidance is more severe
# still - it directs the designer to use 6061-T4 or 6061-O properties in the
# HAZ.
#
# VALIDITY, and it is not satisfied here without checking: Eurocode 9's factors
# are stated for MIG welding of elements up to 15 mm thick. TIG, or thicker
# material, requires a LARGER reduction. The crossbeam is 15.875 mm, so this
# frame sits just outside the stated range and 0.50 may itself be optimistic.
# The four values above, as a range the engine can actually evaluate across
# rather than a comment beside a chosen constant. rho_o,haz is something this
# design DISCOVERS, not something it CHOOSES, so it is deliberately not a
# design variable: an optimiser given control of it would "improve" the frame
# by selecting a softening factor the physical world never agreed to.
HAZ_RANGE = SourcedRange(
    "rho_o,haz",
    [SourcedValue(0.500,
                  "EN 1999-1-1 (Eurocode 9) s6.1.6 via European Aluminium, "
                  "'Design of Aluminium Structures: Introduction to Eurocode 9 "
                  "with Worked Examples': the 0.2% proof strength in the HAZ is "
                  "half the base material for EN AW-6082-T6, and 6xxx alloys in "
                  "T6 lose roughly half their strength in the HAZ. VALIDITY: "
                  "stated for MIG up to 15 mm thick; this frame's crossbeam is "
                  "15.875 mm, so the factor is at or just outside the stated "
                  "range and may itself be optimistic",
                  "Eurocode 9"),
     SourcedValue(0.475, "6061-T6 MIG with 5356 filler: 19 ksi vs 40 ksi parent",
                  "5356 filler"),
     SourcedValue(0.450, "6061-T6 MIG with 4043 filler: 18 ksi vs 40 ksi parent",
                  "4043 filler"),
     SourcedValue(0.375, "6061-T6 as-welded HAZ: 15 ksi vs 40 ksi parent",
                  "as-welded")],
    nominal=0.500)

#: The design value: the LEAST severe defensible one, and the code-based
#: figure. Kept as the nominal so existing results stay comparable - but the
#: gate is now decided by HAZ_RANGE, not by this number alone.
HAZ_FACTOR = HAZ_RANGE.nominal.value
HAZ_EXTENT_MM = 25.0        # b_haz, EN 1999-1-1 clause 6.1.6.3 worked example
HAZ_SOURCE = HAZ_RANGE.nominal.source


def haz_zones(v) -> list:
    """The four spine/pad junction welds, as heat-affected zones.

    The welds run along Y at the spine wall, at the bottom and top of the
    doubler pad - the same four edges the fillet blends, because that is where
    the pad is joined to the spine.
    """
    sx = float(v["spine_x"]) / 2.0
    sy = float(v["spine_y"]) / 2.0
    cb_h = float(v["cb_height"])
    cb_z = SPINE_Z / 2.0 - cb_h / 2.0
    lines = [[[sign * sx, -sy, z], [sign * sx, sy, z]]
             for sign in (-1.0, 1.0) for z in (cb_z, cb_z + cb_h)]
    return [{"name": "spine-pad-weld", "factor": HAZ_FACTOR,
             "extent_mm": HAZ_EXTENT_MM, "source": HAZ_SOURCE, "lines": lines}]


# Fillet radius at the spine/pad T-junction roots. 0 reproduces the original
# sharp-cornered geometry, whose peak stress is singular and cannot converge.
#
# Chosen on engineering grounds, not to flatter the stress number:
#   - r/t = 10/19.05 = 0.53 against the spine thickness, well past the knee
#     where additional radius stops reducing Kt appreciably
#   - consumes 20 mm of the 50.8 mm pad height, leaving 30.8 mm straight
#   - a standard tool radius, and manufacturable as a dressed weld transition
#   - costs +1.5 g on a 3.901 kg frame (+0.11% volume), measured not estimated
# Resolvability follows from the choice rather than driving it: the arc is
# 15.7 mm, about 4.9 elements at 3.2 mm and 5.6 at 2.8 mm.
FILLET_R = 10.0
KT_BASIS = ("Two stated stress-concentration factors, both calibrated against "
            "named FEA runs rather than assumed. Doubler step Kt=1.5 from "
            "P0031@v2 (31.5 -> 47.6 MPa, ratio 1.51). T-junction Kt=1.85 from "
            "c6357ea1badbd (SF 5.226 -> 2.968, ratio 1.76) and c9773b1e66055 "
            "(SF 20.53 -> 10.45, ratio 1.96), mean 1.86. THREE data points "
            "total - a screening aid, not validated Kt values. FEA decides.")

# fixed masses (not searched)
PILOT_KG, PILOT_Y = 90.0, -110.0
FUEL_KG, FUEL_Y = 14.5, 140.0          # fuel + COTS tank allowance
CRADLE_KG_EACH = 0.837                 # always steel: it clamps the turbine
SHOULDER_HALF_W = 240.0                # anthropometric estimate
CB_LEN = 1280.0
SPINE_Z = 450.0
LUG_D = 6.5
ENGINE_Z = 40.0


def section_Z(thick, height):
    return thick * height ** 2 / 6.0


def max_service_temp(sigma_MPa, mat_key):
    """Hottest the frame may get and still clear SF 3.0.

    On the hand build this was the binding acceptance criterion handed to the
    fabricator (273 C, to be demonstrated by instrumented test). Here it is an
    OBJECTIVE, so the search can buy thermal margin deliberately.
    """
    mat = MATERIALS[mat_key]
    curve = mat["yield_derate_curve"]
    need_k = REQUIRED_SF * sigma_MPa / mat["yield_MPa"]
    if need_k > 1.0:
        return None
    best = None
    for i in range(len(curve) - 1):
        t0, k0 = curve[i]
        t1, k1 = curve[i + 1]
        if k0 >= need_k >= k1 and k0 != k1:
            best = t0 + (k0 - need_k) / (k0 - k1) * (t1 - t0)
    return best if best is not None else curve[-1][0]


# ---------------------------------------------------------------- L0 model
def analytic_screen(v, ctx) -> dict:
    """Beam theory + rigid-body statics. Microseconds, no kernel, no solver."""
    mat = MATERIALS[v["material"]]
    rho = mat["density_kg_m3"]
    cb_t, cb_h = v["cb_thick"], v["cb_height"]
    inner, outer = v["inner_x"], v["outer_x"]
    pod_y = v["pod_y"]
    stations = [inner, outer]

    if v.get("doubler"):
        pad_len, pad_t = v["pad_len"], v["pad_thick"]
    else:
        pad_len, pad_t = 0.0, cb_t

    # (a) at the spine root, on the full (padded) section
    root_x = v["spine_x"] / 2.0
    M_root = sum(THRUST_N * (x - root_x) for x in stations if x > root_x)
    # The T-junction is a re-entrant corner, not a clean built-in end.
    sig_root = KT_ROOT_JUNCTION * M_root / section_Z(pad_t, cb_h)
    # (b) at the pad step, on the thin section, with the calibrated Kt
    if pad_len > 0:
        step_x = pad_len / 2.0
        M_step = sum(THRUST_N * (x - step_x) for x in stations if x > step_x)
        sig_step = KT_PAD_STEP * M_step / section_Z(cb_t, cb_h)
    else:
        sig_step = 0.0
    sigma = max(sig_root, sig_step)

    k = derate_factor(mat["yield_derate_curve"], FRAME_TEMP_C, "frame")
    allow = mat["yield_MPa"] * k

    m_cb = CB_LEN * cb_t * cb_h * 1e-9 * rho
    m_pad = pad_len * max(0.0, pad_t - cb_t) * cb_h * 1e-9 * rho
    m_spine = v["spine_x"] * v["spine_y"] * SPINE_Z * 1e-9 * rho
    m_frame = m_cb + m_pad + m_spine
    m_cradles = N_ENGINES * CRADLE_KG_EACH
    m_engines = N_ENGINES * ENGINE_DRY_KG
    m_total = PILOT_KG + m_engines + FUEL_KG + m_frame + m_cradles

    # rigid-body CG: the pilot dominates and is not geometry
    moment_y = (PILOT_KG * PILOT_Y + m_engines * pod_y + FUEL_KG * FUEL_Y
                + (m_frame + m_cradles) * pod_y)
    cg_y = moment_y / m_total
    offset = abs(pod_y - cg_y)
    weight = m_total * 9.80665
    pitch = TOTAL_THRUST_N * offset / 1000.0

    out = {
        "frame_mass_kg": m_frame,
        "system_mass_kg": m_total,
        "bending_stress_MPa": sigma,
        "sf.thermal_derated_yield": (allow / sigma) if sigma > 0 else float("inf"),
        "thrust_to_weight": TOTAL_THRUST_N / weight,
        "thrust_cg_offset_mm": offset,
        "pitch_moment_Nm": pitch,
        "required_cg_shift_mm": pitch / weight * 1000.0,
        "overall_width_mm": 2.0 * (outer + ENGINE_DIA / 2.0),
        "roll_authority_Nm": THRUST_N * 2 * outer / 1000.0,
    }
    t = max_service_temp(sigma, v["material"])
    out["max_service_temp_C"] = t if t is not None else 20.0
    return out


# ---------------------------------------------------------------- geometry
def build_spec(v, ctx) -> dict:
    """The WHOLE welded frame as one part: spine + doubler pad + crossbeam.

    Better than the hand build, which FEA'd spine and crossbeam separately
    with idealised boundary conditions at the joint between them. Here the
    joint is real geometry and the solver sees the actual load path through it.
    """
    mat = MATERIALS[v["material"]]
    cb_t, cb_h = float(v["cb_thick"]), float(v["cb_height"])
    cb_z = SPINE_Z / 2.0 - cb_h / 2.0        # crossbeam at mid-spine
    feats = [
        {"op": "box", "x": float(v["spine_x"]), "y": float(v["spine_y"]),
         "z": SPINE_Z, "at": [0, 0, 0]},
    ]
    if v.get("doubler"):
        feats.append({"op": "box", "x": float(v["pad_len"]),
                      "y": float(v["pad_thick"]), "z": cb_h,
                      "at": [0, 0, cb_z], "mode": "union"})
    feats.append({"op": "box", "x": CB_LEN, "y": cb_t, "z": cb_h,
                  "at": [0, 0, cb_z], "mode": "union"})
    # Fillet the spine/pad T-junction roots. Without this the union of boxes
    # leaves a sharp 90-degree re-entrant corner (270-degree material angle)
    # at |x| = spine_x/2, z = cb_z and cb_z + cb_h. Linear elasticity has no
    # finite stress at such a corner - Williams (1952) gives sigma ~ r**-0.4555
    # - so the peak von Mises there is a mesh artefact that grows without
    # bound under refinement and can never be converged. That is exactly where
    # the 3.2 mm run put its peak, at [-23.505, 4.014, 199.6].
    #
    # Applied BEFORE the lug holes so it does not round them.
    if FILLET_R > 0:
        feats.append({"op": "fillet", "radius": FILLET_R,
                      "edges": {"parallel_to": "Y",
                                "at": {"x": [-float(v["spine_x"]) / 2.0,
                                             float(v["spine_x"]) / 2.0],
                                       "z": [cb_z, cb_z + cb_h]},
                                "tol": 0.01}})
    # harness lug holes through the spine - the life-safety load path
    for z in (SPINE_Z - 50.0, 40.0):
        feats.append({"op": "hole", "d": LUG_D, "at": [0.0, z], "face": ">X"})
    return {"name": "jetpack-frame-opt", "units": "mm",
            "density_kg_m3": mat["density_kg_m3"], "features": feats}


def system_stage(cand, ctx) -> StageResult:
    """L1: recompute T/W and CG offset from the REAL frame mass.

    The L0 estimate used a beam-theory mass. Once the kernel has returned an
    exact volume these derived quantities are upgraded and re-tagged at L1
    fidelity, overwriting the estimate.
    """
    m = cand.result.metrics
    frame = m.get("mass_kg")
    if frame is None:
        return StageResult("system", Fidelity.L1_GEOMETRY, Status.UNKNOWN,
                           warnings=["no geometry mass; cannot size the system"])
    v = cand.values
    pod_y = v["pod_y"]
    m_engines = N_ENGINES * ENGINE_DRY_KG
    m_cradles = N_ENGINES * CRADLE_KG_EACH
    m_total = PILOT_KG + m_engines + FUEL_KG + frame + m_cradles
    moment_y = (PILOT_KG * PILOT_Y + m_engines * pod_y + FUEL_KG * FUEL_Y
                + (frame + m_cradles) * pod_y)
    cg_y = moment_y / m_total
    offset = abs(pod_y - cg_y)
    weight = m_total * 9.80665
    pitch = TOTAL_THRUST_N * offset / 1000.0
    return StageResult(
        "system", Fidelity.L1_GEOMETRY, Status.VALID,
        metrics={"frame_mass_kg": frame,
                 "system_mass_kg": m_total,
                 "thrust_to_weight": TOTAL_THRUST_N / weight,
                 "thrust_cg_offset_mm": offset,
                 "pitch_moment_Nm": pitch,
                 "required_cg_shift_mm": pitch / weight * 1000.0},
        provenance={"basis": "rigid-body statics on the exact kernel mass"})


def build_case(cand, ctx) -> dict:
    """Real CalculiX case on the whole frame: lugs held, engines pushing up."""
    v = cand.values
    # The engines hang UNDER THE CROSSBEAM, whose underside sits at
    # z = SPINE_Z/2 - cb_height/2 - NOT at the frame's global z minimum, which
    # is the bottom of the spine. Selecting {"axis":"z","at":"min"} picked a
    # plane where the frame is only spine_x wide, so every engine-station
    # patch at |x| = 330..430 matched ZERO nodes and the whole promotion
    # failed. Select the crossbeam underside explicitly.
    cb_underside_z = SPINE_Z / 2.0 - float(v["cb_height"]) / 2.0
    loads = []
    for key in ("inner_x", "outer_x"):
        for sx in (-1.0, 1.0):
            loads.append({
                "where": {"all": [{"axis": "z", "at": cb_underside_z,
                                   "tol": 1.0},
                                  {"axis": "x", "at": sx * float(v[key]),
                                   "tol": 30.0}]},
                "force_total_N": [0.0, 0.0, THRUST_N]})
    return {
        # validate_case rejects unknown material keys, and density_kg_m3 is
        # a geometry property, not an FEA one. Strip to what the deck accepts.
        "material": {k: val for k, val in MATERIALS[v["material"]].items()
                     if k != "density_kg_m3"},
        "mesh": {"max_size_mm": 5.0},
        "constraints": [
            {"where": {"cylinder": {"axis": "x",
                                    "center": [0.0, SPINE_Z - 50.0],
                                    "r": LUG_D / 2.0, "tol": 0.8}},
             "dof": [1, 2, 3]},
            {"where": {"cylinder": {"axis": "x", "center": [0.0, 40.0],
                                    "r": LUG_D / 2.0, "tol": 0.8}},
             "dof": [1, 2, 3]},
        ],
        "loads": loads,
        "limit_state": {"name": "thermal_derated_yield",
                        "required_SF": REQUIRED_SF},
    }


# ---------------------------------------------------------------- problem
def make_space() -> DesignSpace:
    return DesignSpace(name="jetpack-frame", version=1, variables=[
        DesignVariable("material", VarType.CATEGORICAL, values=list(MATERIALS),
                       description="alu is light but derates hard; steel holds "
                                   "full yield to 400 C at 2.9x the density"),
        DesignVariable("cb_thick", VarType.CONTINUOUS, lo=9.525, hi=31.75,
                       step=1.5875, units="mm", description="1/16in steps"),
        DesignVariable("cb_height", VarType.CONTINUOUS, lo=38.1, hi=88.9,
                       step=6.35, units="mm"),
        DesignVariable("doubler", VarType.CATEGORICAL, values=[False, True],
                       description="topology: local doubler pad at the joint"),
        DesignVariable("pad_len", VarType.CONTINUOUS, lo=120.0, hi=400.0,
                       step=20.0, units="mm",
                       active_if=lambda v: v.get("doubler") is True),
        DesignVariable("pad_thick", VarType.CONTINUOUS, lo=19.05, hi=57.15,
                       step=3.175, units="mm",
                       active_if=lambda v: v.get("doubler") is True),
        DesignVariable("spine_x", VarType.CONTINUOUS, lo=38.1, hi=76.2,
                       step=6.35, units="mm"),
        DesignVariable("spine_y", VarType.CONTINUOUS, lo=19.05, hi=44.45,
                       step=3.175, units="mm"),
        DesignVariable("inner_x", VarType.CONTINUOUS, lo=330.0, hi=430.0,
                       step=10.0, units="mm"),
        DesignVariable("outer_x", VarType.CONTINUOUS, lo=500.0, hi=620.0,
                       step=10.0, units="mm"),
        DesignVariable("pod_y", VarType.CONTINUOUS, lo=-30.0, hi=120.0,
                       step=10.0, units="mm",
                       description="fore-aft engine station: the CG driver"),
    ], rules=[
        FeasibilityRule("engines_do_not_collide",
                        lambda v: v["outer_x"] - v["inner_x"] >= ENGINE_DIA + 15.0),
        FeasibilityRule("inner_pod_clears_the_pilot",
                        lambda v: v["inner_x"] - ENGINE_DIA / 2.0
                        >= SHOULDER_HALF_W + 15.0),
        FeasibilityRule("pad_thicker_than_beam",
                        lambda v: (not v.get("doubler"))
                        or v["pad_thick"] > v["cb_thick"]),
        FeasibilityRule("pad_shorter_than_beam",
                        lambda v: (not v.get("doubler"))
                        or v["pad_len"] < CB_LEN * 0.4),
        FeasibilityRule("pad_inboard_of_inner_engine",
                        lambda v: (not v.get("doubler"))
                        or v["pad_len"] / 2.0 < v["inner_x"] - 60.0),
        FeasibilityRule("beam_not_absurdly_slender",
                        lambda v: v["cb_height"] / v["cb_thick"] <= 6.0),
        FeasibilityRule("spine_wide_enough_for_lugs",
                        lambda v: v["spine_x"] >= 4.0 * LUG_D),
    ])


def make_requirements() -> RequirementSet:
    return RequirementSet(
        name="jetpack-frame-v2",
        constraints=[
            Constraint("strength", "sf.thermal_derated_yield", Op.GE,
                       REQUIRED_SF, units="-", scale=REQUIRED_SF,
                       source=f"stated design gate SF >= {REQUIRED_SF} against "
                              f"thermal_derated_yield at {FRAME_TEMP_C:.0f} C. "
                              f"Engineer's judgment - no OSHA/ANSI/FAA "
                              f"structural standard exists for a personal "
                              f"jet-propulsion frame."),
            Constraint("can_fly", "thrust_to_weight", Op.GE, 1.15, units="-",
                       scale=1.15,
                       source="below 1.0 it cannot leave the ground; the "
                              "margin above 1.0 IS the control authority. "
                              "1.15 is a stated minimum."),
            Constraint("trimmable", "thrust_cg_offset_mm", Op.LE, 100.0,
                       units="mm", scale=100.0,
                       source="at this T/W a 100mm miss needs ~123mm of pilot "
                              "CG shift against a stated 150mm posture "
                              "authority. Assumption; needs a real pilot."),
            Constraint("packaging", "overall_width_mm", Op.LE, 1400.0,
                       units="mm", source="stated packaging limit"),
        ],
        objectives=[
            Objective("frame_mass", "frame_mass_kg", Sense.MIN, units="kg",
                      description="every kg of frame is a kg of payload lost"),
            Objective("thermal_headroom", "max_service_temp_C", Sense.MAX,
                      units="C",
                      description="hottest the frame may get and still clear "
                                  "SF 3.0 - the binding acceptance criterion "
                                  "on the hand-built jetpack"),
        ],
        preferences=[
            Preference("roll_authority", "roll_authority_Nm", Sense.MAX,
                       description="a wider engine track gives more roll "
                                   "control per unit of differential thrust"),
        ])


def make_evaluator(engine, space, reqs, cache, with_fea=True):
    ctx = EvalContext(space=space, requirements=reqs, engine=engine)
    stages = [
        RuleStage(),
        AnalyticStage(analytic_screen, name="beam_and_statics", version="v1"),
        GeometryStage(spec_builder=build_spec),
        CallableStage(system_stage, "system", Fidelity.L1_GEOMETRY),
    ]
    if with_fea:
        # 5.0 was refused on all three promoted frames (Jacobian gate vs
        # the 6.5mm lug holes). The ladder refines instead of giving up.
        stages.append(FeaStage(build_case, analysis="static",
                               mesh_ladder=[5.0, 4.0, 3.2],
                               fidelity=Fidelity.L3_HIGH_FEA))
    return Evaluator(stages, ctx, cache=cache)


def selftest(space) -> int:
    import random
    from design_engine.geometry import build, mass_properties
    rng = random.Random(0)
    ok = bad = 0
    reasons = {}
    for _ in range(150):
        v = space.sample(rng)
        if not space.is_feasible(v):
            continue
        try:
            spec = build_spec(v, None)
            mass_properties(spec, build(spec))
            ok += 1
        except (SpecError, GeometryError) as exc:
            bad += 1
            key = f"{type(exc).__name__}: {str(exc)[:60]}"
            reasons[key] = reasons.get(key, 0) + 1
    print(f"selftest: {ok} frames built, {bad} refused")
    for r, n in reasons.items():
        print(f"   {n:3d}x {r}")
    return 0 if ok > 0 else 1


def haz_verdict(sf_at_parent: float = 4.633, required_sf: float = REQUIRED_SF,
                emit: bool = True) -> dict:
    """What the frame's gate does across every sourced value of rho_o,haz.

    `sf_at_parent` is the measured safety factor computed against the PARENT
    proof strength, i.e. before any HAZ softening. The filleted frame's
    recorded figure is 4.633.

    Linearity is exact here rather than assumed: the softening factor scales
    the ALLOWABLE, and the stress field does not depend on it, so
    SF(rho) = SF_parent * rho. It reproduces both recorded points -
    4.633 * 0.500 = 2.317 and 4.633 * 0.375 = 1.738 - which is the check that
    the assumption holds for this case. It would NOT hold for a parameter that
    moves stiffness, load path or geometry.
    """
    out = verdict_across(
        HAZ_RANGE,
        lambda rho: (sf_at_parent * rho >= required_sf,
                     {"allowable_MPa": round(276.0 * rho, 1),
                      "sf": round(sf_at_parent * rho, 3)}))
    need = required_value(sf_at_parent * HAZ_RANGE.nominal.value,
                          HAZ_RANGE.nominal.value, required_sf)
    out["required_value"] = need
    out["exceeds_every_source"] = need["required"] > HAZ_RANGE.least_severe.value

    if emit:
        print(f"\nHAZ sensitivity - {HAZ_RANGE.name} across every sourced "
              f"value, gate SF {required_sf}")
        print(f"  {'rho':>6}  {'sf':>7}  {'verdict':<8} source")
        for r in out["evaluated"]:
            print(f"  {r['value']:>6.3f}  {r['sf']:>7.3f}  "
                  f"{'pass' if r['passed'] else 'FAIL':<8} {r['label']}")
        print(f"\n  verdict: {out['verdict'].upper()}")
        print(f"  {out['reason']}")
        print(f"\n  needs rho >= {need['required']:.4f} to pass; the most "
              f"generous source gives {HAZ_RANGE.least_severe.value:g} "
              f"({HAZ_RANGE.least_severe.label}).")
        if out["exceeds_every_source"]:
            print("  NO SOURCED VALUE IS SUFFICIENT. This is not a marginal "
                  "call between references -\n  the frame needs a joint "
                  "strength that no reference supports.")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--population", type=int, default=32)
    ap.add_argument("--generations", type=int, default=12)
    ap.add_argument("--promote", type=int, default=3)
    ap.add_argument("--no-fea", action="store_true")
    ap.add_argument("--haz", action="store_true",
                    help="report the gate across every sourced "
                         "rho_o,haz value and exit (no solver)")
    args = ap.parse_args()

    if args.haz:
        v = haz_verdict()
        return 0 if v["verdict"] == "pass" else 1

    space = make_space()
    if args.selftest:
        return selftest(space)

    reqs = make_requirements()
    engine = DesignEngine(ROOT)
    workdir = ROOT / "optimization" / "jetpack"
    workdir.mkdir(parents=True, exist_ok=True)
    cache = EvaluationCache(workdir / "cache.sqlite")

    print("=" * 78)
    print("STAGE 1 - search (L0 beam+statics, L1 real geometry + system mass)")
    print("=" * 78)
    screen = make_evaluator(engine, space, reqs, cache, with_fea=False)
    memory = FailureMemory(space)
    cfg = OptimizationConfig(population=args.population,
                             generations=args.generations, seed=11,
                             screen_fidelity=Fidelity.L1_GEOMETRY,
                             promote_fidelity=Fidelity.L3_HIGH_FEA,
                             promote_top_k=args.promote, workers=1)
    run = OptimizationRun(EvolutionarySearch(space, reqs, cfg,
                                             failure_memory=memory),
                          screen, reqs, cfg, workdir=workdir,
                          failure_memory=memory)
    t0 = time.time()
    run.run()
    secs = time.time() - t0
    s = run.summary()
    print(f"  {s['evaluations']} evaluations in {secs:.1f}s "
          f"({s['evaluations'] / max(secs, 1e-9):.1f}/s)")
    print(f"  feasible {s['feasible']}  infeasible {s['infeasible']}  "
          f"unknown {s['unknown']}   frontier {s['pareto_size']}")

    base_run = OptimizationRun(
        RandomSearch(space, reqs, cfg),
        make_evaluator(engine, space, reqs, EvaluationCache(), with_fea=False),
        reqs, cfg).run()
    cf = compare_fronts(run.front(), base_run.front(), reqs.objectives)
    pts = [c.result.objective_vector(reqs.objectives)
           for c in run.front() + base_run.front()]
    pts = [p for p in pts if p is not None]
    if pts:
        ref = [max(p[i] for p in pts) * 1.05 + 1e-9 for i in range(2)]
        print(f"  vs random baseline: HV evo "
              f"{hypervolume(run.front(), reqs.objectives, ref):.4g} vs random "
              f"{hypervolume(base_run.front(), reqs.objectives, ref):.4g}; "
              f"random dominated by evo {cf['b_points_dominated_by_a']}"
              f"/{cf['b_size']}, evo dominated by random "
              f"{cf['a_points_dominated_by_b']}/{cf['a_size']}")

    print("\n  FRONTIER - each row is a real trade, not a ranking")
    front = sorted(run.front(), key=lambda c: c.result.metrics["frame_mass_kg"])
    print(f"    {'mass kg':>8s} {'maxT C':>7s} {'T/W':>6s} {'CG mm':>6s} "
          f"{'SF':>6s}  {'material':17s} {'section':>16s} {'stations':>10s}")
    for c in front:
        m, v = c.result.metrics, c.values
        sec = f"{v['cb_thick']:.1f}x{v['cb_height']:.1f}"
        if v.get("doubler"):
            sec += f"+{v['pad_thick']:.0f}"
        print(f"    {m['frame_mass_kg']:8.3f} {m['max_service_temp_C']:7.0f} "
              f"{m['thrust_to_weight']:6.3f} {m['thrust_cg_offset_mm']:6.1f} "
              f"{m['sf.thermal_derated_yield']:6.2f}  {v['material']:17s} "
              f"{sec:>16s} {v['inner_x']:.0f}/{v['outer_x']:.0f}")

    if args.no_fea:
        print(render_run(run, top=2))
        return 0

    print("\n" + "=" * 78)
    print(f"STAGE 2 - promote {args.promote} to REAL CalculiX (authoritative)")
    print("=" * 78)
    fea_eval = make_evaluator(engine, space, reqs, cache, with_fea=True)
    run.evaluator = fea_eval
    t0 = time.time()
    promoted = run.promote(top_k=args.promote, fidelity=Fidelity.L3_HIGH_FEA)
    print(f"  {len(promoted)} solved in {time.time() - t0:.1f}s")
    survived = []
    for c in promoted:
        m = c.result.metrics
        print(f"    {c.candidate_id}  SF={m.get('sf.thermal_derated_yield')}  "
              f"-> {'PASS' if c.feasible else c.status.value.upper()}  "
              f"part {c.geometry_id}")
        if c.feasible:
            survived.append(c)
    demoted = [c for c in promoted if not c.feasible]
    if demoted:
        print(f"  {len(demoted)} DEMOTED by the solver after screening well. "
              f"The higher fidelity is authoritative.")

    print("\n" + "=" * 78)
    print("STAGE 3 - robustness of the recommended frame")
    print("=" * 78)
    arch = run.archetypes()
    winner = arch.get("balanced") or (survived[0] if survived else None)
    rb = None
    if winner is not None:
        rb = robustness(winner, screen,
                        [tolerance_perturbation("cb_thick", 0.3),
                         tolerance_perturbation("cb_height", 0.5),
                         tolerance_perturbation("pod_y", 8.0)],
                        samples=40, seed=4, max_fidelity=Fidelity.L1_GEOMETRY,
                        metrics_of_interest=["sf.thermal_derated_yield",
                                             "frame_mass_kg",
                                             "thrust_cg_offset_mm",
                                             "thrust_to_weight"])
        cgs = rb.metric_stats.get("thrust_cg_offset_mm", {})
        print(f"  {rb.samples} perturbed samples, failure fraction "
              f"{rb.failure_rate:.3f}; worst CG offset {cgs.get('max')} mm")

    print("\n" + "=" * 78)
    print("STAGE 4 - explained recommendation")
    print("=" * 78)
    print(render_run(run, top=3))

    out = workdir / "result.json"
    out.write_text(json.dumps({
        "summary": run.summary(),
        "front": [c.to_dict() for c in run.front()],
        "promoted": [c.to_dict() for c in promoted],
        "robustness": rb.to_dict() if rb else None,
        "failure_memory": memory.report(),
        "sensitivity": run.sensitivity(),
        "kt_basis": KT_BASIS,
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
