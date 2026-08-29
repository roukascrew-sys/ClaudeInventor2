"""Move the screen-error measurements out of prose and into the table.

WHY THIS EXISTS
`KnowledgeBase.correction()` returns `None` for every metric, and the reason
is not that nothing has been measured. This project has measured its own
screening error repeatedly and written it into `observations.reason` as
English:

    "three-point bend vs yield_von_mises, required SF 1.67 (AISC 360 ASD
     Omega_b). Predicted sigma=164.0 MPa, delta=0.8367 mm"

next to the solved `max_von_mises_MPa` of 159.38. The measurement exists and
no code can read it. `calibrations` holds 0 rows.

This is a ONE-OFF migration. It reads what is recoverable, and it is
deliberately conservative about what counts as a sample, because the whole
point of a calibration table is to say how confident the screen is entitled
to be.

    Dry run by default. Pass --apply to write.

THREE JUDGEMENTS, STATED
1. THE FAMILY IS DERIVED, NOT DECLARED. `calibrations.problem` is the scoping
   field that stops a correction learned on a ladder rail being applied to a
   jetpack frame. Nothing in an observation declares it, so it is derived from
   the leading clause of the reason the engineer wrote - "rail vs OSHA...",
   "jetpack frame yield check...". Every inserted row is marked
   `family_derived: true` in its context, because a derived scope is weaker
   than a declared one and a later reader must be able to tell.

2. IDENTICAL PAIRS ARE COLLAPSED. P0024, P0025 and P0026 all carry predicted
   42.1 against measured 62.36 - the same numbers to four figures. Counting
   them as three independent samples would take a two-sample estimate past
   `correction()`'s three-observation threshold and make it look trustworthy.
   They are collapsed to one row carrying `supported_by` so nothing is lost.

3. A PREDICTION WITH NO MEASUREMENT IS NOT A PAIR. Three observations name a
   predicted sigma and have no `max_von_mises_MPa`. They are reported and
   skipped.
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

DEFAULT_DB = Path(__file__).parent.parent / "data" / "knowledge.sqlite"

#: Fidelity levels these pairs sit between. The prediction is closed-form
#: (L0_ANALYTIC); the measurement is a solved FEA (L3_HIGH_FEA). Written as
#: literals because this script must not import the CAD-dependent package.
L0_ANALYTIC, L3_HIGH_FEA = 0, 3

#: (regex, metric name, column holding the measured value)
EXTRACTORS = [
    (re.compile(r"Predicted\s+sigma\s*=\s*([0-9.]+)"),
     "max_von_mises_MPa", "max_von_mises_MPa"),
    (re.compile(r"Predicted\s+Euler\s+SF\s*=\s*([0-9.]+)"),
     "sf.buckling", "safety_factor"),
]


def family_of(reason: str) -> str:
    """The problem family, derived from the engineer's own leading clause.

    Everything up to the first colon or comma, lowercased and trimmed. That is
    where these reasons put the subject: "rail vs OSHA 1926.1053 3.3x proof
    load", "jetpack frame yield check", "base channel vs the OSHA...".

    Returns "unknown" rather than guessing when there is nothing to cut on -
    a wrong scope is worse than an absent one, because it silently authorises
    a correction to travel between unrelated problems.

    Numbers are stripped before comparing. Without that, "jetpack spine
    buckling check under total thrust 1588 N" and the same check at a
    different thrust become two families of one sample each, which is a
    grouping artefact rather than two problems.
    """
    head = re.split(r"[:,]", (reason or "").strip(), maxsplit=1)[0]
    head = re.sub(r"[0-9]+(\.[0-9]+)?", "#", head)      # 4404 -> #, 3.3x -> #x
    head = re.sub(r"\s+", " ", head).strip().lower()
    if not head or len(head) < 4:
        return "unknown"
    return head[:80]


def extract(rows) -> tuple:
    """(pairs, skipped) from observation rows."""
    pairs, skipped = [], []
    for r in rows:
        for pattern, metric, column in EXTRACTORS:
            m = pattern.search(r["reason"] or "")
            if not m:
                continue
            predicted = float(m.group(1))
            measured = r[column]
            if measured in (None, 0):
                skipped.append({"id": r["id"], "geometry_id": r["geometry_id"],
                                "metric": metric, "predicted": predicted,
                                "why": f"no {column} recorded"})
                continue
            pairs.append({"observation_id": r["id"],
                          "geometry_id": r["geometry_id"],
                          "metric": metric, "predicted": predicted,
                          "measured": float(measured),
                          "ratio": predicted / float(measured),
                          "family": family_of(r["reason"])})
    return pairs, skipped


def collapse(pairs) -> list:
    """Fold identical (family, metric, predicted, measured) rows into one.

    Three candidates reporting the same two numbers to four significant
    figures are one measurement recorded three times, not three independent
    samples. Keeping them separate would push a two-sample estimate past the
    three-observation threshold `correction()` uses to decide whether it is
    entitled to answer at all.
    """
    groups = defaultdict(list)
    for p in pairs:
        groups[(p["family"], p["metric"],
                round(p["predicted"], 6), round(p["measured"], 6))].append(p)
    out = []
    for key, members in groups.items():
        first = dict(members[0])
        first["supported_by"] = sorted(m["geometry_id"] for m in members)
        first["duplicates_collapsed"] = len(members) - 1
        out.append(first)
    return sorted(out, key=lambda p: (p["family"], p["metric"], p["predicted"]))


def report(collapsed, min_observations: int = 3) -> dict:
    """What corrections this data would and would not support."""
    per = defaultdict(list)
    for p in collapsed:
        per[(p["family"], p["metric"])].append(p)
    usable, thin = [], []
    for (family, metric), members in sorted(per.items()):
        ratios = [m["ratio"] for m in members]
        spread = max(ratios) / min(ratios) if min(ratios) > 0 else float("inf")
        row = {"family": family, "metric": metric, "n": len(members),
               "ratios": [round(r, 4) for r in ratios],
               "spread": round(spread, 3)}
        (usable if len(members) >= min_observations else thin).append(row)
    return {"usable": usable, "thin": thin}


def apply(conn, collapsed) -> int:
    written = 0
    for p in collapsed:
        try:
            conn.execute(
                "INSERT INTO calibrations (recorded_at, problem, metric,"
                " low_fidelity, high_fidelity, predicted, measured, ratio,"
                " candidate_id, geometry_id, context_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), p["family"], p["metric"], L0_ANALYTIC,
                 L3_HIGH_FEA, p["predicted"], p["measured"], p["ratio"],
                 p["geometry_id"], p["geometry_id"],
                 json.dumps({"backfilled_from": "observations.reason",
                             "backfilled_on": "2026-08-29",
                             "family_derived": True,
                             "observation_id": p["observation_id"],
                             "supported_by": p["supported_by"],
                             "duplicates_collapsed": p["duplicates_collapsed"]})))
            written += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return written


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--apply", action="store_true",
                    help="write the rows (default is a dry run)")
    ap.add_argument("--min-observations", type=int, default=3)
    args = ap.parse_args()

    if not args.db.is_file():
        print(f"no knowledge db at {args.db}")
        return 1
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    existing = conn.execute("SELECT COUNT(*) FROM calibrations").fetchone()[0]
    rows = list(conn.execute("SELECT * FROM observations"))

    pairs, skipped = extract(rows)
    collapsed = collapse(pairs)
    rep = report(collapsed, args.min_observations)

    print(f"observations           : {len(rows)}")
    print(f"calibrations already   : {existing}")
    print(f"pairs recoverable      : {len(pairs)}")
    print(f"after collapsing dupes : {len(collapsed)}")
    if skipped:
        print(f"\npredicted but never measured ({len(skipped)}):")
        for s in skipped:
            print(f"   {s['geometry_id']:<11} {s['metric']:<18} "
                  f"predicted={s['predicted']:<8} {s['why']}")

    print(f"\nper problem family (correction needs n >= {args.min_observations}):")
    for row in rep["usable"]:
        print(f"   USABLE  n={row['n']}  spread={row['spread']}x  "
              f"{row['family']} [{row['metric']}]")
    for row in rep["thin"]:
        print(f"   thin    n={row['n']}  ratios={row['ratios']}  "
              f"{row['family']} [{row['metric']}]")

    if not rep["usable"]:
        print(f"\n  NO family reaches {args.min_observations} distinct pairs, so "
              f"correction() will still return None\n  for every one of them. "
              f"That is the honest state of the evidence, not a bug: this\n  "
              f"project has never solved enough DISTINCT designs within one "
              f"problem family to\n  calibrate its screen. The rows are still "
              f"worth writing - they are the start of\n  that history, and "
              f"they are queryable where prose is not.")

    if args.apply:
        n = apply(conn, collapsed)
        print(f"\nwrote {n} calibration rows")
    else:
        print("\ndry run - pass --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
