"""Always-available analytical SI pre-screen for SW->FB (or any aggressor->
victim) coupled-trace pairs.

Method: classic backward/forward crosstalk saturation model (Johnson &
Graham, "High-Speed Digital Design", ch. 5) —

    Kb_geom   = a dimensionless 0..1 proximity-coupling proxy from
                spacing/dielectric-height geometry (NOT a solved Lm/L, Cm/C
                ratio — see limitations)
    Ltd       = coupled length / propagation velocity (one-way delay)
    Tr        = aggressor edge time
    saturated = Ltd >= Tr / 2
    Vb        = Kb_geom * Vstep * (1 if saturated else 2*Ltd/Tr)

This is a coarse order-of-magnitude pre-screen, always available (no
dependency on openEMS), meant to flag "clearly fine" vs "worth a full-wave
check" — not a replacement for the openEMS run in si_sim/openems_coupled_lines.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

C0_MM_PER_S = 299792458.0 * 1000.0  # mm/s


@dataclass
class AnalyticalSIInputs:
    ic_reference: str
    aggressor_net: str
    victim_net: str
    spacing_mm: float
    coupled_length_mm: float
    dielectric_height_mm: float
    epsilon_r: float
    v_step_v: float
    rise_time_s: float
    shield_remaining_factor: float = 1.0
    shield_notes: str = ""


def _effective_er(epsilon_r: float) -> float:
    # Microstrip effective permittivity approximation (Hammerstad), used only
    # to scale propagation velocity — not a precision impedance calc.
    return (epsilon_r + 1.0) / 2.0 + (epsilon_r - 1.0) / 2.0 * 0.5


def estimate_coupling(inputs: AnalyticalSIInputs) -> Dict[str, Any]:
    s = inputs.spacing_mm
    h = inputs.dielectric_height_mm
    ratio = s / h if h > 0 else float("inf")

    # Geometric proximity-coupling proxy: 1 when traces touch (s=0), falls off
    # as (s/h) grows. This is NOT derived from a field solve — it is a
    # monotonic heuristic curve consistent with published NEXT-vs-spacing
    # trends (closer spacing / thinner dielectric = more coupling).
    kb_geom = 1.0 / (1.0 + ratio ** 2)

    er_eff = _effective_er(inputs.epsilon_r)
    v_prop_mm_per_s = C0_MM_PER_S / math.sqrt(er_eff)
    ltd_s = inputs.coupled_length_mm / v_prop_mm_per_s

    tr = inputs.rise_time_s
    saturated = ltd_s >= (tr / 2.0) if tr > 0 else True
    scale = 1.0 if saturated else min(1.0, 2.0 * ltd_s / tr)

    vb_v = kb_geom * inputs.v_step_v * scale * max(0.0, min(1.0, inputs.shield_remaining_factor))
    vb_mv = vb_v * 1000.0

    if vb_mv < 5.0:
        status = "pass"
    elif vb_mv < 30.0:
        status = "warn"
    else:
        status = "fail"

    result = {
        "source": "toolchain",
        "method": "analytical_proximity_heuristic",
        "ic_reference": inputs.ic_reference,
        "net_name": inputs.victim_net,
        "aggressor_net": inputs.aggressor_net,
        "spacing_mm": s,
        "coupled_length_mm": inputs.coupled_length_mm,
        "dielectric_height_mm": h,
        "spacing_to_height_ratio": round(ratio, 2),
        "kb_geom_proxy": round(kb_geom, 6),
        "shield_remaining_factor": inputs.shield_remaining_factor,
        "one_way_delay_s": ltd_s,
        "rise_time_s": tr,
        "saturated_regime": saturated,
        "headline_metric": f"{vb_mv:.4f} mV induced (est.)",
        "induced_voltage_estimate_v": vb_v,
        "induced_voltage_estimate_mv": round(vb_mv, 4),
        "status": status,
        "thresholds_mv": {"pass_max": 5.0, "warn_max": 30.0},
        "limitations": [
            "kb_geom_proxy is a geometric heuristic (monotonic in spacing/height), "
            "NOT a solved mutual-C/L ratio from a field solver.",
            "Assumes straight parallel coupled-line geometry; actual routing may be "
            "non-parallel (usually reduces real coupling vs. this estimate).",
            "Use si_sim/openems_coupled_lines.py (full-wave) for a higher-confidence "
            "number before signing off a marginal (warn/fail) result.",
        ],
    }
    if inputs.shield_remaining_factor < 1.0:
        result["limitations"].append(
            "Same-layer grounded guard: kb_geom is scaled by shield_remaining_factor "
            f"({inputs.shield_remaining_factor:.2f}). Heuristic, not a solved guard-trace model."
            + (f" {inputs.shield_notes}" if inputs.shield_notes else "")
        )
    return result


def from_toolchain_geometry(
    ic_reference: str,
    aggressor_net: str,
    victim_net: str,
    spacing_mm: float,
    coupled_length_mm: float,
    dielectric_height_mm: float,
    epsilon_r: float,
    v_step_v: float,
    rise_time_s: float,
    shield_remaining_factor: float = 1.0,
    shield_notes: str = "",
) -> Dict[str, Any]:
    return estimate_coupling(
        AnalyticalSIInputs(
            ic_reference=ic_reference,
            aggressor_net=aggressor_net,
            victim_net=victim_net,
            spacing_mm=spacing_mm,
            coupled_length_mm=coupled_length_mm,
            dielectric_height_mm=dielectric_height_mm,
            epsilon_r=epsilon_r,
            v_step_v=v_step_v,
            rise_time_s=rise_time_s,
            shield_remaining_factor=shield_remaining_factor,
            shield_notes=shield_notes,
        )
    )
