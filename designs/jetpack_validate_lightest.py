"""Settle it: does the optimiser's LIGHTEST feasible frame survive CalculiX?

The hand-built frame is 5.530 kg with a real FEA SF of 5.274. The recalibrated
search says its lightest feasible design is 3.901 kg. That claim is screened,
not validated, so it gets a solver run - the tightest-margin design first,
which is the one actually in doubt.
"""
import json, sys, time
sys.path.insert(0, '.')
from design_engine import DesignEngine
from design_engine.fea import ValidationTools
from design_engine.inventor import (EvalContext, EvaluationCache, Evaluator,
                                    Candidate, Fidelity, RuleStage,
                                    AnalyticStage, GeometryStage, CallableStage,
                                    FeaStage)
import designs.jetpack_optimization_run as J

space, reqs = J.make_space(), J.make_requirements()
eng = DesignEngine(J.ROOT)
# the engine's 600s default was blown by 675k-node meshes; give the solver
# real budget for a deliberate single validation
eng.validation = ValidationTools(J.ROOT, eng.log, eng.parts,
                                 eng.validation.ccx_path,
                                 solve_timeout_s=2400)

res = json.load(open('data/optimization/jetpack/result.json'))
front = sorted(res['front'], key=lambda c: c['result']['metrics']['frame_mass_kg'])
target = front[0]
v = space.resolve(target['values'])
print("VALIDATING THE LIGHTEST FEASIBLE FRAME")
print(f"  variables: {v}")
m = target['result']['metrics']
print(f"  screened: {m['frame_mass_kg']:.3f} kg, maxT {m['max_service_temp_C']:.0f} C, "
      f"L0 SF {m['sf.thermal_derated_yield']:.3f}, T/W {m['thrust_to_weight']:.3f}")

ctx = EvalContext(space=space, requirements=reqs, engine=eng)
ev = Evaluator([RuleStage(),
                AnalyticStage(J.analytic_screen, name="beam_and_statics"),
                GeometryStage(spec_builder=J.build_spec),
                CallableStage(J.system_stage, "system", Fidelity.L1_GEOMETRY),
                FeaStage(J.build_case, analysis="static",
                         mesh_ladder=[5.0, 4.0, 3.2],
                         fidelity=Fidelity.L3_HIGH_FEA)],
               ctx, cache=EvaluationCache())
c = Candidate(values=v)
t0 = time.time()
ev.evaluate(c, max_fidelity=Fidelity.L3_HIGH_FEA)
print(f"\n  solved in {time.time()-t0:.0f}s -> status {c.status.value.upper()}  part {c.geometry_id}")
for s in c.result.stages:
    if 'fea' in s.stage:
        print(f"  mesh attempts: {[(a['mesh_mm'], a['meshed']) for a in s.provenance.get('mesh_attempts',[])]}")
        print(f"  mesh used: {s.provenance.get('mesh_mm')} mm")
        print(f"  FEA SF = {s.metrics.get('sf.thermal_derated_yield')}")
        print(f"  max vM = {s.metrics.get('max_von_mises_MPa')} MPa  "
              f"allowable {s.metrics.get('allowable_MPa')} MPa  "
              f"outlier {s.metrics.get('stress_outlier_ratio')}")
        for f in s.failures:
            print(f"  failure[{f.failure_class.value}] trustworthy={f.trustworthy}: {f.message[:200]}")
print(f"\n  frame mass (exact, kernel): {c.result.metrics.get('mass_kg'):.3f} kg")
print(f"  vs hand-built 5.530 kg with FEA SF 5.274")
