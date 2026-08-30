"""Deterministic spec -> CadQuery solid builder.

A part is defined entirely by its spec dict; the same spec always produces the
same solid. Nothing here mutates state or logs — parts.py owns that.

Spec format (v0, units: mm only):

    {
      "name": "bracket",              # required, [a-z0-9-_]+
      "units": "mm",                  # required, only "mm" accepted
      "density_kg_m3": 7850,          # optional -> mass estimate in props
      "features": [                   # applied in order; first must be a base
        {"op": "box", "x": 20, "y": 30, "z": 10},
        {"op": "cylinder", "d": 8, "h": 12, "at": [0, 0, 0], "mode": "union"},
        {"op": "hole", "d": 5, "at": [0, 0], "face": ">Z"},
        {"op": "fillet", "radius": 1.5, "edges": "|Z"}
      ]
    }

Conventions:
- box: centered in x/y, base on z=0, height +z. Optional "at" [x,y,z] offset.
- cylinder: axis +z, base on z=0, centered at origin. Optional "at" offset.
- box/cylinder after the first feature need "mode": "union" or "cut".
- hole: through-everything, drilled normal to the selected face (default ">Z"),
  "at" is [x, y] in that face's workplane coordinates.
- fillet: applied to the solid as it stands at that point in the feature list,
  so a fillet placed before a hole does not round the hole. "edges" is either

    a CadQuery selector string   {"op": "fillet", "radius": 1.5, "edges": "|Z"}

  or a structured selector, for edge sets a string cannot express:

    {"op": "fillet", "radius": 10.0,
     "edges": {"parallel_to": "Y",
               "at": {"x": [-22.225, 22.225], "z": [199.6, 250.4]},
               "tol": 0.01}}

  `parallel_to` keeps only edges running along that axis; `at` keeps only
  those whose midpoint matches one of the listed coordinates on each named
  axis, within `tol`. Added because CadQuery's string selectors address edges
  by extreme or direction ("|Y", ">Z") and cannot say "the four re-entrant
  edges where the doubler pad meets the spine" — which is exactly the fillet
  that governs the jetpack frame's peak stress.

  A fillet whose selector matches NO edges is refused, for the same reason a
  hole that removes no material is refused: silently doing nothing while the
  log records success is the worst failure this engine can have.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

import cadquery as cq


class SpecError(ValueError):
    """The spec is malformed — caught before any geometry is built."""


class GeometryError(RuntimeError):
    """The spec was well-formed but the geometry kernel could not build it."""


_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-_]*$")
_BASE_OPS = ("box", "cylinder")
_ALL_OPS = ("box", "cylinder", "hole", "fillet")
# Unknown keys are rejected, not ignored: a typo'd dimension name that silently
# does nothing while the log records a successful edit is a worst-case failure.
_ALLOWED_KEYS = {
    "box": {"op", "x", "y", "z", "at", "mode"},
    "cylinder": {"op", "d", "h", "at", "mode"},
    "hole": {"op", "d", "at", "face"},
    "fillet": {"op", "radius", "edges"},
}
_ALLOWED_TOP = {"name", "units", "features", "density_kg_m3", "notes", "description"}


def canonical_json(spec: dict) -> str:
    return json.dumps(spec, sort_keys=True, separators=(",", ":"))


def spec_digest(spec: dict) -> str:
    return hashlib.sha256(canonical_json(spec).encode()).hexdigest()[:12]


def _require(feat: dict, idx: int, key: str, kind=(int, float), positive=True):
    if key not in feat:
        raise SpecError(f"features[{idx}] ({feat.get('op')}): missing '{key}'")
    val = feat[key]
    if not isinstance(val, kind) or isinstance(val, bool):
        raise SpecError(f"features[{idx}].{key}: expected number, got {val!r}")
    if positive and val <= 0:
        raise SpecError(f"features[{idx}].{key}: must be > 0, got {val!r}")
    return val


def validate_spec(spec: dict) -> None:
    if not isinstance(spec, dict):
        raise SpecError(f"spec must be a dict, got {type(spec).__name__}")
    name = spec.get("name")
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise SpecError(f"spec.name must match {_NAME_RE.pattern}, got {name!r}")
    if spec.get("units") != "mm":
        raise SpecError(f"spec.units must be 'mm', got {spec.get('units')!r}")
    extra_top = set(spec) - _ALLOWED_TOP
    if extra_top:
        raise SpecError(f"spec has unexpected keys: {sorted(extra_top)}")
    feats = spec.get("features")
    if not isinstance(feats, list) or not feats:
        raise SpecError("spec.features must be a non-empty list")
    for idx, feat in enumerate(feats):
        if not isinstance(feat, dict):
            raise SpecError(f"features[{idx}] must be a dict")
        op = feat.get("op")
        if op not in _ALL_OPS:
            raise SpecError(f"features[{idx}].op: unknown op {op!r}, supported: {_ALL_OPS}")
        extra = set(feat) - _ALLOWED_KEYS[op]
        if extra:
            raise SpecError(
                f"features[{idx}] ({op}): unexpected keys {sorted(extra)} — "
                f"allowed: {sorted(_ALLOWED_KEYS[op])}")
        if idx == 0:
            if op not in _BASE_OPS:
                raise SpecError(f"features[0] must be a base solid {_BASE_OPS}, got {op!r}")
            if feat.get("mode", "base") not in ("base",):
                raise SpecError("features[0] is the base solid; it takes no 'mode'")
        if op == "box":
            for k in ("x", "y", "z"):
                _require(feat, idx, k)
        elif op == "cylinder":
            _require(feat, idx, "d")
            _require(feat, idx, "h")
        elif op == "hole":
            _require(feat, idx, "d")
            at = feat.get("at")
            if not (isinstance(at, list) and len(at) == 2):
                raise SpecError(f"features[{idx}] (hole): 'at' must be [x, y]")
        elif op == "fillet":
            _require(feat, idx, "radius")
            _validate_edge_selector(feat.get("edges"), idx)
        if op in _BASE_OPS and idx > 0 and feat.get("mode") not in ("union", "cut"):
            raise SpecError(
                f"features[{idx}] ({op}): needs 'mode': 'union'|'cut' after the base")


_SEL_KEYS = {"parallel_to", "at", "tol", "sharp_concave"}
_AXES = ("X", "Y", "Z")


def _validate_edge_selector(sel: Any, idx: int) -> None:
    """A string selector, or a structured one — anything else is refused."""
    if isinstance(sel, str):
        return
    if not isinstance(sel, dict):
        raise SpecError(
            f"features[{idx}] (fillet): 'edges' must be a CadQuery selector "
            f"string or a structured selector dict, got {type(sel).__name__}")
    unknown = set(sel) - _SEL_KEYS
    if unknown:
        raise SpecError(
            f"features[{idx}] (fillet): unknown selector keys {sorted(unknown)}, "
            f"supported: {sorted(_SEL_KEYS)}")
    if not sel:
        raise SpecError(
            f"features[{idx}] (fillet): empty selector would match every edge; "
            f"say which edges explicitly")
    axis = sel.get("parallel_to")
    if axis is not None and axis not in _AXES:
        raise SpecError(
            f"features[{idx}] (fillet): 'parallel_to' must be one of {_AXES}, "
            f"got {axis!r}")
    at = sel.get("at", {})
    if not isinstance(at, dict):
        raise SpecError(f"features[{idx}] (fillet): 'at' must be a dict of axis -> values")
    for k, vals in at.items():
        if k not in ("x", "y", "z"):
            raise SpecError(
                f"features[{idx}] (fillet): 'at' axis {k!r} must be x, y or z")
        if not (isinstance(vals, list) and vals
                and all(isinstance(v, (int, float)) for v in vals)):
            raise SpecError(
                f"features[{idx}] (fillet): 'at'[{k!r}] must be a non-empty "
                f"list of numbers")


class _StructuredEdgeSelector(cq.selectors.Selector):
    """Select edges by orientation and position.

    CadQuery's string selectors pick edges by direction or by being extreme on
    an axis. Neither can name an interior edge at a known coordinate, which is
    what a fillet at a specific structural junction needs.
    """

    def __init__(self, sel: dict):
        self.axis = sel.get("parallel_to")
        self.at = sel.get("at", {})
        self.tol = float(sel.get("tol", 0.01))

    def _keep(self, edge) -> bool:
        bb = edge.BoundingBox()
        spans = {"X": bb.xlen, "Y": bb.ylen, "Z": bb.zlen}
        if self.axis:
            # runs along the named axis and is flat in the other two
            if spans[self.axis] <= self.tol:
                return False
            if any(spans[o] > self.tol for o in _AXES if o != self.axis):
                return False
        mid = {"x": (bb.xmin + bb.xmax) / 2.0,
               "y": (bb.ymin + bb.ymax) / 2.0,
               "z": (bb.zmin + bb.zmax) / 2.0}
        return all(
            any(abs(mid[k] - float(v)) <= self.tol for v in vals)
            for k, vals in self.at.items())

    def filter(self, objectList):
        return [o for o in objectList if self._keep(o)]


def _make_primitive(feat: dict) -> cq.Workplane:
    at = feat.get("at", [0, 0, 0])
    if feat["op"] == "box":
        wp = cq.Workplane("XY").box(
            feat["x"], feat["y"], feat["z"], centered=(True, True, False))
    else:  # cylinder
        wp = cq.Workplane("XY").circle(feat["d"] / 2.0).extrude(feat["h"])
    return wp.translate(tuple(at))


def build(spec: dict) -> cq.Workplane:
    """Build the solid. Raises SpecError (bad spec) or GeometryError (kernel)."""
    validate_spec(spec)
    try:
        result = _make_primitive(spec["features"][0])
        for idx, feat in enumerate(spec["features"][1:], start=1):
            op = feat["op"]
            if op in _BASE_OPS:
                prim = _make_primitive(feat)
                result = result.union(prim) if feat["mode"] == "union" else result.cut(prim)
            elif op == "hole":
                face = feat.get("face", ">Z")
                before = result.val().Volume()
                result = (
                    result.faces(face).workplane()
                    .pushPoints([tuple(feat["at"])]).hole(feat["d"])
                )
                # ORIGIN, verified empirically 2026-08-25 on P0026@v3: the
                # workplane CadQuery builds on a selected face uses
                # centerOption="ProjectedOrigin" — the GLOBAL origin projected
                # onto the face plane, NOT the face's own centre. So on an
                # axis-normal face the 'at' pair reads as the two global
                # coordinates that are not the face normal (on a '>X' face,
                # at=[y, z] in world mm). The natural assumption that 'at' is
                # measured from the middle of the face is WRONG and would put
                # every hole in the wrong place on any part whose faces are
                # not centred on the origin.
                #
                # A hole that removes NOTHING is a silent no-op: the 'at'
                # point missed the material on that face, usually because the
                # face's local (u, v) axes are not what the caller assumed
                # (they are face-relative and can be mirrored -- on a ">Y"
                # face, u runs along -X). Caught for real: a shoe bracket was
                # built, validated and nearly signed off with a bolt hole
                # that did not exist, because the op silently did nothing.
                # Same principle as rejecting unknown spec keys.
                removed = before - result.val().Volume()
                if removed <= 1e-9:
                    raise SpecError(
                        f"features[{idx}] (hole): removed no material - the "
                        f"'at' point {feat['at']} misses the solid on face "
                        f"{face!r}. Face-local (u, v) axes are not world x/y "
                        f"and may be mirrored; verify where the point lands "
                        f"before relying on it. A hole that silently cuts "
                        f"nothing is exactly the failure this engine refuses.")
            elif op == "fillet":
                sel = feat["edges"]
                if isinstance(sel, dict) and sel.get("sharp_concave"):
                    # DECLARATIVE, not imperative. This does not say "round
                    # the edge at these coordinates"; it says "no sharp
                    # concave edge shall survive with this radius", and asks
                    # singularity.py which edges those are.
                    #
                    # Written this way because the coordinate version is
                    # brittle in a way that matters. Hand-computing where the
                    # sharp edges sit worked for one frame and silently
                    # selected nothing for 15 of 81 designs in the same space,
                    # because which faces meet which depends on parameters -
                    # a doubler pad thicker than its crossbeam puts the edge
                    # on the crossbeam, a thinner one puts it on the pad. A
                    # selector that must be re-derived per design is a
                    # selector that will be wrong for some design.
                    from .singularity import sharp_concave_edges

                    solid = result.val()
                    flagged = sharp_concave_edges(solid)
                    if not flagged:
                        # Nothing to blend, and the POSTCONDITION is already
                        # met. Unlike a coordinate selector matching nothing -
                        # which means the engineer's intent missed the solid -
                        # this means the intent is satisfied. Not a no-op.
                        continue

                    def _key(a, b):
                        pts = sorted([tuple(round(float(c), 3) for c in a),
                                      tuple(round(float(c), 3) for c in b)])
                        return (pts[0], pts[1])

                    want = {_key(f["p0"], f["p1"]) for f in flagged}
                    edges = []
                    for e in solid.Edges():
                        vtx = e.Vertices()
                        if len(vtx) != 2:
                            continue
                        if _key((vtx[0].X, vtx[0].Y, vtx[0].Z),
                                (vtx[1].X, vtx[1].Y, vtx[1].Z)) in want:
                            edges.append(e)
                    if len(edges) != len(flagged):
                        raise GeometryError(
                            f"features[{idx}] (fillet): detected {len(flagged)} "
                            f"sharp concave edges but could only locate "
                            f"{len(edges)} of them in the solid")
                    blended = solid.fillet(feat["radius"], edges)
                    remaining = sharp_concave_edges(blended)
                    if remaining:
                        raise GeometryError(
                            f"features[{idx}] (fillet): radius "
                            f"{feat['radius']} left {len(remaining)} sharp "
                            f"concave edge(s) standing, so the peak stress "
                            f"can still be unbounded. The postcondition this "
                            f"feature asserts is not met")
                    result = cq.Workplane(obj=blended)
                    continue
                picked = result.edges(
                    sel if isinstance(sel, str) else _StructuredEdgeSelector(sel))
                # Same principle as a hole that removes nothing: a fillet that
                # rounds no edges is a silent no-op, and the part would be
                # validated and signed off without the feature the engineer
                # believes is carrying the stress.
                if not picked.vals():
                    raise SpecError(
                        f"features[{idx}] (fillet): selector {sel!r} matched no "
                        f"edges, so the fillet would do nothing. Check the "
                        f"coordinates against the solid as it stands at this "
                        f"point in the feature list.")
                result = picked.fillet(feat["radius"])
        solids = result.solids().vals()
        if len(solids) != 1:
            raise GeometryError(
                f"spec produced {len(solids)} disjoint solids; a part must be one body")
        return result
    except (SpecError, GeometryError):
        raise
    except Exception as exc:  # OCC kernel errors carry poor types; wrap them
        raise GeometryError(f"geometry kernel failed: {exc}") from exc


def mass_properties(spec: dict, solid: cq.Workplane) -> dict[str, Any]:
    shape = solid.val()
    bb = shape.BoundingBox()
    com = shape.Center()
    props = {
        "volume_mm3": round(shape.Volume(), 6),
        "area_mm2": round(shape.Area(), 6),
        "bbox_mm": {
            "min": [round(bb.xmin, 6), round(bb.ymin, 6), round(bb.zmin, 6)],
            "max": [round(bb.xmax, 6), round(bb.ymax, 6), round(bb.zmax, 6)],
            "size": [round(bb.xlen, 6), round(bb.ylen, 6), round(bb.zlen, 6)],
        },
        "center_of_mass_mm": [round(com.x, 6), round(com.y, 6), round(com.z, 6)],
        "spec_digest": spec_digest(spec),
    }
    density = spec.get("density_kg_m3")
    if density is not None:
        # estimate: derived purely from the user-supplied density, not measured
        mass_kg = shape.Volume() * 1e-9 * density
        props["mass_kg_estimate"] = round(mass_kg, 9)
        props["inertia_kg_m2_about_com"] = inertia_about_com(shape, density)
    return props


def inertia_about_com(shape, density_kg_m3: float) -> list[list[float]]:
    """Exact inertia tensor about the centre of mass, in kg*m^2.

    Computed from the real solid via OpenCascade's volume properties (verified
    against the analytic box result m(b^2+c^2)/12), not approximated from the
    bounding box - a bounding-box inertia would silently mis-state the
    rotational dynamics of anything that is not a solid cuboid.

    OCC returns volume moments in mm^5 for unit density; multiplying by
    density (kg/m^3) and 1e-15 converts mm^5 -> m^5 * (kg/m^3) = kg*m^2.
    """
    from OCP.BRepGProp import BRepGProp
    from OCP.GProp import GProp_GProps
    from OCP.gp import gp_Pnt

    com = shape.Center()
    props = GProp_GProps(gp_Pnt(com.x, com.y, com.z))
    BRepGProp.VolumeProperties_s(shape.wrapped, props)
    m = props.MatrixOfInertia()
    k = density_kg_m3 * 1e-15
    return [[round(m.Value(i, j) * k, 12) for j in (1, 2, 3)] for i in (1, 2, 3)]


def apply_changes(spec: dict, changes: dict[str, Any]) -> tuple[dict, list[dict]]:
    """Apply dot-path changes to a copy of spec; return (new_spec, diff).

    Paths address nested keys/list indices: "features.0.x". A list index equal
    to the list length appends; a value of None deletes the key/element.
    Returns diff as [{"path", "old", "new"}, ...]. Raises SpecError on paths
    that address nothing.
    """
    new_spec = copy.deepcopy(spec)
    diff: list[dict] = []
    for path, value in changes.items():
        keys = path.split(".")
        node: Any = new_spec
        for i, key in enumerate(keys[:-1]):
            node = _step(node, key, path)
            if node is None:
                raise SpecError(f"change path {path!r}: {'.'.join(keys[:i + 1])} not found")
        leaf = keys[-1]
        if isinstance(node, list):
            idx = _list_index(leaf, path)
            if value is None:
                if idx >= len(node):
                    raise SpecError(f"change path {path!r}: index out of range")
                diff.append({"path": path, "old": node[idx], "new": None})
                del node[idx]
            elif idx == len(node):
                diff.append({"path": path, "old": None, "new": value})
                node.append(value)
            elif idx < len(node):
                diff.append({"path": path, "old": node[idx], "new": value})
                node[idx] = value
            else:
                raise SpecError(f"change path {path!r}: index {idx} out of range")
        elif isinstance(node, dict):
            old = node.get(leaf)
            if value is None:
                if leaf not in node:
                    raise SpecError(f"change path {path!r}: key not found")
                diff.append({"path": path, "old": old, "new": None})
                del node[leaf]
            else:
                diff.append({"path": path, "old": old, "new": value})
                node[leaf] = value
        else:
            raise SpecError(f"change path {path!r}: cannot descend into {type(node).__name__}")
    return new_spec, diff


def _step(node: Any, key: str, path: str) -> Any:
    if isinstance(node, list):
        idx = _list_index(key, path)
        return node[idx] if idx < len(node) else None
    if isinstance(node, dict):
        return node.get(key)
    return None


def _list_index(key: str, path: str) -> int:
    if not key.isdigit():
        raise SpecError(f"change path {path!r}: {key!r} is not a list index")
    return int(key)
