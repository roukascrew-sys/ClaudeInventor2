"""Reproducible optimiser benchmark (Phase 21).

Exists because a single-seed comparison told a misleading story: on one seed
random search's frontier dominated the evolutionary one outright, while the
generation trace showed the GA improving monotonically and finding ~40% more
feasible designs. Both observations were real; neither justified a claim.

So: multiple seeds, multiple budgets, a SHARED hypervolume reference point so
the numbers are comparable, and no claim reported that the measurements do
not support.

Metrics per run:
  feasible_count     how much of the space it found that is usable at all
  best_mass/cost     single-objective extremes (a weak yardstick for a
                     multi-objective search, reported for completeness)
  hypervolume        dominated volume in loss space against a shared
                     reference - the honest multi-objective measure
  front_size         number of distinct trade-offs offered
  evaluations        budget actually consumed

Run:  .venv\\Scripts\\python.exe designs\\benchmark_optimizers.py
"""

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from design_engine import DesignEngine
from design_engine.inventor import (EvaluationCache, EvolutionarySearch,
                                    Fidelity, OptimizationConfig,
                                    OptimizationRun, RandomSearch,
                                    compare_fronts, hypervolume)

from bracket_optimization_run import (ROOT, make_evaluator, make_requirements,
                                      make_space)

SEEDS = [1, 2, 3, 4, 5]
BUDGETS = [(24, 8), (24, 20)]      # (population, generations) -> 192 and 480


def one_run(cls, space, reqs, engine, pop, gens, seed):
    cfg = OptimizationConfig(population=pop, generations=gens, seed=seed,
                             screen_fidelity=Fidelity.L1_GEOMETRY, workers=1)
    ev = make_evaluator(engine, space, reqs, EvaluationCache(), with_fea=False)
    t0 = time.perf_counter()
    run = OptimizationRun(cls(space, reqs, cfg), ev, reqs, cfg).run()
    return run, time.perf_counter() - t0


def main() -> int:
    space, reqs = make_space(), make_requirements()
    engine = DesignEngine(ROOT)
    results = {}
    all_points = []

    for pop, gens in BUDGETS:
        budget = pop * gens
        for cls in (RandomSearch, EvolutionarySearch):
            for seed in SEEDS:
                run, secs = one_run(cls, space, reqs, engine, pop, gens, seed)
                key = (cls.name, budget)
                results.setdefault(key, []).append((run, secs))
                all_points.extend(
                    v for v in (c.result.objective_vector(reqs.objectives)
                                for c in run.front()) if v is not None)
                print(f"  ran {cls.name:13s} budget={budget:4d} seed={seed} "
                      f"in {secs:5.1f}s")

    # ONE reference point for every hypervolume number, or they are not
    # comparable across runs.
    ref = [max(p[i] for p in all_points) * 1.05 + 1e-9 for i in range(2)]
    print(f"\nshared hypervolume reference: {[round(r, 5) for r in ref]}")

    table = {}
    for (name, budget), runs in sorted(results.items()):
        hvs, feas, masses, costs, fronts, times = [], [], [], [], [], []
        for run, secs in runs:
            front = run.front()
            # No `or 0.0` guard: hypervolume returned None for >2
            # objectives until 2026-09-02, and that guard silently
            # scored every such run as zero instead of failing.
            hvs.append(hypervolume(front, reqs.objectives, ref))
            fl = [c for c in run.all_candidates if c.feasible]
            feas.append(len(fl))
            masses.append(min((c.result.metrics["mass_kg"] for c in fl),
                              default=float("nan")))
            costs.append(min((c.result.metrics["cost_usd"] for c in fl),
                             default=float("nan")))
            fronts.append(len(front))
            times.append(secs)
        table[f"{name}@{budget}"] = {
            "hypervolume_mean": round(statistics.fmean(hvs), 5),
            "hypervolume_sd": round(statistics.pstdev(hvs), 5),
            "hypervolume_min": round(min(hvs), 5),
            "hypervolume_max": round(max(hvs), 5),
            "feasible_mean": round(statistics.fmean(feas), 1),
            "best_mass_mean": round(statistics.fmean(masses), 5),
            "best_cost_mean": round(statistics.fmean(costs), 2),
            "front_size_mean": round(statistics.fmean(fronts), 1),
            "seconds_mean": round(statistics.fmean(times), 2),
            "seeds": len(runs),
        }

    print("\n" + "=" * 100)
    print(f"{'optimizer@budget':22s} {'HV mean':>10s} {'HV sd':>9s} "
          f"{'feasible':>9s} {'front':>6s} {'best mass':>10s} "
          f"{'best cost':>10s} {'sec':>7s}")
    print("=" * 100)
    for k, v in table.items():
        print(f"{k:22s} {v['hypervolume_mean']:10.4f} {v['hypervolume_sd']:9.4f} "
              f"{v['feasible_mean']:9.1f} {v['front_size_mean']:6.1f} "
              f"{v['best_mass_mean']:10.4f} {v['best_cost_mean']:10.2f} "
              f"{v['seconds_mean']:7.2f}")

    print("\nHEAD-TO-HEAD (per seed, same budget: which front dominates?)")
    for pop, gens in BUDGETS:
        budget = pop * gens
        rnd = results[("random", budget)]
        evo = results[("evolutionary", budget)]
        evo_wins = rnd_wins = ties = 0
        for (r_run, _), (e_run, _) in zip(rnd, evo):
            c = compare_fronts(e_run.front(), r_run.front(), reqs.objectives)
            if c["b_points_dominated_by_a"] > c["a_points_dominated_by_b"]:
                evo_wins += 1
            elif c["b_points_dominated_by_a"] < c["a_points_dominated_by_b"]:
                rnd_wins += 1
            else:
                ties += 1
        print(f"  budget {budget:4d}: evolutionary wins {evo_wins}, "
              f"random wins {rnd_wins}, neither {ties}  (of {len(SEEDS)} seeds)")

    out = ROOT / "optimization" / "benchmark.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"table": table, "reference": ref,
                               "seeds": SEEDS, "budgets": BUDGETS},
                              indent=2), encoding="utf-8")
    print(f"\nwritten: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
