"""Continuous project memory — the chronological layer of the second brain.

The vault (`vault.py`) is a graph of canonical notes: what the project believes
*now*. This module is the other half — an append-only stream of the events that
changed those beliefs, so a future session can see not just the conclusion but
the sequence of evidence that produced it.

Both write into the same vault. There is deliberately no second memory store:
a competing "final"/"new" memory file is the failure mode this design exists to
prevent, so `ProjectMemory` targets one canonical document and refuses to fork.

Stdlib-only, like `vault.py` and `inventor/knowledge.py`. Durable reasoning has
to stay readable when the CAD kernel does not import — which is not theoretical
on this machine, where Smart App Control took CadQuery down while the entire
history remained intact.

Four spec rules are enforced structurally rather than trusted to discipline:

  NEVER FABRICATE      Every field is required. A field with nothing behind it
                       is written as the literal `Unknown`. It cannot be
                       silently omitted, and it is never filled with a plausible
                       guess. Evidence must additionally carry an epistemic
                       label — Observed / Calculated / Inferred / Hypothesized
                       / Unknown — so a later reader can tell a measurement
                       from an opinion without re-deriving it.
  DON'T DUPLICATE      Appending an event whose title already exists raises.
                       The caller must `amend()` the existing entry or
                       `supersede()` it, which is the spec's "update existing
                       knowledge before creating new knowledge" made mechanical.
  DON'T ERASE HISTORY  `supersede()` marks the old event and links forward. No
                       method in this module deletes or rewrites an event body.
  KNOWLEDGE, NOT LOG   `TYPES` is a closed vocabulary of things that change what
                       the project knows. There is no "chore" or "refactor"
                       category, because those are what the git log is for.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Callable, Sequence

MEMORY_NOTE = "Project Memory"
MEMORY_FOLDER = "00_Home"

#: Closed vocabulary. Deliberately has no bucket for routine work — if an event
#: does not fit one of these, the spec's answer is that it should not be
#: recorded at all.
TYPES = ("Architecture", "Design", "Engineering", "Optimization", "Failure",
         "Performance", "Testing", "Research", "Direction")

IMPACTS = ("Low", "Medium", "High", "Critical")

#: Epistemic status of a claim (spec section 10). The point of forcing a label
#: is that "SF 3.844" and "SF should be about 3.8" look identical six months
#: later unless someone wrote down which one was measured.
EPISTEMIC = ("Observed", "Calculated", "Inferred", "Hypothesized", "Unknown")

UNKNOWN = "Unknown"

#: The narrative fields, in the order the spec lays them out.
FIELDS = ("What happened", "Why it matters", "Decision or lesson", "Evidence",
          "Affected systems", "Consequences", "Open questions")

_HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2}) — (.+?)\s*$", re.M)
_FM = re.compile(r"^---\n(.*?)\n---\n", re.S)


class MemoryError_(ValueError):
    """Base for refusals. Named with a trailing underscore so it cannot be
    confused with the builtin MemoryError, which means something else."""


class DuplicateEvent(MemoryError_):
    pass


class UnknownEvent(MemoryError_):
    pass


def _today() -> str:
    return datetime.date.today().isoformat()


def _labelled(item: str) -> bool:
    return any(item.strip().startswith(lbl) for lbl in EPISTEMIC)


class MemoryEvent:
    """One durable event, validated at construction.

    Validation happens here rather than at write time so a malformed event can
    never reach the file. A half-written entry in a memory document is worse
    than no entry, because it still reads as authoritative.

    **Two kinds of citation, because they are two kinds of thing.**
    `related` names VAULT NOTES and renders `[[Title]]`. `related_events`
    names OTHER EVENTS in this document and renders a heading link into it,
    `[[Project Memory#<date> - <title>|<title>]]`, the same form supersede()
    already uses.

    The split exists because the single field that preceded it had no defined
    referent, and the failure was invisible. An event title that happened to
    match a note title resolved - to the NOTE, whichever the author meant -
    while one that did not simply dangled. Three of the four citations in this
    document were in the first case and one in the second, and the difference
    was luck, not intent: nothing recorded which was meant, so the intent is
    unrecoverable rather than provably wrong. The dangling one also returned a
    non-zero exit from bootstrap_research.py, silently killing any `&&` after
    it. A field whose meaning is decided by a filename collision is not a
    field; it is a coin toss that fails a build script when it lands wrong.
    """

    def __init__(self, title: str, *, type: str, impact: str,
                 what_happened: str = "", why_it_matters: str = "",
                 decision: str = "", evidence: Sequence[str] = (),
                 affected: Sequence[str] = (), consequences: str = "",
                 open_questions: str = "", related: Sequence[str] = (),
                 related_events: Sequence[str] = (),
                 date: str | None = None):
        if type not in TYPES:
            raise MemoryError_(
                f"unknown event type {type!r}; must be one of {TYPES}")
        if impact not in IMPACTS:
            raise MemoryError_(
                f"unknown impact {impact!r}; must be one of {IMPACTS}")
        if not title.strip():
            raise MemoryError_("an event needs a title")
        if "—" in title:
            # The heading separator. Allowing it in a title would make the
            # file unparseable on the next read.
            raise MemoryError_("event titles cannot contain an em dash")

        for item in evidence:
            if not _labelled(item):
                raise MemoryError_(
                    f"evidence {item!r} has no epistemic label; prefix it with "
                    f"one of {EPISTEMIC} so a later reader can tell a "
                    f"measurement from an inference")

        self.title = title.strip()
        self.type = type
        self.impact = impact
        self.date = date or _today()
        self.what_happened = what_happened.strip() or UNKNOWN
        self.why_it_matters = why_it_matters.strip() or UNKNOWN
        self.decision = decision.strip() or UNKNOWN
        self.evidence = list(evidence)
        self.affected = list(affected)
        self.consequences = consequences.strip() or UNKNOWN
        self.open_questions = open_questions.strip() or UNKNOWN
        self.related = list(related)
        self.related_events = list(related_events)

    # ------------------------------------------------------------- rendering
    def render(self, date_of: "Callable[[str], str] | None" = None) -> str:
        """Markdown for one event. `##` headings so Obsidian's outline pane
        indexes each event and `[[Project Memory#<heading>]]` can target it.

        `date_of` resolves a cited event's title to its date, which the heading
        link needs and this object cannot know. ProjectMemory supplies it. An
        event citation therefore REFUSES when the cited event does not exist,
        rather than emitting a link that points at nothing - the same reason
        append() refuses a duplicate title.
        """
        out = [f"## {self.date} — {self.title}", "",
               f"**Type:** {self.type} · **Impact:** {self.impact}", ""]

        def para(label: str, text: str) -> None:
            out.extend([f"**{label}:** {text}", ""])

        def bullets(label: str, items: Sequence[str]) -> None:
            if not items:
                out.extend([f"**{label}:** {UNKNOWN}", ""])
                return
            out.extend([f"**{label}:**", ""])
            out.extend(f"- {i}" for i in items)
            out.append("")

        para("What happened", self.what_happened)
        para("Why it matters", self.why_it_matters)
        para("Decision or lesson", self.decision)
        bullets("Evidence", self.evidence)
        para("Affected systems",
             ", ".join(f"`{a}`" for a in self.affected) if self.affected
             else UNKNOWN)
        para("Consequences", self.consequences)
        para("Open questions", self.open_questions)
        cites = [f"[[{r}]]" for r in self.related]
        if self.related_events and date_of is None:
            raise MemoryError_(
                f"{self.title!r} cites events {self.related_events} but no "
                f"resolver was supplied; render through ProjectMemory so the "
                f"cited event's date can be looked up")
        for r in self.related_events:
            cites.append(f"[[{MEMORY_NOTE}#{date_of(r)} \u2014 {r}|{r}]]")
        bullets("Related", cites)
        return "\n".join(out).rstrip() + "\n"


_HEADER = """\
The chronological half of the second brain: the events that changed what this
project knows, newest first. [[Current State]] says what is true now; this file
says how it came to be true, and what was believed before.

Every entry is validated by `design_engine/memory.py` before it lands here.
Nothing here is deleted — a belief that changed is marked superseded and linked
forward, because a future session needs to see the mistakes to avoid repeating
them.

Evidence carries an epistemic label — **Observed** (measured or logged),
**Calculated**, **Inferred**, **Hypothesized**, or **Unknown** — so a later
reader can separate a solver result from an opinion without re-deriving it.

---
"""


class ProjectMemory:
    """The canonical project-memory document, living inside the vault.

    Points at exactly one note. There is no constructor argument for a second
    location, because "just make another memory file" is precisely the drift
    this class is meant to make impossible.
    """

    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root)
        self.path = self.root / MEMORY_FOLDER / f"{MEMORY_NOTE}.md"

    # ------------------------------------------------------------------ read
    def exists(self) -> bool:
        return self.path.exists()

    def _text(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.exists() else ""

    def titles(self) -> list[str]:
        return [m.group(2) for m in _HEADING.finditer(self._text())]

    def events(self) -> list[dict]:
        """Parsed headings, newest first as stored."""
        return [{"date": m.group(1), "title": m.group(2)}
                for m in _HEADING.finditer(self._text())]

    def body_of(self, title: str) -> str:
        """The raw markdown of one event, heading included."""
        text = self._text()
        for m in _HEADING.finditer(text):
            if m.group(2) == title:
                nxt = _HEADING.search(text, m.end())
                return text[m.start():nxt.start() if nxt else len(text)].rstrip()
        raise UnknownEvent(f"no event titled {title!r}")

    # ----------------------------------------------------------------- write
    def _frontmatter(self, count: int) -> str:
        created = _today()
        if self.exists():
            m = _FM.match(self._text())
            if m:
                for line in m.group(1).splitlines():
                    if line.startswith("created:"):
                        created = line.split(":", 1)[1].strip().strip('"')
        return ("---\n"
                "type: index\n"
                "status: active\n"
                "project: ClaudeInventor\n"
                f"created: {created}\n"
                f"updated: {_today()}\n"
                f"events: {count}\n"
                "tags:\n"
                "  - claudeinventor\n"
                "  - memory\n"
                "---\n")

    def _events_region(self) -> str:
        """The stored events, stripped of frontmatter and preamble."""
        text = self._text()
        if not text:
            return ""
        m = _FM.match(text)
        body = text[m.end():] if m else text
        first = _HEADING.search(body)
        return body[first.start():] if first else ""

    def _write(self, events_region: str) -> Path:
        header = f"# {MEMORY_NOTE}\n\n{_HEADER}\n"
        count = len(_HEADING.findall(events_region))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            self._frontmatter(count) + "\n" + header + events_region,
            encoding="utf-8")
        return self.path

    def append(self, event: MemoryEvent) -> Path:
        """Add an event. Refuses to record the same title twice.

        The refusal is the point. Section 6 of the spec says to update existing
        knowledge before creating new knowledge; a duplicate title is the
        signature of a session that did not check first.
        """
        if event.title in self.titles():
            raise DuplicateEvent(
                f"{event.title!r} is already recorded. Use amend() to add "
                f"evidence to it, or supersede() if the belief has changed.")
        existing = self._events_region()
        # Newest first: a session with limited context should reach the most
        # recent state before the archaeology, and be able to stop reading.
        region = (event.render(self._date_of)
                  + ("\n" + existing.lstrip("\n") if existing else ""))
        return self._write(region)

    def amend(self, title: str, note: str, *, label: str = "Observed") -> Path:
        """Add evidence to an existing event without rewriting its body.

        History accretes. The original claim stays exactly as written, with the
        new finding appended and dated beneath it, so a reader can see what was
        known at the time versus what turned up later.
        """
        if label not in EPISTEMIC:
            raise MemoryError_(f"unknown epistemic label {label!r}")
        body = self.body_of(title)          # raises if absent
        addition = f"\n\n**Later ({_today()}):** {label} — {note.strip()}"
        return self._write(
            self._events_region().replace(body, body + addition, 1))

    def supersede(self, old_title: str, new_title: str) -> Path:
        """Mark a belief as replaced, preserving it verbatim.

        Section 7: previous decision -> new evidence -> superseded -> new
        decision. Deleting the old entry would destroy exactly the chain that
        stops a future session re-making the same call.
        """
        body = self.body_of(old_title)
        if new_title not in self.titles():
            raise UnknownEvent(
                f"cannot supersede with {new_title!r}: no such event. Record "
                f"the replacement first, so the link is never dangling.")
        if "**Superseded" in body:
            raise MemoryError_(f"{old_title!r} is already superseded")
        target = f"{self._date_of(new_title)} — {new_title}"
        mark = (f"\n\n**Superseded ({_today()}):** replaced by "
                f"[[{MEMORY_NOTE}#{target}|{new_title}]].")
        return self._write(
            self._events_region().replace(body, body + mark, 1))

    def _date_of(self, title: str) -> str:
        for e in self.events():
            if e["title"] == title:
                return e["date"]
        raise UnknownEvent(title)

    # ---------------------------------------------------------------- checks
    def dangling_links(self) -> list[str]:
        """`[[Targets]]` in the memory with no note in the vault.

        A memory that references notes which do not exist is a memory that
        cannot be followed, so this is surfaced rather than tolerated.
        """
        notes = {p.stem for p in self.root.rglob("*.md")}
        out = []
        for target in re.findall(r"\[\[([^\]|#]+)", self._text()):
            t = target.strip()
            if t and t not in notes:
                out.append(t)
        return sorted(set(out))

    def stats(self) -> dict:
        text = self._text()
        by_type: dict[str, int] = {}
        by_impact: dict[str, int] = {}
        pat = re.compile(r"\*\*Type:\*\* (\w+) · \*\*Impact:\*\* (\w+)")
        for line in text.splitlines():
            m = pat.match(line)
            if m:
                by_type[m.group(1)] = by_type.get(m.group(1), 0) + 1
                by_impact[m.group(2)] = by_impact.get(m.group(2), 0) + 1
        return {"events": len(self.titles()), "by_type": by_type,
                "by_impact": by_impact,
                "superseded": text.count("**Superseded ("),
                "amended": text.count("**Later ("),
                "dangling_links": len(self.dangling_links())}
