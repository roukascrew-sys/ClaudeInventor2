"""Backfill the continuous project memory from what actually happened.

The vault records what the project believes. This backfills the other half —
the sequence of events that changed those beliefs — from evidence that already
exists: git commits, vault notes, the FRACAS log, and test results.

Nothing here is invented. Every entry names a commit hash, a logged solver run,
a test file, or a benchmark, and every evidence line carries an epistemic label
so a later reader can separate a measurement from an inference. Where the cause
of something is genuinely not established, the entry says `Unknown` rather than
offering a plausible story.

Re-runnable. `ProjectMemory.append` refuses duplicate titles, so a second run
reports what is already recorded instead of forking the document.

    .venv\\Scripts\\python.exe scripts\\bootstrap_memory.py [--root PATH]
"""

import argparse
import importlib.util
from pathlib import Path

# Load by path, not through the package: `design_engine/__init__.py` eagerly
# imports the geometry kernel, and the whole point of this layer is that it
# keeps working when the kernel does not.
_HERE = Path(__file__).parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(
        name, _HERE / "design_engine" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mem = _load("ci_memory", "memory.py")
_vault = _load("ci_vault", "vault.py")
ProjectMemory, MemoryEvent = _mem.ProjectMemory, _mem.MemoryEvent
Vault = _vault.Vault

DEFAULT_ROOT = Path.home() / "Downloads" / "ClaudeInventor"


# Oldest first here; ProjectMemory stores newest-first, so appending in
# chronological order leaves the document reading newest to oldest.
EVENTS = [

    MemoryEvent(
        "Deterministic engineering substrate established",
        date="2026-08-23", type="Architecture", impact="High",
        what_happened=(
            "Phases 0-6 built the engine as a chain of tool functions — geometry "
            "(CadQuery), meshing (gmsh), FEA (CalculiX), reporting, sourcing, and "
            "a human sign-off lock — with a FRACAS-style SQLite action log "
            "underneath. Every action writes a `pending` row before doing its "
            "work and finalises it to pass or fail."),
        why_it_matters=(
            "This is the layer everything later sits on. Reports and images are "
            "generated FROM the log rather than authored alongside it, so a "
            "claim in a report cannot drift away from the run that produced it. "
            "Every subsequent capability — optimisation, knowledge, memory — "
            "reads this log rather than keeping its own parallel truth."),
        decision=(
            "One source of truth, written before the work rather than after it. "
            "A result that is not in the log did not happen."),
        evidence=[
            "Observed — commits f1ec1b2 through e68640c (Phases 0-6)",
            "Observed — 213 rows in `data/design_engine.db`, table `actions`",
            "Observed — sign-off gate enforced in code, not by convention (93a3797)"],
        affected=["design_engine/log.py", "design_engine/geometry.py",
                  "design_engine/fea.py", "design_engine/report.py"],
        consequences=(
            "Later layers were built as readers of this log. The knowledge base "
            "ingests it; the memory document cites it."),
        open_questions="Unknown",
        related=["Design Engine", "System Architecture", "Validation Philosophy"]),

    MemoryEvent(
        "Multithreaded CalculiX produces wrong answers",
        date="2026-08-24", type="Failure", impact="Critical",
        what_happened=(
            "Intermittent, irreproducible FEA results were root-caused to "
            "`ccx_MT.exe`, the multithreaded CalculiX binary. The same deck "
            "solved with single-threaded `ccx.exe` gave stable, correct results."),
        why_it_matters=(
            "This is the worst class of bug in a validation tool: it did not "
            "crash, it returned plausible numbers that were wrong, and it did so "
            "only sometimes. Any safety factor computed through it was "
            "untrustworthy without any signal saying so."),
        decision=(
            "Use single-threaded `ccx.exe` only. Solver parallelism is not worth "
            "having if it can silently corrupt a result — and this is a large "
            "part of why solver runs are now the dominant cost of every search."),
        evidence=[
            "Observed — commit 2a6ce42 root-causes the intermittent corruption",
            "Observed — identical decks give stable results under `ccx.exe`",
            "Inferred — the mechanism inside CalculiX itself was not isolated"],
        affected=["design_engine/fea.py", "tools/CalculiX-2.23.0-win-x64"],
        consequences=(
            "Wall-clock cost per evaluation is bounded by one core. That single "
            "constraint shapes the entire multi-fidelity ladder: screening exists "
            "because the solver cannot be made faster."),
        open_questions=(
            "Whether a newer CalculiX build fixes it is untested. The tradeoff "
            "would need re-validation against known-good results before trusting it."),
        related=["ccx_MT produces wrong answers", "Validation Philosophy",
                 "Solver runs cannot be parallelised"]),

    MemoryEvent(
        "Thermal derating uses fire-design data, and that is a real limitation",
        date="2026-08-25", type="Engineering", impact="High",
        what_happened=(
            "Temperature-dependent yield and modulus derating was added, sourced "
            "from EN 1999-1-2:2007 (aluminium) and EN 1993-1-2:2005 (carbon "
            "steel). `derate_factor` refuses to extrapolate beyond the tabulated "
            "range rather than returning an edge value."),
        why_it_matters=(
            "Both standards are FIRE-design tables. They describe short-duration "
            "elevated-temperature response, not sustained service at temperature. "
            "Using them as service data would silently under-predict creep and "
            "long-term degradation on a part that sits hot for hours."),
        decision=(
            "The numbers are usable and sourced, but the limitation is recorded "
            "with them. A derated allowable from a fire table is not a service "
            "allowable, and any sustained-temperature design needs different data."),
        evidence=[
            "Observed — commit 71a9d33 adds derating with named sources",
            "Observed — `tests/test_thermal_derating.py`, 12 tests, including "
            "refusal to extrapolate and refusal of unsourced curves",
            "Inferred — creep exposure is unquantified; no creep data was consulted"],
        affected=["design_engine/fea.py"],
        consequences=(
            "`thermal_derated_yield` became a named limit state. The jetpack "
            "frame is gated on it."),
        open_questions=(
            "What the correct sustained-service data source is for 6061-T6 above "
            "roughly 100 C. Not yet researched."),
        related=["Fire design data is not service data", "6061-T6 Thermal Derating",
                 "Carbon Steel Thermal Derating"]),

    MemoryEvent(
        "Design-intelligence layer built above the engine, not inside it",
        date="2026-08-25", type="Architecture", impact="Critical",
        what_happened=(
            "A search and optimisation layer (`design_engine/inventor/`) was "
            "added: requirements, design space, staged multi-fidelity evaluator "
            "with content-addressed caching, NSGA-II style constrained search, "
            "Pareto analysis, sensitivity, and explainability. The deterministic "
            "engineering code underneath was not rewritten."),
        why_it_matters=(
            "The boundary is the whole design. The optimiser proposes candidates; "
            "the engine decides whether they are valid. Constraints are hard "
            "feasibility gates, never weighted objectives — so a safety-factor "
            "violation can never be traded against mass, no matter how attractive "
            "the mass is."),
        decision=(
            "Deterministic engineering stays authoritative. Intelligence is a "
            "consumer of it. If search and physics disagree, physics wins."),
        evidence=[
            "Observed — commit d330942 adds the layer; engineering modules "
            "unchanged in that commit",
            "Observed — constrained domination (Deb) implemented in "
            "`inventor/pareto.py`; violation ranks before objective",
            "Observed — `tests/test_inventor_core.py` and "
            "`tests/test_inventor_search.py`"],
        affected=["design_engine/inventor/"],
        consequences=(
            "Cache keys include a digest of the engineering source, so changing "
            "the physics invalidates cached results rather than silently reusing "
            "them."),
        open_questions=(
            "The search operates on a single part. Extending it to assembly or "
            "system level is unexplored."),
        related=["The engine decides, the optimiser proposes",
                 "Constraints are gates, not preferences", "Optimization Engine",
                 "Cache keys include the engineering source digest"]),

    MemoryEvent(
        "A stage that ran and returned UNKNOWN was treated as a pass",
        date="2026-08-25", type="Failure", impact="Critical",
        what_happened=(
            "During promotion, an L3 FEA stage errored while a stale L0 metric "
            "for the same constraint persisted on the candidate. The constraint "
            "was evaluated against the screening number and passed. The run "
            "reported `3 solved in 0.0s ... PASS` with no solver invocation and "
            "no part ever materialised."),
        why_it_matters=(
            "The system reported success for work it had not done. This is the "
            "exact failure a validation tool exists to prevent, and it was "
            "produced by an ordinary-looking data-flow assumption: that a metric "
            "present on a candidate is a metric that was measured at the fidelity "
            "the caller believes."),
        decision=(
            "UNKNOWN is not a pass. A stage that RAN and returned UNKNOWN "
            "degrades the entire result to UNKNOWN — it can never be papered over "
            "by a lower-fidelity value that happens to still be attached. Four "
            "explicit states exist for this reason: VALID / INVALID / UNKNOWN / "
            "NOT_EVALUATED."),
        evidence=[
            "Observed — commit 1b4fa49 fixes it",
            "Observed — the reported run showed 0.0s solve time, which is what "
            "made it detectable at all",
            "Calculated — 56 known-bad designs had been mislabelled UNKNOWN by a "
            "related defect where a definitive stage refusal was downgraded"],
        affected=["design_engine/inventor/candidate.py",
                  "design_engine/inventor/evaluate.py"],
        consequences=(
            "Reports now carry a fidelity-mismatch warning when a constraint's "
            "deciding metric came from a lower rung than the caller requested."),
        open_questions="Unknown",
        related=["Silent promotion failure", "UNKNOWN is not a pass"]),

    MemoryEvent(
        "Screening models are optimistic in the unsafe direction",
        date="2026-08-25", type="Optimization", impact="High",
        what_happened=(
            "The L0 analytic screening model predicted safety factors 76-96% "
            "higher than the converged FEA for the same geometry. The root cause "
            "was a missing stress concentration at the T-junction for designs "
            "without a doubler pad. Adding Kt = 1.85 at the root junction brought "
            "L0 within 1.8% of the solver on the checked cases."),
        why_it_matters=(
            "The error was not random, it was biased toward declaring designs "
            "safe. An entire Pareto frontier was shaped by it. On the strength of "
            "screened numbers a '23% lighter' result was reported, and the solver "
            "later showed screening had been optimistic by roughly 1.8x — the "
            "claim was retracted."),
        decision=(
            "Screened is not validated. A screening number may rank candidates; "
            "it may never be reported as a result. Nothing leaves the system as a "
            "finding until a converged solver run has confirmed it."),
        evidence=[
            "Observed — commit 372e7a7 recalibrates L0 against two new FEA points",
            "Observed — post-calibration L0 error 1.8% on the checked cases",
            "Calculated — the 1.85 factor was fitted to two solver results, which "
            "is thin; it is a correction, not a validated Kt"],
        affected=["design_engine/inventor/adapters.py"],
        consequences=(
            "Calibration pairs are now harvested automatically by the knowledge "
            "base whenever a candidate is promoted across fidelities."),
        open_questions=(
            "Whether Kt = 1.85 generalises beyond this joint geometry is "
            "untested. Two points is not a model."),
        related=["Screened is not validated",
                 "Screening models are optimistic in the unsafe direction",
                 "Screening models need automatic calibration",
                 "Multi Fidelity Evaluation"]),

    MemoryEvent(
        "Evolutionary search beats random, but only measurably across seeds",
        date="2026-08-25", type="Optimization", impact="High",
        what_happened=(
            "A single-seed comparison suggested random search was outperforming "
            "the evolutionary optimiser. Re-running across five seeds reversed "
            "the conclusion: evolutionary at a 192-evaluation budget beat random "
            "at 480 evaluations, hypervolume 14.87 against 9.97, winning 5 of 5 "
            "seeds."),
        why_it_matters=(
            "The single-seed result was not noise around a true value, it pointed "
            "the opposite way. Acting on it would have removed the better "
            "optimiser and more than doubled the solver budget needed for a worse "
            "frontier."),
        decision=(
            "One seed is an anecdote. No stochastic optimiser comparison is "
            "reportable from a single run; report seed count and spread or report "
            "nothing."),
        evidence=[
            "Observed — commit 76f5aa4 adds the multi-seed benchmark",
            "Observed — hypervolume 14.87 (evolutionary, 192 evals) against 9.97 "
            "(random, 480 evals), 5/5 seeds",
            "Observed — `designs/benchmark_optimizers.py`"],
        affected=["design_engine/inventor/optimizers.py"],
        consequences=(
            "Evolutionary search was kept as the default. Benchmarks in this "
            "project now report across seeds by default."),
        open_questions=(
            "Five seeds is enough to establish direction here but not to "
            "characterise variance. No confidence interval was computed."),
        related=["One seed is an anecdote", "Optimizer Benchmark"]),

    MemoryEvent(
        "The load selector silently matched the wrong face",
        date="2026-08-25", type="Failure", impact="High",
        what_happened=(
            "A load was applied using selector `{axis: z, at: min}`, which "
            "resolved to the spine base rather than the intended crossbeam "
            "underside, matching zero nodes. The bounded mesh ladder had been "
            "retrying and masking the symptom, so the failure surfaced as slow "
            "meshing rather than as a wrong boundary condition."),
        why_it_matters=(
            "A robustness mechanism hid a correctness bug. The mesh ladder was "
            "doing its job — recovering from mesh refusals — and in doing so it "
            "converted a loud, specific failure into a vague, slow one."),
        decision=(
            "Recovery mechanisms must not swallow the diagnostic. A selector "
            "matching zero nodes is a definitive error and must fail loudly "
            "before any retry logic engages."),
        evidence=[
            "Observed — commit 7903ebc loads the crossbeam underside at "
            "`SPINE_Z/2 - cb_height/2`",
            "Observed — the failing selector matched 0 nodes"],
        affected=["design_engine/mesh.py", "design_engine/fea.py"],
        consequences=(
            "Zero-match selectors are treated as definitive failures rather than "
            "as candidates for retry."),
        open_questions="Unknown",
        related=["Load selector picked the wrong face", "Meshing is non-monotonic"]),

    MemoryEvent(
        "Second brain built as two layers, deliberately stdlib-only",
        date="2026-08-26", type="Architecture", impact="High",
        what_happened=(
            "Two connected stores were added: a numeric knowledge base "
            "(`inventor/knowledge.py`) that ingested 73 real solver observations "
            "from the FRACAS log and learned a solver cost model, and an Obsidian "
            "vault (`vault.py`) holding 46 reasoning notes. Both were written "
            "with no dependency on the CAD kernel."),
        why_it_matters=(
            "History is a database problem, not a CAD problem. The two layers "
            "answer different questions — the knowledge base answers 'how much "
            "will this cost and has it failed before', the vault answers 'why "
            "does the system work this way' — and neither should stop working "
            "because a geometry dependency broke."),
        decision=(
            "The reasoning layer never imports the engineering kernel. It is "
            "enforced by test, not by convention."),
        evidence=[
            "Observed — commit 9f8943e",
            "Observed — 73 observations ingested; solver cost model "
            "t = 40.1s * (nodes/100k)^1.661 fitted from n=39 runs",
            "Observed — 46 vault notes, zero broken links",
            "Calculated — `correction()` returns None rather than 1.0 on thin "
            "data, so a coincidence is never dressed up as a calibration"],
        affected=["design_engine/inventor/knowledge.py", "design_engine/vault.py"],
        consequences=(
            "`broken_links()` immediately caught two dangling promises in the "
            "project's own roadmap, which were then written properly."),
        open_questions=(
            "The cost model's +-1.5-1.9x band is too wide to gate on directly, "
            "which is why `affordable()` returns a three-way verdict including "
            "'marginal' rather than a yes/no."),
        related=["The knowledge layer is stdlib-only", "Engineering Knowledge Base",
                 "Refuse rather than invent"]),
]


# Recorded separately because it supersedes an earlier belief, and the
# replacement has to exist before the link can be made.
SAC_OLD = MemoryEvent(
    "Smart App Control blocks the CAD kernel",
    date="2026-08-26", type="Failure", impact="Critical",
    what_happened=(
        "`import cadquery` began failing through the chain "
        "`sketch_solver -> nlopt -> _nlopt.pyd`. Windows Smart App Control was "
        "found to be enforcing (`VerifiedAndReputablePolicyState = 1`) and "
        "`_nlopt.pyd` is unsigned. The failure was attributed to Smart App "
        "Control blocking the unsigned DLL, and the engine was reported as "
        "blocked pending a decision to disable Smart App Control."),
    why_it_matters=(
        "The attribution drove a recommendation with a one-way consequence: "
        "turning Smart App Control off cannot be undone without reinstalling "
        "Windows."),
    decision=(
        "Reported as an environment blocker requiring a user decision. No "
        "attempt was made to circumvent the security control."),
    evidence=[
        "Observed — `import cadquery` failed at `_nlopt.pyd`",
        "Observed — `VerifiedAndReputablePolicyState = 1` (enforcing)",
        "Observed — `_nlopt.pyd` is unsigned, unchanged since 2026-08-23, and "
        "carried no Mark-of-the-Web",
        "Inferred — that Smart App Control was the cause. This inference was "
        "the weak link and it later proved wrong."],
    affected=["design_engine/geometry.py", ".venv"],
    consequences="The engine and the full test suite were reported unrunnable.",
    open_questions="Whether Smart App Control was actually the cause.",
    related=["Smart App Control blocks the CAD kernel"])


SAC_NEW = MemoryEvent(
    "The CAD kernel blockage was misattributed to Smart App Control",
    date="2026-08-27", type="Failure", impact="Critical",
    what_happened=(
        "`import cadquery` now succeeds in 3.7s and `import nlopt` succeeds, "
        "while Smart App Control is still enforcing with "
        "`VerifiedAndReputablePolicyState = 1` — the same value observed when "
        "the failure was diagnosed. The full suite runs: 189 passed. Nothing "
        "was changed to achieve this; no security setting was altered."),
    why_it_matters=(
        "The earlier causal claim was wrong, and it was wrong in a costly "
        "direction: it recommended an irreversible action (disabling Smart App "
        "Control, which cannot be re-enabled without reinstalling Windows) to "
        "fix something Smart App Control was not causing. Correlation between "
        "an enforcing security policy and an unsigned DLL was treated as "
        "mechanism without testing it."),
    decision=(
        "A policy state that is present both when a thing fails and when it "
        "succeeds is not the cause of the failure. Before attributing a failure "
        "to an environment control — especially one whose remedy is "
        "irreversible — establish the mechanism, or state plainly that the "
        "cause is unknown."),
    evidence=[
        "Observed — `import cadquery` succeeds, CadQuery 2.8.0, 3.7s, 2026-08-27",
        "Observed — `VerifiedAndReputablePolicyState = 1`, unchanged",
        "Observed — full suite 189 passed",
        "Unknown — the actual mechanism of the original failure. A transient "
        "lock, an in-progress scan, or a first-run reputation check are all "
        "consistent with the evidence and none was confirmed."],
    affected=["design_engine/geometry.py", ".venv"],
    consequences=(
        "The engine is runnable. `Current State` is corrected. The recommendation "
        "to disable Smart App Control is withdrawn."),
    open_questions=(
        "Whether the failure recurs. If it does, capture the Code Integrity "
        "event log at the moment of failure rather than reasoning from policy "
        "state afterwards."),
    related=["Current State", "Refuse rather than invent"])


LATE = [
    MemoryEvent(
        "An in-process stdlib-only assertion proved nothing",
        date="2026-08-27", type="Testing", impact="Medium",
        what_happened=(
            "The tests asserting that the knowledge and memory layers never "
            "import the CAD kernel checked `sys.modules` inside the running "
            "test process. Run alone they passed; run in the full suite they "
            "failed, because by then other test files had already imported "
            "cadquery into the same interpreter. Both outcomes were meaningless "
            "— the assertion measured global process state, not the module."),
        why_it_matters=(
            "The test guarded an architectural invariant that the whole "
            "reasoning layer depends on, and it had been reported as passing. A "
            "green test that measures the wrong thing is worse than no test, "
            "because it stops anyone looking."),
        decision=(
            "An isolation property has to be tested in isolation. The check now "
            "loads the module in a clean subprocess and inspects that "
            "interpreter's `sys.modules`, which is order-independent and "
            "actually tests the claim."),
        evidence=[
            "Observed — 2 failed, 187 passed with the in-process assertion",
            "Observed — 189 passed after moving the check into a subprocess, "
            "both standalone and in full-suite order"],
        affected=["tests/test_memory.py", "tests/test_inventor_knowledge.py"],
        consequences=(
            "Both stdlib-only layers now have a check that cannot pass by "
            "accident of test ordering."),
        open_questions=(
            "Other tests in the suite may carry the same class of global-state "
            "assumption. Not audited."),
        related=["The knowledge layer is stdlib-only"]),

    MemoryEvent(
        "Continuous project memory added as the chronological half of the vault",
        date="2026-08-27", type="Architecture", impact="Medium",
        what_happened=(
            "`design_engine/memory.py` was added: a validated, append-only event "
            "stream written into the existing vault at `00_Home/Project Memory`. "
            "It enforces a closed event vocabulary, requires every field, "
            "requires an epistemic label on every evidence line, refuses "
            "duplicate titles, and supersedes rather than deletes."),
        why_it_matters=(
            "The vault answers 'what does this project believe'. It could not "
            "answer 'how did it come to believe that, and what did it believe "
            "before'. Superseded reasoning is what stops a future session "
            "re-making a decision that has already been tried and rejected."),
        decision=(
            "One memory store, inside the existing vault. A second parallel "
            "memory document is the drift this design exists to prevent, so the "
            "class targets exactly one file and has no parameter for another."),
        evidence=[
            "Observed — `tests/test_memory.py`, 17 tests covering the refusals",
            "Observed — the module loads with no CAD kernel present, verified in "
            "a clean subprocess"],
        affected=["design_engine/memory.py", "scripts/bootstrap_memory.py"],
        consequences=(
            "Maintenance rules were added to `CLAUDE.md` so the memory is "
            "updated during work rather than only when asked."),
        open_questions=(
            "Whether newest-first ordering stays readable as the document grows. "
            "If it does not, the fix is a derived index, not a second file."),
        related=["Project Memory", "Home", "The knowledge layer is stdlib-only"]),
]


def build(root: Path) -> None:
    pm = ProjectMemory(root)
    added = skipped = 0
    for ev in EVENTS + [SAC_OLD, SAC_NEW] + LATE:
        try:
            pm.append(ev)
            added += 1
            print(f"  + {ev.date}  {ev.title}")
        except _mem.DuplicateEvent:
            skipped += 1
            print(f"  = {ev.date}  {ev.title}  (already recorded)")

    # The correction chain: the old belief stays readable, marked and linked
    # forward. Deleting it would destroy exactly the reasoning a future session
    # needs in order not to repeat the mistake.
    try:
        pm.supersede(SAC_OLD.title, SAC_NEW.title)
        print(f"  ~ superseded: {SAC_OLD.title!r} -> {SAC_NEW.title!r}")
    except _mem.MemoryError_ as e:
        print(f"  = supersede skipped: {e}")

    print(f"\n  {added} added, {skipped} already present")
    return pm


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = ap.parse_args()

    print(f"Project memory -> {args.root}\n")
    pm = build(args.root)

    stats = pm.stats()
    print("\n  stats:", {k: v for k, v in stats.items() if k != "by_type"})
    print("  by type:", stats["by_type"])

    dangling = pm.dangling_links()
    if dangling:
        print("\n  DANGLING LINKS (targets with no note in the vault):")
        for d in dangling:
            print(f"    - {d}")
        print("  These are promises the vault has not kept yet.")
    else:
        print("\n  no dangling links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
