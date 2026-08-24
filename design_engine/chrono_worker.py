"""Chrono worker — runs INSIDE the separate `chrono` conda env, not the venv.

PyChrono is conda-only and cannot be installed alongside CadQuery in the
project's pip venv, so the kinematics layer is bridged by process: this script
reads a job JSON, runs the multibody solve, and writes a result JSON. It
imports pychrono and the standard library only — nothing from design_engine —
because none of the project's own packages exist in that env.

UNITS: the job is in **SI (m, kg, s, N)**. The rest of the engine works in mm,
and kinematics.py converts at the boundary rather than here, so that mixing
mm with kg (which would silently yield milli-newtons) is impossible inside the
solve. Forces come back in newtons, directly usable as an FEA load.

Joint types and what they mean mechanically — this choice changes the answer,
so it is explicit and never defaulted silently:
  revolute  — carries bending moment as well as force. Reaction moments appear
              on each joint and the force couple between joints vanishes.
  spherical — force only, no moment. Load is carried as a force couple between
              joints. This is the classic hinge idealisation and is what
              exposes the horizontal pull a hinge leaf actually sees.
Both are verified against closed form in tests/test_kinematics.py.
"""

import json
import math
import sys

import pychrono as chrono

JOINT_TYPES = {
    "revolute": chrono.ChLinkLockRevolute,
    "spherical": chrono.ChLinkLockSpherical,
    "point_plane": chrono.ChLinkLockPointPlane,
}


def _v(seq):
    return chrono.ChVector3d(float(seq[0]), float(seq[1]), float(seq[2]))


def _mag(v):
    return math.sqrt(v.x ** 2 + v.y ** 2 + v.z ** 2)


def _axis_quaternion(axis):
    """Rotation taking the joint's local z onto `axis` (Chrono joints act on z)."""
    a = _v(axis)
    n = _mag(a)
    if n == 0:
        raise ValueError("joint axis must be a non-zero vector")
    a = chrono.ChVector3d(a.x / n, a.y / n, a.z / n)
    z = chrono.ChVector3d(0, 0, 1)
    dot = a.x * z.x + a.y * z.y + a.z * z.z
    if dot > 1 - 1e-12:
        return chrono.ChQuaterniond(1, 0, 0, 0)
    if dot < -1 + 1e-12:
        return chrono.ChQuaterniond(0, 1, 0, 0)          # 180 deg about x
    cross = chrono.ChVector3d(z.y * a.z - z.z * a.y,
                              z.z * a.x - z.x * a.z,
                              z.x * a.y - z.y * a.x)
    q = chrono.ChQuaterniond(1 + dot, cross.x, cross.y, cross.z)
    q.Normalize()
    return q


def apply_external_forces(job, bodies):
    """Point loads at a body-local offset from its COM, world-frame force.

    Uses the modern ChLoadBodyForce/ChLoadContainer mechanism, not the
    legacy ChForce/body.AddForce() API -- that one segfaults in this Chrono
    build (verified directly: a minimal repro crashes the process).
    """
    container = chrono.ChLoadContainer()
    for f in job.get("external_forces", []):
        body = bodies[f["body"]]
        load = chrono.ChLoadBodyForce(
            body, _v(f["force_N"]), False, _v(f["at_m"]), False)
        container.Add(load)
    return container


def build_system(job):
    sys_ = chrono.ChSystemNSC()
    # DIRECT solver, not Chrono's default iterative one. The default
    # (Barzilai-Borwein / PSOR family) silently UNDER-CONVERGES on statics
    # problems and returns a plausible-looking but wrong reaction set: on the
    # verification rig below it gave foot Fz = -18.6 N where the closed form
    # is -1000.0 N, with no error or warning. SparseQR reproduces the closed
    # form exactly (-250.000, 0, -1000.000). SparseLU was also tried and
    # FAILS on this problem ("zero diagonal" -> all-zero reactions), so the
    # choice of QR here is deliberate, not arbitrary.
    #
    # This is why the earlier door-hinge results looked right: those cases
    # happened to be well-conditioned enough for the iterative solver to get
    # close. Well-conditioned luck is not verification.
    sys_.SetSolver(chrono.ChSolverSparseQR())
    sys_.SetGravitationalAcceleration(_v(job["gravity_m_s2"]))

    bodies = {}
    for b in job["bodies"]:
        body = chrono.ChBody()
        body.SetMass(float(b["mass_kg"]))
        I = b["inertia_kg_m2"]
        body.SetInertiaXX(_v([I[0][0], I[1][1], I[2][2]]))
        if any(abs(I[i][j]) > 0 for i, j in ((0, 1), (0, 2), (1, 2))):
            body.SetInertiaXY(_v([I[0][1], I[0][2], I[1][2]]))
        body.SetPos(_v(b["com_m"]))
        if b.get("fixed"):
            body.SetFixed(True)
        sys_.AddBody(body)
        bodies[b["id"]] = body

    links = []
    for j in job["joints"]:
        cls = JOINT_TYPES.get(j["type"])
        if cls is None:
            raise ValueError(
                "unknown joint type %r; supported: %s"
                % (j["type"], sorted(JOINT_TYPES)))
        a, b = j["between"]
        if a not in bodies or b not in bodies:
            raise ValueError("joint %r references unknown body" % j["id"])
        link = cls()
        frame = chrono.ChFramed(_v(j["at_m"]),
                                _axis_quaternion(j.get("axis", [0, 0, 1])))
        link.Initialize(bodies[a], bodies[b], frame)
        sys_.AddLink(link)
        links.append((j, link))
    return sys_, bodies, links


def reactions(links):
    """Joint reactions, rotated into WORLD coordinates.

    GetReaction2() reports in the joint marker's own frame. For a joint whose
    frame was rotated to align its local z with a given axis (every
    non-default 'axis' in a job), that is NOT the world frame -- e.g. a
    point_plane wall contact reports its reaction as local +z when the
    physical force is along world +x. Returning that raw would hand the
    caller a vector in a frame it never specified, and it feeds straight into
    an FEA load case, so it is converted here.
    """
    out = []
    for j, link in links:
        r = link.GetReaction2()
        q = _axis_quaternion(j.get("axis", [0, 0, 1]))
        f_w = q.Rotate(r.force)
        t_w = q.Rotate(r.torque)
        out.append({
            "joint_id": j["id"],
            "type": j["type"],
            "force_N": [f_w.x, f_w.y, f_w.z],
            "force_magnitude_N": _mag(f_w),
            "torque_Nm": [t_w.x, t_w.y, t_w.z],
            "torque_magnitude_Nm": _mag(t_w),
            "frame": "world",
        })
    return out


def run(job):
    sys_, bodies, links = build_system(job)
    loads = apply_external_forces(job, bodies)
    sys_.Add(loads)
    analysis = job.get("analysis", "static")

    if analysis == "static":
        sys_.DoStaticLinear()
        return {
            "analysis": "static",
            "reactions": reactions(links),
            "bodies": {bid: {"pos_m": [b.GetPos().x, b.GetPos().y, b.GetPos().z]}
                       for bid, b in bodies.items()},
        }

    # dynamic: step through the motion, keeping the worst reaction seen
    dt = float(job.get("dt_s", 1e-3))
    duration = float(job.get("duration_s", 1.0))
    steps = max(1, int(round(duration / dt)))
    peak = {}
    track = job.get("track_body")
    trajectory = []
    for step in range(steps):
        sys_.DoStepDynamics(dt)
        for rec in reactions(links):
            jid = rec["joint_id"]
            cur = peak.get(jid)
            if cur is None or rec["force_magnitude_N"] > cur["force_magnitude_N"]:
                rec["at_time_s"] = (step + 1) * dt
                peak[jid] = rec
        if track and track in bodies and step % max(1, steps // 200) == 0:
            p = bodies[track].GetPos()
            trajectory.append({"t_s": (step + 1) * dt,
                               "pos_m": [p.x, p.y, p.z]})
    return {
        "analysis": "dynamic",
        "duration_s": duration, "dt_s": dt, "steps": steps,
        "reactions": list(peak.values()),
        "trajectory": trajectory,
        "bodies": {bid: {"pos_m": [b.GetPos().x, b.GetPos().y, b.GetPos().z]}
                   for bid, b in bodies.items()},
    }


def main():
    if len(sys.argv) != 3:
        print("usage: chrono_worker.py <job.json> <out.json>", file=sys.stderr)
        return 2
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        job = json.load(fh)
    try:
        result = run(job)
        result["ok"] = True
        result["chrono_available"] = True
    except Exception as exc:                       # report, never half-write
        result = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
    with open(sys.argv[2], "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
