"""Obsidian second-brain writer — the reasoning layer.

Two connected but distinct layers, per the vault specification:

    REPOSITORY   executable truth: code, tests, schemas, simulation results
    VAULT        reasoning truth: intent, decisions, failures, lessons

This module writes the vault. It is deliberately stdlib-only, like
`inventor/knowledge.py`, so the durable reasoning stays readable when the CAD
kernel is not importable — which is not hypothetical: Smart App Control
blocked an unsigned nlopt DLL on this machine and took CadQuery down while the
history remained perfectly intact.

Three rules from the spec are enforced structurally rather than trusted:

  UPDATE, DON'T DUPLICATE   `write()` refuses to create `Note_2.md` variants.
                            An existing note is updated in place, preserving
                            its `created` date and bumping `updated`.
  NEVER OVERWRITE HISTORY   `supersede()` marks the old note superseded and
                            links the replacement, rather than deleting it.
  DON'T FABRICATE           `confidence` is optional and never defaulted. A
                            note with no evidence says so.
"""

from __future__ import annotations

import datetime
import re
from pathlib import Path
from typing import Iterable, Sequence

FOLDERS = [
    "00_Home",
    "01_Requirements/System_Requirements",
    "01_Requirements/Design_Requirements",
    "01_Requirements/User_Requirements",
    "02_Designs/Concepts", "02_Designs/Candidates",
    "02_Designs/Approved", "02_Designs/Rejected",
    "03_Engineering/Geometry", "03_Engineering/FEA", "03_Engineering/Dynamics",
    "03_Engineering/Kinematics", "03_Engineering/Thermal",
    "03_Engineering/Materials", "03_Engineering/Mass_Properties",
    "03_Engineering/Manufacturing",
    "04_Optimization/Design_Spaces", "04_Optimization/Variables",
    "04_Optimization/Objectives", "04_Optimization/Constraints",
    "04_Optimization/Experiments", "04_Optimization/Optimization_Runs",
    "04_Optimization/Pareto", "04_Optimization/Surrogates",
    "04_Optimization/Sensitivity",
    "05_Failures/Bugs", "05_Failures/Engineering_Failures",
    "05_Failures/Simulation_Failures", "05_Failures/Design_Failures",
    "06_Architecture/System_Architecture", "06_Architecture/Design_Engine",
    "06_Architecture/Optimization_Engine", "06_Architecture/Data_Models",
    "06_Architecture/Architecture_Decisions",
    "07_Research/Papers", "07_Research/Standards", "07_Research/Methods",
    "07_Research/Technologies", "07_Research/References",
    "08_Code_Memory/Modules", "08_Code_Memory/Interfaces",
    "08_Code_Memory/APIs", "08_Code_Memory/Technical_Debt",
    "09_Sessions", "10_Decisions", "11_Lessons", "_Templates",
]

NOTE_TYPES = {
    "requirement", "design", "candidate", "experiment", "result", "failure",
    "decision", "lesson", "research", "material", "method", "architecture",
    "code-module", "optimization-run", "hypothesis", "open-question",
    "benchmark", "session", "index",
}
STATUSES = {"active", "proposed", "validated", "superseded", "rejected",
            "experimental", "unknown", "resolved", "complete"}
CONFIDENCE = {"high", "medium", "low", "unknown"}

_FM = re.compile(r"^---\n(.*?)\n---\n", re.S)


class VaultError(ValueError):
    pass


def _today() -> str:
    return datetime.date.today().isoformat()


def _yaml(props: dict) -> str:
    """Minimal YAML emitter for flat frontmatter plus a tag list."""
    lines = ["---"]
    for k, v in props.items():
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            lines.append(f"{k}:")
            for item in v:
                lines.append(f"  - {item}")
        elif isinstance(v, bool):
            lines.append(f"{k}: {'true' if v else 'false'}")
        else:
            s = str(v)
            lines.append(f'{k}: "{s}"' if (":" in s or s.startswith("[")) else f"{k}: {s}")
    lines.append("---")
    return "\n".join(lines)


def link(name: str) -> str:
    return f"[[{name}]]"


class Vault:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    # ---------------------------------------------------------------- setup
    def ensure_structure(self) -> int:
        made = 0
        for f in FOLDERS:
            p = self.root / f
            if not p.exists():
                p.mkdir(parents=True, exist_ok=True)
                made += 1
        return made

    def path_for(self, folder: str, title: str) -> Path:
        return self.root / folder / f"{title}.md"

    def find(self, title: str) -> Path | None:
        """Locate a note anywhere in the vault by title."""
        hits = list(self.root.rglob(f"{title}.md"))
        return hits[0] if hits else None

    # ---------------------------------------------------------------- write
    def write(self, folder: str, title: str, body: str, *, type: str,
              status: str = "active", confidence: str | None = None,
              tags: Sequence[str] = (), extra: dict | None = None,
              links: Sequence[str] = ()) -> Path:
        """Create or UPDATE a note. Never creates a numbered duplicate.

        The spec's most important rule: when durable knowledge already exists,
        update the canonical note rather than adding a competing one. So an
        existing note keeps its original `created` date and gets a fresh
        `updated`, and a note of the same title elsewhere in the vault is
        updated where it already lives rather than forked into a new folder.
        """
        if type not in NOTE_TYPES:
            raise VaultError(f"unknown note type {type!r}")
        if status not in STATUSES:
            raise VaultError(f"unknown status {status!r}")
        if confidence is not None and confidence not in CONFIDENCE:
            raise VaultError(f"unknown confidence {confidence!r}")

        existing = self.find(title)
        path = existing if existing is not None else self.path_for(folder, title)
        created = _today()
        if existing is not None:
            m = _FM.match(existing.read_text(encoding="utf-8"))
            if m:
                for line in m.group(1).splitlines():
                    if line.startswith("created:"):
                        created = line.split(":", 1)[1].strip().strip('"')

        props = {"type": type, "status": status, "project": "ClaudeInventor",
                 "created": created, "updated": _today()}
        if confidence:
            props["confidence"] = confidence
        if extra:
            props.update(extra)
        props["tags"] = list(tags) or ["claudeinventor"]

        text = _yaml(props) + "\n\n" + f"# {title}\n\n" + body.strip() + "\n"
        if links:
            text += "\n## Related\n\n" + "\n".join(f"- {link(l)}" for l in links) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def supersede(self, old_title: str, new_title: str) -> Path:
        """Mark a note superseded and point at its replacement.

        History is never erased. An AI reading this vault later needs to see
        not just what the project believes now, but what it used to believe
        and what changed its mind.
        """
        p = self.find(old_title)
        if p is None:
            raise VaultError(f"cannot supersede unknown note {old_title!r}")
        text = p.read_text(encoding="utf-8")
        m = _FM.match(text)
        if not m:
            raise VaultError(f"{old_title!r} has no frontmatter")
        fm = m.group(1)
        fm = re.sub(r"^status:.*$", "status: superseded", fm, flags=re.M)
        if "superseded_by:" not in fm:
            fm += f'\nsuperseded_by: "[[{new_title}]]"'
        fm = re.sub(r"^updated:.*$", f"updated: {_today()}", fm, flags=re.M)
        p.write_text("---\n" + fm + "\n---\n" + text[m.end():], encoding="utf-8")
        return p

    # --------------------------------------------------------------- checks
    def broken_links(self) -> list[tuple[str, str]]:
        """Wikilinks with no target note. A dangling link is a promise the
        vault has not kept, and it is worth surfacing rather than hiding."""
        titles = {p.stem for p in self.root.rglob("*.md")}
        out = []
        for p in self.root.rglob("*.md"):
            for target in re.findall(r"\[\[([^\]|#]+)", p.read_text(encoding="utf-8")):
                t = target.strip()
                if t and t not in titles:
                    out.append((p.stem, t))
        return sorted(set(out))

    def stats(self) -> dict:
        notes = list(self.root.rglob("*.md"))
        by_type: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for p in notes:
            m = _FM.match(p.read_text(encoding="utf-8"))
            if not m:
                continue
            for line in m.group(1).splitlines():
                if line.startswith("type:"):
                    k = line.split(":", 1)[1].strip()
                    by_type[k] = by_type.get(k, 0) + 1
                elif line.startswith("status:"):
                    k = line.split(":", 1)[1].strip()
                    by_status[k] = by_status.get(k, 0) + 1
        return {"notes": len(notes), "by_type": by_type, "by_status": by_status,
                "broken_links": len(self.broken_links())}
