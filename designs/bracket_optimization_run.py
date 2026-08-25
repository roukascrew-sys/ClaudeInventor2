"""End-to-end design optimisation: an L-bracket, searched then solver-validated.

This is the integration proof for the `inventor` layer. It is NOT a mock: the
L1 stage builds real CadQuery solids and takes exact OCC mass properties, and
the promotion stage runs real CalculiX through the existing
`DesignEngine.run_fea_static`, writing real rows to the FRACAS log.

    USER REQUIREMENT
        carry 1200 N at the top of an upstand
        fit inside 130 x 95 x 105 mm
        stay under 1.4 kg
        keep a safety factor of at least 2.5
        be as light and as cheap as practical
            |
        RequirementSet  ->  DesignSpace  ->  search
            |
        L0 analytic screen        ~microseconds   (rules + beam theory)
        L1 real geometry + cost   ~24 ms          (CadQuery + OCC)
            |
        promote the best few
            |
        L3 CalculiX static FEA    ~10-60 s        AUTHORITATIVE
            |
        robustness sweep -> Pareto frontier -> explained recommendation

Topology, not just dimensions: `ribbed` is a categorical variable that adds a
gusset FEATURE to the spec, and `rib_h` only exists when it is True. That is a
change of architecture, and `active_if` keeps a non-ribbed design's identity
free of the unused rib variable so the cache treats them as one design.

The L0 rib model is deliberately crude (an effective-height reduction) and
says so. Watching FEA disagree with it is the point: the cheap model steers
the search, the solver decides the answer.

Run:  .venv\\Scripts\\python.exe designs\\bracket_optimization_run.py [--selftest]
"""

import argparse
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from design_engine import DesignEngine
from design_engine.geometry import GeometryError, SpecError
from design_engine.inventor import (AnalyticStage, Candidate, Constraint,
                                    CostStage, DesignSpace, DesignVariable,
                                    EvalContext, EvaluationCache, Evaluator,
                                    EvolutionarySearch, FailureMemory,
                                    FeaStage, FeasibilityRule, Fidelity,
                                    GeometryStage, Objective, Op,
                                    OptimizationConfig, OptimizationRun,
                                    Preference, RandomSearch, RequirementSet,
                                    RuleStage, Sense, VarType,
                                    compare_fronts, hypervolume,
                                    machining_cost_model, render_run,
                                    robustness, tolerance_perturbation)

ROOT = Path(__file__).parent.parent / "data"

# ---------------------------------------------------------------- problem
LOAD_N = 1200.0                 # vertical load at the top of the upstand
ENVELOPE = (130.0, 95.0, 105.0)  # x, y, z mm
MASS_LIMIT_KG = 1.4
REQUIRED_SF = 2.5

MATERIALS = {
    "6061-T6511": {
        "name": "6061-T6511", "E_MPa": 68900, "nu": 0.33, "yield_MPa": 276,
        "density_kg_m3": 2700,
        "source": "OnlineMetals product pages (pid 1145, 1087), reused from "
                  "this project's ladder build: yield 40 ksi. E and nu are "
                  "standard published values for wrought aluminium.",
        "stock_usd_per_kg": 13.0,
        "machining_min_per_cm3": 0.55,
    },
    "1018-cold-finish": {
        "name": "1018-cold-finish", "E_MPa": 205000, "nu": 0.29,
        "yield_MPa": 372, "density_kg_m3": 7850,
        "source": "OnlineMetals pid 4790 (ASTM-A108): yield 54 ksi. E and nu "
                  "are standard published values for carbon steel.",
        "stock_usd_per_kg": 3.2,
        "machining_min_per_cm3": 1.35,
    },
}

COST_BASIS = ("ESTIMATE, not a quotation. The project price book holds real "
              "captured prices for Bolt Depot fasteners and a few OnlineMetals "
              "steel bars only - it contains no 6061 stock - so rather than "
              "invent a catalogue price this model uses stated $/kg and "
              "machining-rate assumptions. Flagged as an estimate everywhere "
              "it is reported.")


# ---------------------------------------------------------------- geometry
def build_spec(values: dict, ctx) -> dict:
    """Variable assignment -> a real design_engine spec.

    A spec_builder rather than dot-path edits, because `ribbed` changes the
    FEATURE LIST, not a dimension. Dot paths cannot add or remove a feature.
    """
    mat = MATERIALS[values["material"]]
    t = float(values["plate_t"])
    w = float(values["width"])
    h = float(values["upstand_h"])
    L = float(values["plate_len"])

    features = [
        # base plate, centred in x/y, sitting on z=0
        {"op": "box", "x": L, "y": w, "z": t, "at": [0, 0, 0]},
        # upstand at the -x end, rising from the plate
        {"op": "box", "x": t, "y": w, "z": h,
         "at": [-(L / 2.0 - t / 2.0), 0, t], "mode": "union"},
    ]
    if values.get("ribbed"):
        rib_h = float(values["rib_h"])
        rib_len = float(values["rib_len"])
        # gusset spanning from the upstand out along the plate
        features.append({
            "op": "box", "x": rib_len, "y": float(values["rib_t"]), "z": rib_h,
            "at": [-(L / 2.0) + t + rib_len / 2.0, 0, t], "mode": "union"})
    # two mounting holes through the plate, clear of the upstand and rib
    hole_x = L / 2.0 - 18.0
    for sign in (-1.0, 1.0):
        features.append({"op": "hole", "d": 9.0,
                         "at": [hole_x, sign * (w / 2.0 - 14.0)], "face": ">Z"})

    return {"name": "opt-bracket", "units": "mm",
            "density_kg_m3": mat["density_kg_m3"], "features": features}


# ---------------------------------------------------------------- L0 model
def analytic_screen(values: dict, ctx) -> dict:
    """Cantilever bending at the base of the upstand.

    The critical section is the upstand where it meets the plate: a rectangle
    `width` wide by `plate_t` thick, carrying moment LOAD_N * upstand_h.

    RIB MODEL IS CRUDE AND SAYS SO: a gusset is represented as an effective
    reduction in moment arm, h_eff = h - 0.6*rib_h. That is monotone and in
    the right direction but it is not a section calculation. It exists to
    RANK candidates cheaply, and FEA overrides it at promotion. Anywhere the
    two disagree, the solver is right.
    """
    mat = MATERIALS[values["material"]]
    t, w, h = float(values["plate_t"]), float(values["width"]), float(values["upstand_h"])
    h_eff = h
    if values.get("ribbed"):
        h_eff = max(6.0, h - 0.6 * float(values["rib_h"]))
    Z = w * t ** 2 / 6.0
    stress = (LOAD_N * h_eff / Z) if Z > 0 else float("inf")
    return {
        "analytic_bending_stress_MPa": stress,
        "sf.yield_von_mises": (mat["yield_MPa"] / stress) if stress > 0 else float("inf"),
        "section_Z_mm3": Z,
    }


def make_cost_model(values_material_key="material"):
    """Per-material cost model, dispatched at evaluation time."""
    def fn(values: dict, metrics: dict, ctx) -> dict:
        mat = MATERIALS[values[values_material_key]]
        inner = machining_cost_model(
            rate_usd_per_hr=75.0, setup_min=12.0,
            min_per_cm3_removed=mat["machining_min_per_cm3"],
            stock_usd_per_kg=mat["stock_usd_per_kg"],
            stock_density_kg_m3=mat["density_kg_m3"], basis=COST_BASIS)
        return inner(values, metrics, ctx)
    fn.__name__ = "bracket_cost_model"
    return fn


# ---------------------------------------------------------------- FEA case
def build_case(cand: Candidate, ctx) -> dict:
    """Real CalculiX case: bolt-hole bores fixed, load on the upstand top."""
    mat = MATERIALS[cand.values["material"]]
    L = float(cand.values["plate_len"])
    w = float(cand.values["width"])
    t = float(cand.values["plate_t"])
    hole_x = L / 2.0 - 18.0
    material = {k: mat[k] for k in ("name", "E_MPa", "nu", "yield_MPa", "source")}
    return {
        "material": material,
        "mesh": {"max_size_mm": 3.0},
        "constraints": [
            {"where": {"cylinder": {"axis": "z", "center": [hole_x, sign * (w / 2.0 - 14.0)],
                                    "r": 4.5, "tol": 0.8}},
             "dof": [1, 2, 3]}
            for sign in (-1.0, 1.0)
        ],
        "loads": [{"where": {"axis": "z", "at": "max"},
                   "force_total_N": [0.0, 0.0, -LOAD_N]}],
        "limit_state": {"name": "yield_von_mises", "required_SF": REQUIRED_SF},
    }


# ---------------------------------------------------------------- assembly
def make_space() -> DesignSpace:
    return DesignSpace(name="l-bracket", version=1, variables=[
        DesignVariable("material", VarType.CATEGORICAL,
                       values=list(MATERIALS), description="alloy choice"),
        DesignVariable("plate_len", VarType.CONTINUOUS, lo=70.0, hi=125.0,
                       step=2.5, units="mm"),
        DesignVariable("width", VarType.CONTINUOUS, lo=40.0, hi=90.0,
                       step=2.5, units="mm"),
        DesignVariable("plate_t", VarType.CONTINUOUS, lo=4.0, hi=16.0,
                       step=0.5, units="mm"),
        DesignVariable("upstand_h", VarType.CONTINUOUS, lo=30.0, hi=95.0,
                       step=2.5, units="mm"),
        # --- topology switch: adding a rib changes the FEATURE LIST ---
        DesignVariable("ribbed", VarType.CATEGORICAL, values=[False, True],
                       description="add a gusset between plate and upstand"),
        DesignVariable("rib_h", VarType.CONTINUOUS, lo=10.0, hi=70.0, step=2.5,
                       units="mm", active_if=lambda v: v.get("ribbed") is True),
        DesignVariable("rib_len", VarType.CONTINUOUS, lo=15.0, hi=70.0, step=2.5,
                       units="mm", active_if=lambda v: v.get("ribbed") is True),
        DesignVariable("rib_t", VarType.CONTINUOUS, lo=4.0, hi=14.0, step=0.5,
                       units="mm", active_if=lambda v: v.get("ribbed") is True),
    ], rules=[
        # microsecond-cost geometric sanity, so nothing impossible reaches the
        # kernel let alone the solver
        FeasibilityRule("rib_shorter_than_upstand",
                        lambda v: (not v.get("ribbed")) or v["rib_h"] < v["upstand_h"] - 5.0),
        FeasibilityRule("rib_fits_on_plate",
                        lambda v: (not v.get("ribbed"))
                        or v["rib_len"] < v["plate_len"] - v["plate_t"] - 30.0),
        FeasibilityRule("rib_not_wider_than_bracket",
                        lambda v: (not v.get("ribbed")) or v["rib_t"] <= v["width"] - 10.0),
        FeasibilityRule("holes_clear_of_upstand",
                        lambda v: (v["plate_len"] / 2.0 - 18.0)
                        > (-(v["plate_len"] / 2.0) + v["plate_t"] + 12.0)),
        FeasibilityRule("holes_inside_width", lambda v: v["width"] / 2.0 - 14.0 > 6.0),
    ])


def make_requirements() -> RequirementSet:
    return RequirementSet(
        name="l-bracket-v1",
        constraints=[
            Constraint("strength", "sf.yield_von_mises", Op.GE, REQUIRED_SF,
                       units="-", scale=REQUIRED_SF,
                       source=f"stated design gate: SF >= {REQUIRED_SF} on "
                              f"yield. Engineering judgment for a bolted "
                              f"structural bracket, not a code citation."),
            Constraint("mass_budget", "mass_kg", Op.LE, MASS_LIMIT_KG,
                       units="kg", source="stated requirement: 1.4 kg budget"),
            Constraint("envelope_x", "bbox_x_mm", Op.LE, ENVELOPE[0], units="mm",
                       source="stated mounting envelope"),
            Constraint("envelope_y", "bbox_y_mm", Op.LE, ENVELOPE[1], units="mm",
                       source="stated mounting envelope"),
            Constraint("envelope_z", "bbox_z_mm", Op.LE, ENVELOPE[2], units="mm",
                       source="stated mounting envelope"),
        ],
        objectives=[
            Objective("mass", "mass_kg", Sense.MIN, units="kg",
                      description="as light as practical"),
            Objective("cost", "cost_usd", Sense.MIN, units="USD",
                      description="as cheap as practical (estimate)"),
        ],
        preferences=[
            Preference("simplicity", "machining_minutes", Sense.MIN,
                       description="prefer easier machining"),
        ])


def make_evaluator(engine, space, reqs, cache, with_fea=True):
    ctx = EvalContext(space=space, requirements=reqs, engine=engine)
    stages = [
        RuleStage(),
        AnalyticStage(analytic_screen, name="beam_screen", version="v1"),
        GeometryStage(spec_builder=build_spec),
        CostStage(make_cost_model(), name="cost", version="v1"),
    ]
    if with_fea:
        stages.append(FeaStage(build_case, analysis="static", mesh_mm=3.0,
                               fidelity=Fidelity.L3_HIGH_FEA))
    return Evaluator(stages, ctx, cache=cache)


# ---------------------------------------------------------------- selftest
def selftest(space) -> int:
    """Build every kind of design once before spending search time."""
    import random
    rng = random.Random(0)
    ok = bad = 0
    reasons = {}
    for _ in range(120):
        vals = space.sample(rng)
        if not space.is_feasible(vals):
            continue
        try:
            spec = build_spec(vals, None)
            from design_engine.geometry import build, mass_properties
            mass_properties(spec, build(spec))
            ok += 1
        except (SpecError, GeometryError) as exc:
            bad += 1
            reasons[type(exc).__name__] = reasons.get(type(exc).__name__, 0) + 1
    print(f"selftest: {ok} built, {bad} refused  {reasons}")
    return 0 if ok > 0 else 1


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--population", type=int, default=28)
    ap.add_argument("--generations", type=int, default=10)
    ap.add_argument("--promote", type=int, default=3)
    ap.add_argument("--no-fea", action="store_true")
    args = ap.parse_args()

    space = make_space()
    if args.selftest:
        return selftest(space)

    reqs = make_requirements()
    engine = DesignEngine(ROOT)
    workdir = ROOT / "optimization" / "bracket"
    workdir.mkdir(parents=True, exist_ok=True)
    cache = EvaluationCache(workdir / "cache.sqlite")

    print("=" * 72)
    print("STAGE 1 - cheap search (L0 analytic + L1 real geometry & cost)")
    print("=" * 72)
    screen_eval = make_evaluator(engine, space, reqs, cache, with_fea=False)
    memory = FailureMemory(space)
    cfg = OptimizationConfig(population=args.population,
                             generations=args.generations, seed=17,
                             screen_fidelity=Fidelity.L1_GEOMETRY,
                             promote_fidelity=Fidelity.L3_HIGH_FEA,
                             promote_top_k=args.promote, workers=4)
    run = OptimizationRun(EvolutionarySearch(space, reqs, cfg, failure_memory=memory),
                          screen_eval, reqs, cfg, workdir=workdir,
                          failure_memory=memory)
    t0 = time.time()
    run.run()
    screen_seconds = time.time() - t0
    s = run.summary()
    print(f"  {s['evaluations']} evaluations in {screen_seconds:.1f}s  "
          f"({s['evaluations']/max(screen_seconds,1e-9):.1f}/s)")
    print(f"  feasible {s['feasible']}   infeasible {s['infeasible']}   "
          f"unknown {s['unknown']}")
    print(f"  pareto frontier: {s['pareto_size']}   "
          f"cache hit rate {s['cost']['cache']['hit_rate']:.1%}")

    print("\n  --- baseline comparison: random search, same budget ---")
    base_cache = EvaluationCache()
    base_eval = make_evaluator(engine, space, reqs, base_cache, with_fea=False)
    base_run = OptimizationRun(RandomSearch(space, reqs, cfg), base_eval, reqs,
                               cfg).run()

    def best(r, metric):
        feas = [c for c in r.all_candidates if c.feasible]
        vals = [c.result.metrics.get(metric) for c in feas]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else float("inf")

    print(f"  evolutionary : best mass {best(run,'mass_kg'):.4f} kg   "
          f"best cost ${best(run,'cost_usd'):.2f}   "
          f"feasible {len([c for c in run.all_candidates if c.feasible])}   "
          f"front {len(run.front())}")
    print(f"  random       : best mass {best(base_run,'mass_kg'):.4f} kg   "
          f"best cost ${best(base_run,'cost_usd'):.2f}   "
          f"feasible {len([c for c in base_run.all_candidates if c.feasible])}   "
          f"front {len(base_run.front())}")

    # Single-objective "best" is the WRONG yardstick for a multi-objective
    # search: NSGA-II spends budget spreading along the frontier instead of
    # driving one axis to its extreme, so it can lose on "best mass" while
    # producing a strictly better set of trade-offs. Compare the fronts.
    fe, fr = run.front(), base_run.front()
    cmpf = compare_fronts(fe, fr, reqs.objectives)
    print(f"  front comparison: {cmpf['b_points_dominated_by_a']}/{cmpf['b_size']} "
          f"random points are dominated by the evolutionary front; "
          f"{cmpf['a_points_dominated_by_b']}/{cmpf['a_size']} the other way")
    allpts = fe + fr
    vecs = [c.result.objective_vector(reqs.objectives) for c in allpts]
    vecs = [v for v in vecs if v is not None]
    if vecs and len(reqs.objectives) == 2:
        ref = [max(v[i] for v in vecs) * 1.1 + 1e-9 for i in range(2)]
        hv_e, hv_r = (hypervolume(fe, reqs.objectives, ref),
                      hypervolume(fr, reqs.objectives, ref))
        print(f"  hypervolume (same reference, larger is better): "
              f"evolutionary {hv_e:.5g}  random {hv_r:.5g}")

    if args.no_fea:
        print("\n--no-fea: stopping before solver validation")
        print(render_run(run, top=2))
        return 0

    print("\n" + "=" * 72)
    print(f"STAGE 2 - promote top {args.promote} to REAL CalculiX FEA (authoritative)")
    print("=" * 72)
    fea_eval = make_evaluator(engine, space, reqs, cache, with_fea=True)
    run.evaluator = fea_eval
    t0 = time.time()
    promoted = run.promote(top_k=args.promote, fidelity=Fidelity.L3_HIGH_FEA)
    fea_seconds = time.time() - t0
    print(f"  {len(promoted)} candidates through real FEA in {fea_seconds:.1f}s")

    survived, demoted = [], []
    for c in promoted:
        sf_fea = c.result.metrics.get("sf.yield_von_mises")
        fid = c.result.metric_fidelity.get("sf.yield_von_mises")
        tag = fid.label if fid else "?"
        verdict = "PASS" if c.feasible else c.status.value.upper()
        print(f"    {c.candidate_id}  SF={sf_fea}  [{tag}]  -> {verdict}"
              f"   geometry {c.geometry_id}")
        (survived if c.feasible else demoted).append(c)
    if demoted:
        print(f"  {len(demoted)} candidate(s) DEMOTED by the solver after "
              f"screening well. The higher fidelity is authoritative.")

    print("\n" + "=" * 72)
    print("STAGE 3 - robustness of the recommended design")
    print("=" * 72)
    arch = run.archetypes()
    winner = arch.get("balanced") or (survived[0] if survived else None)
    rb = None
    if winner is not None:
        perts = [tolerance_perturbation("plate_t", 0.15),
                 tolerance_perturbation("width", 0.4),
                 tolerance_perturbation("upstand_h", 0.4)]
        rb = robustness(winner, screen_eval, perts, samples=40, seed=5,
                        max_fidelity=Fidelity.L1_GEOMETRY,
                        metrics_of_interest=["sf.yield_von_mises", "mass_kg",
                                             "cost_usd"])
        print(f"  {rb.samples} perturbed samples, observed failure fraction "
              f"{rb.failure_rate:.3f}")
        print(f"  worst-case SF over the sweep: "
              f"{rb.worst_case.get('sf.yield_von_mises')}")

    print("\n" + "=" * 72)
    print("STAGE 4 - explained recommendation")
    print("=" * 72)
    print(render_run(run, top=3))

    if rb is not None:
        print("\nROBUSTNESS OF THE BALANCED CHOICE")
        print(json.dumps(rb.to_dict(), indent=2))

    print("\nFAILURE MEMORY (learned empirically, artifacts excluded)")
    print(json.dumps(memory.report(), indent=2)[:2000])

    out = workdir / "result.json"
    out.write_text(json.dumps({
        "summary": run.summary(),
        "front": [c.to_dict() for c in run.front()],
        "promoted": [c.to_dict() for c in promoted],
        "robustness": rb.to_dict() if rb else None,
        "failure_memory": memory.report(),
        "sensitivity": run.sensitivity(),
    }, indent=2, default=str), encoding="utf-8")
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
