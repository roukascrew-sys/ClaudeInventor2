---
name: design-engine-loop
description: Use this whenever Gideon asks to design, analyze, validate, cost, or produce a physical part or assembly through the design-engine — create or change part geometry, run FEA / stress checks, tolerance stackups, generate a BOM or price a build, produce the condensed report, or release a signed-off part (production package / 3D viewer). Trigger on requests like "design a bracket that holds X", "will this part survive Y load", "what would 10 of these cost", "validate and release this part". This is NOT for editing the design-engine's own source code (that is normal dev work on the repo), and NOT for pure engineering discussion with no artifact — answer those directly.
---

# Design → Validation → Production Loop

Disciplined operation of the design-engine (`C:\Users\rouka\Downloads\design-engine`).
The engine already enforces its hard gates in code; this skill is the process
for driving it so the loop converges instead of thrashing, and so nothing is
ever claimed that the log cannot back.

## Environment invariants

- Interpreter: `C:\Users\rouka\Downloads\design-engine\.venv\Scripts\python.exe`
  (Python 3.12 venv — never the system 3.14).
- Working data root: `design-engine\data` (gitignored, persistent). Demos and
  experiments go to a scratch root, never the real `data\`.
- API surface (all on `DesignEngine(root)`): `create_part`, `edit_part`,
  `get_part`, `create_assembly`, `check_tolerance_stackup`, `run_fea_static`,
  `run_kinematics`, `sign_off`, `generate_bom`, `export_production_package`,
  `generate_viewer`, `generate_assembly_viewer`, `generate_report`, plus
  `eng.log` queries (`failures`, `failure_mode_counts`, `version_history`,
  `lineage`, `pending_actions`).
- Kinematics runs in a SEPARATE conda env (`chrono`), bridged by subprocess.
  Never add pychrono to the pip venv. `chrono_available()` reports status.

## Hard rules (some enforced in code, all enforced in process)

1. **The log is the source of truth.** Reports, images, viewers are generated
   FROM it. Never hand-author a number into any deliverable — if a value is
   not in a log row, it does not appear anywhere.
2. **Never retry blind.** After a validation failure, the next `edit_part`
   MUST pass `addresses_failure_id=<the fail row id>`. The code accepts an
   edit without it; the process does not.
3. **Material data must carry a source** (the code refuses it otherwise).
   If Gideon hasn't given one and no reputable source is at hand, stop and
   ask — do not fill in a plausible number.
4. **Sign-off is Gideon's act, not Claude's.** Present the evidence (SF vs
   limit state, report, residual doubts) and ask him to sign. Never call
   `eng.sign_off(...)` on his behalf, even though the code cannot tell the
   difference. Production functions hard-refuse without a valid signature,
   and also refuse if the spec was changed after signing or a later
   validation failed — do not try to work around a refusal; it is the system
   working.
5. **No invented part numbers or prices.** `generate_bom` only resolves SKUs
   from `design_engine/data/price_book.json`. To add an item: capture a
   public catalog page (browser), record supplier, part number, price tiers,
   `source_url`, `captured_at`. Heed the staleness warning before purchases.

## Scope gate

Run the full process below for anything that creates or changes geometry with
structural implications, or moves a part toward release.

Skip it (direct single calls, cheap tier) for read-only work: log queries,
regenerating the report, `get_part`, re-rendering a viewer for an already
signed version, price-book staleness checks.

---

## The Process

### Step 1 — Read the log, not your memory
`version_history`, `failures()`, `pending_actions()` for the parts involved.
Pending rows mean a previous run was interrupted — surface that to Gideon
before piling new work on top. Prior failure records constrain what to try
next; that is what they are for.

### Step 2 — Predict before you compute
Before any solver run, write down closed-form expectations: sigma = F/A,
sigma = 6FL/bh^2, u = sigma*L/E, worst-case stack = sum of tolerances —
whatever fits. Falsifiable numbers, stated in the `reason` string of the run.
A solver result that disagrees wildly with the hand calculation means one of
them is wrong, and finding out which is the actual work.

### Step 3 — Ripple analysis
Enumerate what else this geometry change touches, two hops out, before
editing:
- assemblies and tolerance chains referencing the part (re-run stackups);
- BOM stock sizing — a bbox change can push the blank past the stocked bar
  cross-section or change which length is cheapest;
- mesh size vs the thinnest wall/radius the change creates (the Jacobian
  gate will reject a coarse mesh on a thin feature — pick
  `max_size_mm` below the thinnest wall up front);
- existing sign-offs — any edit produces a new unsigned version, and
  tampering with a signed spec invalidates its signature by digest;
- stale artifacts (viewer, package, report) that referenced the old version.
If 3+ items are live, present the list to Gideon before writing the edit.

### Step 4 — Execute the design step
`create_part` / `edit_part` with a `reason` that carries the engineering
"why" (and the prediction from Step 2). After a failure, include
`addresses_failure_id` — rule 2.

### Step 5 — Validate against the named limit state
`run_fea_static` with sourced material, a named limit state, and the mesh
size chosen in Step 3. Then check the solver against Step 2:
- median stress vs nominal hand-calc (bulk field correctness);
- displacement vs closed form (stiffness path);
- max stress location — is the peak where the mechanics says it belongs?
If the SF lands within ~20% of `required_SF`, refine the mesh once and re-run:
a gate decision on an unconverged number is not a decision.

### Step 5b — If the part is in a mechanism, compute the load rather than assume it
An assembly with `joints` can be solved by `run_kinematics`, and its peak joint
reaction is returned in newtons — directly usable as a `force_total_N` for
`run_fea_static`. Prefer that over an assumed load, and say in the FEA
`reason` which kinematics log row the number came from.

Joint type is a mechanical decision, never a default: `revolute` carries
bending moment (the force couple between joints vanishes), `spherical` is
force-only (the couple appears). For sizing a part that a joint pulls on,
`spherical` is usually the honest choice — `revolute` hides that pull as an
internal moment. Both are verified against closed form.

### Step 6 — Gate outcome
- **Pass:** regenerate the report; present SF, margins, and any residual
  doubts (do not round doubt away).
- **Fail:** the failure record (mode + magnitude + location) is already in
  the log. Interpret it — thin section? stress raiser? wrong load path? —
  decide the next move, and loop to Step 3 with `addresses_failure_id`.
  This interpretation is the judgment step; do not delegate it to a cheap
  model (see routing below).

### Step 7 — Confidence rubric (before proposing sign-off)
- Solver vs Step 2 predictions agree within tolerance?
- Mesh convergence confirmed if the margin was thin?
- Every Step 3 ripple item actually checked, not just asserted fine?
- Stackups re-run on the final geometry? Edge cases (min/max tolerance,
  load direction reversals) considered?
Any real doubt → back to the step that raised it. State the final assessment
and what kept it from being higher.

### Step 8 — Sign-off and production
Present the evidence and ask Gideon to sign (rule 4). After he signs:
`generate_bom` (budget honesty: over budget runs anyway, labelled
proof-of-concept — never hide the label), `export_production_package`,
`generate_viewer`, final report. All of these self-refuse if the signature
is missing or invalidated.

---

## Model routing (from the build plan)

When spawning subagents (Agent tool `model` parameter), route by the kind of
thinking, not the size of the task:

- **haiku** — boilerplate with no judgment: report regeneration, log queries,
  price-book staleness sweeps, re-rendering artifacts for signed versions.
- **sonnet** — the standard orchestration loop: composing specs, running
  tools, comparing numbers against stated predictions.
- **opus** — engineering judgment: interpreting a failure record, choosing
  the next design move, anything where being wrong wastes a full loop
  iteration or risks a bad release recommendation.

Working inline (no subagent) is fine and usual; the routing applies when work
is actually delegated.

## Notes for editing this skill
- The "~20% of required_SF" convergence trigger and the "3+ ripple items"
  reporting threshold are starting points — tune them as the loop shows how
  often they over/under-trigger.
- If the engine API grows (e.g. Chrono kinematics layer, deferred 2026-08-23),
  add the new tools to the invariants list and give them a Step 5-style
  verification discipline before trusting them.
- Keep this file in sync between the repo copy
  (`design-engine\.claude\skills\design-engine-loop\SKILL.md`, canonical) and
  the install copy (`Downloads\.claude\skills\design-engine-loop\SKILL.md`).
