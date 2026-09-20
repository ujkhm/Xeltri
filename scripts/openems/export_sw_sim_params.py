#!/usr/bin/env python3
"""
Export TPS56637 SW-node simulation parameters for openEMS (prep step).

Reads analysis/board_model.json stackup + PCB geometry for U3 SW/FB region.
Does NOT run openEMS — outputs JSON for a follow-up EM simulation session.

Usage:
  python scripts/openems/export_sw_sim_params.py --ic U3
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from kicad_readonly.pcb import load_pcb
from kicad_readonly.pcb_paths import format_pcb_source_banner, resolve_pcb_path
from kicad_readonly.sw_copper import analyze_sw_copper, segment_dict, sw_fb_clearance, sw_segments
from kicad_readonly.buck_nets import fb_net_name, resolve_buck_nets, waveform_defaults
DEFAULT_BOARD_MODEL = REPO_ROOT / "analysis" / "board_model.json"
DEFAULT_OUT = REPO_ROOT / "analysis" / "u3_sw_sim_params.json"

# TPS56637 datasheet defaults (Rev. A) — verify against your BOM/schematic
TPS56637_DEFAULTS = {
    "f_sw_hz": 500_000,
    "t_on_min_ns": 50,
    "v_in_assumed_v": 12.0,
    "v_out_assumed_v": 5.0,
    "i_out_assumed_a": 2.0,
    "l_inductor_uh": 3.3,
}


def _load_stackup(board_model_path: Path) -> list[dict]:
    data = json.loads(board_model_path.read_text(encoding="utf-8"))
    return data["board"]["stackup"]


def _openems_stackup_layers(stackup: list[dict]) -> list[dict]:
    """Map KiCad stackup to openEMS-friendly dielectric/copper list."""
    layers = []
    for entry in stackup:
        name = entry.get("name", "")
        if entry.get("type") == "copper" or name.endswith(".Cu"):
            layers.append(
                {
                    "name": name,
                    "kind": "copper",
                    "thickness_mm": entry.get("thickness_mm"),
                }
            )
        elif entry.get("epsilon_r"):
            layers.append(
                {
                    "name": name,
                    "kind": "dielectric",
                    "thickness_mm": entry.get("thickness_mm"),
                    "epsilon_r": entry.get("epsilon_r"),
                    "loss_tangent": entry.get("loss_tangent"),
                    "material": entry.get("material"),
                }
            )
    return layers


def _buck_waveform(params: dict) -> dict:
    vin = params["v_in_assumed_v"]
    vout = params["v_out_assumed_v"]
    iout = params["i_out_assumed_a"]
    l_h = params["l_inductor_uh"] * 1e-6
    fsw = params["f_sw_hz"]
    d = vout / vin
    t = 1.0 / fsw
    ton = d * t
    ipp = (vin - vout) * ton / l_h
    il_peak = iout + ipp / 2.0
    # SW node (sync buck): toggles ~0 V to ~Vin; edge ~t_on_min as lower bound
    tr = params["t_on_min_ns"] * 1e-9
    return {
        "duty_cycle": round(d, 4),
        "period_s": t,
        "t_on_s": ton,
        "i_ripple_pp_a": round(ipp, 4),
        "i_peak_a": round(il_peak, 4),
        "sw_v_low_v": 0.0,
        "sw_v_high_v": vin,
        "sw_rise_time_s_assumed": tr,
        "sw_fall_time_s_assumed": tr,
        "dv_dt_v_per_s": round(vin / tr, 2),
        "note": (
            "Ideal square wave with datasheet tON(MIN) as edge-time placeholder; "
            "replace with measured rise/fall or EVM data for accuracy."
        ),
    }


def _geometry_bundle(model, ic_ref: str) -> dict:
    fb_net = fb_net_name(model, ic_ref)
    sw_cu = analyze_sw_copper(model, ic_ref)
    sw_segs = sw_segments(model, ic_ref)
    fb_segs = [s for s in model.segments if model.net_name(s.net_id) == fb_net]
    ic_fp = next((f for f in model.footprints if f.reference == ic_ref), None)
    clearance = sw_fb_clearance(model, ic_ref)
    min_dist = clearance.get("distance_mm")

    return {
        "ic_reference": ic_ref,
        "sw_net": sw_cu["sw_net"],
        "fb_net": fb_net,
        "sw_segment_count": sw_cu["sw_segment_count"],
        "sw_zone_count": sw_cu["sw_zone_count"],
        "sw_wide_trace_count": sw_cu["sw_wide_trace_count"],
        "sw_segments": [segment_dict(s) for s in sw_segs],
        "sw_copper_detected": sw_cu["sw_copper_detected"],
        "sw_copper_source": sw_cu["sw_copper_source"],
        "sw_zones_detected": sw_cu["sw_zones_detected"],
        "sw_total_trace_length_mm": sw_cu["sw_total_trace_length_mm"],
        "sw_wide_trace_length_mm": sw_cu["sw_wide_trace_length_mm"],
        "sw_fb_min_distance_mm": round(min_dist, 4) if min_dist else None,
        "ic_at_mm": list(ic_fp.at) if ic_fp else None,
        "toolchain_note": sw_cu["toolchain_note"],
        "leftover_copper": sw_cu.get("leftover_copper"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Export SW sim params for openEMS")
    parser.add_argument(
        "--pcb",
        type=Path,
        default=None,
        help="Path to .kicad_pcb (default: auto — portable KiCad copy if newer)",
    )
    parser.add_argument("--board-model", type=Path, default=DEFAULT_BOARD_MODEL)
    parser.add_argument("--ic", type=str, default="U3")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--vin", type=float, default=None)
    parser.add_argument("--vout", type=float, default=None)
    parser.add_argument("--iout", type=float, default=None)
    args = parser.parse_args()

    pcb_path, pcb_reason = resolve_pcb_path(args.pcb)
    print(format_pcb_source_banner(pcb_path, pcb_reason))

    stackup = _openems_stackup_layers(_load_stackup(args.board_model))
    model = load_pcb(pcb_path)
    ic = next((f for f in model.footprints if f.reference == args.ic), None)
    nets = resolve_buck_nets(model, args.ic)
    sim_params = waveform_defaults(ic, nets)
    if args.vin is not None:
        sim_params["v_in_assumed_v"] = args.vin
    if args.vout is not None:
        sim_params["v_out_assumed_v"] = args.vout
    if args.iout is not None:
        sim_params["i_out_assumed_a"] = args.iout

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "toolchain",
        "purpose": "openEMS prep — not EM simulation results",
        "pcb_file": str(pcb_path),
        "pcb_resolution": pcb_reason,
        "ic": args.ic,
        "stackup_openems": stackup,
        "board_thickness_mm": model.board_thickness_mm,
        "copper_layers": model.copper_layers,
        "tps56637_waveform_analytical": _buck_waveform(sim_params),
        "geometry": _geometry_bundle(model, args.ic),
        "openems_next_steps": [
            "Install openEMS + CSXCAD (Octave/Python) on analysis machine",
            "Build 2.5D/3D model: F.Cu SW polygon + In1 GNDA/GND planes from zones_geometry.json",
            "Excite SW with pulsed voltage source (500 kHz, Vin amplitude)",
            "Probe coupled voltage on FB net path / AGND",
            "Sweep: SW pour area, SW-FB spacing, edge rate",
        ],
        "limitations": [
            "Analytical waveform is ideal buck; no dead-time, ring, or package parasitics",
            "Geometry is segment/zone index only; openEMS needs meshed shapes",
            "Assumed Vin/Vout/Iout — confirm from schematic operating point",
        ],
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote: {args.out}")
    geom = out["geometry"]
    print(
        f"  SW-FB min: {geom['sw_fb_min_distance_mm']} mm, "
        f"SW copper: {geom['sw_copper_source']} "
        f"(zones={geom['sw_zone_count']}, wide_traces={geom['sw_wide_trace_count']})"
    )
    print(f"  f_sw={sim_params['f_sw_hz']/1000} kHz, Vsw={args.vin} V pk-pk (ideal)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
