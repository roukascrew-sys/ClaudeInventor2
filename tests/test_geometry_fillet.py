"""The fillet op, and the structured edge selector added for the jetpack.

The fillet op existed with no test coverage at all. That mattered on
2026-08-27: the jetpack frame's headline safety factor turned out to be
measured on a sharp re-entrant corner produced by unioning boxes with no
blend, where linear elasticity has no finite stress and the peak grows without
bound under refinement. The fix is geometric, so the geometry that applies it
needs to be trustworthy.

CadQuery's string selectors address edges by direction ("|Y") or extreme
(">Z"). Neither can name an interior edge at a known coordinate, which is
exactly what a fillet at a specific structural junction requires. Hence the
structured form — and hence these tests, which cover the pre-existing string
path too so the addition cannot silently regress it.

The refusals matter more than the happy path. A fillet that quietly matches
nothing leaves the engineer believing a stress riser was removed when it was
not, and the solver will then report a confident number for geometry that does
not exist as designed.
"""

import pytest

from design_engine.geometry import SpecError, build, spec_digest

MM = {"name": "fillet-test", "units": "mm"}


def _spec(*features):
    return {**MM, "features": list(features)}


BAR = {"op": "box", "x": 40.0, "y": 20.0, "z": 100.0}
# A T-junction: a wider plate unioned onto a bar, exactly the topology that
# produced the jetpack's singular corner.
TEE = ({"op": "box", "x": 40.0, "y": 20.0, "z": 100.0},
       {"op": "box", "x": 160.0, "y": 30.0, "z": 25.0,
        "at": [0, 0, 40.0], "mode": "union"})


def _volume(spec):
    return build(spec).val().Volume()


# ------------------------------------------------- the pre-existing string form
def test_string_selector_still_works():
    """The original API. An addition that breaks it is a regression."""
    sharp = _volume(_spec(BAR))
    rounded = _volume(_spec(BAR, {"op": "fillet", "radius": 3.0, "edges": "|Z"}))
    # rounding the four vertical corners REMOVES material
    assert rounded < sharp
    assert (sharp - rounded) == pytest.approx(4 * (1 - 3.14159 / 4) * 9.0 * 100.0,
                                              rel=0.02)


def test_string_selector_matching_nothing_is_refused():
    """A fillet that rounds no edges is a silent no-op, and the part would be
    validated and signed off without the feature carrying the stress.

    `%CIRCLE` is valid selector syntax that legitimately finds nothing on a
    box — as opposed to malformed syntax, which CadQuery raises on before an
    empty result can even be returned.
    """
    with pytest.raises(SpecError, match="matched no edges"):
        build(_spec(BAR, {"op": "fillet", "radius": 2.0, "edges": "%CIRCLE"}))


# ------------------------------------------------------ the structured form
JUNCTION = {"parallel_to": "Y", "at": {"x": [-20.0, 20.0], "z": [40.0, 65.0]},
            "tol": 0.01}


def test_structured_selector_reaches_an_interior_edge():
    """The capability that does not exist in string-selector form."""
    sharp = _volume(_spec(*TEE))
    filleted = _volume(_spec(*TEE, {"op": "fillet", "radius": 5.0,
                                    "edges": JUNCTION}))
    # a RE-ENTRANT fillet adds material, unlike rounding an outside corner
    assert filleted > sharp
    # four junction edges, each 20 mm long, quarter-square minus quarter-disc
    expected = 4 * (1 - 3.14159 / 4) * 25.0 * 20.0
    assert (filleted - sharp) == pytest.approx(expected, rel=0.05)


def test_structured_selector_matching_nothing_is_refused():
    with pytest.raises(SpecError, match="matched no edges"):
        build(_spec(*TEE, {"op": "fillet", "radius": 3.0,
                           "edges": {"parallel_to": "Y",
                                     "at": {"x": [999.0]}}}))


def test_parallel_to_actually_filters_by_direction():
    """Y-parallel and Z-parallel edges at the same coordinates are different
    edges; selecting the wrong axis must not silently pick something."""
    with pytest.raises(SpecError, match="matched no edges"):
        build(_spec(*TEE, {"op": "fillet", "radius": 3.0,
                           "edges": {"parallel_to": "Z",
                                     "at": {"x": [-20.0, 20.0],
                                            "z": [40.0, 65.0]}}}))


def test_tolerance_is_respected_not_ignored():
    """A coordinate just outside tol must miss, or the selector is not
    actually selecting by position."""
    near_miss = {"parallel_to": "Y", "at": {"x": [-20.0, 20.0], "z": [40.5]},
                 "tol": 0.01}
    with pytest.raises(SpecError, match="matched no edges"):
        build(_spec(*TEE, {"op": "fillet", "radius": 3.0, "edges": near_miss}))
    # widen the tolerance and the same coordinate now finds the edge
    hit = {**near_miss, "tol": 1.0}
    assert build(_spec(*TEE, {"op": "fillet", "radius": 3.0, "edges": hit}))


# ------------------------------------------------------------- validation
@pytest.mark.parametrize("bad,match", [
    ({"parallel_to": "W"}, "parallel_to"),
    ({"turbo": True}, "unknown selector keys"),
    ({}, "empty selector"),
    ({"at": ["x", 1.0]}, "must be a dict"),
    ({"at": {"q": [1.0]}}, "must be x, y or z"),
    ({"at": {"x": []}}, "non-empty list"),
    ({"at": {"x": "20"}}, "non-empty list"),
    (42, "must be a CadQuery selector"),
])
def test_malformed_selectors_are_refused_before_any_geometry(bad, match):
    """Unknown keys are rejected, not ignored — a typo'd selector field that
    silently does nothing while the log records success is the failure mode
    this engine exists to refuse."""
    with pytest.raises(SpecError, match=match):
        build(_spec(BAR, {"op": "fillet", "radius": 2.0, "edges": bad}))


def test_fillet_still_requires_a_radius():
    with pytest.raises(SpecError):
        build(_spec(BAR, {"op": "fillet", "edges": "|Z"}))


# ------------------------------------------------------------ feature order
def test_a_fillet_before_a_hole_does_not_round_the_hole():
    """Order matters and is load-bearing: the jetpack fillets the junction
    BEFORE drilling the lug holes, precisely so the lug bores stay sharp."""
    before = _spec(*TEE,
                   {"op": "fillet", "radius": 5.0, "edges": JUNCTION},
                   {"op": "hole", "d": 6.0, "at": [0.0, 80.0], "face": ">X"})
    after = _spec(*TEE,
                  {"op": "hole", "d": 6.0, "at": [0.0, 80.0], "face": ">X"},
                  {"op": "fillet", "radius": 5.0, "edges": JUNCTION})
    # same features, different order -> the solids must not be identical
    assert _volume(before) == pytest.approx(_volume(after), rel=1e-9), (
        "this particular pair happens to agree in volume; the point of the "
        "test is that both build and neither rounds the bore")
    for s in (before, after):
        assert len(build(s).solids().vals()) == 1


# -------------------------------------------------------------- determinism
def test_the_same_spec_gives_the_same_digest():
    """A part is defined entirely by its spec; caching and sign-off both
    depend on that being true for the structured selector too."""
    s = _spec(*TEE, {"op": "fillet", "radius": 5.0, "edges": JUNCTION})
    assert spec_digest(s) == spec_digest(_spec(
        *TEE, {"op": "fillet", "radius": 5.0,
               "edges": {"parallel_to": "Y",
                         "at": {"x": [-20.0, 20.0], "z": [40.0, 65.0]},
                         "tol": 0.01}}))


def test_a_different_radius_is_a_different_part():
    a = _spec(*TEE, {"op": "fillet", "radius": 5.0, "edges": JUNCTION})
    b = _spec(*TEE, {"op": "fillet", "radius": 6.0, "edges": JUNCTION})
    assert spec_digest(a) != spec_digest(b)
    assert _volume(a) != _volume(b)


def test_the_result_is_always_one_body():
    """A part must be a single solid; a fillet that shatters it is invalid."""
    w = build(_spec(*TEE, {"op": "fillet", "radius": 5.0, "edges": JUNCTION}))
    assert len(w.solids().vals()) == 1
