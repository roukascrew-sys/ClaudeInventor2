"""Stress on a cross section, not at a node.

WHY THIS EXISTS
On 2026-09-30 the static gate was shown to have been dividing by a stress that
does not converge: every jetpack junction run reported its peak sitting 0.4894
mm from a re-entrant corner, where linear elasticity has no finite stress
(Williams 1952), and a safety factor was published from it anyway. The gate now
refuses that. What it left behind is a hole - there was no limit state defined
at a weld at all.

EN 1999-1-1 (Eurocode 9) 8.6.3.4 closes it, and the important words in the
clause are FULL CROSS SECTION. The design resistance in the heat-affected zone
is checked against the stress on the cross section at the fusion boundary and
at the toe of the weld, not against a nodal maximum. That is not a softer
check; it is a DIFFERENT quantity, and it is one that converges, because a
section resultant is fixed by equilibrium and cannot be inflated by refining
the mesh near a notch.

This module produces that quantity.

HOW THE RESULTANT IS OBTAINED, AND WHY IT IS EXACT
Not by integrating the stress field over the cut. CalculiX's FORC output is the
TOTAL nodal force at every node - applied load plus reaction, zero at interior
nodes - so the resultant transmitted across a plane is the sum of FORC over
every node on one side of it. That is the free-body equation of the discrete
system: exact to solver precision, and independent of how well the stress field
is interpolated anywhere. The same sum gives the transmitted moment.

Only the section's GEOMETRY - area, centroid, second moments - comes from
intersecting the mesh with the plane, and only the extreme-fibre stress divides
by it.

The field-interpolated traction is computed too, and reported, purely as a
cross-check. It interpolates linearly between the corner nodes of a quadratic
element on a straight-edged approximation of a curved one, so it is a
lower-order reading of the same field, and unlike the resultant it CAN pick up
a local concentration where the section clips one. Both facts make it a
diagnostic and not a gate. Where the two disagree, believe the resultant.
"""

from __future__ import annotations

import numpy as np


class SectionError(ValueError):
    """A section that cannot be cut, or cannot be trusted once cut."""


# A node landing exactly on the plane is assigned to the negative side, always
# the same way, so the free body is well defined whichever way the mesh falls.
# The tolerance is geometric (mm), not relative: at 1e-9 mm no real coordinate
# is ambiguous, and a node genuinely on the plane contributes no area either
# way.
_ON_PLANE_MM = 1e-9

_TET_EDGES = ((0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3))


def plane(point, normal) -> tuple:
    """A validated (origin, unit normal) pair."""
    try:
        p0 = np.asarray([float(v) for v in point], dtype=float)
        n = np.asarray([float(v) for v in normal], dtype=float)
    except (TypeError, ValueError):
        raise SectionError(
            "section plane: point and normal must each be three numbers"
        ) from None
    if p0.shape != (3,) or n.shape != (3,):
        raise SectionError(
            "section plane: point and normal need three components each")
    mag = float(np.linalg.norm(n))
    if mag == 0.0:
        raise SectionError(
            "section plane: the normal has zero length, so it defines no plane")
    return p0, n / mag


def _basis(n):
    """Two unit vectors spanning the plane, right-handed with n: e1 x e2 = n."""
    a = np.array([0.0, 0.0, 1.0]) if abs(n[2]) < 0.9 else np.array([1.0, 0.0, 0.0])
    e1 = np.cross(a, n)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(n, e1)
    return e1, e2


def cut(mesh: dict, point, normal) -> dict:
    """Intersect a C3D10 mesh with a plane.

    Returns the section's area, centroid and in-plane second moments, plus the
    cut facets themselves. The intersection is taken on the four CORNER nodes
    of each tet - the straight-edged element - so a curved boundary is
    approximated by flat facets. That error is second order in element size and
    appears as a small area error; it is reported rather than assumed away,
    because the extreme-fibre stress divides by these numbers.
    """
    p0, n = plane(point, normal)
    conn = np.asarray(mesh["connectivity"], dtype=np.int64)
    if conn.ndim != 2 or conn.shape[1] < 4:
        raise SectionError(
            f"section cut: expected tet connectivity with at least 4 columns, "
            f"got shape {conn.shape}")
    tags = np.asarray(mesh["node_tags"], dtype=np.int64)
    coords = np.asarray(mesh["coords"], dtype=float)

    order = np.argsort(tags)
    srt = tags[order]
    corner = conn[:, :4]
    pos = np.searchsorted(srt, corner)
    if (np.any(pos >= srt.size)
            or np.any(srt[np.clip(pos, 0, srt.size - 1)] != corner)):
        raise SectionError(
            "section cut: the connectivity references a node tag that is not "
            "in node_tags - the mesh is inconsistent and no section is taken "
            "from it")
    xyz = coords[order[pos]]                            # (nelem, 4, 3)

    d = np.einsum("ijk,k->ij", xyz - p0, n)             # signed distance
    above = d > _ON_PLANE_MM
    n_above = above.sum(axis=1)
    straddle = np.nonzero((n_above > 0) & (n_above < 4))[0]

    e1, e2 = _basis(n)
    facets = []
    for e in straddle:
        de = d[e]
        pe = xyz[e]
        ce = corner[e]
        verts = []
        for a, b in _TET_EDGES:
            da, db = de[a], de[b]
            if (da > _ON_PLANE_MM) == (db > _ON_PLANE_MM):
                continue
            denom = da - db
            if denom == 0.0:
                continue
            t = da / denom                              # 0 at a, 1 at b
            verts.append((pe[a] + t * (pe[b] - pe[a]),
                          int(ce[a]), int(ce[b]), float(t)))
        if len(verts) < 3:
            continue

        P = np.array([v[0] for v in verts])
        mid = P.mean(axis=0)
        ang = np.arctan2((P - mid) @ e2, (P - mid) @ e1)
        idx = np.argsort(ang)
        P = P[idx]
        verts = [verts[j] for j in idx]
        # Fan from the first vertex. A plane cuts a tet in a triangle or a
        # convex quadrilateral and never anything worse, so a fan is exact.
        a_signed = 0.0
        cen = np.zeros(3)
        for k in range(1, len(P) - 1):
            ta = 0.5 * float(np.cross(P[k] - P[0], P[k + 1] - P[0]) @ n)
            a_signed += ta
            cen += ta * (P[0] + P[k] + P[k + 1]) / 3.0
        if a_signed == 0.0:
            continue
        facets.append({"area": abs(a_signed),
                       "centroid": cen / a_signed,
                       "verts": verts})

    if not facets:
        raise SectionError(
            f"section cut: the plane through {p0.tolist()} with normal "
            f"{n.tolist()} intersects no element. Either it misses the part or "
            f"it lies outside it - a section with no area carries no stress")

    areas = np.array([f["area"] for f in facets])
    cents = np.array([f["centroid"] for f in facets])
    total = float(areas.sum())
    if total <= 0.0:
        raise SectionError("section cut: the intersection has zero area")

    centroid = (areas[:, None] * cents).sum(axis=0) / total
    rel = cents - centroid
    u1 = rel @ e1
    u2 = rel @ e2
    # Each facet is treated as its area concentrated at its own centroid. A
    # facet's OWN second moment about that centroid is of order (area x h^2),
    # so the omission is second order in element size - the same order as the
    # flat-facet approximation of the boundary above - and vanishes as the mesh
    # refines.
    return {
        "point_mm": p0.tolist(),
        "normal": n.tolist(),
        "e1": e1.tolist(),
        "e2": e2.tolist(),
        "area_mm2": total,
        "centroid_mm": centroid.tolist(),
        "Q11_mm4": float((areas * u1 * u1).sum()),
        "Q22_mm4": float((areas * u2 * u2).sum()),
        "Q12_mm4": float((areas * u1 * u2).sum()),
        "facets": len(facets),
        "elements_cut": int(straddle.size),
        "_facets": facets,
    }


def resultants(mesh: dict, forc: dict, point, normal,
               stress: dict | None = None) -> dict:
    """Force, moment, and the stresses they imply on one cross section.

    `forc` is CalculiX's FORC block: total nodal force, applied plus reaction.
    Summing it over the nodes strictly on the +normal side gives the internal
    force the section transmits, tension positive along the normal. This is the
    free-body equation of the discrete model; it holds exactly whatever the
    mesh does near a notch elsewhere in the part.
    """
    sec = cut(mesh, point, normal)
    p0 = np.asarray(sec["point_mm"])
    n = np.asarray(sec["normal"])
    e1 = np.asarray(sec["e1"])
    e2 = np.asarray(sec["e2"])
    C = np.asarray(sec["centroid_mm"])

    tags = np.asarray(mesh["node_tags"], dtype=np.int64)
    coords = np.asarray(mesh["coords"], dtype=float)
    dn = (coords - p0) @ n
    side = dn > _ON_PLANE_MM

    F = np.zeros(3)
    M = np.zeros(3)
    counted = 0
    for tag, x in zip(tags[side], coords[side]):
        v = forc.get(int(tag))
        if v is None:
            continue
        f = np.asarray(v[:3], dtype=float)
        if not f.any():
            continue
        F += f
        M += np.cross(x - C, f)
        counted += 1

    A = sec["area_mm2"]
    N = float(F @ n)
    V = float(np.linalg.norm(F - N * n))

    # sigma(u1, u2) = N/A + beta*u1 + gamma*u2, fitted to the transmitted
    # moment. With a right-handed (e1, e2, n), M.e1 = integral(sigma*u2)dA and
    # M.e2 = -integral(sigma*u1)dA. Those two first moments give beta and gamma
    # through the section's own second-moment matrix.
    S2 = float(M @ e1)
    S1 = -float(M @ e2)
    Q = np.array([[sec["Q11_mm4"], sec["Q12_mm4"]],
                  [sec["Q12_mm4"], sec["Q22_mm4"]]])
    bending_ok = True
    try:
        beta, gamma = np.linalg.solve(Q, np.array([S1, S2]))
    except np.linalg.LinAlgError:
        beta = gamma = 0.0
        bending_ok = False

    sigma_mem = N / A
    extreme = 0.0
    extreme_at = None
    for f in sec["_facets"]:
        r = f["centroid"] - C
        s = sigma_mem + beta * float(r @ e1) + gamma * float(r @ e2)
        if abs(s) > abs(extreme):
            extreme = s
            extreme_at = f["centroid"].tolist()

    out = {
        "area_mm2": round(A, 6),
        "centroid_mm": [round(v, 4) for v in C.tolist()],
        "normal": [round(v, 9) for v in n.tolist()],
        "nodes_on_positive_side": int(side.sum()),
        "nodes_with_force": counted,
        "elements_cut": sec["elements_cut"],
        "facets": sec["facets"],
        "axial_N": round(N, 6),
        "shear_N": round(V, 6),
        "force_N": [round(v, 6) for v in F.tolist()],
        "moment_Nmm": [round(v, 6) for v in M.tolist()],
        "sigma_membrane_MPa": round(sigma_mem, 6),
        "sigma_extreme_MPa": round(extreme, 6),
        "sigma_extreme_at_mm": ([round(v, 4) for v in extreme_at]
                                if extreme_at else None),
        "tau_average_MPa": round(V / A, 6),
        "bending_resolved": bending_ok,
    }
    if not bending_ok:
        out["bending_warning"] = (
            "the section's second-moment matrix is singular, so the bending "
            "component could not be separated from the membrane one and only "
            "N/A is reported. A section degenerate to a line or a point does "
            "this, and so does a single cut facet.")
    if counted == 0:
        out["free_body_warning"] = (
            "no node on the positive side of this plane carries a nodal force, "
            "so the transmitted resultant came out zero. Either the plane has "
            "every load and every reaction on one side of it, or the FORC block "
            "was not requested. A zero resultant is not evidence of a zero "
            "stress.")

    if stress:
        out["field_check"] = field_traction(sec, stress)
    return out


def field_traction(sec: dict, stress: dict) -> dict:
    """The traction read straight off the interpolated stress field.

    A cross-check on the resultant, never a gate. Each cut facet's vertices lie
    on element edges, so the stress there is interpolated between the two
    CORNER nodes of that edge - linear, on a quadratic field. The resultant
    above needs none of this.
    """
    n = np.asarray(sec["normal"])
    worst = 0.0
    worst_at = None
    worst_pair = (0.0, 0.0)
    Fint = np.zeros(3)
    area_seen = 0.0
    missing = 0

    for f in sec["_facets"]:
        acc = np.zeros(6)
        got = 0
        for _xyz, ta, tb, t in f["verts"]:
            sa = stress.get(int(ta))
            sb = stress.get(int(tb))
            if sa is None or sb is None:
                continue
            acc += ((1.0 - t) * np.asarray(sa[:6], dtype=float)
                    + t * np.asarray(sb[:6], dtype=float))
            got += 1
        if got == 0:
            missing += 1
            continue
        sxx, syy, szz, sxy, syz, szx = acc / got
        S = np.array([[sxx, sxy, szx], [sxy, syy, syz], [szx, syz, szz]])
        tr = S @ n
        s_perp = float(tr @ n)
        tau = float(np.linalg.norm(tr - s_perp * n))
        comb = float(np.sqrt(s_perp * s_perp + 3.0 * tau * tau))
        Fint += f["area"] * tr
        area_seen += f["area"]
        if comb > worst:
            worst = comb
            worst_at = f["centroid"].tolist()
            worst_pair = (s_perp, tau)

    if area_seen == 0.0:
        return {"available": False,
                "reason": "no cut facet had stress at both ends of any edge"}
    Nf = float(Fint @ n)
    return {
        "available": True,
        "max_combined_MPa": round(worst, 6),
        "max_combined_at_mm": ([round(v, 4) for v in worst_at]
                               if worst_at else None),
        "sigma_perp_MPa": round(worst_pair[0], 6),
        "tau_MPa": round(worst_pair[1], 6),
        "integrated_axial_N": round(Nf, 6),
        "integrated_shear_N": round(float(np.linalg.norm(Fint - Nf * n)), 6),
        "facets_without_stress": missing,
        "facet_area_mm2": round(area_seen, 6),
    }
