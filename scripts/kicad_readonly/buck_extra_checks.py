"""Additional buck-converter layout checks: BOOT cap distance, SW copper length
scoring, and GND via count under the IC footprint (thermal-pad proxy).

These were previously listed as a known gap in region_summary.py's
follow_up_optimization ("Add BOOT cap distance and SW trace length scoring in
a future pass") — this module closes that gap with the same toolchain/
ai_assisted separation used elsewhere.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .circuit_graph import find_boot_cap_on_path
from .buck_nets import resolve_buck_nets
from .design_rules import load_clearance_mm
from .pcb import pad_world_polygon
from .power_layout_checks import _status_from_threshold
from .sw_copper import analyze_sw_copper, sw_zone_polygons
from .geometry import _shapely_polygon

Point = Tuple[float, float]

# mm — conservative defaults; ANY IC-specific override should come from
# datasheet_ref.py profiles, these are the toolchain fallback thresholds.
BOOT_CAP_PASS_MM = 3.0
BOOT_CAP_WARN_MM = 5.0
SW_LEN_PASS_MM = 3.0
SW_LEN_WARN_MM = 6.0
THERMAL_VIA_PASS_COUNT = 4
THERMAL_VIA_WARN_COUNT = 1
THERMAL_VIA_SEARCH_RADIUS_MM = 2.5


def _footprint_pad(fp: Footprint, net_name: str) -> Optional[Point]:
    for pad in fp.pads:
        if pad.net_name == net_name:
            return pad.at
    return None


def _nearest_cap_on_two_nets(
    model: PcbModel,
    exclude_ref: str,
    net_a: str,
    net_b: str,
    anchor: Point,
) -> Optional[Dict[str, Any]]:
    """Find a 2-pin passive bridging net_a and net_b (e.g. BOOT cap), nearest to anchor."""
    best: Optional[Dict[str, Any]] = None
    for fp in model.footprints:
        if fp.reference == exclude_ref:
            continue
        pad_nets = {p.net_name for p in fp.pads if p.net_name}
        if net_a not in pad_nets or net_b not in pad_nets:
            continue
        pad_on_a = next((p for p in fp.pads if p.net_name == net_a), None)
        if pad_on_a is None:
            continue
        d = math.hypot(pad_on_a.at[0] - anchor[0], pad_on_a.at[1] - anchor[1])
        if best is None or d < best["distance_mm"]:
            best = {
                "reference": fp.reference,
                "value": fp.value,
                "pad_at_mm": list(pad_on_a.at),
                "distance_mm": round(d, 3),
            }
    return best


def _pad_box(pad) -> Any:
    return pad_world_polygon(pad)


def _sw_node_copper(model: PcbModel, ic: Footprint, exclude_refs: Sequence[str]):
    """SW pour plus SW pads on nearby passives (e.g. inductor pad 1)."""
    from shapely.ops import unary_union

    sw_net = resolve_buck_nets(model, ic.reference)["sw"]
    geoms = [
        g
        for g in (_shapely_polygon(p) for p in sw_zone_polygons(model, ic.reference))
        if g is not None
    ]
    skip = set(exclude_refs) | {ic.reference}
    for fp in model.footprints:
        if fp.reference in skip:
            continue
        for pad in fp.pads:
            if pad.net_name != sw_net:
                continue
            geoms.append(_pad_box(pad))
    geoms = [g for g in geoms if g is not None]
    if not geoms:
        return None
    return unary_union(geoms)


def _vias_near(model: PcbModel, center: Point, radius_mm: float, net_names: Sequence[str]) -> int:
    count = 0
    for via in model.vias:
        if net_names and model.net_name(via.net_id) not in net_names:
            continue
        if math.hypot(via.at[0] - center[0], via.at[1] - center[1]) <= radius_mm:
            count += 1
    return count


def analyze_buck_extra_checks(
    model: PcbModel,
    ic: Footprint,
) -> Dict[str, Any]:
    """BOOT cap distance, SW copper length score, GND-via-under-IC count for one buck IC."""
    nets = resolve_buck_nets(model, ic.reference)
    boot_net = nets.get("boot")
    sw_net = nets["sw"]

    boot_pad = _footprint_pad(ic, boot_net) if boot_net else None
    boot_cap = None
    if not nets.get("has_boot_pin"):
        boot_status, boot_notes = (
            "info",
            "no BOOT/BST pin on this IC (internal bootstrap or pin not named BOOT)",
        )
    else:
        boot_status, boot_notes = ("info", "no BOOT pad/net found")
    if boot_pad and boot_net:
        # Direct BOOT—C—SW first; if KiCad auto-named an intermediate net
        # because of a series 0Ω, walk that path too.
        boot_cap = _nearest_cap_on_two_nets(
            model, ic.reference, boot_net, sw_net, boot_pad
        )
        if boot_cap:
            boot_cap["topology"] = "direct_capacitor"
            boot_cap["path_text"] = f"{boot_net} — {boot_cap['reference']}({boot_cap['value']}) — {sw_net}"
            boot_cap["series_zero_ohm"] = []
            boot_cap["intermediate_auto_nets"] = []
        else:
            boot_cap = find_boot_cap_on_path(model, ic, boot_net, sw_net, boot_pad)
        if boot_cap and boot_cap.get("distance_mm") is not None:
            boot_status, boot_notes = _status_from_threshold(
                boot_cap["distance_mm"], BOOT_CAP_PASS_MM, BOOT_CAP_WARN_MM, higher_is_better=False
            )
            zrefs = boot_cap.get("series_zero_ohm") or []
            auto = boot_cap.get("intermediate_auto_nets") or []
            extra = ""
            if zrefs:
                extra = (
                    f"; series 0Ω {', '.join(zrefs)}"
                    + (f" via auto-net {', '.join(auto)}" if auto else "")
                )
            boot_notes = boot_notes + extra
            if boot_cap.get("path_text"):
                boot_notes = boot_notes + f" | {boot_cap['path_text']}"
        elif boot_cap:
            boot_status, boot_notes = ("info", boot_cap.get("path_text") or "BOOT path found")
        else:
            boot_status, boot_notes = (
                "warn",
                "no capacitor on BOOT–SW (including paths through series 0Ω jumpers)",
            )

    drc = load_clearance_mm(Path(model.source)) if getattr(model, "source", None) else {
        "netclass_default_mm": 0.2,
        "board_min_mm": 0.09,
        "source": "fallback",
    }
    class_mm = float(drc.get("netclass_default_mm") or 0.2)
    boot_to_sw_copper_mm = None
    boot_to_sw_pin_mm = None
    boot_drc_limited = False
    boot_package_limited = False
    boot_opposite_side_of_ic = False
    boot_drc_notes = ""
    sw_pad = _footprint_pad(ic, sw_net)
    if boot_cap and boot_cap.get("pad_at_mm") and sw_pad:
        cap_at = tuple(boot_cap["pad_at_mm"])
        boot_to_sw_pin_mm = round(
            math.hypot(cap_at[0] - sw_pad[0], cap_at[1] - sw_pad[1]), 3
        )
    if boot_cap:
        try:
            cap_fp = next(
                (f for f in model.footprints if f.reference == boot_cap.get("reference")),
                None,
            )
            sw_node = _sw_node_copper(
                model, ic, [boot_cap.get("reference") or ""]
            )
            if cap_fp is not None and boot_pad:
                vx_c = cap_fp.at[0] - ic.at[0]
                vy_c = cap_fp.at[1] - ic.at[1]
                vx_b = boot_pad[0] - ic.at[0]
                vy_b = boot_pad[1] - ic.at[1]
                boot_opposite_side_of_ic = (vx_c * vx_b + vy_c * vy_b) < 0.0
            if sw_node is not None and not sw_node.is_empty and cap_fp is not None:
                best_edge = None
                for pad in cap_fp.pads:
                    d_edge = float(sw_node.distance(_pad_box(pad)))
                    if best_edge is None or d_edge < best_edge:
                        best_edge = d_edge
                if best_edge is not None:
                    boot_to_sw_copper_mm = round(best_edge, 3)
                    boot_drc_limited = boot_to_sw_copper_mm <= class_mm * 1.25
                    cap_ref = boot_cap.get("reference", "?")
                    if boot_drc_limited:
                        boot_drc_notes = (
                            f"{cap_ref}-to-SW node (pour+L pad) {boot_to_sw_copper_mm:.2f} mm ≤ "
                            f"1.25× Default class {class_mm:.2f} mm — DRC floor vs SW copper."
                        )
                    else:
                        boot_drc_notes = (
                            f"{cap_ref}-to-SW node (pour+L pad) {boot_to_sw_copper_mm:.2f} mm; "
                            f"Default class {class_mm:.2f} mm."
                        )
                    if boot_opposite_side_of_ic:
                        boot_drc_notes += (
                            " C4 and IC BOOT pin are on opposite sides of the package; "
                            "moving onto the BOOT pin crosses the SW path or the QFN body."
                        )
                        if boot_to_sw_copper_mm is not None and boot_to_sw_copper_mm <= 2.0:
                            boot_package_limited = True
                    boot_notes = boot_notes + f"; {boot_drc_notes}"
        except Exception:
            boot_drc_notes = "SW copper distance to BOOT cap not computed."

    sw_cu = analyze_sw_copper(model, ic.reference)
    if sw_cu.get("sw_zone_extent_mm"):
        sw_len_mm = sw_cu["sw_zone_extent_mm"]
        sw_len_kind = "zone_extent"
    elif sw_cu["sw_wide_trace_length_mm"]:
        sw_len_mm = sw_cu["sw_wide_trace_length_mm"]
        sw_len_kind = "wide_trace"
    else:
        sw_len_mm = sw_cu["sw_total_trace_length_mm"]
        sw_len_kind = "trace"
    sw_len_status, sw_len_notes = (
        _status_from_threshold(
            sw_len_mm, SW_LEN_PASS_MM, SW_LEN_WARN_MM, higher_is_better=False
        )
        if sw_len_mm
        else ("info", "no SW copper length to score")
    )
    if sw_len_mm and sw_len_kind == "zone_extent":
        sw_len_notes = (
            sw_len_notes
            + f" (zone bbox longer side; area {sw_cu.get('sw_zone_area_mm2')} mm2)"
        )

    gnd_via_count = _vias_near(
        model, ic.at, THERMAL_VIA_SEARCH_RADIUS_MM, ("GND", "GNDA", "AGND")
    )
    if gnd_via_count >= THERMAL_VIA_PASS_COUNT:
        thermal_status, thermal_notes = (
            "pass",
            f"{gnd_via_count} GND via(s) within {THERMAL_VIA_SEARCH_RADIUS_MM} mm of IC center",
        )
    elif gnd_via_count >= THERMAL_VIA_WARN_COUNT:
        thermal_status, thermal_notes = (
            "warn",
            f"only {gnd_via_count} GND via(s) nearby; consider more thermal/stitching vias",
        )
    else:
        thermal_status, thermal_notes = (
            "warn",
            "no GND via found near IC — verify thermal pad venting separately",
        )

    overall = "pass"
    for st in (boot_status, sw_len_status, thermal_status):
        if st == "fail":
            overall = "fail"
        elif st == "warn" and overall == "pass":
            overall = "warn"

    return {
        "source": "toolchain",
        "ic_reference": ic.reference,
        "boot_net": boot_net,
        "boot_cap": boot_cap,
        "boot_cap_distance_mm": boot_cap["distance_mm"] if boot_cap else None,
        "boot_cap_status": boot_status,
        "boot_cap_notes": boot_notes,
        "boot_cap_thresholds_mm": {"pass_max": BOOT_CAP_PASS_MM, "warn_max": BOOT_CAP_WARN_MM},
        "boot_to_sw_copper_mm": boot_to_sw_copper_mm,
        "boot_to_sw_pin_mm": boot_to_sw_pin_mm,
        "boot_drc_clearance_mm": class_mm,
        "boot_drc_limited": boot_drc_limited,
        "boot_opposite_side_of_ic": boot_opposite_side_of_ic,
        "boot_package_limited": boot_package_limited,
        "boot_drc_notes": boot_drc_notes,
        "sw_length_used_mm": round(sw_len_mm, 4) if sw_len_mm else None,
        "sw_length_kind": sw_len_kind if sw_len_mm else None,
        "sw_length_status": sw_len_status,
        "sw_length_notes": sw_len_notes,
        "sw_length_thresholds_mm": {"pass_max": SW_LEN_PASS_MM, "warn_max": SW_LEN_WARN_MM},
        "gnd_via_count_near_ic": gnd_via_count,
        "thermal_via_status": thermal_status,
        "thermal_via_notes": thermal_notes,
        "thermal_via_search_radius_mm": THERMAL_VIA_SEARCH_RADIUS_MM,
        "overall_status": overall,
    }


def analyze_all_buck_extra_checks(
    model: PcbModel, ic_refs: Sequence[str]
) -> List[Dict[str, Any]]:
    results = []
    by_ref = {fp.reference: fp for fp in model.footprints}
    for ref in ic_refs:
        ic = by_ref.get(ref)
        if ic is None:
            continue
        results.append(analyze_buck_extra_checks(model, ic))
    return results
