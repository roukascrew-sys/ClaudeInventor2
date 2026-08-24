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
- fillet: CadQuery edge selector string (e.g. "|Z", ">Z").
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
            if not isinstance(feat.get("edges"), str):
                raise SpecError(f"features[{idx}] (fillet): 'edges' selector string required")
        if op in _BASE_OPS and idx > 0 and feat.get("mode") not in ("union", "cut"):
            raise SpecError(
                f"features[{idx}] ({op}): needs 'mode': 'union'|'cut' after the base")


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
        for feat in spec["features"][1:]:
            op = feat["op"]
            if op in _BASE_OPS:
                prim = _make_primitive(feat)
                result = result.union(prim) if feat["mode"] == "union" else result.cut(prim)
            elif op == "hole":
                face = feat.get("face", ">Z")
                result = (
                    result.faces(face).workplane()
                    .pushPoints([tuple(feat["at"])]).hole(feat["d"])
                )
            elif op == "fillet":
                result = result.edges(feat["edges"]).fillet(feat["radius"])
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
        props["mass_kg_estimate"] = round(shape.Volume() * 1e-9 * density, 9)
    return props


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
