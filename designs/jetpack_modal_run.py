"""Does the jetpack frame resonate with its own engines?

Roadmap B2, and the plan's "if only one thing gets done" item. Four JetCat
P400-PRO turbines run at up to 98,000 rpm — 1633 Hz at the shaft — bolted to a
frame whose natural frequencies have never been computed. Every static result
on this frame, including the validated SF 4.633, rests on the unexamined
assumption that no mode sits near that.

If a mode IS near it, the static answer is wrong in a way no amount of mesh
refinement would reveal: the frame would be driven at resonance and the true
stress amplitude is the static one multiplied by a damping-limited
amplification factor, which for a lightly damped aluminium weldment can be
one to two orders of magnitude.

HARMONICS. A rotor excites at its running speed and at multiples of it, so a
mode at 2x shaft frequency is as dangerous as one at 1x. This checks the first
three.

WHAT THIS RUN IS NOT. Free-free vibration of the bare frame with the harness
lugs pinned. It does not carry the engine masses, the fuel, or the pilot, all
of which lower the frequencies substantially — a 3.65 kg engine hung on a
3.9 kg frame is not a perturbation. The numbers here are therefore an UPPER
BOUND on the frame's modes, and the real structure sits lower. That is stated
rather than corrected because point masses are not yet supported in the modal
deck, and inventing an equivalent stiffness would be worse than reporting the
bound honestly.

    .venv\\Scripts\\python.exe designs\\jetpack_modal_run.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, '.')
from design_engine import DesignEngine
from design_engine.fea import ValidationTools
import designs.jetpack_optimization_run as J

# JetCat P400-PRO maximum shaft speed. Sourced with the rest of the engine
# data in the Jetpack Frame vault note.
MAX_RPM = 98_000.0
SHAFT_HZ = MAX_RPM / 60.0
HARMONICS = 3
# 20% separation is the usual first-pass rotordynamic screen. Not a standard
# with a clause number behind it — stated as the assumption it is.
REQUIRED_SEPARATION = 0.20
OUT = Path("data/modal/jetpack.json")


def find_part(spec, parts_root=Path("data/parts")):
    from design_engine.geometry import spec_digest
    want = spec_digest(spec)
    for props in sorted(parts_root.glob("P*/v*/props.json")):
        try:
            if json.loads(props.read_text())["spec_digest"] == want:
                return f"{props.parent.parent.name}@{props.parent.name}"
        except (KeyError, json.JSONDecodeError):
            continue
    return None


def main() -> int:
    eng = DesignEngine(J.ROOT)
    eng.validation = ValidationTools(J.ROOT, eng.log, eng.parts,
                                     eng.validation.ccx_path,
                                     solve_timeout_s=3600)

    space = J.make_space()
    res = json.load(open('data/optimization/jetpack/result.json'))
    front = sorted(res['front'],
                   key=lambda c: c['result']['metrics']['frame_mass_kg'])
    v = space.resolve(front[0]['values'])
    spec = J.build_spec(v, None)
    gid = find_part(spec)
    if gid is None:
        gid = eng.parts.create_part(
            spec, reason="modal analysis of the lightest feasible frame"
        )["geometry_id"]

    mat = dict(J.MATERIALS[v["material"]])
    print(f"MODAL — {gid}, {mat['name']}")
    print(f"  excitation {SHAFT_HZ:.1f} Hz ({MAX_RPM:,.0f} rpm), "
          f"{HARMONICS} harmonics, {REQUIRED_SEPARATION:.0%} separation required")
    print(f"  NOTE: bare frame. Engine, fuel and pilot masses are NOT carried,")
    print(f"        so these are an UPPER BOUND — the real modes sit lower.\n")

    cb_h = float(v["cb_height"])
    case = {
        "material": {k: val for k, val in mat.items()
                     if k in ("name", "E_MPa", "nu", "yield_MPa", "source",
                              "density_kg_m3", "service_temp_C",
                              "yield_derate_curve", "E_derate_curve",
                              "derate_source")},
        "mesh": {"max_size_mm": 3.2},
        "constraints": [
            {"where": {"cylinder": {"axis": "x",
                                    "center": [0.0, J.SPINE_Z - 50.0],
                                    "r": J.LUG_D / 2.0, "tol": 0.8}},
             "dof": [1, 2, 3]},
            {"where": {"cylinder": {"axis": "x", "center": [0.0, 40.0],
                                    "r": J.LUG_D / 2.0, "tol": 0.8}},
             "dof": [1, 2, 3]},
        ],
        "loads": [],
        "limit_state": {"name": "resonance_separation",
                        "required_SF": REQUIRED_SEPARATION,
                        "excitation_hz": SHAFT_HZ,
                        "harmonics": HARMONICS},
    }

    out = eng.validation.fea_modal(
        gid, case, reason=(
            f"B2: do the frame's modes clear {MAX_RPM:,.0f} rpm turbine "
            f"excitation and its first {HARMONICS} harmonics"),
        n_modes=20)

    freqs = out["mode_frequencies_hz"]
    print(f"  {len(freqs)} modes, {freqs[0]:.1f} Hz to {freqs[-1]:.1f} Hz\n")
    print(f"  {'mode':>5} {'Hz':>10}   nearest harmonic")
    print("  " + "-" * 46)
    for i, f in enumerate(freqs, start=1):
        k = max(1, min(HARMONICS, round(f / SHAFT_HZ)))
        sep = abs(f - SHAFT_HZ * k) / (SHAFT_HZ * k)
        flag = "  <-- CLASH" if sep < REQUIRED_SEPARATION else ""
        print(f"  {i:>5} {f:>10.1f}   {k}x = {SHAFT_HZ*k:8.1f} Hz  "
              f"{sep*100:6.1f}%{flag}")

    print(f"\n  RESULT: {out['result'].upper()}   "
          f"closest separation {out['safety_factor']*100:.1f}% "
          f"(required {REQUIRED_SEPARATION:.0%})")
    for c in out["clashes"]:
        print(f"    mode {c['mode']} at {c['mode_hz']:.1f} Hz vs harmonic "
              f"{c['harmonic']} ({c['excitation_hz']:.1f} Hz): "
              f"{c['separation']*100:.1f}%")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "geometry_id": gid, "shaft_hz": SHAFT_HZ, "harmonics": HARMONICS,
        "required_separation": REQUIRED_SEPARATION,
        "mode_frequencies_hz": freqs, "result": out["result"],
        "closest_separation": out["safety_factor"],
        "clashes": out["clashes"],
        "caveat": ("bare frame; engine, fuel and pilot masses not carried, so "
                   "these frequencies are an upper bound"),
    }, indent=2), encoding="utf-8")
    print(f"\n  written: {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
