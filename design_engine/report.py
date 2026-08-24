"""Condensed report generator (Phase 3).

Renders one self-contained HTML file from the FRACAS log — and ONLY from the
log. No store files, no solver output, no independently-authored content: if a
number is not in a log row, it is not in the report. Static images (Phase 4
diagnostics) are embedded base64 from paths recorded in details_json
["artifacts"]; a recorded-but-missing artifact renders as a visible MISSING
marker, never silently skipped.

Report generation is itself a logged action (phase='report'); the snapshot is
taken before its own row is finalized, so each report's row shows up in the
next report, not its own.
"""

from __future__ import annotations

import base64
import datetime
import html
import json
import re
from pathlib import Path

from .log import ActionLog

_GID_RE = re.compile(r"^(P\d{4})@v(\d+)$")

_CSS = """
body { font-family: 'Segoe UI', system-ui, sans-serif; margin: 0; padding: 24px;
       background: #f4f5f7; color: #1a1d21; }
h1 { font-size: 22px; margin: 0 0 4px; }
h2 { font-size: 16px; margin: 28px 0 8px; border-bottom: 2px solid #d0d4da;
     padding-bottom: 4px; }
.meta { color: #5a626e; font-size: 12px; }
table { border-collapse: collapse; width: 100%; background: #fff; font-size: 13px; }
th, td { border: 1px solid #d0d4da; padding: 5px 8px; text-align: left;
         vertical-align: top; }
th { background: #e8eaee; font-weight: 600; }
code, .mono { font-family: Consolas, monospace; font-size: 12px; }
.badge { display: inline-block; padding: 1px 8px; border-radius: 3px;
         font-size: 11px; font-weight: 700; color: #fff; }
.badge.pass { background: #1e7e34; }
.badge.fail { background: #b02a37; }
.badge.pending { background: #b8860b; }
.warn { background: #fff3cd; border: 1px solid #b8860b; padding: 8px 12px;
        border-radius: 4px; }
.missing { background: #f8d7da; border: 1px solid #b02a37; padding: 8px 12px;
           border-radius: 4px; font-weight: 600; }
.empty { color: #5a626e; font-style: italic; }
figure { margin: 12px 0; background: #fff; border: 1px solid #d0d4da;
         padding: 8px; display: inline-block; }
figcaption { font-size: 12px; color: #5a626e; margin-top: 4px; }
img { max-width: 640px; display: block; }
"""


def _esc(val) -> str:
    return html.escape(str(val))


def _badge(result: str) -> str:
    return f'<span class="badge {_esc(result)}">{_esc(result).upper()}</span>'


def _details(row) -> dict:
    return json.loads(row["details_json"]) if row["details_json"] else {}


def _table(headers: list[str], rows: list[list[str]], empty_note: str) -> str:
    if not rows:
        return f'<p class="empty">{_esc(empty_note)}</p>'
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _fmt_props(props: dict) -> str:
    if not props:
        return "—"
    bits = [f"V = {props.get('volume_mm3', '?')} mm³"]
    size = (props.get("bbox_mm") or {}).get("size")
    if size:
        bits.append(f"bbox {size[0]} × {size[1]} × {size[2]} mm")
    if "mass_kg_estimate" in props:
        bits.append(f"mass ≈ {props['mass_kg_estimate']} kg (est., from spec density)")
    return _esc(" · ".join(str(b) for b in bits))


def _fmt_diff(diff: list[dict]) -> str:
    if not diff:
        return "—"
    lines = [
        f"{d.get('path')}: {json.dumps(d.get('old'))} → {json.dumps(d.get('new'))}"
        for d in diff]
    return '<span class="mono">' + "<br>".join(_esc(x) for x in lines) + "</span>"


def _scalars(details: dict) -> str:
    """Compact k=v rendering of scalar detail values (numbers/strings/bools)."""
    skip = {"artifacts", "properties", "diff", "property_delta", "chains"}
    bits = [f"{k}={v}" for k, v in sorted(details.items())
            if k not in skip and isinstance(v, (int, float, str, bool))]
    return _esc(" · ".join(bits)) if bits else "—"


def _embed_images(rows, data_root: Path) -> str:
    figures = []
    for row in rows:
        for art in _details(row).get("artifacts", []):
            path = Path(art)
            if not path.is_absolute():
                path = data_root / path
            caption = (f"log #{row['id']} · {_esc(row['action'])} · "
                       f"{_esc(row['geometry_version'] or '')}")
            if path.is_file():
                b64 = base64.b64encode(path.read_bytes()).decode()
                figures.append(
                    f'<figure><img src="data:image/png;base64,{b64}" '
                    f'alt="{caption}"><figcaption>{caption} · '
                    f'{_esc(path.name)}</figcaption></figure>')
            else:
                figures.append(
                    f'<div class="missing">MISSING ARTIFACT: {_esc(str(path))} '
                    f'(recorded by log #{row["id"]}, file not found)</div>')
    return "".join(figures) if figures else \
        '<p class="empty">No diagnostic images recorded yet.</p>'


def generate_report(log: ActionLog, out_path: str | Path,
                    data_root: str | Path | None = None) -> Path:
    out_path = Path(out_path)
    data_root = Path(data_root) if data_root else log.db_path.parent
    action_id = log.open_action("report", "generate_report")
    try:
        rows = [r for r in log.rows() if r["id"] != action_id]
        parts_seen = sorted({
            m.group(1) for r in rows if r["geometry_version"]
            for m in [_GID_RE.match(r["geometry_version"])] if m})

        # --- current parts state (latest passed version per part) ---
        part_rows = []
        for pn in parts_seen:
            hist = log.version_history(pn)
            if not hist:
                continue
            latest = hist[-1]
            det = _details(latest)
            part_rows.append([
                f'<span class="mono">{_esc(latest["geometry_version"])}</span>',
                _fmt_props(det.get("properties", {})),
                _esc(det.get("properties", {}).get("spec_digest", "—")),
                _esc(latest["reason"] or "—"),
                _esc(latest["timestamp"]),
            ])

        # --- assemblies: latest stackup per assembly id ---
        stack_rows = []
        checked = set()
        for row in reversed(rows):
            if row["action"] != "check_tolerance_stackup" or row["result"] == "pending":
                continue
            aid = row["geometry_version"]
            if aid in checked:
                continue
            checked.add(aid)
            det = _details(row)
            for chain in det.get("chains", []):
                wc, req = chain["worst_case_mm"], chain["requirement_mm"]
                stack_rows.append([
                    f'<span class="mono">{_esc(aid)}</span>',
                    _esc(chain["name"]),
                    _esc(chain["nominal_mm"]),
                    _esc(f"[{wc['min']}, {wc['max']}]"),
                    _esc(f"[{chain['rss_mm']['min']}, {chain['rss_mm']['max']}]"),
                    _esc(json.dumps(req)),
                    _esc(chain["worst_margin_mm"]),
                    _badge(chain["result"]),
                ])
        stack_rows.reverse()

        # --- validation runs ---
        val_rows = [
            [_esc(r["id"]), _esc(r["timestamp"]), _esc(r["action"]),
             f'<span class="mono">{_esc(r["geometry_version"] or "—")}</span>',
             _scalars(_details(r)), _badge(r["result"]),
             _esc(r["failure_mode"] or "—")]
            for r in rows if r["phase"] == "validation"]

        # --- failures (FRACAS) ---
        fail_rows = [
            [_esc(r["id"]), _esc(r["timestamp"]), _esc(r["action"]),
             f'<span class="mono">{_esc(r["geometry_version"] or "—")}</span>',
             _esc(r["failure_mode"] or "—"), _esc(r["reason"] or "—")]
            for r in rows if r["result"] == "fail"]
        mode_rows = [[_esc(m), _esc(n)] for m, n in log.failure_mode_counts()]

        # --- change history (exact diffs) ---
        change_rows = []
        for r in rows:
            if r["action"] not in ("create_part", "edit_part") or r["result"] != "pass":
                continue
            det = _details(r)
            diff_html = _fmt_diff(det.get("diff", []))
            if det.get("addresses_failure_id") is not None:
                diff_html += (f'<br><span class="mono">addresses failure '
                              f'#{_esc(det["addresses_failure_id"])}</span>')
            change_rows.append([
                _esc(r["id"]), _esc(r["timestamp"]), _esc(r["action"]),
                f'<span class="mono">{_esc(r["geometry_version"])}</span>',
                diff_html, _esc(r["reason"] or "—")])

        # --- sourcing: latest BOM per geometry version ---
        bom_rows, bom_notes, seen_bom = [], [], set()
        for row in reversed(rows):
            if row["action"] != "generate_bom" or row["result"] != "pass":
                continue
            gid = row["geometry_version"]
            if gid in seen_bom:
                continue
            seen_bom.add(gid)
            det = _details(row)
            for ln in det.get("lines", []):
                qty = f'{ln["qty_required"]} req'
                if ln.get("overbuy"):
                    qty += f' / {ln["qty_purchased"]} bought'
                elif ln["kind"] == "stock":
                    qty += f' / {ln.get("bars")} bar(s)'
                bom_rows.append([
                    f'<span class="mono">{_esc(gid)}</span>',
                    _esc(ln["ref"]),
                    f'<a href="{_esc(ln["source_url"])}">'
                    f'{_esc(ln["supplier"])} #{_esc(ln["part_number"])}</a>',
                    _esc(ln["description"]), qty,
                    _esc(f'{ln["total_usd"]:.2f}'),
                ])
            budget = det.get("budget")
            note = (f'<p><b>{_esc(gid)}</b> — {det["assemblies"]} assemblies · '
                    f'as-specified <b>${det["as_specified_total_usd"]:.2f}</b> · '
                    f'min-cost baseline <b>${det["min_cost_total_usd"]:.2f}</b> '
                    f'(saving ${det["min_cost_saving_usd"]:.2f})')
            if budget:
                note += (f' · budget ${budget["budget_usd"]:.2f} → '
                         f'<b>{_esc(budget["label"])}</b>')
            note += "</p>"
            for sub in det.get("substitutions", []):
                note += (f'<div class="warn">Min-cost substitution '
                         f'<span class="mono">{_esc(sub["from_sku"])} → '
                         f'{_esc(sub["sku"])}</span> saves '
                         f'${sub["saving_usd"]:.2f} — NOT applied to the '
                         f'as-specified BOM. Caveat: {_esc(sub["caveat"])}</div>')
            pricing = det.get("pricing", {})
            note += (f'<p class="meta">Prices as of {_esc(pricing.get("price_as_of"))} '
                     f'({_esc(pricing.get("age_days"))} days old) · '
                     f'{_esc(pricing.get("basis"))}</p>')
            if pricing.get("staleness_warning"):
                note += (f'<div class="missing">STALE PRICING: '
                         f'{_esc(pricing["staleness_warning"])}</div>')
            bom_notes.append(note)
        bom_notes.reverse()
        bom_rows.reverse()

        pending = [r for r in log.pending_actions() if r["id"] != action_id]
        pending_note = "" if not pending else (
            f'<div class="warn">⚠ {len(pending)} action(s) were never '
            f'finalized — evidence of an interrupted run. Row ids: '
            f'{_esc([r["id"] for r in pending])}</div>')

        signed = [[_esc(r["id"]), _esc(r["action"]),
                   _esc(r["geometry_version"] or "—"), _esc(r["signed_off_by"])]
                  for r in rows if r["signed_off_by"]]

        n_pass = sum(1 for r in rows if r["result"] == "pass")
        n_fail = sum(1 for r in rows if r["result"] == "fail")
        generated = datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds")

        html_doc = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Design Engine Report</title><style>{_CSS}</style></head><body>
<h1>Design Engine — Condensed Report</h1>
<p class="meta">Generated {_esc(generated)} · source of truth:
<span class="mono">{_esc(str(log.db_path))}</span> · {len(rows)} logged actions
({n_pass} pass / {n_fail} fail) · every figure below is generated from the log,
never authored independently.</p>
{pending_note}
<h2>Parts — current state</h2>
{_table(["Version", "Properties", "Spec digest", "Last change reason", "When (UTC)"],
        part_rows, "No parts created yet.")}
<h2>Tolerance stackups — worst-case is the gate, RSS informational</h2>
{_table(["Assembly", "Chain", "Nominal (mm)", "Worst-case (mm)", "RSS (mm)",
         "Requirement (mm)", "Worst margin (mm)", "Result"],
        stack_rows, "No stackup checks recorded yet.")}
<h2>Validation runs</h2>
{_table(["#", "When (UTC)", "Action", "Target", "Key values", "Result", "Failure mode"],
        val_rows, "No validation runs recorded yet — Phase 4 not started.")}
<h2>Diagnostic images (from validation runs)</h2>
{_embed_images(rows, data_root)}
<h2>Sourcing / BOM — cached public pricing, not live supplier data</h2>
{"".join(bom_notes)}
{_table(["Version", "Ref", "Supplier / part #", "Description", "Qty", "USD"],
        bom_rows, "No BOM generated yet — production is gated on sign-off.")}
<h2>Failure log (FRACAS)</h2>
{_table(["#", "When (UTC)", "Action", "Target", "Failure mode", "Reason given"],
        fail_rows, "No failures recorded.")}
<h3>Failure modes by frequency</h3>
{_table(["Failure mode", "Count"], mode_rows, "No failures recorded.")}
<h2>Change history — exact diffs</h2>
{_table(["#", "When (UTC)", "Action", "Produced", "Diff", "Reason"],
        change_rows, "No geometry changes recorded.")}
<h2>Sign-offs</h2>
{_table(["#", "Action", "Target", "Signed off by"], signed,
        "No sign-offs recorded — production gate is closed.")}
</body></html>"""
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_doc, encoding="utf-8")
    except Exception as exc:
        log.close_action(action_id, "fail",
                         failure_mode=f"{type(exc).__name__}: {exc}")
        raise
    log.close_action(action_id, "pass",
                     details={"out_path": str(out_path), "rows_rendered": len(rows)})
    return out_path
