"""Phase 6 contract tests — BOM, sourcing, costing.

All expected costs are hand-computed from the captured supplier tiers in
data/price_book.json:

  M6x20 cap screw zinc  #23106: $0.21 ea / $17.48 per 100 / $157.00 per 1000
  M6x20 cap screw blkox #6540 : $0.17 ea / $11.20 per 100 /  $98.60 per 1000
  M6 hex nut cl10.9     #6883 : $0.06 ea /  $4.20 per 100 /  $37.00 per 1000
  1/2" A36 square bar   #10277: 12in $4.50 / 24in $4.80 / 36in $6.56 / 48in $8.19

10 assemblies of a 10 x 10 x 100 mm bar part, 4 screws + 4 nuts each:
  stock  : blank = 100 + 3 mm allowance; 48in bar (1219.2 mm) yields 11 blanks,
           so 1 bar = $8.19 (cheaper than 2 x 24in = $9.60 or 5 x 12in = $22.50)
  screws : 40 units -> 40 x $0.21 = $8.40 (one 100-bag at $17.48 is dearer)
  nuts   : 40 units -> 40 x $0.06 = $2.40
  as-specified total = $18.99 ; min-cost swaps screws to black oxide
  (40 x $0.17 = $6.80) -> $17.39, saving $1.60
"""

import json

import pytest

from design_engine import DesignEngine
from design_engine.production import SignOffRequired
from design_engine.sourcing import (SourcingError, cost_for_qty,
                                    load_price_book, select_stock)

BAR = {"name": "sourced-bar", "units": "mm",
       "features": [{"op": "box", "x": 10, "y": 10, "z": 100}]}

S235 = {"name": "S235JR", "E_MPa": 210000, "nu": 0.3, "yield_MPa": 235,
        "source": "EN 10025-2 nominal values, t<=16mm"}

BOM_SPEC = {
    "quantity": 10,
    "lines": [
        {"ref": "blank", "kind": "stock",
         "function_class": "square_bar_steel_a36", "per_assembly": 1},
        {"ref": "screws", "kind": "catalog", "sku": "boltdepot-23106",
         "per_assembly": 4},
        {"ref": "nuts", "kind": "catalog", "sku": "boltdepot-6883",
         "per_assembly": 4},
    ],
}


@pytest.fixture(scope="module")
def book():
    return load_price_book()


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    return DesignEngine(tmp_path_factory.mktemp("p6") / "data")


@pytest.fixture(scope="module")
def signed_gid(eng):
    gid = eng.create_part(BAR, reason="sourcing test bar")["geometry_id"]
    run = eng.run_fea_static(gid, {
        "material": dict(S235),
        "mesh": {"max_size_mm": 5.0},
        "constraints": [{"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]}],
        "loads": [{"where": {"axis": "z", "at": "max"},
                   "force_total_N": [0, 0, 1000]}],
        "limit_state": {"name": "yield_von_mises", "required_SF": 2.0},
    }, reason="validate before sourcing")
    assert run["result"] == "pass"
    eng.sign_off(gid, "Gideon", "approve sourced-bar v1 for prototype build")
    return gid


# ---------- pure pricing math ----------

def test_quantity_break_dp_is_optimal(book):
    screw = book["_by_sku"]["boltdepot-23106"]
    # 40 singles beat a 100-bag
    assert cost_for_qty(screw, 40)["total_usd"] == pytest.approx(8.40)
    assert cost_for_qty(screw, 40)["overbuy"] == 0
    # at 90 the bag wins: 90 x 0.21 = 18.90 > 17.48
    c90 = cost_for_qty(screw, 90)
    assert c90["total_usd"] == pytest.approx(17.48)
    assert c90["qty_purchased"] == 100 and c90["overbuy"] == 10
    # 1000 uses the bulk tier, not ten bags (157.00 < 174.80)
    assert cost_for_qty(screw, 1000)["total_usd"] == pytest.approx(157.00)
    # mixed packs: 1050 = one 1000-bulk + 50 singles = 157.00 + 10.50
    assert cost_for_qty(screw, 1050)["total_usd"] == pytest.approx(167.50)
    with pytest.raises(SourcingError):
        cost_for_qty(screw, 0)


def test_stock_selection_picks_cheapest_usable_length(book):
    bar = book["_by_sku"]["onlinemetals-10277"]
    sel = select_stock(bar, required_cross_mm=10.0, required_length_mm=100.0,
                       pieces=10, cut_allowance_mm=3.0)
    assert sel["length_in"] == 48          # 1219.2 mm -> 11 blanks of 103 mm
    assert sel["blanks_per_bar"] == 11
    assert sel["bars"] == 1
    assert sel["total_usd"] == pytest.approx(8.19)
    assert sel["utilisation_pct"] == pytest.approx(82.02, abs=0.01)
    # a part thicker than the bar cannot be sourced from it
    with pytest.raises(SourcingError, match="cannot yield this part"):
        select_stock(bar, 25.0, 100.0, 1, 3.0)
    # nothing stocked is long enough for a 7 m blank
    with pytest.raises(SourcingError, match="no stocked length fits"):
        select_stock(bar, 10.0, 7000.0, 1, 3.0)


def test_price_book_entries_carry_provenance(book):
    for item in book["items"]:
        assert item["source_url"].startswith("https://")
        assert item["captured_at"]
        assert item["supplier"] in book["suppliers"]
        assert item["part_number"]


# ---------- the gated tool ----------

def test_bom_requires_sign_off(eng):
    gid = eng.create_part({**BAR, "name": "unsigned-bar"},
                          reason="unsigned part")["geometry_id"]
    with pytest.raises(SignOffRequired, match="sign_off_missing"):
        eng.generate_bom(gid, BOM_SPEC, reason="premature BOM")
    row = eng.log.rows(action="generate_bom", result="fail")[-1]
    assert "sign_off_missing" in row["failure_mode"]


def test_bom_costs_and_min_cost_baseline(eng, signed_gid):
    bom = eng.generate_bom(signed_gid, BOM_SPEC,
                           reason="prototype build of 10 units")
    by_ref = {ln["ref"]: ln for ln in bom["lines"]}
    assert by_ref["blank"]["total_usd"] == pytest.approx(8.19)
    assert by_ref["blank"]["sku"] == "onlinemetals-10277"
    assert by_ref["screws"]["total_usd"] == pytest.approx(8.40)
    assert by_ref["nuts"]["total_usd"] == pytest.approx(2.40)
    assert bom["as_specified_total_usd"] == pytest.approx(18.99)

    # min-cost baseline is always generated
    assert bom["min_cost_total_usd"] == pytest.approx(17.39)
    assert bom["min_cost_saving_usd"] == pytest.approx(1.60)
    subs = bom["substitutions"]
    assert len(subs) == 1
    assert subs[0]["from_sku"] == "boltdepot-23106"
    assert subs[0]["sku"] == "boltdepot-6540"
    # the trade is surfaced, not silently applied
    assert "corrosion" in subs[0]["caveat"].lower()
    assert by_ref["screws"]["sku"] == "boltdepot-23106"  # as-specified unchanged

    # provenance travels with the costing
    assert bom["pricing"]["price_as_of"] == "2026-08-23"
    assert "not live" in bom["pricing"]["basis"]
    assert json.loads(open(bom["bom_path"], encoding="utf-8").read())[
        "as_specified_total_usd"] == pytest.approx(18.99)


def test_over_budget_is_labelled_not_blocked(eng, signed_gid):
    bom = eng.generate_bom(signed_gid, BOM_SPEC,
                           reason="same build against a tight budget",
                           budget_usd=15.0)
    assert bom["budget"]["over_budget"] is True
    assert bom["budget"]["label"] == "proof-of-concept"
    assert bom["budget"]["delta_usd"] == pytest.approx(3.99)
    assert bom["budget"]["min_cost_over_budget"] is True   # 17.39 > 15.00
    # produced anyway: the action passed and the lines are all present
    assert eng.log.rows(action="generate_bom", result="pass")[-1] is not None
    assert len(bom["lines"]) == 3

    within = eng.generate_bom(signed_gid, BOM_SPEC,
                              reason="same build, realistic budget",
                              budget_usd=25.0)
    assert within["budget"]["over_budget"] is False
    assert within["budget"]["label"] == "within-budget"


def test_unknown_sku_is_refused_not_invented(eng, signed_gid):
    spec = {"quantity": 1, "lines": [
        {"ref": "mystery", "kind": "catalog", "sku": "mcmaster-91290A115",
         "per_assembly": 1}]}
    with pytest.raises(SourcingError, match="not in the price book"):
        eng.generate_bom(signed_gid, spec, reason="should refuse to guess")
    row = eng.log.rows(action="generate_bom", result="fail")[-1]
    assert "does not invent part numbers" in row["failure_mode"]


def test_report_renders_bom_from_the_log(eng, signed_gid):
    eng.generate_bom(signed_gid, BOM_SPEC, reason="BOM for the report",
                     budget_usd=15.0)
    doc = eng.generate_report().read_text(encoding="utf-8")
    assert "Sourcing / BOM" in doc
    assert "bolt_depot #23106" in doc and "online_metals #10277" in doc
    assert "$18.99" in doc and "$17.39" in doc
    assert "proof-of-concept" in doc
    # the substitution trade is visible with its caveat, not buried
    assert "boltdepot-23106 → boltdepot-6540" in doc
    assert "corrosion protection" in doc
    assert "not live supplier data" in doc


def test_bom_spec_validation(eng, signed_gid):
    bad_specs = [
        ({"quantity": 0, "lines": BOM_SPEC["lines"]}, "positive integer"),
        ({"quantity": 1, "lines": []}, "non-empty list"),
        ({"quantity": 1, "lines": [{"ref": "x", "kind": "vibes",
                                    "per_assembly": 1}]}, "kind must be"),
        ({"quantity": 1, "turbo": True, "lines": BOM_SPEC["lines"]},
         "unexpected keys"),
        ({"quantity": 1, "lines": [
            {"ref": "a", "kind": "catalog", "sku": "boltdepot-6883",
             "per_assembly": 1},
            {"ref": "a", "kind": "catalog", "sku": "boltdepot-6883",
             "per_assembly": 1}]}, "duplicate line ref"),
    ]
    for spec, msg in bad_specs:
        with pytest.raises(SourcingError, match=msg):
            eng.generate_bom(signed_gid, spec, reason="must be rejected")


# ---------- shape-aware stock fit, process lines (gaps found by the hinge test) ----------

def test_stock_fit_depends_on_bar_shape(book):
    """A single 'cross_section_mm' cannot express a flat or round bar."""
    from design_engine.sourcing import cross_section_fits

    flat = book["_by_sku"]["onlinemetals-10001"]      # 3.175 x 38.1 mm
    ok, why = cross_section_fits(flat, 2.5, 32.0)     # hinge-leaf-shaped blank
    assert ok and "flat bar" in why                   # wide part, thin bar: fits
    assert not cross_section_fits(flat, 2.5, 45.0)[0]  # wider than the bar
    assert not cross_section_fits(flat, 6.0, 20.0)[0]  # thicker than the bar

    rnd = book["_by_sku"]["onlinemetals-4790"]        # 4.7625 mm dia
    # a rectangular blank must fit the bar's DIAGONAL, not its side
    assert not cross_section_fits(rnd, 3.5, 3.5)[0]   # diagonal 4.95 > 4.7625
    assert cross_section_fits(rnd, 3.0, 3.0)[0]       # diagonal 4.24 < 4.7625
    # a part that is itself round only needs the diameter covered
    ok, why = cross_section_fits(rnd, 3.5, 3.5, blank_section="round")
    assert ok and "round blank" in why


def test_process_line_requires_a_cited_rate(eng, signed_gid):
    """Machining cost is caller-supplied and must be cited, like material data."""
    base = {"ref": "mill", "kind": "process", "per_assembly": 1,
            "rate_usd_per_hr": 75.0, "minutes_each": 6.0,
            "setup_minutes": 30.0,
            "source": "local shop quote 2026-08-24, 3-axis mill, $75/hr"}

    bom = eng.generate_bom(signed_gid, {"quantity": 10, "lines": [dict(base)]},
                           reason="machining cost with a cited rate")
    line = bom["lines"][0]
    # 10 x 6 min run + 30 min setup = 90 min at $75/hr = $112.50
    assert line["total_minutes"] == pytest.approx(90.0)
    assert line["total_usd"] == pytest.approx(112.50)
    assert bom["as_specified_total_usd"] == pytest.approx(112.50)

    uncited = {k: v for k, v in base.items() if k != "source"}
    with pytest.raises(SourcingError, match="does not invent manufacturing costs"):
        eng.generate_bom(signed_gid, {"quantity": 1, "lines": [uncited]},
                         reason="uncited rate must be refused")


def test_unknown_line_keys_are_refused(eng, signed_gid):
    """A typo'd key must not silently do nothing."""
    with pytest.raises(SourcingError, match="unexpected keys"):
        eng.generate_bom(signed_gid, {"quantity": 1, "lines": [
            {"ref": "screws", "kind": "catalog", "sku": "boltdepot-23106",
             "per_assembly": 1, "quantitiy": 5}]},   # typo
            reason="typo'd key must be caught")
