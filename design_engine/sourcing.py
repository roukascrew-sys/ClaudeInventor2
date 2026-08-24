"""BOM, sourcing and costing (Phase 6) — behind the Phase 5 sign-off lock.

Grounding rule: **no invented part numbers, no invented prices.** Every line
item resolves to an entry in `data/price_book.json`, each of which carries the
supplier, the supplier's own part number, the source URL, and the date the
price was read off the public catalog page. A SKU that is not in the book is a
hard error — this module will not guess a price.

Prices are CACHED, not live (McMaster-Carr API deliberately not used, Gideon
2026-08-23). Every costing result carries `price_as_of` and a
`staleness_warning` when the book is older than its own `staleness_warn_days`.

Quantity-break math is exact, not a rule of thumb: `cost_for_qty` runs a small
dynamic program over the supplier's own pack tiers and returns the genuinely
cheapest combination of packs that yields at least the required quantity,
along with the resulting overbuy.

Min-cost baseline: for every line, the cheapest item sharing that line's
`function_class` is evaluated. Substitutions are **reported, never applied
silently**, and any `substitution_caveat` in the price book (e.g. black oxide
having no sacrificial corrosion protection where zinc does) is surfaced with
the saving so the trade is visible rather than buried.

Budget: per Gideon's standing instruction, exceeding the budget does NOT block
the BOM. The package is produced anyway and labelled `proof-of-concept`.
"""

from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

from .log import ActionLog
from .parts import PartStore, _check_reason
from .production import ProductionTools

PRICE_BOOK_PATH = Path(__file__).parent / "data" / "price_book.json"

# Bandsaw kerf plus facing allowance per cut. ESTIMATE, not a measured shop
# value: 1.5 mm kerf (typical 0.035-0.050 in bimetal blade) + 1.5 mm facing.
# Override per BOM with "cut_allowance_mm".
DEFAULT_CUT_ALLOWANCE_MM = 3.0


class SourcingError(ValueError):
    """The BOM spec is malformed, or a SKU/stock size cannot be sourced."""


def load_price_book(path: str | Path = PRICE_BOOK_PATH) -> dict:
    book = json.loads(Path(path).read_text(encoding="utf-8"))
    book["_by_sku"] = {item["sku"]: item for item in book["items"]}
    return book


def book_staleness(book: dict, today: datetime.date | None = None) -> dict:
    today = today or datetime.date.today()
    captured = datetime.date.fromisoformat(book["captured_at"])
    age = (today - captured).days
    limit = book.get("staleness_warn_days", 90)
    warning = None
    if age > limit:
        warning = (f"price book captured {book['captured_at']} is {age} days old "
                   f"(warn threshold {limit}) — re-capture before purchasing")
    return {"price_as_of": book["captured_at"], "age_days": age,
            "staleness_warning": warning}


def cost_for_qty(item: dict, qty: int) -> dict:
    """Cheapest combination of the supplier's own pack tiers for >= qty units.

    Exact dynamic program over pack tiers (not a nearest-break heuristic), so
    e.g. 90 M6 screws correctly costs one 100-bag at $17.48 rather than
    90 x $0.21 = $18.90.
    """
    if qty <= 0:
        raise SourcingError(f"quantity must be > 0, got {qty}")
    breaks = item.get("price_breaks")
    if not breaks:
        raise SourcingError(f"{item['sku']}: no price_breaks in the price book")
    if qty > 1_000_000:
        raise SourcingError(f"{item['sku']}: quantity {qty} exceeds the 1e6 guard")

    best = [math.inf] * (qty + 1)
    choice: list[dict | None] = [None] * (qty + 1)
    best[0] = 0.0
    for q in range(1, qty + 1):
        for br in breaks:
            prev = max(0, q - br["pack_qty"])
            cand = best[prev] + br["pack_usd"]
            if cand < best[q]:
                best[q] = cand
                choice[q] = br
    packs: dict[int, int] = {}
    q = qty
    while q > 0:
        br = choice[q]
        packs[br["pack_qty"]] = packs.get(br["pack_qty"], 0) + 1
        q = max(0, q - br["pack_qty"])
    purchased = sum(pq * n for pq, n in packs.items())
    return {
        "qty_required": qty,
        "qty_purchased": purchased,
        "overbuy": purchased - qty,
        "packs": [{"pack_qty": pq, "count": n} for pq, n in sorted(packs.items())],
        "total_usd": round(best[qty], 4),
        "effective_unit_usd": round(best[qty] / qty, 6),
    }


def cross_section_fits(item: dict, a_mm: float, b_mm: float,
                       blank_section: str = "rect") -> tuple[bool, str]:
    """Can the part's two smaller dimensions be cut from this bar's section?

    Depends on the stock's shape, so a single 'cross_section_mm' number is not
    enough — a 38 mm wide flat bar can yield a 32 mm wide part despite being
    3.2 mm thick, and a round bar must swallow the blank's DIAGONAL, not its
    side. Returns (fits, explanation).
    """
    spec = item["spec"]
    form = item.get("stock_form", "square_bar")
    lo, hi = min(a_mm, b_mm), max(a_mm, b_mm)
    if form == "flat_bar":
        th, w = spec["thickness_mm"], spec["width_mm"]
        ok = lo <= th + 1e-9 and hi <= w + 1e-9
        return ok, (f"flat bar {th} x {w} mm vs blank {lo:.3f} x {hi:.3f} mm "
                    f"(thickness must cover {lo:.3f}, width must cover {hi:.3f})")
    if form == "round_bar":
        d = spec["cross_section_mm"]
        if blank_section == "round":
            # part is itself round and coaxial with the bar: only the
            # diameter must be covered, not the bounding-box diagonal
            ok = hi <= d + 1e-9
            return ok, (f"round bar dia {d} mm vs round blank dia {hi:.3f} mm "
                        f"(declared blank_section='round')")
        diag = math.hypot(lo, hi)
        ok = diag <= d + 1e-9
        return ok, (f"round bar dia {d} mm vs blank diagonal {diag:.3f} mm "
                    f"(a rectangular blank needs the diagonal; if the part is "
                    f"itself round, declare blank_section='round' on the line)")
    cross = spec["cross_section_mm"]
    ok = hi <= cross + 1e-9
    return ok, f"square bar {cross} mm vs blank {lo:.3f} x {hi:.3f} mm"


def select_stock(item: dict, required_cross_mm: float, required_length_mm: float,
                 pieces: int, cut_allowance_mm: float,
                 blank_a_mm: float | None = None,
                 blank_b_mm: float | None = None,
                 blank_section: str = "rect") -> dict:
    """Cheapest purchasable stock length that yields `pieces` blanks.

    Nesting is 1-D: each blank consumes required_length + cut allowance, and
    only whole blanks are counted per bar (remainder is scrap).
    """
    a = blank_a_mm if blank_a_mm is not None else required_cross_mm
    b = blank_b_mm if blank_b_mm is not None else required_cross_mm
    fits, why = cross_section_fits(item, a, b, blank_section)
    if not fits:
        raise SourcingError(
            f"{item['sku']}: stock section cannot yield this part - {why}")
    per_blank = required_length_mm + cut_allowance_mm
    options = []
    for br in item["length_breaks"]:
        blanks_per_bar = int(br["length_mm"] // per_blank)
        if blanks_per_bar < 1:
            continue
        bars = math.ceil(pieces / blanks_per_bar)
        options.append({
            "length_in": br["length_in"], "length_mm": br["length_mm"],
            "bars": bars, "blanks_per_bar": blanks_per_bar,
            "total_usd": round(bars * br["usd"], 4),
            "unit_usd": br["usd"],
            "weight_lb_total": round(bars * br["weight_lb"], 4),
        })
    if not options:
        raise SourcingError(
            f"{item['sku']}: no stocked length fits a {per_blank:.1f} mm blank "
            f"(longest is {item['length_breaks'][-1]['length_mm']} mm)")
    best = min(options, key=lambda o: (o["total_usd"], o["length_mm"]))
    best["utilisation_pct"] = round(
        100.0 * pieces * required_length_mm
        / (best["bars"] * best["length_mm"]), 2)
    return best


def process_cost(line: dict, qty: int, ctx: str) -> dict:
    """Cost of a manufacturing operation (machining, welding, finishing).

    The engine has no shop-rate database and will not invent one: the caller
    supplies the rate and the time, and MUST cite where they came from
    (a quote, a shop's published rate, a measured cycle time). This mirrors
    the rule that material properties must carry a `source` - an uncited
    number that looks like a cost is exactly the sort of thing that ends up
    in a decision unchallenged.

    Setup is charged once for the batch; run time is charged per piece.
    """
    for key in ("rate_usd_per_hr", "minutes_each"):
        v = line.get(key)
        if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
            raise SourcingError(f"{ctx}.{key}: non-negative number required, got {v!r}")
    setup = line.get("setup_minutes", 0.0)
    if not isinstance(setup, (int, float)) or isinstance(setup, bool) or setup < 0:
        raise SourcingError(f"{ctx}.setup_minutes: non-negative number required")
    src = line.get("source")
    if not isinstance(src, str) or not src.strip():
        raise SourcingError(
            f"{ctx}.source: required - cite where the rate and cycle time come "
            f"from (quote, published shop rate, measured time). This engine "
            f"does not invent manufacturing costs.")
    rate = float(line["rate_usd_per_hr"])
    run_min = float(line["minutes_each"]) * qty
    total_min = run_min + float(setup)
    return {
        "rate_usd_per_hr": rate,
        "minutes_each": float(line["minutes_each"]),
        "setup_minutes": float(setup),
        "total_minutes": round(total_min, 4),
        "qty_required": qty, "qty_purchased": qty, "overbuy": 0,
        "total_usd": round(rate * total_min / 60.0, 4),
        "source": src.strip(),
    }


_LINE_KEYS = {
    "catalog": {"ref", "kind", "sku", "per_assembly"},
    "stock": {"ref", "kind", "function_class", "per_assembly", "blank_section"},
    "process": {"ref", "kind", "per_assembly", "rate_usd_per_hr",
                "minutes_each", "setup_minutes", "source", "description",
                "supplier", "function_class"},
}


class SourcingTools:
    def __init__(self, root: str | Path, log: ActionLog, parts: PartStore,
                 production: ProductionTools,
                 price_book_path: str | Path = PRICE_BOOK_PATH):
        self.root = Path(root)
        self.log = log
        self.parts = parts
        self.production = production
        self.price_book_path = Path(price_book_path)

    # ---------- line resolution ----------

    def _resolve_catalog(self, book: dict, line: dict, qty: int) -> dict:
        sku = line.get("sku")
        item = book["_by_sku"].get(sku)
        if item is None:
            raise SourcingError(
                f"line {line.get('ref')!r}: SKU {sku!r} is not in the price book — "
                f"this engine does not invent part numbers or prices")
        cost = cost_for_qty(item, qty)
        return {
            "ref": line["ref"], "kind": "catalog", "sku": sku,
            "supplier": item["supplier"], "part_number": item["part_number"],
            "description": item["description"],
            "function_class": item["function_class"],
            "source_url": item["source_url"], "captured_at": item["captured_at"],
            **cost,
        }

    def _resolve_stock(self, book: dict, line: dict, pieces: int,
                       bbox_size: list, cut_allowance_mm: float) -> dict:
        blank_section = line.get("blank_section", "rect")
        if blank_section not in ("rect", "round"):
            raise SourcingError(
                f"line {line.get('ref')!r}: blank_section must be "
                f"'rect' or 'round', got {blank_section!r}")
        candidates = [i for i in book["items"]
                      if i["kind"] == "stock"
                      and i["function_class"] == line.get("function_class")]
        if not candidates:
            raise SourcingError(
                f"line {line.get('ref')!r}: no stock item in the price book with "
                f"function_class {line.get('function_class')!r}")
        dims = sorted(bbox_size)
        required_cross = dims[1]          # bar must span the 2 smaller dims
        required_length = dims[2]
        priced = []
        for item in candidates:
            try:
                sel = select_stock(item, required_cross, required_length,
                                   pieces, cut_allowance_mm,
                                   blank_a_mm=dims[0], blank_b_mm=dims[1],
                                   blank_section=blank_section)
            except SourcingError:
                continue
            priced.append((item, sel))
        if not priced:
            raise SourcingError(
                f"line {line.get('ref')!r}: no stocked size in "
                f"{[c['sku'] for c in candidates]} can yield a "
                f"{dims[0]:.2f} x {dims[1]:.2f} x {required_length:.2f} mm blank")
        item, sel = min(priced, key=lambda p: p[1]["total_usd"])
        return {
            "ref": line["ref"], "kind": "stock", "sku": item["sku"],
            "supplier": item["supplier"], "part_number": item["part_number"],
            "description": item["description"],
            "function_class": item["function_class"],
            "source_url": item["source_url"], "captured_at": item["captured_at"],
            "blank_mm": [round(required_cross, 3), round(required_cross, 3),
                         round(required_length, 3)],
            "cut_allowance_mm": cut_allowance_mm,
            "qty_required": pieces, "qty_purchased": sel["bars"],
            **{k: v for k, v in sel.items() if k != "bars"},
            "bars": sel["bars"],
        }

    # ---------- min-cost baseline ----------

    def _min_cost_line(self, book: dict, resolved: dict, qty: int) -> dict:
        """Cheapest same-function alternative for one resolved catalog line."""
        alts = [i for i in book["items"]
                if i["kind"] == "catalog"
                and i["function_class"] == resolved["function_class"]]
        best_sku, best_cost = resolved["sku"], resolved["total_usd"]
        best_item = book["_by_sku"][resolved["sku"]]
        for item in alts:
            cost = cost_for_qty(item, qty)["total_usd"]
            if cost < best_cost - 1e-9:
                best_sku, best_cost, best_item = item["sku"], cost, item
        out = {"ref": resolved["ref"], "sku": best_sku,
               "total_usd": round(best_cost, 4),
               "saving_usd": round(resolved["total_usd"] - best_cost, 4),
               "substituted": best_sku != resolved["sku"]}
        if out["substituted"]:
            out["from_sku"] = resolved["sku"]
            out["description"] = best_item["description"]
            out["caveat"] = best_item.get(
                "substitution_caveat",
                "No caveat recorded in the price book — verify fit, form and "
                "function against the as-specified item before substituting.")
        return out

    # ---------- the gated tool ----------

    def generate_bom(self, geometry_id: str, bom_spec: dict, reason: str,
                     budget_usd: float | None = None) -> dict:
        action_id = self.log.open_action(
            "production", "generate_bom", geometry_version=str(geometry_id),
            reason=str(reason))
        try:
            _check_reason(reason)
            self.production.verify_sign_off(geometry_id)  # THE LOCK
            if not isinstance(bom_spec, dict):
                raise SourcingError("bom_spec must be a dict")
            extra = set(bom_spec) - {"quantity", "lines", "cut_allowance_mm"}
            if extra:
                raise SourcingError(f"bom_spec: unexpected keys {sorted(extra)}")
            assemblies = bom_spec.get("quantity")
            if not isinstance(assemblies, int) or isinstance(assemblies, bool) \
                    or assemblies < 1:
                raise SourcingError(
                    f"bom_spec.quantity: positive integer required, got {assemblies!r}")
            lines = bom_spec.get("lines")
            if not isinstance(lines, list) or not lines:
                raise SourcingError("bom_spec.lines: non-empty list required")
            cut_allowance = bom_spec.get("cut_allowance_mm", DEFAULT_CUT_ALLOWANCE_MM)

            part = self.parts.get_part(geometry_id)
            bbox_size = part["properties"]["bbox_mm"]["size"]
            book = load_price_book(self.price_book_path)
            stale = book_staleness(book)

            resolved, min_cost_lines = [], []
            refs = set()
            for line in lines:
                ref = line.get("ref")
                if not isinstance(ref, str) or not ref:
                    raise SourcingError("every line needs a non-empty 'ref'")
                if ref in refs:
                    raise SourcingError(f"duplicate line ref {ref!r}")
                refs.add(ref)
                per = line.get("per_assembly")
                if not isinstance(per, int) or isinstance(per, bool) or per < 1:
                    raise SourcingError(
                        f"line {ref!r}: per_assembly must be a positive integer")
                qty = per * assemblies
                kind = line.get("kind")
                if kind in _LINE_KEYS:
                    unknown = set(line) - _LINE_KEYS[kind]
                    if unknown:
                        raise SourcingError(
                            f"line {ref!r} ({kind}): unexpected keys "
                            f"{sorted(unknown)} - allowed: "
                            f"{sorted(_LINE_KEYS[kind])}")
                if kind == "catalog":
                    r = self._resolve_catalog(book, line, qty)
                    resolved.append(r)
                    min_cost_lines.append(self._min_cost_line(book, r, qty))
                elif kind == "process":
                    r = {"ref": ref, "kind": "process",
                         "sku": None, "supplier": line.get("supplier", "in-house"),
                         "part_number": "-",
                         "description": line.get("description", "process operation"),
                         "function_class": line.get("function_class", "process"),
                         "source_url": "", "captured_at": "-",
                         **process_cost(line, qty, f"line {ref!r}")}
                    resolved.append(r)
                    min_cost_lines.append(
                        {"ref": ref, "sku": None, "total_usd": r["total_usd"],
                         "saving_usd": 0.0, "substituted": False})
                elif kind == "stock":
                    r = self._resolve_stock(book, line, qty, bbox_size, cut_allowance)
                    resolved.append(r)
                    min_cost_lines.append(
                        {"ref": ref, "sku": r["sku"], "total_usd": r["total_usd"],
                         "saving_usd": 0.0, "substituted": False})
                else:
                    raise SourcingError(
                        f"line {ref!r}: kind must be 'catalog', 'stock' or "
                        f"'process', got {line.get('kind')!r}")

            as_specified = round(sum(r["total_usd"] for r in resolved), 4)
            min_cost = round(sum(m["total_usd"] for m in min_cost_lines), 4)
            subs = [m for m in min_cost_lines if m["substituted"]]

            budget = None
            if budget_usd is not None:
                over = as_specified > budget_usd
                budget = {
                    "budget_usd": budget_usd,
                    "as_specified_usd": as_specified,
                    "over_budget": over,
                    "delta_usd": round(as_specified - budget_usd, 4),
                    "min_cost_over_budget": min_cost > budget_usd,
                    "label": "proof-of-concept" if over else "within-budget",
                    "note": ("Over budget — package produced anyway and labelled "
                             "proof-of-concept per standing instruction; not "
                             "blocked.") if over else None,
                }

            bom = {
                "geometry_id": geometry_id,
                "assemblies": assemblies,
                "part_bbox_mm": bbox_size,
                "cut_allowance_mm": cut_allowance,
                "cut_allowance_basis": (
                    "ESTIMATE: ~1.5 mm bandsaw kerf + ~1.5 mm facing; not a "
                    "measured value from your shop"),
                "lines": resolved,
                "as_specified_total_usd": as_specified,
                "min_cost_total_usd": min_cost,
                "min_cost_saving_usd": round(as_specified - min_cost, 4),
                "min_cost_lines": min_cost_lines,
                "substitutions": subs,
                "budget": budget,
                "pricing": {
                    **stale,
                    "basis": "cached public catalog pricing; excludes tax and "
                             "shipping; not live supplier data",
                    "suppliers": book["suppliers"],
                },
            }

            out_dir = self.root / "production" / geometry_id.replace("@", "_")
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "bom.json").write_text(
                json.dumps(bom, indent=2), encoding="utf-8")
        except Exception as exc:
            self.log.close_action(
                action_id, "fail", failure_mode=f"{type(exc).__name__}: {exc}")
            raise
        self.log.close_action(action_id, "pass", details=bom)
        return {**bom, "action_id": action_id,
                "bom_path": str(out_dir / "bom.json")}
