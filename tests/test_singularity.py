"""Geometric stress-singularity detection (roadmap A2).

The regression this locks down is the most consequential error the project has
made: `P0047@v1` reported FEA SF 3.844 with its peak sitting 1.28 mm off a
sharp 270-degree re-entrant corner, where linear elasticity has no finite
stress at all. The existing stress-outlier heuristic read 1.633 — inside its
"physically sound" band — because it detects constraint singularities, which
are decoupled from the bulk field, and a geometric singularity is not.

So the tests that matter here are the discriminations:
  - convex edges are NOT singular (a box corner has finite stress)
  - a tangent blend is NOT an edge (filleting removes the singularity)
  - a peak far from a singular edge is clean even when the solid has them
  - an analysis that could not run says `unknown`, never `clean`
"""

import json
import math
from pathlib import Path

import pytest

from design_engine.geometry import build
from design_engine.singularity import (DEFAULT_RADIUS_ELEMENTS, TANGENT_TOL_DEG,
                                       _williams_exponent, classify_peak,
                                       sharp_concave_edges)

_PARTS = Path(__file__).parent.parent / "data" / "parts"

BOX = {"name": "plain-box", "units": "mm",
       "features": [{"op": "box", "x": 40.0, "y": 20.0, "z": 100.0}]}

# A T-junction: a wider plate unioned onto a bar. This is the topology that
# produced the jetpack's singular corner.
TEE = {"name": "tee", "units": "mm", "features": [
    {"op": "box", "x": 40.0, "y": 20.0, "z": 100.0},
    {"op": "box", "x": 160.0, "y": 20.0, "z": 25.0,
     "at": [0, 0, 40.0], "mode": "union"}]}

JUNCTION_SEL = {"parallel_to": "Y", "at": {"x": [-20.0, 20.0], "z": [40.0, 65.0]},
                "tol": 0.01}


def _solid(spec):
    return build(spec).val()


# ------------------------------------------------------- the physics constant
def test_williams_exponent_matches_the_published_value():
    """A 270-degree re-entrant corner gives sigma ~ r**-0.4555.

    This is the classic L-shaped-domain eigenvalue. Recovering it from the
    characteristic equation by bisection — rather than hard-coding it — is what
    makes the exponent trustworthy at other angles.
    """
    assert _williams_exponent(270.0) == pytest.approx(0.4555, abs=0.001)


def test_no_singularity_at_or_below_a_flat_face():
    """180 degrees is flat and 90 is a convex corner; neither is singular."""
    assert _williams_exponent(180.0) == 0.0
    assert _williams_exponent(90.0) == 0.0
    assert _williams_exponent(1.0) == 0.0


def test_sharper_re_entrant_corners_are_stronger_singularities():
    """A crack (360 degrees) is the limiting case at exp -> 0.5."""
    mild = _williams_exponent(200.0)
    sharp = _williams_exponent(270.0)
    crack = _williams_exponent(359.9)
    assert 0 < mild < sharp < crack
    assert crack == pytest.approx(0.5, abs=0.01)


# ------------------------------------------------------------ edge detection
def test_a_plain_box_has_no_re_entrant_edges():
    """Every edge of a box is convex. Stress at an outside corner is finite,
    and flagging them would make the check useless by crying wolf."""
    assert sharp_concave_edges(_solid(BOX)) == []


def test_a_t_junction_has_re_entrant_edges_at_270_degrees():
    edges = sharp_concave_edges(_solid(TEE))
    assert edges, "unioning a plate onto a bar must produce re-entrant corners"
    assert all(e["interior_angle_deg"] == pytest.approx(270.0, abs=0.5)
               for e in edges)
    assert all(e["singularity_exponent"] == pytest.approx(0.4555, abs=0.001)
               for e in edges)


def test_filleting_removes_the_edge_it_blends():
    """The whole premise: a fillet does not soften the corner, it replaces it
    with a tangent-continuous blend, so the sharp edge leaves the topology."""
    sharp = _solid(TEE)
    filleted = _solid({**TEE, "features": TEE["features"] + [
        {"op": "fillet", "radius": 5.0, "edges": JUNCTION_SEL}]})

    def at_junction(solid):
        return [e for e in sharp_concave_edges(solid)
                if abs(abs(e["point"][0]) - 20.0) < 0.5
                and any(abs(e["point"][2] - z) < 0.5 for z in (40.0, 65.0))]

    assert at_junction(sharp), "the unfilleted junction must be detected"
    assert not at_junction(filleted), "the fillet must remove that edge"


def test_edges_carry_their_endpoints_for_distance_measurement():
    """A 1280 mm edge sampled at its midpoint would read 600 mm away from a
    peak sitting on its end. Distance must be point-to-segment."""
    for e in sharp_concave_edges(_solid(TEE)):
        assert len(e["p0"]) == 3 and len(e["p1"]) == 3
        assert math.dist(e["p0"], e["p1"]) == pytest.approx(e["length_mm"],
                                                            rel=1e-6)


# --------------------------------------------------------------- peak verdict
def test_a_peak_on_a_re_entrant_edge_is_reported_singular():
    solid = _solid(TEE)
    edges = sharp_concave_edges(solid)
    on_edge = list(edges[0]["p0"])
    v = classify_peak(solid, on_edge, mesh_size_mm=3.2, edges=edges)
    assert v["verdict"] == "singular"
    assert v["singularity_exponent"] == pytest.approx(0.4555, abs=0.001)
    assert "does not" not in v["reason"]
    assert "refining will only raise it" in v["reason"]


def test_a_peak_far_from_every_edge_is_clean_even_when_edges_exist():
    """Presence of a singularity somewhere does not condemn every result on
    the part — only a peak sitting in one."""
    solid = _solid(TEE)
    v = classify_peak(solid, [78.0, 0.0, 52.5], mesh_size_mm=3.2)
    assert v["verdict"] == "clean"
    assert v["singular_edges"] > 0          # they exist, just not here
    assert v["nearest_mm"] > 2 * 3.2


def test_a_box_gives_clean_with_no_edges_at_all():
    v = classify_peak(_solid(BOX), [0.0, 0.0, 50.0], mesh_size_mm=3.2)
    assert v["verdict"] == "clean"
    assert v["singular_edges"] == 0


def test_distance_is_measured_to_the_segment_not_the_sample_point():
    """The bug this prevents: a long edge whose recorded sample point is far
    from the peak, while the edge itself passes right through it."""
    solid = _solid(TEE)
    edges = [e for e in sharp_concave_edges(solid) if e["length_mm"] > 15.0]
    assert edges, "need a long edge for this test to mean anything"
    e = edges[0]
    end = list(e["p0"])
    assert math.dist(end, e["point"]) > 3.0, "sample point must differ from end"
    v = classify_peak(solid, end, mesh_size_mm=1.0, edges=[e])
    assert v["verdict"] == "singular"
    assert v["nearest_mm"] < 1e-3


def test_the_search_radius_scales_with_element_size():
    """A coarse mesh smears the singular field over a wider region, so the
    radius is expressed in elements rather than millimetres."""
    solid = _solid(TEE)
    edges = sharp_concave_edges(solid)
    near = list(edges[0]["p0"])
    near[0] += 5.0                       # 5 mm off the edge, along x
    assert classify_peak(solid, near, mesh_size_mm=1.0,
                         edges=edges)["verdict"] == "clean"
    assert classify_peak(solid, near, mesh_size_mm=4.0,
                         edges=edges)["verdict"] == "singular"


def test_unanalysable_geometry_is_unknown_never_clean():
    """Reporting 'clean' for an analysis that did not happen is precisely the
    failure this module exists to prevent."""
    class Exploding:
        def Faces(self):
            raise RuntimeError("kernel unavailable")

    v = classify_peak(Exploding(), [0, 0, 0], mesh_size_mm=3.2)
    assert v["verdict"] == "unknown"
    assert "could not be analysed" in v["reason"]


# ------------------------------------------------------- the real regression
@pytest.mark.skipif(not (_PARTS / "P0047" / "v1" / "spec.json").is_file(),
                    reason="P0047 not in this working copy")
def test_P0047_the_original_false_negative_is_now_caught():
    """SF 3.844 was reported for a peak on a 270-degree corner, and every
    check in the engine passed it. This is that exact case."""
    solid = _solid(json.loads((_PARTS / "P0047" / "v1" / "spec.json").read_text()))
    v = classify_peak(solid, [-23.505, 4.014, 199.6], mesh_size_mm=3.2)
    assert v["verdict"] == "singular"
    assert v["nearest_mm"] == pytest.approx(1.28, abs=0.05)
    assert v["interior_angle_deg"] == pytest.approx(270.0, abs=0.5)


@pytest.mark.skipif(not (_PARTS / "P0048" / "v1" / "spec.json").is_file(),
                    reason="P0048 not in this working copy")
def test_P0048_the_filleted_peak_is_clear_of_every_singular_edge():
    """The fillet moved the peak onto the blend surface. Note this asserts the
    PEAK is clear, not that the part is free of singularities — it is not.
    The fillet blends the x-z profile and leaves sharp edges where it runs out
    against the side walls at y = +/-9.525."""
    solid = _solid(json.loads((_PARTS / "P0048" / "v1" / "spec.json").read_text()))
    v = classify_peak(solid, [29.513, -0.024, 199.225], mesh_size_mm=3.2)
    assert v["verdict"] == "clean"
    assert v["singular_edges"] > 0
    assert v["nearest_mm"] > DEFAULT_RADIUS_ELEMENTS * 3.2
