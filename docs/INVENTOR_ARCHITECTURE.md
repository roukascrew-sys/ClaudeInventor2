# ClaudeInventor — design-intelligence layer

A search and optimisation layer above the deterministic engineering engine.
It **proposes**; the engine **decides**.

```
Human requirements
      |
RequirementSet          constraints (hard) | objectives (vector) | preferences (soft)
      |
DesignSpace             mixed types, dependencies, conditional variables, cheap rules
      |
Optimizer.ask()         RandomSearch | EvolutionarySearch (NSGA-II style)
      |
Candidate               content-addressed id, lineage, evidence
      |
Evaluator               staged + cached + fidelity-tagged
      |     L0  RuleStage, AnalyticStage      ~4 us      pure python
      |     L1  GeometryStage, CostStage      ~24 ms     CadQuery + OCC (real solids)
      |     L2  FeaStage (coarse mesh)        ~8 s       CalculiX
      |     L3  FeaStage (converged)          8-290 s    CalculiX  <- AUTHORITATIVE
      |
EvaluationResult        metrics + per-metric fidelity + constraint results + failures
      |
Optimizer.tell()        constrained domination: feasibility before objectives
      |
promote()               best few re-run at L3; the solver may DEMOTE them
      |
pareto / archetypes / robustness / sensitivity / failure memory
      |
explain                 why this design, what it trades, what is still unknown
```

## Why staging is mandatory, not an optimisation

Measured on this repository (see `docs/ARCHITECTURE_AUDIT.md`):

| Level | Work | Cost | Throughput |
|---|---|---|---|
| L0 | spec-only analytic | ~4 µs | ~250,000/s |
| L1 | real solid + exact mass properties | ~24 ms | ~40/s |
| L2 | coarse mesh + solve | ~8 s | ~0.1/s |
| L3 | converged mesh + solve | 8–290 s | ~0.01/s |

Six orders of magnitude. A population is filtered at L0/L1 and only a handful
of finalists ever consume solver time.

## The five rules this layer enforces structurally

1. **The engine decides.** No stage computes a safety factor the engine could
   compute, and no stage converts an engine refusal into a pass. When L1 and
   L3 disagree about the same metric, L3 overwrites it and the metric's
   fidelity tag records which model answered. Observed in practice: an
   analytic SF of 16.6 against a solver SF of 39.2 on the same part.

2. **Constraints are not preferences.** `apply_requirements` sets INVALID from
   a hard constraint regardless of objective values, and the optimiser uses
   constrained domination (Deb) so a feasible candidate always beats an
   infeasible one. Low mass cannot buy off a violated safety factor.

3. **Four states, not two.** `VALID / INVALID / UNKNOWN / NOT_EVALUATED`.
   A missing metric is UNKNOWN, and a *mandatory* constraint blocks on UNKNOWN
   as well as INVALID — an unevaluated safety gate must never read as a pass.
   Conversely a stage that definitively refused (a design-space rule, an
   unbuildable spec) yields INVALID even though no metrics exist, so a known
   refusal is not downgraded to ignorance.

4. **Numerical artifacts do not steer search.** Two audit findings are encoded:
   meshing is non-monotonic (a part that meshes at 3 mm and 8 mm can fail at
   5 mm), so a mesh refusal is UNKNOWN, never INFEASIBLE; and a stress outlier
   ratio above ~1.9 marks a constraint-corner singularity, so such a
   "failure" is classed NUMERICAL, marked `trustworthy=False`, and excluded
   from both `FailureMemory` and failure-informed mutation.

5. **Nothing is invented.** Mandatory constraints require a `source`. Point
   masses require a `source`. Cost models carry a `basis` string. Derating
   curves refuse to extrapolate. Robustness reports an observed failure
   fraction *and the sample count*, never a fitted reliability figure.

## Caching and determinism

`EvaluationCache` is keyed per stage on:

```
candidate values (float-normalised) + design-space digest + stage name
  + stage config digest + base-spec digest + CODE_DIGEST
```

`CODE_DIGEST` is a hash of `geometry.py`, `mesh.py` and `fea.py` source. An
explicit version constant would go stale the moment someone edits `fea.py` and
forgets to bump it, and a stale cache returning a pre-bugfix safety factor is
exactly what this project refuses elsewhere. Editing the engine invalidates
the cache automatically.

Candidate identity is **content-addressed**, and inactive variables are
excluded from it — two designs differing only in a variable neither uses are
the same design and share one cache entry.

## Concurrency

`Evaluator.evaluate_many(workers=N)` uses threads. Any stage may declare
`thread_safe = False`; if one is present the evaluator falls back to serial
for the whole population rather than corrupting shared state. `FeaStage`
declares it, because `PartStore._next_part_number()` allocates by globbing the
parts directory and `ActionLog` holds a single SQLite connection — both race.

Materialisation into `PartStore` happens **only** at promotion, so a
100,000-candidate sweep does not create 100,000 part directories or log rows.

## Extending it

* **A new physics/analysis** → write a stage (anything with `name`,
  `fidelity`, `config_digest()`, `run()`), or wrap a function in
  `CallableStage`. That is the documented adapter boundary for subsystems not
  yet integrated (kinematics, assembly mass properties, thermal).
* **A new problem class** → supply an `AnalyticStage` model and a
  `spec_builder`. Nothing in `inventor/` assumes a design is a beam, is
  mechanical, has FEA, or has a fixed number of components. `models.py` is a
  library, not a framework assumption.
* **A new optimiser** → subclass `Optimizer` and implement `ask`/`tell`;
  register it in `OPTIMIZERS`.
* **Topology, not just dimensions** → a categorical variable that changes the
  feature list, plus `active_if` on the variables it enables. See `ribbed` in
  `designs/bracket_optimization_run.py`.

## Backward compatibility

`design_engine/inventor/` is a pure addition. No existing engineering API
changed. `create_part`, `edit_part`, `run_fea_static`, `run_fea_buckling`,
`run_kinematics`, `check_mass_properties`, `check_tolerance_stackup`,
`sign_off`, `export_production_package`, `generate_bom`, `generate_viewer`
and `generate_report` behave exactly as before, and the 88 pre-existing tests
pass unmodified.

## Measured performance (Phase 21)

`designs/benchmark_optimizers.py`, 5 seeds per cell, shared hypervolume
reference `[0.23967, 191.49606]`, L0+L1 screening on the L-bracket problem
(9 variables, 5 feasibility rules, 2 objectives, 5 hard constraints).

| optimizer@budget | HV mean | HV sd | feasible | front size | best mass (kg) | best cost ($) | sec |
|---|---|---|---|---|---|---|---|
| evolutionary@192 | **14.87** | 1.90 | 146.2 | 3.4 | 0.1050 | 80.04 | 9.7 |
| random@192 | 6.62 | 3.02 | 94.6 | 2.8 | 0.1413 | 112.50 | 8.7 |
| evolutionary@480 | **19.99** | 0.39 | 336.8 | 15.0 | 0.0761 | 66.95 | 22.3 |
| random@480 | 9.97 | 3.27 | 235.0 | 2.8 | 0.1265 | 97.24 | 24.9 |

Head-to-head per seed (whose frontier dominates the other's):
**evolutionary 5/5 at budget 192, and 5/5 at budget 480.**

Two things worth stating plainly:

* **`evolutionary@192` beats `random@480`** on hypervolume (14.87 vs 9.97) —
  better trade-offs from 2.5x fewer evaluations. That is the claim the staged
  architecture exists to support, and it is measured rather than asserted.
* **Random search is high variance** (HV sd 3.0–3.3 versus 0.39–1.90). A
  single seed of this benchmark showed random winning outright, which is why
  the benchmark takes multiple seeds. One seed is an anecdote.

Cost accounting from a real end-to-end run (`bracket_optimization_run.py`,
192 screened + 3 promoted): 192 L1 evaluations in 12.1 s, then 3 real
CalculiX solves in 18.2 s (6.07 s each). The promoted candidates reused their
cached L0/L1 stages — only the FEA stage ran fresh.

**The solver demoted 2 of the 3 promoted candidates.** Their analytic
screening safety factors did not survive contact with a real mesh. That is
the multi-fidelity contract working as intended, not a defect.
