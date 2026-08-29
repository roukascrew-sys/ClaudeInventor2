"""Continuous project memory.

Loaded by path rather than imported from the package, for the same reason as
`test_inventor_knowledge.py`: the module is deliberately stdlib-only and must
keep working when the geometry kernel does not. Smart App Control blocked an
unsigned nlopt DLL on this machine and took CadQuery down while the accumulated
history stayed perfectly readable — these tests ran straight through that
outage, and they must keep being able to.

What is under test is not "does it write markdown". It is the set of refusals
that stop a memory document rotting into a transcript nobody trusts:

  - it will not record a fabricated or unlabelled claim
  - it will not record the same event twice
  - it will not erase a belief that changed
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_MOD = Path(__file__).parent.parent / "design_engine" / "memory.py"

_ISOLATION_PROBE = """\
import importlib.util, sys
spec = importlib.util.spec_from_file_location('probe', r'{path}')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print(','.join(n for n in ('cadquery', 'OCP', 'nlopt') if n in sys.modules))
"""


def assert_loads_without_cad_kernel(module_path):
    """Load `module_path` in a clean interpreter and assert the CAD kernel
    never got pulled in.

    A subprocess, not a `sys.modules` check in-process: by the time the full
    suite reaches this file, other tests have already imported cadquery, so an
    in-process assertion silently degrades into "has anyone, anywhere, imported
    cadquery" — which passes alone and fails in the suite while proving nothing
    either way. Only a fresh interpreter actually tests the property.
    """
    proc = subprocess.run([sys.executable, "-c",
                           _ISOLATION_PROBE.format(path=module_path)],
                          capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, f"module failed to load in isolation:\n{proc.stderr}"
    leaked = proc.stdout.strip()
    assert leaked == "", (
        f"{leaked} was imported while loading {Path(module_path).name}; this "
        f"layer must stay usable when the CAD kernel is unavailable")


def _load():
    spec = importlib.util.spec_from_file_location("memory_under_test", _MOD)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


mem = _load()


@pytest.fixture()
def pm(tmp_path):
    return mem.ProjectMemory(tmp_path)


def _event(title="An event", **kw):
    kw.setdefault("type", "Architecture")
    kw.setdefault("impact", "High")
    kw.setdefault("what_happened", "something happened")
    return mem.MemoryEvent(title, **kw)


# ------------------------------------------------------------------ stdlib
def test_module_is_stdlib_only():
    """The reasoning layer must outlive the CAD kernel, by construction."""
    assert_loads_without_cad_kernel(_MOD)


# ------------------------------------------------------ refusing to fabricate
def test_unknown_type_is_refused():
    """A closed vocabulary is what keeps the file knowledge, not a changelog."""
    with pytest.raises(mem.MemoryError_):
        _event(type="Chore")
    with pytest.raises(mem.MemoryError_):
        _event(impact="Catastrophic")


def test_evidence_must_carry_an_epistemic_label():
    """'SF 3.844' and 'SF should be about 3.8' look identical six months later
    unless someone recorded which one was measured."""
    with pytest.raises(mem.MemoryError_) as e:
        _event(evidence=["safety factor is 3.844"])
    assert "epistemic label" in str(e.value)

    ok = _event(evidence=["Observed — FEA SF 3.844 on P0047@v1",
                          "Inferred — screening was optimistic"])
    assert len(ok.evidence) == 2


def test_missing_fields_become_unknown_never_invented(pm):
    """A field with nothing behind it is written as Unknown. It is never
    quietly dropped, and never filled with something plausible."""
    pm.append(_event("Sparse event", what_happened="a thing occurred"))
    body = pm.body_of("Sparse event")
    # every spec field is present ...
    for field in mem.FIELDS:
        assert f"**{field}:**" in body
    # ... and the ones with no content say so honestly
    assert "**Why it matters:** Unknown" in body
    assert "**Open questions:** Unknown" in body
    assert "**Evidence:** Unknown" in body


def test_title_cannot_break_the_heading_format():
    """An em dash in a title would make the file unparseable on next read."""
    with pytest.raises(mem.MemoryError_):
        _event("Bad — title")


# ----------------------------------------------------- refusing to duplicate
def test_duplicate_titles_are_refused_with_the_alternative(pm):
    """Section 6: update existing knowledge before creating new knowledge. A
    duplicate title is the signature of a session that did not check first."""
    pm.append(_event("Solver cost model"))
    with pytest.raises(mem.DuplicateEvent) as e:
        pm.append(_event("Solver cost model", type="Performance"))
    assert "amend()" in str(e.value) and "supersede()" in str(e.value)
    assert len(pm.titles()) == 1


def test_amend_adds_evidence_without_rewriting_history(pm):
    """The original claim survives verbatim; the later finding sits beneath it,
    so a reader sees what was known then versus what turned up after."""
    pm.append(_event("Screening is accurate", what_happened="L0 looked good"))
    pm.amend("Screening is accurate",
             "solver showed screening optimistic by 1.8x", label="Observed")
    body = pm.body_of("Screening is accurate")
    assert "L0 looked good" in body            # untouched
    assert "**Later (" in body
    assert "Observed — solver showed screening optimistic by 1.8x" in body
    assert pm.stats()["amended"] == 1


def test_amend_on_a_missing_event_refuses(pm):
    with pytest.raises(mem.UnknownEvent):
        pm.amend("never recorded", "note")


# -------------------------------------------------- refusing to erase history
def test_supersede_preserves_the_old_belief_and_links_forward(pm):
    """Section 7: previous decision -> new evidence -> superseded -> new
    decision. Deleting the old entry destroys the chain that stops a future
    session re-making the same call."""
    pm.append(_event("ccx_MT is faster", what_happened="used the MT solver"))
    pm.append(_event("ccx_MT produces wrong answers", type="Failure",
                     impact="Critical"))
    pm.supersede("ccx_MT is faster", "ccx_MT produces wrong answers")

    old = pm.body_of("ccx_MT is faster")
    assert "used the MT solver" in old          # the mistake is still readable
    assert "**Superseded (" in old
    assert "ccx_MT produces wrong answers" in old
    assert len(pm.titles()) == 2                # nothing was removed


def test_cannot_supersede_with_an_unrecorded_event(pm):
    """Record the replacement first, so the forward link is never dangling."""
    pm.append(_event("Old belief"))
    with pytest.raises(mem.UnknownEvent):
        pm.supersede("Old belief", "A belief that was never written down")


def test_double_supersede_is_refused(pm):
    pm.append(_event("Old belief"))
    pm.append(_event("New belief"))
    pm.supersede("Old belief", "New belief")
    with pytest.raises(mem.MemoryError_):
        pm.supersede("Old belief", "New belief")


# ------------------------------------------------------------------ ordering
def test_newest_event_is_first(pm):
    """A session with limited context should reach current state before the
    archaeology, and be able to stop reading."""
    pm.append(_event("Oldest", date="2026-08-23"))
    pm.append(_event("Middle", date="2026-08-25"))
    pm.append(_event("Newest", date="2026-08-27"))
    assert pm.titles() == ["Newest", "Middle", "Oldest"]


def test_appending_preserves_every_earlier_event(pm):
    for i in range(6):
        pm.append(_event(f"Event {i}", what_happened=f"body {i}"))
    assert len(pm.titles()) == 6
    for i in range(6):
        assert f"body {i}" in pm.body_of(f"Event {i}")


# -------------------------------------------------------------------- checks
def test_dangling_links_are_surfaced(pm):
    """A memory referencing notes that do not exist cannot be followed."""
    (pm.root / "00_Home").mkdir(parents=True, exist_ok=True)
    (pm.root / "00_Home" / "Current State.md").write_text("x", encoding="utf-8")
    pm.append(_event("Linked event", related=["Current State", "Nonexistent Note"]))
    assert pm.dangling_links() == ["Nonexistent Note"]


def test_stats_counts_what_is_actually_there(pm):
    pm.append(_event("A", type="Failure", impact="Critical"))
    pm.append(_event("B", type="Architecture", impact="High"))
    s = pm.stats()
    assert s["events"] == 2
    assert s["by_type"] == {"Failure": 1, "Architecture": 1}
    assert s["by_impact"] == {"Critical": 1, "High": 1}


def test_frontmatter_created_date_survives_later_appends(pm):
    """The document's own history is history too."""
    pm.append(_event("First"))
    original = pm.path.read_text(encoding="utf-8")
    created = [l for l in original.splitlines() if l.startswith("created:")][0]
    pm.append(_event("Second"))
    after = pm.path.read_text(encoding="utf-8")
    assert created in after
    assert "events: 2" in after


def test_file_is_utf8_regardless_of_console_encoding(pm):
    """Windows consoles are cp1252; the file must not be."""
    pm.append(_event("Unicode event", what_happened="an em dash — and a dot ·"))
    raw = pm.path.read_bytes()
    assert "—".encode("utf-8") in raw
    assert pm.path.read_text(encoding="utf-8").count("—") > 0


# ------------------------------------------ event citations vs note citations
def test_event_citations_render_as_heading_links_into_this_document(pm):
    """A cited EVENT is a heading here, not a note sitting beside it.

    Regression for a field with no defined referent. `related` rendered every
    entry as `[[Title]]`, so an event citation resolved only when a note
    happened to share the event's title — and then resolved to the NOTE.
    Three of the four citations in the real document were in that case and one
    dangled; the difference was a filename collision, not intent.
    """
    pm.append(_event("The earlier event", date="2026-08-01"))
    pm.append(_event("The later event", related_events=["The earlier event"]))
    text = pm.path.read_text(encoding="utf-8")
    assert ("[[Project Memory#2026-08-01 \u2014 The earlier event"
            "|The earlier event]]") in text
    # and NOT the bare form, which would point at a note of that name if one
    # ever appeared, and at nothing until then
    assert "- [[The earlier event]]" not in text


def test_a_heading_link_into_the_memory_does_not_read_as_dangling(pm):
    r"""The link target is `Project Memory`, which exists; the `#` is an anchor.

    Both link checkers stop at `#` (`\[\[([^\]|#]+)`), so this form keeps the
    broken-link gate meaningful instead of silencing it. That matters more
    than it looks: bootstrap_research.py returns 1 while any link dangles, so
    one bad citation kills every `&&` chained after it.
    """
    # the document header itself links [[Current State]]; give it a target so
    # this test measures the citation and nothing else
    (pm.root / "00_Home").mkdir(parents=True, exist_ok=True)
    (pm.root / "00_Home" / "Current State.md").write_text("x", encoding="utf-8")
    pm.append(_event("Cited event", date="2026-08-02"))
    pm.append(_event("Citing event", related_events=["Cited event"]))
    assert pm.dangling_links() == []


def test_citing_an_event_that_does_not_exist_is_refused(pm):
    """Refuse rather than emit a link to nothing — as append() already does
    for a duplicate title."""
    with pytest.raises(mem.UnknownEvent):
        pm.append(_event("Cites a ghost", related_events=["No such event"]))


def test_rendering_event_citations_without_a_resolver_is_refused():
    """A MemoryEvent cannot know another event's date, so it must not guess."""
    ev = _event("Standalone", related_events=["Something"])
    with pytest.raises(mem.MemoryError_):
        ev.render()


def test_note_citations_are_untouched_by_the_split(pm):
    """`related` still means vault notes and still renders bare."""
    pm.append(_event("Note citer", related=["Current State"]))
    assert "- [[Current State]]" in pm.path.read_text(encoding="utf-8")
