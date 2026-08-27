"""Bootstrap the ClaudeInventor Obsidian vault from what the project knows.

Per section 22 of the vault spec: create the structure, the canonical Home /
Current State / Roadmap / Open Questions notes, architecture notes for the
major subsystems, and initial failure and lesson notes from real documented
bugs — and explicitly do NOT create hundreds of notes merely because files
exist. Every note here records something that was actually learned, with the
evidence that supports it.

Re-runnable. `Vault.write` updates canonical notes in place rather than
forking duplicates, so running this again after more work refreshes the graph
instead of littering it.

    .venv\\Scripts\\python.exe scripts\\bootstrap_vault.py [--root PATH]
"""

import argparse
import sys
from pathlib import Path

# Load vault.py BY PATH, not through the package. `design_engine/__init__.py`
# eagerly imports the geometry kernel, so `from design_engine.vault import ...`
# would drag in CadQuery - and the whole point of the vault (and the knowledge
# base) is that the reasoning layer keeps working when the kernel does not.
# Proven necessary: the CAD kernel stopped importing on this machine for a
# still-unexplained reason, and this script still had to run.
import importlib.util

_VAULT = Path(__file__).parent.parent / "design_engine" / "vault.py"
_spec = importlib.util.spec_from_file_location("ci_vault", _VAULT)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
Vault = _mod.Vault

DEFAULT_ROOT = Path.home() / "Downloads" / "ClaudeInventor"


def build(v: Vault) -> None:
    made = v.ensure_structure()
    print(f"  folders ensured ({made} created)")

    # ---------------------------------------------------------- templates
    v.write("_Templates", "Template - Failure", type="failure", status="unknown",
            body="""Symptom:

Conditions:

Affected design:

Failure mode:

Root cause:

Evidence:

Attempted fixes:

Successful fix:

Regression test:

Lesson learned:""")
    v.write("_Templates", "Template - Decision", type="decision", status="proposed",
            body="""Context:

Decision:

Alternatives considered:

Why selected:

Tradeoffs:

Consequences:

Evidence:

Supersedes:""")
    v.write("_Templates", "Template - Session", type="session", status="complete",
            body="""## Goal

## Starting State

## Changes

## Discoveries

## Failures

## Decisions

## Tests

## Remaining Work

## Important Context""")

    # -------------------------------------------------------------- home
    v.write("00_Home", "Home", type="index", body="""
The institutional memory of ClaudeInventor. The repository is executable
truth; this vault is reasoning truth — why the system works the way it does.

## Orientation
- [[Current State]] — what is true right now
- [[Project Memory]] — how it came to be true, newest first
- [[Roadmap]] — what is worth doing next, ranked by evidence
- [[Open Questions]] — what we do not know

## Architecture
- [[System Architecture]] · [[Design Engine]] · [[Optimization Engine]]
- [[Architecture Decisions]]

## Engineering
- [[Validation Philosophy]] · [[Multi Fidelity Evaluation]] · [[Evaluation Cache]]
- [[6061-T6 Thermal Derating]] · [[Carbon Steel Thermal Derating]]

## Designs
- [[Jetpack Frame]] · [[Extension Ladder]]

## Optimization
- [[Jetpack Frame Optimization Run]] · [[Optimizer Benchmark]]

## What went wrong, and what it taught us
- [[Recent Failures]] · [[Lessons]]
""", tags=["claudeinventor", "index"])

    v.write("00_Home", "Current State", type="index", confidence="high", body="""
Verified against the repository at commit `9f8943e`, 2026-08-27.

## What exists and works
- **Deterministic engineering engine** (`design_engine/`): geometry, meshing,
  CalculiX FEA (static + buckling), kinematics via Chrono, mass properties,
  tolerance stackup, sourcing, sign-off gate, FRACAS log, viewers.
- **Design-intelligence layer** (`design_engine/inventor/`): requirements,
  design space, candidates, staged evaluator with cache, NSGA-II style search,
  Pareto, robustness, sensitivity, failure memory, explainability.
- **Knowledge base** (`design_engine/inventor/knowledge.py`): 73 real solver
  observations ingested from the FRACAS log, calibration pairs, and a learned
  solver cost model. Stdlib-only.
- **Project memory** (`design_engine/memory.py`): the chronological half of
  this vault — validated, append-only, supersedes rather than deletes.
  [[Project Memory]]
- **189 tests passing**, 2026-08-27.

## Nothing is blocked
The CAD kernel imports and the full suite runs. An earlier entry here reported
the engine as blocked by Windows Smart App Control; **that attribution was
wrong** — Smart App Control is still enforcing and CadQuery imports anyway. See
[[The CAD kernel blockage was misattributed to Smart App Control]]. The
mechanism of the original failure is still unknown.

## Best validated results
- Jetpack frame, searched: **3.901 kg at FEA SF 3.844** (`P0047@v1`)
- Jetpack frame, hand-built: **5.530 kg at FEA SF 5.274** (`P0031@v2`)
- Both clear the 3.0 gate. See [[Jetpack Frame Optimization Run]].
""", links=["Home", "Project Memory", "Roadmap", "Open Questions"])

    v.write("00_Home", "Roadmap", type="index", body="""
Ranked by expected impact, from measured evidence rather than a wishlist.
Full reasoning in the linked notes.

1. **Verify mesh convergence** — the headline SF 3.844 was computed at one
   mesh size and never checked. [[Mesh convergence is unverified]]
2. **Use the L2 coarse-FEA rung** — screening jumps L1 to L3, which is why a
   76% model error survived to the frontier. [[Multi Fidelity Evaluation]]
3. **Make FEA parallelisable** — it is 97% of wall time and runs serial.
   [[Solver runs cannot be parallelised]]
4. **Automate model calibration** — currently a human reads FEA results and
   edits a constant. [[Screening models need automatic calibration]]
5. **Recover from solver timeouts** — [[Solver timeout wastes the full budget]]
6. **Extend search to the system level** — only the frame is searched today.
""", links=["Current State", "Open Questions"])

    v.write("00_Home", "Open Questions", type="open-question", confidence="unknown",
            body="""
Genuinely unresolved. Recording them is more useful than guessing.

- **Is SF 3.844 converged?** Computed at 3.2 mm, the first size that passed
  the Jacobian gate. No convergence study was run.
  → [[Mesh convergence is unverified]]
- **What temperature does the frame actually reach?** Every thermal-headroom
  number is conditional on an assumed 150 °C. Never measured.
- **Is a 150 mm pilot CG shift realistic?** The trimmability constraint rests
  on it and no real pilot has been tested.
- **Does the steel branch mean anything for sustained service?** Its 500–742 °C
  headroom comes from a fire-design table. → [[Fire design data is not service data]]
- **Why did one optimisation run briefly become non-deterministic?** Traced to
  a status bug and no longer reproducible, but never fully root-caused. A
  regression test now guards it.
""", links=["Current State"])

    # ------------------------------------------------------ architecture
    v.write("06_Architecture/System_Architecture", "System Architecture",
            type="architecture", confidence="high", body="""
Two layers with a hard boundary between them.

```
inventor/     proposes     search, optimisation, candidates
    |
adapters      translates   the ONLY place that knows CadQuery/CalculiX exist
    |
design_engine decides      geometry, mesh, solver, gates, sign-off, log
```

The optimiser never decides validity. It proposes; the engine decides. That
single sentence is the load-bearing idea of the whole system, and several
architecture decisions exist only to make it structurally true rather than a
convention someone might forget.

The FRACAS log is the source of truth. Reports, viewers and the knowledge base
are all *derived* from it and never authored independently.
""", links=["Design Engine", "Optimization Engine", "Validation Philosophy",
            "The engine decides, the optimiser proposes"])

    v.write("06_Architecture/Design_Engine", "Design Engine", type="architecture",
            confidence="high", body="""
The deterministic substrate. Predates the optimisation layer and was not
modified by it beyond additive changes.

- `geometry.py` — pure spec → CadQuery solid. Same spec always gives the same
  digest and the same solid. The cleanest layer in the system.
- `parts.py` — versioned store. Allocates part numbers by globbing a
  directory, which is why it is not thread-safe.
- `log.py` — FRACAS SQLite. Pending row written *before* the work, finalised
  after. Single connection, hence serial.
- `mesh.py` — gmsh + a C3D10 Jacobian quality gate that refuses bad meshes.
- `fea.py` — CalculiX. Named limit states, rigid-body-mode and equilibrium
  checks, thermal derating.
- `production.py` — sign-off lock bound to the spec digest, enforced in code.

Measured cost: geometry ~24 ms, coarse FEA ~8 s, converged FEA 8–290 s (up to
800 s on a 675k-node frame).
""", links=["System Architecture", "Multi Fidelity Evaluation",
            "Validation Philosophy"])

    v.write("06_Architecture/Optimization_Engine", "Optimization Engine",
            type="architecture", confidence="high", body="""
`design_engine/inventor/` — a pure addition. No existing engineering API
changed and all 88 pre-existing tests still pass.

- **requirements** — constraints (hard gates), objectives (a vector, never
  collapsed), preferences (soft tie-breakers), kept distinct.
- **space** — mixed variable types with real dependencies: `bounds_from`,
  `values_from`, `active_if`, derived values, microsecond feasibility rules.
- **candidate** — content-addressed identity, full lineage, and every metric
  tagged with the fidelity that produced it.
- **evaluate** — staged evaluator with a per-stage cache.
- **adapters** — the only module that touches the engineering engine.
- **optimizers** — RandomSearch baseline and an NSGA-II style
  EvolutionarySearch using constrained domination.
- **knowledge** — stdlib-only history store. [[Engineering Knowledge Base]]
""", links=["System Architecture", "Multi Fidelity Evaluation",
            "Evaluation Cache", "Engineering Knowledge Base"])

    v.write("06_Architecture/Architecture_Decisions", "Architecture Decisions",
            type="index", body="""
- [[The engine decides, the optimiser proposes]]
- [[Constraints are gates, not preferences]]
- [[UNKNOWN is not a pass]]
- [[Numerical artifacts must not steer search]]
- [[Cache keys include the engineering source digest]]
- [[The knowledge layer is stdlib-only]]
""")

    v.write("06_Architecture/Architecture_Decisions",
            "The engine decides, the optimiser proposes", type="decision",
            confidence="high", body="""
**Context:** A search layer that can compute its own safety factors will
eventually convince itself of something untrue, because a cheap model is
always available and always faster than a solver.

**Decision:** No stage in `inventor/` computes a quantity the engine can
compute, and no stage converts an engine refusal into a pass. When two
fidelities disagree about the same metric the higher one overwrites it, and
the metric carries a fidelity tag recording which model answered.

**Alternatives considered:** Let the optimiser hold its own physics and
reconcile later — rejected, because reconciliation never happens under time
pressure and the cheap number is the one that gets quoted.

**Tradeoffs:** Search is slower and must materialise real parts to get real
answers.

**Evidence:** Measured disagreements of 76% and 96% between the screening
model and CalculiX on the jetpack frame, both in the *unsafe* direction. One
design screened at SF 5.226 and measured 2.968, below its gate. See
[[Screening models are optimistic in the unsafe direction]].
""", links=["System Architecture", "Validation Philosophy",
            "Screening models are optimistic in the unsafe direction"])

    v.write("06_Architecture/Architecture_Decisions",
            "Constraints are gates, not preferences", type="decision",
            confidence="high", body="""
**Context:** The natural way to build a multi-objective optimiser is a
weighted score. Under a weighted score a design that violates a safety factor
can win by being light and cheap.

**Decision:** Hard constraints are feasibility gates. `apply_requirements`
sets INVALID from a violated mandatory constraint regardless of objective
values, and the optimiser uses constrained domination (Deb): a feasible
candidate always beats an infeasible one, and two infeasible candidates are
compared on total normalised violation only.

**Consequences:** Objectives stay a vector the whole way through; the output
is a Pareto frontier, not a winner.

**Evidence:** Directly tested — `test_apply_requirements_blocks_on_hard_failure_regardless_of_objectives`
gives a candidate a near-zero mass and a safety factor of 0.5 and asserts it
is INVALID.
""", links=["System Architecture", "Optimization Engine"])

    v.write("06_Architecture/Architecture_Decisions", "UNKNOWN is not a pass",
            type="decision", confidence="high", body="""
**Context:** Two-state validity (valid/invalid) forces every unevaluated or
unevaluable result into one bucket, and the tempting bucket is "fine".

**Decision:** Four states — VALID, INVALID, UNKNOWN, NOT_EVALUATED. A missing
metric is UNKNOWN, and a *mandatory* constraint blocks on UNKNOWN as well as
INVALID. Conversely a stage that definitively refused yields INVALID even
though no metrics exist, so a known refusal is never downgraded to ignorance.
A stage that ran and could not answer degrades the whole result to UNKNOWN.

**Evidence:** The last clause was added after a real failure. A promotion
errored inside the solver stage while the L0 estimate for the same metric was
still present, so the constraint passed *at L0 fidelity* and the run printed
`3 solved in 0.0s ... PASS` with no solver run and no part created. See
[[Silent promotion failure]].
""", links=["Silent promotion failure", "Validation Philosophy"])

    v.write("06_Architecture/Architecture_Decisions",
            "Numerical artifacts must not steer search", type="decision",
            confidence="high", body="""
**Context:** A solver or mesher failure looks like a design failure to a naive
optimiser, which will then avoid a perfectly good region of the design space.

**Decision:** Failures are classified, and `NUMERICAL` is separated from the
physical modes and marked `trustworthy=False`. Untrustworthy failures are
excluded from `FailureMemory` and from failure-informed mutation. Two specific
rules follow:
- A mesh refusal is UNKNOWN, never INFEASIBLE — meshing is non-monotonic.
  [[Meshing is non-monotonic]]
- A stress outlier ratio above ~1.9 marks a constraint-corner singularity, so
  the "failure" is evidence about the model, not the design.

**Evidence:** The 1.9 threshold is calibrated on 24 real runs — physically
sound models sat at 1.00–1.20, artificial constraint singularities at
1.95–2.12. Used correctly in the jetpack promotions, where outlier ratios of
1.63–1.76 confirmed the demotions were *real* and not artifacts.
""", links=["Meshing is non-monotonic", "Optimization Engine"])

    v.write("06_Architecture/Architecture_Decisions",
            "Cache keys include the engineering source digest", type="decision",
            confidence="high", body="""
**Context:** A cached safety factor computed by buggy code is worse than no
cache, because it is indistinguishable from a fresh correct answer.

**Decision:** Every cache key includes `CODE_DIGEST`, a hash of the source of
`geometry.py`, `mesh.py` and `fea.py`, alongside the candidate values, design
space digest, stage name and stage config.

**Alternatives considered:** A hand-maintained version constant — rejected,
because it goes stale the moment someone edits `fea.py` and forgets to bump
it, and the failure mode is silent.

**Tradeoffs:** Any edit to those modules invalidates the whole cache, even a
comment. Accepted: correctness beats reuse.
""", links=["Evaluation Cache", "Optimization Engine"])

    v.write("06_Architecture/Architecture_Decisions",
            "The knowledge layer is stdlib-only", type="decision",
            confidence="high", body="""
**Context:** The knowledge base answers questions about history. History is a
database problem, not a CAD problem.

**Decision:** `inventor/knowledge.py` and `vault.py` import nothing beyond the
standard library — no geometry, no solver, not even the sibling `candidate`
module at runtime (it is a `TYPE_CHECKING` import only).

**Evidence, and it stopped being hypothetical the same day it was written:**
Windows Smart App Control began enforcing and blocked the unsigned
`_nlopt.pyd`, so CadQuery would not import and the entire engine went down.
The knowledge base and its 15 tests kept working, and the accumulated
engineering history stayed fully readable.
[[Smart App Control blocks the CAD kernel]]
""", links=["Engineering Knowledge Base",
            "Smart App Control blocks the CAD kernel"])

    # ----------------------------------------------------- engineering
    v.write("03_Engineering/FEA", "Validation Philosophy", type="architecture",
            confidence="high", body="""
Rules the engine enforces in code rather than trusting to discipline.

- **Named limit states.** A gate is a margin against a named limit state with
  a required safety factor — never a score or a pass percentage.
  Currently: `yield_von_mises`, `elastic_buckling`, `thermal_derated_yield`.
- **Refuse on ambiguity.** Unknown spec keys, holes that remove no material,
  empty node selections, out-of-range derating and unsourced material data are
  all hard refusals. Silence is treated as the dangerous outcome.
- **Non-linear gate.** A failure writes its mode and magnitude to the log
  *before* returning control, and the next edit references that failure id, so
  nothing is ever retried blind.
- **Sign-off is a code lock**, bound to the spec digest and invalidated by any
  later edit or later failed validation.
- **Model checks before answers.** Rigid-body-mode detection, equilibrium over
  all nodes, and a completeness check run before any safety factor is reported.
""", links=["Design Engine", "The engine decides, the optimiser proposes",
            "UNKNOWN is not a pass"])

    v.write("03_Engineering/Materials", "6061-T6 Thermal Derating",
            type="material", confidence="high", body="""
Source: **EN 1999-1-2:2007 Table 1a**, row EN AW-6061 T6, extracted from the
standard text rather than recalled. 0.2% proof strength ratio k₀.₂,θ:

| °C | 20 | 100 | 150 | 200 | 250 | 300 | 350 | 550 |
|---|---|---|---|---|---|---|---|---|
| k₀.₂,θ | 1.00 | 0.95 | 0.91 | 0.79 | 0.55 | 0.31 | 0.10 | 0 |

Modulus from Table 2 (E ratio): 1.00 / 0.97 / 0.93 / 0.86 / 0.78 / 0.68 /
0.54 / 0.40 at 20–400 °C.

**The consequence that matters:** retention collapses to 0.10 by 350 °C, so an
aluminium structure **saturates near 345 °C no matter how thick it is**. Past
that line no section helps and only a material change does. This finding
survived a full model recalibration unchanged, because it is set by the
material curve rather than by any sizing assumption.

**Limitation:** this is a FIRE-DESIGN table, stated for up to 2 hours thermal
exposure. It does not cover creep or thermal cycling.
[[Fire design data is not service data]]
""", links=["Carbon Steel Thermal Derating", "Jetpack Frame",
            "Fire design data is not service data"])

    v.write("03_Engineering/Materials", "Carbon Steel Thermal Derating",
            type="material", confidence="high", body="""
Source: **EN 1993-1-2:2005 Table 3.1**, extracted from the standard text.
Effective yield reduction k_y,θ:

| °C | 20–400 | 500 | 600 | 700 | 800 | 1200 |
|---|---|---|---|---|---|---|
| k_y,θ | 1.000 | 0.780 | 0.470 | 0.230 | 0.110 | 0 |

Carbon steel holds **full effective yield to 400 °C**, which is why the
jetpack engine cradles are 1018 steel and not aluminium: they clamp a turbine
casing with a 480–750 °C exhaust gas temperature.

**Limitation, and it is a serious one:** k_y,θ is an *effective yield at 2%
total strain* for short-duration fire exposure. Using it to claim
sustained-service headroom at 500–740 °C — as the optimiser's steel branch
implicitly does — is not defensible. Creep and oxidation are unassessed.
[[Fire design data is not service data]]
""", links=["6061-T6 Thermal Derating", "Fire design data is not service data"])

    v.write("04_Optimization/Experiments", "Multi Fidelity Evaluation",
            type="method", confidence="high", body="""
Measured on this repository, not assumed:

| Level | Work | Cost | Throughput |
|---|---|---|---|
| L0 | rules + closed form, spec only | ~4 µs | ~250,000/s |
| L1 | real solid + exact mass properties | ~24 ms | ~40/s |
| L2 | coarse mesh + solve | ~8 s | ~0.1/s |
| L3 | converged mesh + solve | 8–800 s | ~0.01/s |

Six orders of magnitude between L0 and L3. Staging is not an optimisation
nicety here; it is the only reason search is possible at all.

**The L2 rung is defined but unused, and that is a real gap.** Screening jumps
L1 → L3, which is how a 76% model error survived long enough to shape an
entire Pareto frontier. A coarse 8-second rung would have exposed it after a
handful of solves. Ranked #2 on [[Roadmap]].
""", links=["Design Engine", "Optimization Engine", "Roadmap",
            "Screening models are optimistic in the unsafe direction"])

    v.write("04_Optimization/Surrogates", "Evaluation Cache", type="method",
            confidence="high", body="""
Per-stage, content-addressed. Key = candidate values (float-normalised) +
design-space digest + stage name + stage config digest + base-spec digest +
`CODE_DIGEST`.

Per-stage rather than per-evaluation, so a candidate promoted from L1 to L3
does not re-run its geometry, and a change to the FEA case does not invalidate
its mass properties. Observed working: promoted jetpack candidates showed
their L0/L1 stages as `(cached)` with only the solver stage running fresh.

Candidate identity is content-addressed and *excludes inactive variables*, so
two designs differing only in a variable neither of them uses are recognised
as the same design and share one entry.

**Honest limit:** within a single run the hit rate is low (9.8% on the jetpack
run) because each generation proposes genuinely new designs. The cache pays
off across re-runs and promotions, not inside one generation.
""", links=["Cache keys include the engineering source digest",
            "Optimization Engine"])

    v.write("08_Code_Memory/Modules", "Engineering Knowledge Base",
            type="code-module", confidence="high",
            extra={"module": "design_engine/inventor/knowledge.py"}, body="""
The numeric half of the second brain. The vault holds reasoning; this holds
measurements.

**Purpose:** make the FRACAS log *askable*. Has a design like this been solved
before, does the cheap model systematically lie in this regime, which corner
of the design space keeps failing, can I afford to promote this candidate.

**Invariants:**
- Derived, never authored. Every row keeps `source_action_id` back to the
  logged action. Ingest is idempotent.
- Refuses thin data. `correction()` returns `None`, never a neutral 1.0, below
  `min_observations`.
- Every answer carries its evidence — ratios, sample counts, the actual prior
  designs matched.

**State:** 73 solver observations ingested from the real log. Learned solver
cost model t ≈ 40.1 s × (nodes/100k)^1.661 from 39 runs.

**Known limitation:** at n=39 the cost model's band is ×1.5–1.9 wide, too wide
to gate on, so `affordable()` returns a three-way verdict including
`marginal` rather than a confident binary. Restricting the fit does not help —
the exponent goes physically implausible as n drops.

Tests: `tests/test_inventor_knowledge.py`, 15 passing, loaded by path so they
run with the CAD kernel unavailable.
""", links=["The knowledge layer is stdlib-only", "Optimization Engine",
            "Solver timeout wastes the full budget"])

    # ------------------------------------------------------------ designs
    v.write("02_Designs/Approved", "Jetpack Frame", type="design",
            status="experimental", confidence="medium", body="""
Structural frame for a 4 × JetCat P400-PRO side-pod rig. **Structure only** —
this is not an airworthy aircraft and the project has never claimed it is.

**Sourced inputs:** 397 N thrust each, 3.65 kg dry, 148.4 mm diameter, EGT
480–750 °C, cross-checked across three sources.

**Architecture:** spine + doubler pad + crossbeam, welded, engines outboard at
|x| = 330–550 mm and near the plane of the pilot's back.

**Why 4 engines and not the 5 originally chosen:** five about a symmetry plane
forces one onto the centreline, where a 148.4 mm engine intersects the pilot's
torso and exhausts onto their back and legs. Not a tuning problem.

**Why the engines are outboard and not aft:** slung 130 mm aft, the thrust line
misses the system CG by ~170 mm and needs a ~215 mm pilot CG shift to trim —
beyond plausible authority. Outboard-and-in-plane brings it to ~77 mm.

**Validated designs:**
- `P0031@v2` hand-built — 5.530 kg, FEA SF 5.274, ~257 °C headroom
- `P0047@v1` searched — 3.901 kg, FEA SF 3.844, ~217 °C headroom

**NOT covered:** propulsion, fuel systems and fire, fatigue at 98,000 rpm,
exhaust plume mapping, attitude control, and any physical qualification.
Those are what actually kill jetpack pilots.
""", links=["Jetpack Frame Optimization Run", "6061-T6 Thermal Derating",
            "Fire design data is not service data",
            "Mesh convergence is unverified"])

    v.write("02_Designs/Approved", "Extension Ladder", type="design",
            status="validated", confidence="high", body="""
20 ft extension ladder, 7 signed parts covering every load path: bending,
buckling, bearing, shear, joint transfer, base friction. Validated against
OSHA 1926.1053, ANSI A14.2 geometry limits and AISC 360 ASD.

Kept in the vault as the project's reference example of a *complete* design
loop with genuine fail→fix cycles recorded in the log rather than a curated
result.

Two durable engineering lessons came out of it:
- A naive hollow-rectangle formula under-predicted an open C-channel by 72%.
  The neutral axis of an open channel is not at mid-height.
  → [[Analytic section models must match the real section]]
- The intermittent solver corruption was root-caused here.
  → [[ccx_MT produces wrong answers]]
""", links=["ccx_MT produces wrong answers",
            "Analytic section models must match the real section"])

    # ------------------------------------------------- optimization runs
    v.write("04_Optimization/Optimization_Runs", "Jetpack Frame Optimization Run",
            type="optimization-run", status="complete", confidence="high", body="""
**Goal:** re-run a hand-designed frame as a search problem and find out
whether the optimiser beats a human.

**Design space:** 11 variables — material, crossbeam section, doubler topology
(`active_if` on three dependent variables), spine section, two engine stations,
fore-aft pod position. 7 microsecond feasibility rules.

**Objectives:** minimise frame mass, maximise thermal headroom. Genuinely
trading — a thicker beam survives a hotter structure but weighs more.

**Constraints (gates, not objectives):** SF ≥ 3.0 thermal-derated,
thrust/weight ≥ 1.15, thrust–CG offset ≤ 100 mm, width ≤ 1400 mm. A jetpack
the pilot cannot trim is not a worse design, it is not a design.

**Result:** 386 evaluated, 259 feasible, 40 on the frontier, 4 solved by
CalculiX.

| | mass | FEA SF | headroom |
|---|---|---|---|
| hand-built `P0031@v2` | 5.530 kg | 5.274 | ~257 °C |
| searched `P0047@v1` | **3.901 kg** | 3.844 | ~217 °C |

29.5% lighter — **and not a strict win.** It bought mass by spending margin
and 40 °C of headroom. The frontier states that rather than hiding it.

**Findings:**
- The aluminium branch saturates at ~345 °C; crossing to steel costs ~6 kg.
- Recalibration **vindicated the hand-built doubler**: beforehand the frontier
  was nearly doubler-free, afterwards nearly every competitive design has one.
- Sensitivity: `cb_height` dominates thermal headroom (ρ=+0.34); `pad_thick`
  and `cb_thick` dominate mass (ρ=+0.285, +0.285) over n=333.
""", links=["Jetpack Frame", "Screening models are optimistic in the unsafe direction",
            "Silent promotion failure", "Load selector picked the wrong face",
            "Optimizer Benchmark", "Mesh convergence is unverified"])

    v.write("04_Optimization/Experiments", "Optimizer Benchmark", type="benchmark",
            status="complete", confidence="high", body="""
5 seeds × 2 budgets, shared hypervolume reference, on the L-bracket problem.

| optimizer@budget | HV mean | HV sd | feasible | front |
|---|---|---|---|---|
| evolutionary@192 | **14.87** | 1.90 | 146.2 | 3.4 |
| random@192 | 6.62 | 3.02 | 94.6 | 2.8 |
| evolutionary@480 | **19.99** | 0.39 | 336.8 | 15.0 |
| random@480 | 9.97 | 3.27 | 235.0 | 2.8 |

Evolutionary wins 5/5 seeds at both budgets. `evolutionary@192` beats
`random@480` — better trade-offs from 2.5× fewer evaluations.

**The methodological lesson is the durable part.** A single seed showed random
search winning outright and looked like a defect in the GA. Random is simply
high variance (HV sd 3.0–3.3 vs 0.39–1.90) and that seed was an outlier.
→ [[One seed is an anecdote]]
""", links=["One seed is an anecdote", "Optimization Engine"])

    # ----------------------------------------------------------- failures
    v.write("05_Failures", "Recent Failures", type="index", body="""
Failures are high-value information here, not embarrassments. Each links to
its root cause, fix and regression test.

**Architectural**
- [[Silent promotion failure]] — the most dangerous one
- [[Load selector picked the wrong face]]
- [[Promotion spent solver time where nothing was in doubt]]

**Modelling**
- [[Screening models are optimistic in the unsafe direction]]
- [[Analytic section models must match the real section]]

**Numerical / environmental**
- [[Meshing is non-monotonic]]
- [[ccx_MT produces wrong answers]]
- [[Solver timeout wastes the full budget]]
- [[Smart App Control blocks the CAD kernel]]
""")

    v.write("05_Failures/Bugs", "Silent promotion failure", type="failure",
            status="resolved", confidence="high",
            extra={"severity": "critical", "failure_kind": "architecture"}, body="""
**Symptom:** A promotion run printed `3 solved in 0.0s ... PASS` for three
candidates. No solver ran, no part was materialised, and all three reported
VALID.

**Conditions:** L3 `FeaStage` raised (a material dict carried an extra key the
deck validator correctly rejected) while the L0 beam model had already
supplied a value for the same metric.

**Failure mode:** The constraint evaluated against the surviving L0 value and
passed *at L0 fidelity*. The failed high-fidelity stage changed nothing.

**Root cause:** `apply_requirements` derived status only from constraint
results. A stage that ran and could not answer had no way to invalidate a
result that a lower-fidelity stage had already made look fine.

**Why this is the worst possible bug here:** the entire value of the system is
that a screened design cannot masquerade as a validated one. This bug made
exactly that happen, silently, and printed PASS while doing it.

**Fix:** a stage that RAN and returned UNKNOWN now degrades the whole result
to UNKNOWN. Stages skipped by a fidelity ceiling are NOT_EVALUATED, so
ordinary cheap screening is unaffected.

**Regression test:** `test_failed_promotion_cannot_leave_a_design_looking_validated`

**Lesson:** [[UNKNOWN is not a pass]]
""", links=["UNKNOWN is not a pass", "Jetpack Frame Optimization Run"])

    v.write("05_Failures/Simulation_Failures", "Load selector picked the wrong face",
            type="failure", status="resolved", confidence="high",
            extra={"severity": "high", "failure_kind": "simulation"}, body="""
**Symptom:** Every FEA promotion failed with
`selector {...axis z at min, axis x at -340...} matched 0 nodes`.

**Root cause:** The engines hang under the *crossbeam*, whose underside sits
at `SPINE_Z/2 - cb_height/2`. The case builder selected `{"axis":"z","at":"min"}`
— the bottom of the *spine*, a plane where the frame is only `spine_x` wide.
Every engine-station patch at |x| = 330–430 mm matched nothing.

**What made it hard to see:** a mesh-refinement ladder had just been added,
and it dutifully refined 5.0 → 4.0 → 3.2 mm against what looked like a meshing
problem. The plausible symptom masked the real defect.

**Fix:** select the crossbeam underside explicitly by z position.

**Lesson:** when a new mechanism and a new bug arrive together, the mechanism
will absorb the blame. Verify the selector finds nodes *before* spending
solver time — a check that costs milliseconds and saved ~25 minutes per run
afterwards.
""", links=["Jetpack Frame Optimization Run", "Meshing is non-monotonic"])

    v.write("05_Failures/Simulation_Failures", "Meshing is non-monotonic",
            type="failure", status="resolved", confidence="high",
            extra={"severity": "medium", "failure_kind": "simulation"}, body="""
**Symptom:** A part meshes cleanly at 8.0 mm and 3.0 mm but is refused at
5.0 mm by the Jacobian quality gate.

**Why it matters far more than it looks:** an automated search that treats a
mesh refusal as *infeasible* silently deletes valid regions of the design
space, and nothing in the output would ever reveal it.

**Fix, in two parts:**
1. A mesh refusal is classified `NUMERICAL`, marked `trustworthy=False`, and
   yields UNKNOWN — never INFEASIBLE.
2. `FeaStage` takes a bounded `mesh_ladder` and refines instead of giving up,
   recording every attempt. If all rungs are refused it still returns UNKNOWN
   rather than pretending.

**Evidence it was needed:** all three promoted jetpack frames were refused at
5.0 mm because of their 6.5 mm lug holes. With the ladder, 3.2 mm succeeded at
401k nodes.

**Regression tests:** `test_mesh_ladder_refines_instead_of_giving_up`,
`test_mesh_ladder_exhausted_is_unknown_not_a_pass`
""", links=["Numerical artifacts must not steer search",
            "Load selector picked the wrong face"])

    v.write("05_Failures/Simulation_Failures", "ccx_MT produces wrong answers",
            type="failure", status="resolved", confidence="high",
            extra={"severity": "critical", "failure_kind": "simulation"}, body="""
**Symptom:** Intermittent corrupted CalculiX results across multiple sessions —
identical input decks producing different answers.

**Root cause:** `ccx_MT.exe`, the multithreaded solver. Measured on an
identical buckling job with the same mesh and same deck: single-threaded gave
the correct answer 5/5 times, bit-identical. Multithreaded gave a wrong answer
4/5 times.

**Fix:** `ValidationTools` defaults to `threads=1` and single-threaded
`ccx.exe`. Multithreading is opt-in with the measurement recorded in the
docstring. A tight loop went from 1-in-15 and 1-in-27 corrupted to **0 in 30**.

**Lesson:** an intermittent wrong *answer* is far more dangerous than a crash,
because nothing surfaces it. Suspect the tool, not just the model — and prove
it with a controlled repeat rather than a hunch.
""", links=["Validation Philosophy", "Extension Ladder"])

    v.write("05_Failures/Engineering_Failures",
            "Screening models are optimistic in the unsafe direction",
            type="failure", status="resolved", confidence="high",
            extra={"severity": "high", "failure_kind": "engineering"}, body="""
**Symptom:** Two promoted jetpack frames measured far weaker than screened:

| design | screened SF | measured SF | error |
|---|---|---|---|
| aluminium, no doubler | 5.226 | **2.968** | 76% |
| steel, no doubler | 20.532 | 10.454 | 96% |

The aluminium one **failed its 3.0 gate**. Outlier ratios of 1.65 and 1.76,
below the 1.9 artifact threshold, confirmed these were real stresses.

**Root cause:** a genuine modelling gap. With no doubler the model applied no
stress-concentration factor at all, treating the crossbeam/spine T-junction as
a clean cantilever root. It is a re-entrant corner where a 1280 mm beam meets
the spine.

**Fix:** `KT_ROOT_JUNCTION = 1.85`, the mean of the two measured ratios. After
recalibration the model predicted 3.912 against a measured 3.844 — **1.8%
error**, and slightly conservative.

**Consequence beyond the number:** before recalibration the frontier was almost
entirely doubler-free; afterwards nearly every competitive design carries one.
The cheap model had been discarding a real structural idea a human had already
found the hard way.

**Lesson:** [[Screened is not validated]]
""", links=["Screened is not validated", "Jetpack Frame Optimization Run",
            "Multi Fidelity Evaluation"])

    v.write("05_Failures/Bugs", "Promotion spent solver time where nothing was in doubt",
            type="failure", status="resolved", confidence="high",
            extra={"severity": "medium", "failure_kind": "architecture"}, body="""
**Symptom:** A promotion round consumed two full 600 s solver timeouts and
returned no information.

**Root cause:** `promote()` sorted by constraint violation, which is 0 for
*every* feasible candidate, then took an arbitrary slice. It picked the two
chunkiest frontier members — screened at SF 14.7 and 22.6, never in doubt —
which were also the most expensive to solve, at 675k and 431k nodes.

**Fix:** `_promotion_order()` ranks by tightest normalised constraint margin
first, then the derived archetypes, then the rest. A solve is worth most where
the screening answer sits closest to the gate.

**Regression test:** `test_promotion_spends_solver_time_where_the_answer_is_in_doubt`

**Lesson:** a tie-break that is always a tie is not an ordering. When every
candidate scores identically on the sort key, the result is arbitrary — and
arbitrary is expensive when each item costs ten minutes.
""", links=["Jetpack Frame Optimization Run",
            "Solver timeout wastes the full budget"])

    v.write("05_Failures/Simulation_Failures", "Solver timeout wastes the full budget",
            type="failure", status="active", confidence="high",
            extra={"severity": "medium", "failure_kind": "simulation"}, body="""
**Symptom:** A solve that exceeds `solve_timeout_s` returns nothing at all,
having consumed the entire budget.

**Status: partially addressed.** The engine's timeout guard correctly refuses
to report a partial result, and `KnowledgeBase.affordable()` can now predict
the cost from history — it correctly calls a 675k-node solve `no` at an
estimated 956 s against a 600 s budget.

**Still open:** `FeaStage` retries a refused *mesh* but not a timed-out
*solve*. It should coarsen within the Jacobian gate, or raise the budget
deliberately, rather than losing the run.

**Honest limit on the prediction:** at n=39 the cost model's band is ×1.5–1.9,
so two of four known cases come back `marginal` rather than a confident call.
Restricting the fit does not tighten it.
""", links=["Engineering Knowledge Base",
            "Promotion spent solver time where nothing was in doubt", "Roadmap"])

    # Kept verbatim as the historical record. The conclusion was wrong, and
    # deleting it would remove exactly the reasoning that shows how.
    v.write("05_Failures/Bugs", "Smart App Control blocks the CAD kernel",
            type="failure", status="superseded", confidence="low",
            extra={"severity": "critical", "failure_kind": "environment",
                   "superseded_by":
                       "[[The CAD kernel blockage was misattributed to Smart App Control]]"},
            body="""
> **This note's conclusion was wrong.** It is kept because the reasoning error
> is more instructive than the fix. See
> [[The CAD kernel blockage was misattributed to Smart App Control]].

**Symptom:** `import cadquery` fails with
`DLL load failed while importing _nlopt: An Application Control policy has
blocked this file.` The entire engine is unusable; the test suite cannot even
be collected.

**Diagnosis at the time (read-only, no attempt to circumvent):**
- Smart App Control is **ON and enforcing** —
  `HKLM:\\SYSTEM\\CurrentControlSet\\Control\\CI\\Policy\\VerifiedAndReputablePolicyState = 1`
- `_nlopt.pyd` is **NotSigned**, and has no Mark-of-the-Web, so `Unblock-File`
  is irrelevant.
- The file is unchanged since the venv was created on 2026-08-23. The suite
  passed 156 tests earlier the same day.

**Import chain:** `cadquery → sketch → sketch_solver → nlopt → _nlopt.pyd`.

**Conclusion drawn (wrong):** that Smart App Control was blocking the DLL, that
this was an OS security posture change, and that the only practical remedy was
to disable Smart App Control — a one-way action requiring a Windows reinstall
to reverse.

**Where the reasoning failed:** every observation above is still true today,
and CadQuery imports fine. An enforcing policy plus an unsigned DLL is a
correlation. It was never tested as a mechanism.
""", links=["The CAD kernel blockage was misattributed to Smart App Control",
            "The knowledge layer is stdlib-only", "Current State"])

    v.write("05_Failures/Bugs",
            "The CAD kernel blockage was misattributed to Smart App Control",
            type="failure", status="resolved", confidence="high",
            extra={"severity": "critical", "failure_kind": "reasoning"}, body="""
**Symptom:** the engine was reported unrunnable and a one-way remedy was
recommended — disabling Windows Smart App Control, which cannot be re-enabled
without reinstalling Windows.

**What is observed now (2026-08-27):**
- `import cadquery` succeeds. CadQuery 2.8.0, 3.7 s.
- `import nlopt` succeeds.
- Full suite: **189 passed**.
- `VerifiedAndReputablePolicyState = 1` — **still enforcing, unchanged.**
- Nothing was altered to achieve this. No security setting was touched.

**Root cause of the misdiagnosis:** a policy state present both when the import
failed and when it succeeds cannot be what distinguishes the two cases. Smart
App Control being enforcing was treated as the mechanism on the strength of it
being plausible and concurrent.

**Root cause of the original import failure:** **Unknown.** A transient file
lock, an in-progress antimalware scan, and a first-run reputation check are all
consistent with the evidence. None was confirmed, and saying which one it was
would be inventing an answer.

**Fix:** none was required. The recommendation to disable Smart App Control is
withdrawn.

**If it recurs:** capture the Code Integrity operational event log at the
moment of failure. Reasoning backwards from policy state afterwards is what
produced the wrong answer the first time.

**Lesson:** before attributing a failure to an environment control — especially
one whose remedy is irreversible — establish the mechanism, or say plainly that
the cause is unknown. [[Refuse rather than invent]]
""", links=["Smart App Control blocks the CAD kernel", "Refuse rather than invent",
            "Current State", "Project Memory"])

    v.write("05_Failures/Engineering_Failures",
            "Analytic section models must match the real section",
            type="failure", status="resolved", confidence="high",
            extra={"severity": "high", "failure_kind": "engineering"}, body="""
**Symptom:** Two ladder channels failed FEA at safety factors of 0.834 and
0.498 against a hand calculation that had passed them.

**Root cause:** the hand calculation used a hollow-rectangle formula for an
**open C-channel**. An open channel's neutral axis is not at mid-height. The
naive formula under-predicted stress by **72%**.

**Fix:** compute the real centroid and second moment. The corrected hand
figure of 323.9 MPa matched FEA's 330.8 MPa within 2%, confirming the solver
was right and the formula was wrong.

**Same failure mode, later, different geometry:** the jetpack's beam model
ignored the T-junction stress riser.
→ [[Screening models are optimistic in the unsafe direction]]

**Lesson:** an analytic model is only as good as its assumed section. When
cheap and expensive disagree, suspect the cheap model's *geometry assumption*
first — that is where both of these failures actually lived.
""", links=["Extension Ladder",
            "Screening models are optimistic in the unsafe direction"])

    v.write("05_Failures/Bugs", "Solver runs cannot be parallelised",
            type="open-question", status="active", confidence="high", body="""
FEA is **97% of wall time** on a real optimisation run and executes strictly
serially. That is the largest single throughput constraint in the system.

**Why it is serial, and it is not laziness:**
- `ActionLog` holds one SQLite connection created in `__init__`. Using it from
  another thread raises.
- `PartStore._next_part_number()` allocates ids by globbing the parts
  directory and taking the max — a textbook race.

`FeaStage` therefore declares `thread_safe = False`, and the evaluator honours
it by falling back to serial for the whole population rather than corrupting
the audit log. That is the correct behaviour given the constraint; the
constraint itself is what should be removed.

**What would fix it:** `check_same_thread=False` plus an explicit lock around
log writes, and a lock (or a monotonic counter) around part-number allocation.
Both are small, additive and testable — but must not be attempted while the
CAD kernel is blocked, because they cannot be verified.

Ranked #3 on [[Roadmap]].
""", links=["Roadmap", "Design Engine", "Optimization Engine"])

    v.write("04_Optimization/Surrogates", "Screening models need automatic calibration",
            type="open-question", status="active", confidence="high", body="""
The correction that took the jetpack screening model from 76–96% error down to
1.8% was applied **by a human reading two FEA results and editing a constant**
in a design script.

That loop is exactly what a surrogate should own: maintain a correction from
observed (screened, measured) pairs, carry its own uncertainty, and refuse to
correct where it has no data.

**Half of it already exists.** `KnowledgeBase.correction()` harvests
calibration pairs automatically from any promoted candidate — any metric that
appears at two different fidelities — computes a geometric-mean factor, and
returns `None` rather than a neutral 1.0 below three observations. It also
flags itself untrustworthy when the ratios disagree by more than 1.5×.

**What is missing:** nothing consumes it yet. The evaluator does not ask the
knowledge base for a correction before screening, and the design scripts still
carry hand-edited constants.

**The hard part is not the fitting, it is the scoping.** A correction learned
on a T-junction beam must not silently be applied to a pressure vessel. The
`problem` field exists for this and is currently set by the caller, which is a
weak guarantee.

Ranked #4 on [[Roadmap]].
""", links=["Roadmap", "Engineering Knowledge Base",
            "Screening models are optimistic in the unsafe direction"])

    # ------------------------------------------------------------ lessons
    v.write("11_Lessons", "Lessons", type="index", body="""
- [[Screened is not validated]]
- [[One seed is an anecdote]]
- [[Fire design data is not service data]]
- [[Refuse rather than invent]]
""")

    v.write("11_Lessons", "Screened is not validated", type="lesson",
            confidence="high", body="""
I reported that the optimiser beat the hand-built frame by 23% on mass at
matched thermal headroom. That comparison used **screened** numbers. The
solver later showed those screening numbers were optimistic by ~1.8×, and the
lighter designs the search preferred **failed their gate**.

The claim had to be retracted and restated on solver evidence. The corrected
answer was still good (29.5% lighter, validated) but it was a different claim.

**The rule:** state the evidence level *in the same breath as the number*,
every time, not as a caveat added later. "3.9 kg" and "3.9 kg, screened, not
solver-validated" are different claims, and only one of them is honest before
the solver has run.

This is why every metric in the system carries a fidelity tag and why the
explainability report prints it beside every value.
""", links=["Screening models are optimistic in the unsafe direction",
            "The engine decides, the optimiser proposes",
            "Jetpack Frame Optimization Run"])

    v.write("11_Lessons", "One seed is an anecdote", type="lesson",
            confidence="high", body="""
On one seed, random search's Pareto frontier dominated the evolutionary
optimiser's 5/5 and looked like a genuine defect in the GA. Across 5 seeds the
result reversed decisively: evolutionary won 5/5 at both budgets, with roughly
2.2× the hypervolume.

Random search is simply **high variance** (HV sd 3.0–3.3 against 0.39–1.90).
A single draw can look excellent.

**The rule:** never report a stochastic comparison from one seed, and build
the multi-seed harness *before* forming an opinion, not after being surprised.
""", links=["Optimizer Benchmark"])

    v.write("11_Lessons", "Fire design data is not service data", type="lesson",
            confidence="high", body="""
Both material derating curves in this project come from Eurocode **fire-design**
tables: EN 1999-1-2 (stated for up to 2 hours exposure) and EN 1993-1-2
Table 3.1 (effective yield at 2% total strain, short duration).

They are real, published, traceable data — and using them to claim
sustained-service capability is not defensible. A jetpack thermally cycles its
structure every flight. Creep, oxidation and thermal-cycling fatigue are all
outside what these tables describe.

This matters most on the optimiser's **steel branch**, which reports 500–742 °C
of headroom. Those numbers are arithmetically correct and engineering-wise
misleading, and they are labelled as such everywhere they appear.

**The rule:** a curve's *provenance* constrains what it can be used for. Record
the exposure conditions alongside the values, and check them before quoting a
number in a new context.
""", links=["6061-T6 Thermal Derating", "Carbon Steel Thermal Derating",
            "Jetpack Frame"])

    v.write("11_Lessons", "Refuse rather than invent", type="lesson",
            confidence="high", body="""
The most consistently valuable design rule in this codebase. Everywhere a
value could be silently guessed, the system refuses instead:

- Derating curves refuse to extrapolate past their measured range.
- Mandatory constraints require a `source` string.
- Point masses require a `source`.
- `correction()` returns `None`, never a neutral 1.0, on thin data.
- `affordable()` returns `marginal` when the band genuinely straddles.
- Unknown spec keys, no-op holes and empty node selections are hard errors.
- A hole that removes no material is refused — it once nearly shipped a
  bracket with a bolt hole that did not exist.

Every one of these was added after silence produced a wrong answer that looked
like a right one. **A refusal is visible; a fabricated default is not.**
""", links=["UNKNOWN is not a pass", "Validation Philosophy",
            "Engineering Knowledge Base"])

    v.write("05_Failures/Design_Failures", "Mesh convergence is unverified",
            type="open-question", status="active", confidence="high", body="""
**The headline result of the jetpack optimisation — `P0047@v1` at SF 3.844 —
was computed at a single mesh size** (3.2 mm, the first that passed the
Jacobian gate) and never checked for convergence.

Discretisation error is therefore unquantified, and every comparison against
the hand-built frame inherits it. The two designs were also solved at
*different* mesh sizes, which makes the 29.5%-lighter comparison weaker than
it looks.

**What would settle it:** solve `P0047@v1` at 3.2, 2.6 and 2.2 mm and check
that the reported safety factor is asymptoting. Expensive — 401k nodes took
381 s, and 2.2 mm would be several times that — but it is the difference
between a number and a measurement.

Ranked #1 on [[Roadmap]].
""", links=["Jetpack Frame Optimization Run", "Roadmap", "Jetpack Frame"])

    # ------------------------------------------------------------ session
    v.write("09_Sessions", "2026-08-25 Design intelligence layer and second brain",
            type="session", status="complete",
            extra={"date": "2026-08-25", "agent": "Claude Code"}, body="""
## Goal
Evolve ClaudeInventor from a deterministic validation toolkit into a design
search system, re-run the jetpack through it, and begin a second brain.

## Starting State
88 tests passing. A strong engineering engine with no search layer at all, and
crucially nothing between "do nothing" and "build geometry in the kernel".

## Changes
- New `design_engine/inventor/` package: requirements, design space,
  candidates, staged cached evaluator, adapters, optimisers, Pareto,
  robustness, sensitivity, failure memory, explainability.
- `thermal_derated_yield` limit state and mass-properties gates in the engine.
- `knowledge.py` — stdlib-only history store, 73 real observations ingested.
- `vault.py` + this vault.
- Viewer fog bug fixed (geometry vanished on zoom-out past 1600 mm).

## Discoveries
- The fidelity ladder spans **six orders of magnitude** (4 µs → 800 s), which
  is what makes staging mandatory rather than merely nice.
- The screening model was optimistic by **76–96%**; after recalibration, 1.8%.
- Recalibration **vindicated the hand-built doubler**.
- Aluminium saturates near 345 °C regardless of section.

## Failures
[[Silent promotion failure]] · [[Load selector picked the wrong face]] ·
[[Promotion spent solver time where nothing was in doubt]] ·
[[Meshing is non-monotonic]] ·
[[The CAD kernel blockage was misattributed to Smart App Control]]

## Decisions
[[UNKNOWN is not a pass]] · [[Numerical artifacts must not steer search]] ·
[[Cache keys include the engineering source digest]] ·
[[The knowledge layer is stdlib-only]]

## Tests
189 passing as of 2026-08-27 (88 pre-existing, unmodified).

## Remaining Work
See [[Roadmap]]. Mesh convergence for the headline SF 3.844 is the top item;
nothing is blocked.

## Important Context
I over-claimed once in this session — reporting a 23% mass win from screened
numbers that the solver later contradicted. The retraction is recorded in
[[Screened is not validated]]. Anyone continuing here should assume screening
numbers are optimistic until a solver says otherwise.
""", links=["Current State", "Jetpack Frame Optimization Run", "Roadmap"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(DEFAULT_ROOT))
    args = ap.parse_args()
    v = Vault(args.root)
    print(f"Bootstrapping vault at {v.root}")
    build(v)
    s = v.stats()
    print(f"\n  notes        : {s['notes']}")
    print(f"  by type      : {dict(sorted(s['by_type'].items()))}")
    print(f"  by status    : {dict(sorted(s['by_status'].items()))}")
    broken = v.broken_links()
    if broken:
        print(f"\n  BROKEN LINKS ({len(broken)}) - a dangling link is a promise "
              f"the vault has not kept:")
        for src, target in broken[:25]:
            print(f"    {src} -> {target}")
    else:
        print("  broken links : none")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
