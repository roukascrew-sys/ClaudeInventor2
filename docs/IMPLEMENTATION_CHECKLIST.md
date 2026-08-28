# Implementation checklist — physical realism roadmap

Concrete record of what has been implemented against the roadmap
(`ClaudeInventor/00_Home/Physical Realism Roadmap`, artifact
`54db049b-758c-4a07-91d4-06e62acb7724`), with commit hashes, file paths and
line numbers so every claim here can be checked in version control rather than
taken on trust.

**How to verify any row:** `git show <commit> -- <path>`, or
`git log -L<start>,<end>:<path>` to see how a specific block arrived.

Line numbers are the **definition line** of each item, verified with `git show`
against the commit named. They are not updated when later edits shift them —
the commit hash is the stable reference.

Status vocabulary: `DONE` (implemented + tested + committed) ·
`PARTIAL` (landed but incomplete, with what remains stated) ·
`BLOCKED` (cannot proceed, with the blocker named) · `TODO`.

---

## Track A · Verification before enrichment

### A1 — Mesh convergence with a reported Grid Convergence Index
**Status: BLOCKED** — solver memory, not method.

| What | Where |
|---|---|
| Study driver (sharp geometry) | `designs/jetpack_mesh_convergence.py` @ `3a0f77a` |
| Study driver (filleted) | `designs/jetpack_fillet_convergence.py` @ `3a0f77a` |
| Both geometries, one mesh | `designs/jetpack_convergence_round2.py` @ `3a0f77a` |

Blocker: 2.8 mm (~504k nodes) crashes `ccx.exe` with `0xC0000005` at a ~6.1 GB
working set. Reproduced on **both** the sharp and filleted geometries (416 s and
1665 s respectively), which rules out the corner as the cause. No GCI can be
computed from a single mesh size, so no convergence claim is made.

Groundwork that did land: `peak_rss_mb` instrumentation (see A4) means the next
attempt can predict the wall instead of discovering it.

---

### A2 — Automatic detection of geometric stress singularities
**Status: DONE** — commit `26fcb74`.

The gap this closes: the stress-outlier heuristic detects peaks pinned to a
*constraint* patch and reads them as artifacts. It cannot see a **geometric**
singularity at a sharp re-entrant corner, because that peak is fed by the
surrounding field rather than decoupled from it. `P0047@v1` read 1.633 —
comfortably "clean" — while its peak sat 1.28 mm off a 270° material corner
where linear elasticity has no finite stress at all.

| What | File | Line @ `26fcb74` |
|---|---|---|
| `TANGENT_TOL_DEG` — blend vs edge threshold | `design_engine/singularity.py` | 52 |
| `DEFAULT_RADIUS_ELEMENTS` — search radius | `design_engine/singularity.py` | 57 |
| `Singularity` record | `design_engine/singularity.py` | 65 |
| `_adjacent_faces()` — edge→faces by OCC hash | `design_engine/singularity.py` | 73 |
| `_interior_angle_deg()` — the convexity test | `design_engine/singularity.py` | 92 |
| `_reversed_in_face()` — edge orientation in face | `design_engine/singularity.py` | 140 |
| `sharp_concave_edges()` | `design_engine/singularity.py` | 146 |
| `_williams_exponent()` — bisection on the char. eq. | `design_engine/singularity.py` | 191 |
| `classify_peak()` — 3-way verdict | `design_engine/singularity.py` | 222 |
| `_edge_distance()` — point-to-SEGMENT | `design_engine/singularity.py` | 282 |
| import of the detector | `design_engine/fea.py` | 42–43 |
| check runs after the outlier gate | `design_engine/fea.py` | 970 |
| `singularity` in the logged details | `design_engine/fea.py` | 1017 |
| `singularity` in the returned payload | `design_engine/fea.py` | 1070 |
| Tests (15) | `tests/test_singularity.py` | whole file |

**Verified against the real parts:**

| Part | Sharp edges | Peak verdict | Nearest |
|---|---|---|---|
| `P0047@v1` (sharp) | 12 | **SINGULAR** | 1.28 mm |
| `P0048@v1` (filleted) | 8 | **CLEAN** | 9.51 mm |
| plain box | 0 | CLEAN | — |

End-to-end through a real CalculiX solve on a T-junction: **outlier ratio 1.044
— the cleanest possible reading — while the new check reports SINGULAR at
1.46 mm.** That is precisely the false negative that produced SF 3.844.

`_williams_exponent` independently recovers **0.4555** at 270°, the published
L-shaped-domain eigenvalue, by bisection on the characteristic equation rather
than by hard-coding it.

**Three implementation approaches were tried; the first two are wrong and are
documented in the module so they are not retried:**
1. Dihedral angles between mesh boundary triangles — cannot work; a concave
   fillet's facets are each slightly re-entrant (~198° at r=10, h=3.2).
2. Stepping off the edge and testing "is this still on face B" — projecting
   onto the *unbounded* surface accepts points outside the face, so both
   directions pass and 6 of a box's 12 convex edges came back as 270° corners.
3. **Correct:** `n_b × t`, with `t` in the edge's orientation *within* face B.
   Purely topological, so it survives faces with holes — which matters, because
   the pad's underside has the spine passing through it and its centroid lies
   in the hole.

**Finding this produced:** filleting took `P0047`→`P0048` from 12 to 8 sharp
edges, not to zero. The 15.71 mm remainders are exactly π×10/2 — the fillet arc
— where the blend runs out against the side walls at y = ±9.525. **The fillet is
a 2D blend of a 3D corner.** The current peak is 9.51 mm clear of them, so
SF 4.633 is not sitting on one, but that is luck rather than design.

---

### A3 — Extend the closed-form verification suite
**Status: PARTIAL** — two benchmarks exist, more needed.

| Benchmark | Where | Agreement |
|---|---|---|
| Cantilever bar vs analytic | `tests/test_phase4.py::test_solver_matches_analytic` | within gate |
| Euler buckling, fixed-pinned | `tests/test_buckling.py` | 0.16% |

Still to add: plate bending, thick-walled cylinder, notch Kt vs Peterson.

---

### A4 — Solver memory measured and modelled
**Status: DONE** — commit `3df4911`. *(Not a numbered roadmap item; it emerged
from A1's blocker and is recorded here because it is real implemented work.)*

| What | File | Lines @ `3df4911` |
|---|---|---|
| `_PROCESS_MEMORY_COUNTERS` struct | `design_engine/fea.py` | 572 |
| `_peak_rss_mb()` — peak, returns `None` never `0` | `design_engine/fea.py` | 585 |
| `_SolverRun` result carrier | `design_engine/fea.py` | 611 |
| `_run_solver()` — Popen, reads peak before kill | `design_engine/fea.py` | 622 |
| `peak_rss_mb` in the logged details payload | `design_engine/fea.py` | 1015 |
| Memory named in the solver-crash message | `design_engine/fea.py` | 916 |
| `peak_rss_mb` column | `design_engine/inventor/knowledge.py` | 73 |
| Ingest mapping | `design_engine/inventor/knowledge.py` | 201, 213 |
| `solver_memory_model()` | `design_engine/inventor/knowledge.py` | 452 |
| `predict_memory()` | `design_engine/inventor/knowledge.py` | 490 |
| `available_memory_mb()` | `design_engine/inventor/knowledge.py` | 502 |
| `affordable()` — memory checked **first** | `design_engine/inventor/knowledge.py` | 533 |
| Tests (7) | `tests/test_inventor_knowledge.py` | appended |

Verified: 15.8 MB captured on a 999-node solve. Historical rows read `None`,
never `0`. Model stays `None` until four real measured solves accumulate —
intended, and it means the veto is inert today.

---

## Track B · Real failure modes

### B1 — Fatigue as a named limit state
**Status: TODO**

### B2 — Modal analysis and a resonance separation gate
**Status: TODO** — the roadmap's "if only one thing gets done" item.

### B3 — Joints modelled as joints (weld throat, HAZ knockdown, bolt preload)
**Status: TODO**

### B4 — Load cases and combinations
**Status: TODO**

---

## Track C · Physical correlation

### C1 — Statistical allowables and honest knockdowns
**Status: TODO**

### C2 — Physical test rig and the loop back into calibration
**Status: TODO**

### C3 — Tolerance stackup connected to stress
**Status: TODO**

---

## Track D · Manufacturability

### D1 — Manufacturing constraints as feasibility rules
**Status: TODO**

### D2 — Dimensioned drawings
**Status: TODO**

---

## Track E · Additive manufacturing (far horizon)

E1–E4 **TODO**, deliberately deferred. Gated on A, C1 and D. A printed part is a
different material with different physics; none of the wrought 6061-T6511
allowables in the engine transfer to it.

---

## Supporting work already committed

Not roadmap items, but the foundation the roadmap depends on.

| What | Commit | Where |
|---|---|---|
| Fillet at the singular junction (`FILLET_R = 10 mm`) | `3a0f77a` | `designs/jetpack_optimization_run.py` |
| Structured edge selector for fillets | `3a0f77a` | `design_engine/geometry.py` |
| Fillet-matches-nothing refusal | `3a0f77a` | `design_engine/geometry.py` |
| Fillet test coverage (19 tests, was zero) | `3a0f77a` | `tests/test_geometry_fillet.py` |
| Vault-read enforcement hook | `3a0f77a` | `scripts/hooks/require_vault_query.py` |
| Vault query + receipt logging | `3a0f77a` | `scripts/vault_query.py` |
| Order-independent sign-off tests | `0c8a2c3` | `tests/test_phase5.py`, `tests/test_assembly_viewer.py` |
