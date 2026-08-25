# Architecture audit — before the design-intelligence layer

Recorded 2026-08-25 from the actual implementation, not from documentation.
Baseline at commit `71a9d33`: **88 tests passing in 42 s**.

## Layer map (what the code actually does)

| Module | Role | State? | Cost | Verdict |
|---|---|---|---|---|
| `geometry.py` | spec → CadQuery solid, `mass_properties`, `spec_digest`, `apply_changes` | **pure** | ~20 ms build | Reuse as-is. The cleanest layer. |
| `parts.py` | `PartStore`: versioning + disk + log | disk + log | ~11 ms/part, globs dir every create | **Must stay out of the search loop** |
| `log.py` | `ActionLog`, FRACAS SQLite, pending→pass/fail | sqlite | ~1 ms/row | Provenance backbone; one row per candidate would be log spam |
| `mesh.py` | gmsh wrapper, C3D10 quality gate, node selectors | pure | 0.2–0.5 s | Reliability is **non-monotonic** (see traps) |
| `fea.py` | CalculiX static/buckling, limit states, derating, RBM + equilibrium checks | log | **8–290 s** | Authoritative. Never bypass. |
| `kinematics.py` + `chrono_worker.py` | Chrono via subprocess into a separate conda env | log | seconds | Integrate via adapter, keep the subprocess boundary |
| `massprops.py` | assembly CG / thrust-line / T:W limit states | log | ~1 ms | Cheap enough for L1 |
| `assembly.py` | assembly spec + worst-case/RSS tolerance stackup | log | ~1 ms | Analytic ⇒ excellent cheap constraint |
| `sourcing.py` | price book, `cost_for_qty`, `select_stock`, `process_cost`; `generate_bom` | log | ~22 µs | **Pure cost fns are ungated** ⇒ usable as an L1 objective |
| `production.py` | sign-off lock bound to spec digest | log | — | Do not touch. Code-level gate. |
| `report.py`, `viewer.py` | HTML/three.js generated **from the log** | reads log | — | Extend, don't duplicate |

## Measured cost — this is what dictates the fidelity ladder

| Level | Work | Measured | Throughput |
|---|---|---|---|
| **L0** | `validate_spec` + `spec_digest` (spec dict only) | 1.9 µs + 3.7 µs | ~250,000/s |
| **L1** | `build` + `mass_properties` + STEP export | ~24 ms | ~40/s |
| **L2** | mesh @8 mm (3.3 k nodes) + coarse solve | ~0.2 s + ~8 s | ~0.1/s |
| **L3** | mesh @3 mm + solve, 60 k–270 k nodes | 8 s → **290 s** | ~0.01/s |

Six orders of magnitude between L0 and L3. Staged filtering is not an
optimisation nicety here, it is the only way search is possible at all.

**Consequence for L0:** it must not import CadQuery or touch disk. Anything
that builds geometry is already L1.

## Properties that must not regress

1. **Determinism** — identical spec ⇒ identical digest ⇒ identical geometry.
2. **Log-first** — pending row written *before* the work, finalised after.
3. **Named limit states** — the gate is a margin against a named limit state
   with a `required_SF`, never a score or a pass percentage.
4. **Refuse on ambiguity** — unknown spec keys, no-op holes, empty node
   selections, out-of-range derating, unsourced materials/masses are all
   hard refusals. Silence is treated as the dangerous outcome.
5. **Sign-off lock** — enforced in code, bound to the spec digest, invalidated
   by later edits or a later failed validation.
6. **Non-linear gate** — a failure writes mode + magnitude *before* returning
   control; the next edit references that failure id.

## Coupling problems (why search cannot just call the existing API)

- **(A) `PartStore._next_part_number()` globs the parts directory on every
  create**, and each part writes a directory + 3 files + a log row. ~93/s and
  degrading. 100 k candidates would mean 100 k directories.
- **(B) `fea_static(geometry_id, …)` requires a materialised part.** Fine for
  promoted candidates, impossible for a population.
- **(C) One log row per action.** Correct for engineering actions, wrong for
  a 100 k-candidate sweep.
- **(D) No spec-level analytic model exists.** This is the real gap: there is
  currently *nothing* between "do nothing" and "build geometry". L0 has to be
  built.

## Traps found while auditing

- **Meshing is non-monotonic.** `mesh_step` at 5.0 mm fails the Jacobian gate
  on a part that meshes fine at 3.0 mm and 8.0 mm. An automated search
  **must** treat a mesh failure as `UNKNOWN`, never as `INFEASIBLE` — the
  latter would silently carve valid regions out of the design space.
- **`fea_static` used to hide its own outlier advisory** from callers (fixed
  in `71a9d33`). Any new layer must surface, not swallow, engine advisories.
- **A constraint-corner artifact reads as a real failure.** The outlier ratio
  is the discriminator (calibrated: ~1.0–1.2 physical, ~1.95–2.5 artifact).
  Failure classification must use it or it will chase phantom failures.
- **`generate_bom` is sign-off gated but the pricing functions are not** — so
  cost is available as an objective without touching the production gate.

## Where the new layer attaches

```
inventor/  (new — search & optimisation, proposes)
    │  candidates are plain specs + variable vectors, in memory
    │  cached on (spec digest, case digest, engine version)
    ▼
adapters  ──────────────────────────────────────────────┐
    │  L0 analytic (pure python)                        │
    │  L1 geometry.build + mass_properties + sourcing   │  existing engine
    │  L2/L3 PartStore.create_part → fea_static/buckling│  (decides)
    ▼                                                   │
design_engine/  (unchanged, authoritative) ─────────────┘
```

Only candidates **promoted** to L2+ are materialised into `PartStore` and the
FRACAS log. Everything below that lives in the cache. The optimiser never
decides validity — it proposes; the engine decides.
