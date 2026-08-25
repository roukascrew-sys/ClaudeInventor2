"""Jetpack delivery: sign-off, production packages, BOM, assembly viewer.

Runs AFTER designs/jetpack_full_run.py and reads its summary JSON, so the
identifiers and results here come from the log rather than being retyped.

Sign-off is the code-level gate for everything downstream (production package,
BOM, viewer all call verify_sign_off), so it happens first. Each certificate
carries its own scope limits: what was validated, at what temperature, against
which named limit state, and what was NOT covered. A signature that does not
say what it excludes is worse than no signature.

Run with the project venv from the repo root:
    .venv\\Scripts\\python.exe designs\\jetpack_deliver.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from design_engine import DesignEngine
from design_engine.sourcing import SourcingError

ROOT = Path(__file__).parent.parent / "data"
eng = DesignEngine(ROOT)

summary = json.loads((ROOT / "jetpack_full_summary.json").read_text(encoding="utf-8"))
if not summary["all_pass"]:
    raise SystemExit(
        f"REFUSING to deliver: not every gate passed -> {summary['results']}. "
        f"Sign-off must never paper over a failed gate.")

SPINE = summary["parts"]["spine"]
CB = summary["parts"]["crossbeam"]
CRADLE = summary["parts"]["cradle"]
ASM = summary["assembly_id"]
mp = summary["mass_properties"]
t_cb = summary["max_permissible_crossbeam_temp_C"]

COMMON_EXCLUSIONS = (
    "NOT COVERED BY THIS SIGN-OFF: propulsion of any kind (combustion, thrust "
    "generation, FADEC, fuel delivery, starting, surge/flameout); fuel tank "
    "structure, venting, fire and crash-worthiness; every bolted joint's local "
    "bearing and hole-edge stress; harness webbing and buckles; fatigue and "
    "vibration (the turbines run to 98,000 RPM); exhaust plume mapping; flight "
    "control, attitude stability and pilot workload; and any physical "
    "qualification testing. This sign-off certifies STRUCTURE against named "
    "limit states with sourced material data. It makes no claim that the "
    "assembly flies or is safe to wear."
)

THERMAL_CAVEAT = (
    "The derating curves are FIRE-DESIGN data (EN 1999-1-2 up to 2 hours "
    "exposure; EN 1993-1-2 Table 3.1 effective yield at 2% strain, short "
    "duration). They do not cover creep or thermal-cycling fatigue under "
    "repeated service, which is what a jetpack actually does to its structure."
)

print("=== Step 1: sign-off ===")
signoffs = {}

signoffs["crossbeam"] = eng.sign_off(
    CB, signed_off_by="Gideon",
    statement=(
        f"Jetpack engine crossbeam, 6061-T6511, carrying 4x397 N JetCat "
        f"P400-PRO thrust at |x|=350/540 mm into a 240 mm bolted lap joint. "
        f"Validated: thermal_derated_yield at 150 C (k_0,2=0.910 per "
        f"EN 1999-1-2 Table 1a, allowable 251.2 MPa) and elastic_buckling, "
        f"both against required_SF 3.0. required_SF=3.0 is Gideon's engineer's "
        f"judgment, NOT a code citation - no OSHA/ANSI/FAA structural standard "
        f"exists for a personal jet-propulsion frame. "
        f"ACCEPTANCE CRITERION BINDING ON ANY BUILD: the crossbeam must be "
        f"demonstrated by instrumented test to stay below {t_cb:.0f} C in "
        f"service. Above that temperature this part does not clear its gate "
        f"and this sign-off does not apply. The 150 C design assumption is NOT "
        f"a measurement. {THERMAL_CAVEAT} "
        f"The doubler plate on the far side of the lap joint and the joint "
        f"bolts themselves are NOT modelled. {COMMON_EXCLUSIONS}"))

signoffs["spine"] = eng.sign_off(
    SPINE, signed_off_by="Gideon",
    statement=(
        f"Jetpack spine, 6061-T6511, carrying 1588 N total thrust from the "
        f"crossbeam joint into two M6 harness lug bores. Validated: "
        f"thermal_derated_yield at 150 C against required_SF 3.0. "
        f"The ONE-ENGINE-OUT case (one engine of four failed, unbalanced "
        f"moment 204,296 N*mm, sigma 18.7 MPa, SF 13.4 at 150 C) is a HAND "
        f"CALCULATION stated in the run log, not an FEA result - it is "
        f"reported as analysis, not as solver-validated. "
        f"Same {t_cb:.0f} C temperature acceptance criterion and the same "
        f"150 C unmeasured assumption as the crossbeam. {THERMAL_CAVEAT} "
        f"The lug BORES are validated; the harness that attaches to them is "
        f"not. {COMMON_EXCLUSIONS}"))

signoffs["cradle"] = eng.sign_off(
    CRADLE, signed_off_by="Gideon",
    statement=(
        f"Jetpack engine cradle, 1018 carbon steel, one per engine, carrying "
        f"397 N from the engine casing bore into 4x M6 bolts. Validated: "
        f"thermal_derated_yield at 400 C (k_y=1.000 per EN 1993-1-2 "
        f"Table 3.1 - carbon steel retains full effective yield to 400 C, "
        f"which is why this part is steel and not aluminium next to a "
        f"480-750 C EGT turbine) against required_SF 3.0. "
        f"The bore traction is a documented simplification of a clamped "
        f"contact problem, not contact mechanics; local bore stresses are "
        f"indicative. Steel selection addresses STRENGTH at temperature only - "
        f"oxidation/scaling of plain carbon steel in repeated 400 C service is "
        f"NOT assessed and a corrosion-resistant grade may be required. "
        f"{THERMAL_CAVEAT} "
        f"The bracket joining this cradle to the crossbeam is NOT modelled. "
        f"{COMMON_EXCLUSIONS}"))

for name, s in signoffs.items():
    print(f"  {name:10s} {s['geometry_id']}  #{s['sign_off_id']}  "
          f"token {s['token']}  digest {s['spec_digest']}")

print("\n=== Step 2: production packages ===")
for name, gid in (("spine", SPINE), ("crossbeam", CB), ("cradle", CRADLE)):
    pkg = eng.export_production_package(
        gid, reason=f"jetpack {name} released for fabrication")
    print(f"  {name:10s} -> {pkg.get('package_dir', pkg)}")

print("\n=== Step 3: BOM ===")
print("  HONEST LIMIT: this project's price book holds real captured prices "
      "for Bolt Depot fasteners and a few OnlineMetals STEEL bars only. It "
      "contains NO 6061 aluminium stock and no steel plate in the sizes this "
      "design needs. Rather than invent prices, the BOM below prices the "
      "fasteners and the machining, and the raw material is listed as an "
      "unpriced buy-out with the exact stock size a supplier must quote.")

RAW_MATERIAL_TO_QUOTE = [
    ("spine", "6061-T6511 aluminium flat bar, 50.8 x 25.4 mm x 450 mm", 1),
    ("crossbeam", "6061-T6511 aluminium flat bar, 50.8 x 19.05 mm x 1280 mm "
                  "plus 240 x 19.05 x 50.8 mm doubler", 1),
    ("cradle", "1018 carbon steel plate, 8 mm x 175 x 175 mm", 4),
]
for ref, desc, qty in RAW_MATERIAL_TO_QUOTE:
    print(f"    UNPRICED  {qty}x  {ref:10s} {desc}")

bom_spec = {
    "quantity": 1,
    "lines": [
        {"ref": "cradle-bolt", "kind": "catalog",
         "sku": "boltdepot-23108", "per_assembly": 4},
        {"ref": "cradle-nut", "kind": "catalog",
         "sku": "boltdepot-6883", "per_assembly": 4},
        {"ref": "cradle-washer", "kind": "catalog",
         "sku": "boltdepot-27842", "per_assembly": 4},
        {"ref": "cradle-machining", "kind": "process", "per_assembly": 1,
         "rate_usd_per_hr": 75.0, "minutes_each": 35.0, "setup_minutes": 30.0,
         "description": "waterjet 148.4mm bore + 4x M6 clearance holes, deburr",
         "source": "shop rate assumption, not a supplier quotation"},
    ],
}
try:
    bom = eng.generate_bom(
        CRADLE, bom_spec,
        reason="jetpack engine cradle: fasteners and machining, one cradle")
    print(f"\n  cradle BOM total: ${bom['as_specified_total_usd']:.2f} per cradle "
          f"(x4 cradles = ${bom['as_specified_total_usd']*4:.2f})")
    for line in bom["lines"]:
        print(f"    {line['ref']:18s} {line.get('sku') or '-':22s} "
              f"${line['total_usd']:8.2f}  {line['description'][:44]}")
    if bom.get("price_book_stale"):
        print(f"  NOTE: {bom['price_book_stale']}")
except SourcingError as exc:
    print(f"  BOM refused: {exc}")
    bom = None

print("\n=== Step 4: assembly viewer ===")
viewer = eng.generate_assembly_viewer(
    ASM, reason="full jetpack assembly: spine + crossbeam + 4 engine cradles, "
                "every component signed off")
print(f"  {viewer['viewer_path']}")
print(f"  {viewer['size_kb']:.1f} kB, {viewer['triangles']} triangles, "
      f"{len(viewer.get('components', []))} components")

print("\n=== Step 5: report ===")
report = eng.generate_report()
print(f"  {report}")

print("\n=== Delivered ===")
print(f"  assembly       {ASM}")
print(f"  total mass     {mp['total_mass_kg']:.1f} kg")
print(f"  thrust         {mp['thrust_magnitude_N']:.0f} N   "
      f"T/W {mp['thrust_to_weight']:.3f}")
print(f"  thrust-CG miss {mp['thrust_cg_offset_mm']:.1f} mm  -> pilot holds a "
      f"{mp['pilot_trim']['required_cg_shift_mm']:.0f} mm CG shift "
      f"(authority {mp['pilot_trim']['available_cg_shift_mm']:.0f} mm)")
print(f"  crossbeam max permissible service temperature {t_cb:.0f} C "
      f"- MUST be demonstrated by test before any build")
