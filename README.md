# ClaudeInventor

A deterministic engineering validation engine, and a search layer above it.

Geometry is built with CadQuery, meshed with gmsh, solved with CalculiX, and
gated against **named limit states** with sourced material data. Every action
writes to a FRACAS-style log before it runs and finalises to pass or fail, so a
result that is not in the log did not happen.

> **Scope.** This is a structural validator. It reasons about stress, stability,
> vibration and fatigue in parts and weldments. It is not an aircraft design
> tool, it does not model propulsion, fuel systems, fire or flight control, and
> the jetpack frame in `designs/` is a structural study — **not an airworthy
> aircraft**, and never claimed to be.

---

## The idea it is built around

Most of this codebase is refusals. That is deliberate, and it is the result of
being wrong in ways that looked right:

- A safety factor of **3.844** was reported, passed every check, and was
  meaningless — its peak stress sat 1.28 mm from a sharp re-entrant corner
  where linear elasticity has no finite stress at all.
- A **1.044** stress-outlier ratio — the cleanest reading the old heuristic can
  give — was returned for a peak sitting on exactly such a corner.
- A frame validated at SF 4.633 turned out to have **four natural modes inside
  the 20% band** around its own engines' 1633 Hz shaft frequency. Nothing had
  ever computed them.

None of those were arithmetic errors. The mathematics was right every time. So
the engine's value is not accuracy — it is knowing where its answers stop
being meaningful, and saying so.

```
UNKNOWN is not a pass.
```

That single rule, applied everywhere, is most of the architecture.

---

## Architecture

```
inventor/       proposes      search, optimisation, candidate generation
    |
adapters        translates    the ONLY place that knows CadQuery/CalculiX exist
    |
design_engine   decides       geometry, mesh, solver, gates, sign-off, log
```

**The optimiser proposes; the engine decides.** The search layer never rules on
validity. Several architecture decisions exist only to make that structurally
true rather than a convention someone could forget.

### Layers

| | |
|---|---|
| `design_engine/` | geometry, meshing, FEA, limit states, sign-off lock, FRACAS log |
| `design_engine/inventor/` | design space, candidates, staged evaluation, NSGA-II style search, knowledge base |
| `scripts/` | vault + memory generators, the vault-query receipt tool, hooks |
| `designs/` | real design runs — jetpack frame, extension ladder, brackets |
| `docs/` | architecture audit, implementation checklist |

---

## Named limit states

A gate is always a margin against a *named* limit state, never a score or a
pass percentage.

| Limit state | What it checks |
|---|---|
| `yield_von_mises` | peak von Mises against yield |
| `thermal_derated_yield` | …against a temperature-derated yield (EN 1999-1-2 / EN 1993-1-2) |
| `elastic_buckling` | eigenvalue buckling, with a load-scaling self-check |
| `resonance_separation` | natural frequencies clear of an excitation and its harmonics |
| `fatigue_life` | cycles to failure on a sourced S-N curve |

Plus `thrust_cg_alignment` and `thrust_to_weight` as force/moment resultants.

---

## What it refuses, and why

Each of these exists because the alternative is a confident wrong answer.

- **Unsourced material data.** `E`, `nu`, `yield` and every derating curve must
  carry a `source` string. The engine invents no material properties.
- **Extrapolating a curve.** Derating and S-N curves are measured data over a
  stated range. Out of range is an error, not a clamp.
- **A defaulted endurance limit.** Steel has one; **aluminium does not**. It
  must be stated explicitly, including as `None`, because defaulting it decides
  whether a part can last forever.
- **A peak on a geometric singularity.** Reported for static work, and a hard
  refusal for fatigue — life goes as range<sup>−m</sup>, so an unbounded peak
  drives predicted life to zero as the mesh refines. Meaningless, not
  conservative.
- **A fillet or hole that changes nothing.** A feature that silently does
  nothing while the log records success is the worst failure available.
- **An unmet dependency between validators.** A stage whose declared inputs are
  missing returns UNKNOWN naming what it lacked, rather than running on an
  assumed value.
- **A cyclic coupling.** Refused, not ordered — picking an evaluation order for
  a feedback loop would be inventing an answer.
- **Production without a signed-off token**, bound to the spec digest and
  invalidated by any later edit or failed validation. Enforced in code.

---

## Setup

Python **3.12** is required — CadQuery/OCP wheel support is inconsistent above
it. See [`SETUP.md`](SETUP.md) for exact versions and source URLs for the
binary dependencies, which are deliberately not vendored.

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python.exe -m pytest tests/ -q
```

CalculiX 2.23 goes in `tools/`. Use **single-threaded `ccx.exe` only** —
`ccx_MT.exe` was measured returning wrong eigenvalues roughly four times in
five on an identical job, and a validation tool that is sometimes silently
wrong is worse than one that is slow.

### What a fresh clone does not have

`data/` is runtime state and is **not** version controlled: the FRACAS log,
generated geometry and solver runs live there. A clone therefore starts with an
empty log, so the knowledge base has no observations and the solver cost model
rebuilds from zero. That is intended — but back `data/` up separately if the
run history matters to you, because git will not.

---

## The second brain

Two layers, both **stdlib-only on purpose** so they keep working when the CAD
kernel does not:

- **`00_Home/Project Memory.md`** — the chronological half. What changed, why it
  mattered, and what was believed before that turned out wrong. Every evidence
  line carries an epistemic label (`Observed` / `Calculated` / `Inferred` /
  `Hypothesized` / `Unknown`) and the module refuses an unlabelled one.
- **An Obsidian vault** — the graph half. Decisions with their alternatives,
  failures as symptom → root cause → fix → regression test, and lessons.

The notes themselves are not in this repo; the **generators are**, so the whole
vault rebuilds from committed code:

```bash
.venv\Scripts\python.exe scripts\bootstrap_vault.py
.venv\Scripts\python.exe scripts\bootstrap_memory.py
```

Structural guarantees rather than good intentions: `write()` cannot create a
`Note_2.md`, `supersede()` links old to new instead of deleting, `append()`
refuses a duplicate title, and `broken_links()` reports any promise the vault
has not kept.

### The read step is enforced

An instruction to consult prior work was skipped once, and the skipped read
cost hours re-deriving a finding the vault already held — recorded, wrongly, as
a validated decision. Prose instructions are unfalsifiable: nothing
distinguishes "read it" from "said I read it".

So the read now leaves a receipt in the same log as the solver runs:

```bash
.venv\Scripts\python.exe scripts\vault_query.py <topic words>
```

A `PreToolUse` hook blocks edits to `design_engine/**` and `designs/**` without
a logged query in the last 30 minutes. Whether it happened is a query
— `SELECT * FROM actions WHERE action='vault_query'` — not a claim.

---

## Status

323 tests. The engine runs real geometry through real CalculiX.

**Verified against closed-form solutions**, which is what catches the class of
error that returns plausible numbers:

| Benchmark | Agreement |
|---|---|
| Cantilever first natural frequency vs Euler–Bernoulli | **+0.07%** |
| Fixed-pinned Euler buckling | **0.16%** |
| Williams singularity exponent at 270° | recovers **0.4555** by bisection |

**Known gaps, stated rather than buried:**

- **Mesh convergence is unverified.** A 2.8 mm solve crashes CalculiX at a
  6.1 GB working set on the development machine, so no safety factor here
  carries a discretisation error bound.
- **The jetpack frame resonates with its own engines** and is unresolved.
- **32.45 kg of attached mass** — engines, fuel, cradles — exists only as
  constants in the analytic screen and never reaches the solved model.
- **Damping has never been measured**, so the stress amplitude at resonance is
  Unknown and the fatigue machinery cannot yet answer for this frame.
- **Nothing has ever been compared against a physical measurement.**

The last one is the important one. Verification is solving the equations right;
validation is solving the right equations. Everything above is the first.

See [`docs/IMPLEMENTATION_CHECKLIST.md`](docs/IMPLEMENTATION_CHECKLIST.md) for
what is implemented against the roadmap, with commit hashes and line numbers
verified by `git show` rather than asserted.
