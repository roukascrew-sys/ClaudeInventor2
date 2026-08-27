"""Phase 5 contract tests — the sign-off lock.

Every refusal path is exercised: unsigned, unvalidated, failed-validation,
post-sign-off tamper, and new-version-needs-new-signature. The lock must be
code (SignOffRequired raised + fail row logged), never a skippable step.
"""

import json

import pytest

from design_engine import DesignEngine
from design_engine.production import SignOffError, SignOffRequired

BAR = {
    "name": "signed-bar",
    "units": "mm",
    "features": [{"op": "box", "x": 10, "y": 10, "z": 100}],
}

S235 = {"name": "S235JR", "E_MPa": 210000, "nu": 0.3, "yield_MPa": 235,
        "source": "EN 10025-2 nominal values, t<=16mm"}


def _case(force_z, required_sf=2.0):
    return {
        "material": dict(S235),
        "mesh": {"max_size_mm": 5.0},
        "constraints": [{"where": {"axis": "z", "at": "min"}, "dof": [1, 2, 3]}],
        "loads": [{"where": {"axis": "z", "at": "max"},
                   "force_total_N": [0, 0, force_z]}],
        "limit_state": {"name": "yield_von_mises", "required_SF": required_sf},
    }


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    return DesignEngine(tmp_path_factory.mktemp("p5") / "data")


@pytest.fixture
def validated_gid(eng, request):
    """A fresh, FEA-validated, *unsigned* geometry version.

    Function-scoped on purpose. Sign-off state is per-version and the tests
    below mutate it (sign it, tamper with the spec, invalidate it with a failed
    re-check). A module-scoped part made every test's precondition depend on
    which other tests had already run in the same process, so a test could pass
    in file order and fail alone or under -k. Each test now establishes its own
    precondition; the engine (and its log) stays module-scoped.
    """
    gid = eng.create_part({**BAR, "name": f"signed-bar-{request.node.name}"},
                          reason="sign-off test bar")["geometry_id"]
    run = eng.run_fea_static(gid, _case(1000),
                             reason="validate before sign-off tests")
    assert run["result"] == "pass"
    return gid


@pytest.fixture
def signed_gid(eng, validated_gid):
    """`validated_gid` with a sign-off explicitly recorded against it — the
    precondition for every test whose subject is what *invalidates* one."""
    eng.sign_off(validated_gid, "Gideon",
                 "approve signed-bar v1 for prototype production, "
                 "500 N service load envelope")
    return validated_gid


def test_production_locked_without_sign_off(eng, validated_gid):
    with pytest.raises(SignOffRequired, match="sign_off_missing"):
        eng.export_production_package(validated_gid, reason="premature export")
    row = eng.log.rows(action="export_production_package", result="fail")[-1]
    assert "sign_off_missing" in row["failure_mode"]


def test_sign_off_refused_without_validation(eng):
    gid = eng.create_part({**BAR, "name": "never-validated"},
                          reason="unvalidated part")["geometry_id"]
    with pytest.raises(SignOffError, match="no validation run"):
        eng.sign_off(gid, "Gideon", "approve for production")
    assert eng.log.rows(action="sign_off", result="fail")


def test_sign_off_refused_with_blank_signer(eng, validated_gid):
    with pytest.raises(SignOffError, match="name"):
        eng.sign_off(validated_gid, "  ", "approve")


def test_sign_off_then_production_package(eng, validated_gid):
    out = eng.sign_off(validated_gid, "Gideon",
                       "approve signed-bar v1 for prototype production, "
                       "500 N service load envelope")
    assert len(out["token"]) == 16
    row = eng.log.rows(action="sign_off", result="pass")[-1]
    assert row["signed_off_by"] == "Gideon"
    det = json.loads(row["details_json"])
    assert det["validated_safety_factor"] > 2.0

    pkg = eng.export_production_package(validated_gid,
                                        reason="release prototype package")
    assert sorted(pkg["files"]) == ["part.step", "sign_off_certificate.json",
                                    "spec.json", "validation_summary.json"]
    cert = json.loads((eng.root / "production" /
                       validated_gid.replace("@", "_") /
                       "sign_off_certificate.json").read_text())
    assert cert["signed_off_by"] == "Gideon"
    assert cert["token"] == out["token"]
    summary = json.loads((eng.root / "production" /
                          validated_gid.replace("@", "_") /
                          "validation_summary.json").read_text())
    assert summary["limit_state"] == "yield_von_mises"
    assert summary["material"]["source"].startswith("EN 10025-2")


def test_new_version_needs_new_sign_off(eng, validated_gid):
    new_gid = eng.edit_part(validated_gid, {"features.0.x": 12},
                            reason="post-sign-off change")["new_geometry_id"]
    with pytest.raises(SignOffRequired, match="sign_off_missing"):
        eng.export_production_package(new_gid, reason="unsigned new version")


def test_tampered_spec_invalidates_sign_off(eng, signed_gid):
    spec_path = (eng.parts.root / signed_gid.split("@")[0]
                 / f"v{signed_gid.split('@v')[1]}" / "spec.json")
    spec = json.loads(spec_path.read_text())
    spec["features"][0]["x"] = 9.5  # silent post-sign-off tamper
    spec_path.write_text(json.dumps(spec, indent=2))
    try:
        # "sign_off_invalid:" with the colon — bare "sign_off_invalid" is also
        # a prefix of "sign_off_invalidated", the *validation* refusal path.
        with pytest.raises(SignOffRequired, match="sign_off_invalid:"):
            eng.export_production_package(signed_gid,
                                          reason="export after tamper")
    finally:
        spec["features"][0]["x"] = 10
        spec_path.write_text(json.dumps(spec, indent=2))
    # restored spec: the sign-off is whole again
    eng.export_production_package(signed_gid, reason="export after restore")


def test_later_failed_validation_invalidates_sign_off(eng, signed_gid):
    run = eng.run_fea_static(
        signed_gid, _case(100000, required_sf=1.5),
        reason="overload re-check after sign-off must invalidate the token")
    assert run["result"] == "fail"
    with pytest.raises(SignOffRequired, match="sign_off_invalidated"):
        eng.export_production_package(signed_gid,
                                      reason="export after failed re-check")
    row = eng.log.rows(action="export_production_package", result="fail")[-1]
    assert "sign_off_invalidated" in row["failure_mode"]
