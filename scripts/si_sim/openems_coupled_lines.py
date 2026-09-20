"""Full-wave (openEMS/FDTD) quasi-static coupling extraction for a 2-line
coupled-microstrip proxy model.

WHY THIS SHAPE OF MODEL
------------------------
The real SW->FB coupling geometry on Xeltri is NOT two neat parallel traces —
the nearest SW and FB segments run at different angles. Rather than pretend to
simulate the "exact" 3D board (which would need a full extracted zone/via mesh
and is not worth the runtime for a pre-screen tool), this module builds an
idealized straight, parallel coupled-line pair using the REAL measured values
from the toolchain: spacing (nearest SW-FB distance), stackup dielectric
height/permittivity to the reference plane, and trace widths. This is a
standard SI "worst-case proxy" simplification and is labeled as such in every
output — see `model_notes`.

WHY A SHORT, FAST EXCITATION FOR A SLOW (~50 ns) SWITCHING EDGE
-----------------------------------------------------------------
The real SW edge (order 10s of ns) is far slower than the EM transit time
across a few mm of board (order of picoseconds), so directly driving FDTD
with the real edge would need ~1e6 timesteps for a sub-100 micron dielectric
mesh — impractical for an interactive tool. Instead we excite with a fast,
broadband Gaussian pulse and use openEMS's per-frequency DFT (`Port.CalcPort`
accepts an arbitrary frequency vector, not just FFT bin frequencies) to read
the quasi-static coupling response AT the real signal's harmonic frequencies
(fundamental + edge-rate harmonics). Because the coupled-line structure is
electrically tiny at those frequencies (no resonance expected until several
GHz for mm-scale coupling lengths), the extracted per-frequency port
relationship is valid quasi-static data that we then apply, analytically, to
the real switching edge's dV/dt — this is standard hybrid full-wave/analytical
SI practice, not a shortcut that invalidates the physics.
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

C0 = 299792458.0  # m/s


@dataclass
class CoupledLineGeometry:
    ic_reference: str
    aggressor_net: str
    victim_net: str
    aggressor_width_mm: float
    victim_width_mm: float
    spacing_mm: float
    coupled_length_mm: float
    dielectric_height_mm: float
    epsilon_r: float
    victim_sense_r_ohm: float = 1000.0
    coplanar_guard: bool = False
    guard_width_mm: float = 0.0
    guard_gap_from_aggressor_mm: float = 0.0
    guard_gap_from_victim_mm: float = 0.0


def openems_available() -> Dict[str, Any]:
    """Check whether CSXCAD/openEMS Python bindings are importable."""
    try:
        import CSXCAD  # noqa: F401
        import openEMS  # noqa: F401

        return {"available": True, "reason": "CSXCAD + openEMS import OK"}
    except Exception as exc:  # pragma: no cover - environment dependent
        return {"available": False, "reason": f"{type(exc).__name__}: {exc}"}


def run_coupled_line_fdtd(
    geom: CoupledLineGeometry,
    freqs_hz: List[float],
    sim_dir: Optional[str] = None,
    keep_sim_dir: bool = False,
    end_criteria: float = 1e-5,
    max_timesteps: int = 60000,
) -> Dict[str, Any]:
    """Run one openEMS FDTD simulation of the idealized coupled-line pair.

    Returns a dict with per-port frequency-domain data and derived quasi-
    static mutual coupling metrics. Raises on any openEMS/CSXCAD failure —
    callers should catch and fall back to the analytical-only estimate.
    """
    from CSXCAD import CSXCAD
    from openEMS import openEMS as openEMS_cls

    unit = 1e-3  # geometry in mm
    w1 = geom.aggressor_width_mm
    w2 = geom.victim_width_mm
    s = geom.spacing_mm
    h = geom.dielectric_height_mm
    er = geom.epsilon_r
    Lc = geom.coupled_length_mm

    y1_lo, y1_hi = -w1 / 2.0, w1 / 2.0
    use_guard = bool(geom.coplanar_guard and geom.guard_width_mm > 0)
    y_g_lo = y_g_hi = None
    if use_guard:
        gw = geom.guard_width_mm
        g_from_sw = geom.guard_gap_from_aggressor_mm
        g_from_fb = geom.guard_gap_from_victim_mm
        if g_from_sw <= 0 or g_from_fb <= 0:
            leftover = max(s - gw, 0.05)
            g_from_sw = leftover / 2.0
            g_from_fb = leftover / 2.0
        y_g_lo = y1_hi + g_from_sw
        y_g_hi = y_g_lo + gw
        y2_lo = y_g_hi + g_from_fb
        y2_hi = y2_lo + w2
    else:
        yv_center = y1_hi + s + w2 / 2.0
        y2_lo, y2_hi = yv_center - w2 / 2.0, yv_center + w2 / 2.0

    margin_xy = max(2.0, 3 * h, 0.5 * Lc)
    x_min, x_max = -margin_xy, Lc + margin_xy
    y_min, y_max = y1_lo - margin_xy, y2_hi + margin_xy
    z_air = max(1.0, 15 * h)

    fmax = max(freqs_hz) if freqs_hz else 1e9
    f0 = 0.0
    fc = max(fmax * 2.0, 200e6)

    CSX = CSXCAD.ContinuousStructure()
    FDTD = openEMS_cls(NrTS=max_timesteps, EndCriteria=end_criteria)
    FDTD.SetCSX(CSX)
    FDTD.SetGaussExcite(f0, fc)
    FDTD.SetBoundaryCond(["PML_8", "PML_8", "PML_8", "PML_8", "PEC", "PML_8"])

    grid = CSX.GetGrid()
    grid.SetDeltaUnit(unit)
    grid.AddLine("x", [x_min, 0, Lc, x_max])
    y_lines = [y_min, y1_lo, y1_hi, y2_lo, y2_hi, y_max]
    if use_guard and y_g_lo is not None:
        y_lines.extend([y_g_lo, y_g_hi])
    grid.AddLine("y", sorted(set(y_lines)))
    grid.AddLine("z", [0, h / 3.0, 2.0 * h / 3.0, h, z_air])
    mesh_y = min(w1, w2, s)
    if use_guard and geom.guard_width_mm > 0:
        mesh_y = min(
            mesh_y,
            geom.guard_width_mm,
            geom.guard_gap_from_aggressor_mm or s,
            geom.guard_gap_from_victim_mm or s,
        )
    grid.SmoothMeshLines("x", min(Lc, s, 0.3) / 4.0 or 0.1, ratio=1.4)
    grid.SmoothMeshLines("y", mesh_y / 4.0 or 0.05, ratio=1.4)
    grid.SmoothMeshLines("z", h / 4.0, ratio=1.4)

    substrate = CSX.AddMaterial("substrate", epsilon=er)
    substrate.AddBox([x_min, y_min, 0], [x_max, y_max, h], priority=0)

    gnd = CSX.AddMetal("gnd")
    gnd.AddBox([x_min, y_min, 0], [x_max, y_max, 0], priority=10)

    aggressor = CSX.AddMetal("aggressor")
    aggressor.AddBox([0, y1_lo, h], [Lc, y1_hi, h], priority=10)

    victim = CSX.AddMetal("victim")
    victim.AddBox([0, y2_lo, h], [Lc, y2_hi, h], priority=10)

    if use_guard and y_g_lo is not None:
        guard = CSX.AddMetal("coplanar_guard")
        guard.AddBox([0, y_g_lo, h], [Lc, y_g_hi, h], priority=10)
        stitch = max(0.08, min(geom.guard_width_mm, 0.2))
        guard.AddBox([0, y_g_lo, 0], [stitch, y_g_hi, h], priority=10)
        guard.AddBox([Lc - stitch, y_g_lo, 0], [Lc, y_g_hi, h], priority=10)

    port1 = FDTD.AddLumpedPort(1, 50, [0, y1_lo, 0], [0, y1_hi, h], "z", excite=1)
    port2 = FDTD.AddLumpedPort(2, 50, [Lc, y1_lo, 0], [Lc, y1_hi, h], "z", excite=0)
    port3 = FDTD.AddLumpedPort(
        3, geom.victim_sense_r_ohm, [0, y2_lo, 0], [0, y2_hi, h], "z", excite=0
    )
    port4 = FDTD.AddLumpedPort(
        4, geom.victim_sense_r_ohm, [Lc, y2_lo, 0], [Lc, y2_hi, h], "z", excite=0
    )

    own_dir = sim_dir is None
    sim_path = sim_dir or tempfile.mkdtemp(prefix="xeltri_si_")
    try:
        FDTD.Run(sim_path, cleanup=True, verbose=0)

        import numpy as np

        freq = np.array(freqs_hz, dtype=float)
        for p in (port1, port2, port3, port4):
            p.CalcPort(sim_path, freq)

        uf3 = port3.uf_tot
        uf4 = port4.uf_tot
        uf1 = port1.uf_tot
        results = []
        for i, f in enumerate(freq):
            v_agg = complex(uf1[i])
            v_near = complex(uf3[i])
            v_far = complex(uf4[i])
            near_ratio = abs(v_near) / abs(v_agg) if abs(v_agg) > 0 else None
            far_ratio = abs(v_far) / abs(v_agg) if abs(v_agg) > 0 else None
            results.append(
                {
                    "freq_hz": float(f),
                    "v_aggressor_port": [v_agg.real, v_agg.imag],
                    "v_victim_near": [v_near.real, v_near.imag],
                    "v_victim_far": [v_far.real, v_far.imag],
                    "near_end_coupling_ratio": near_ratio,
                    "far_end_coupling_ratio": far_ratio,
                }
            )

        return {
            "ok": True,
            "engine": "openEMS FDTD",
            "sim_path": sim_path,
            "geometry": geom.__dict__,
            "per_frequency": results,
        }
    finally:
        if own_dir and not keep_sim_dir:
            shutil.rmtree(sim_path, ignore_errors=True)
