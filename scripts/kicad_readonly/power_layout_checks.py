"""Buck converter layout checks: SW-FB spacing and input-cap proximity."""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .buck_nets import BUCK_VALUE_MARKERS, find_buck_ics, resolve_buck_nets
from .geometry import polygons_intersect, segment_intersects_polygon
from .pcb import Footprint, PcbModel, Segment, pad_world_polygon
from .sw_copper import analyze_sw_copper, sw_fb_clearance
from .sw_fb_shield import analyze_sw_fb_shield

Point = Tuple[float, float]

# mm — conservative defaults for TPS56637-class bucks (toolchain thresholds)
SW_FB_PASS_MM = 2.0
SW_FB_WARN_MM = 1.0
# Gap endpoints within this of the IC center count as pin-escape, not inductor.
SW_FB_IC_NEAR_MM = 4.0
VIN_CAP_PASS_MM = 4.0
VIN_CAP_WARN_MM = 6.0

_CAP_VALUE_RE = re.compile(r"(\d+\.?\d*)\s*(p|n|u|m)?f", re.IGNORECASE)
_IC_REF_FROM_NET_RE = re.compile(r"Net-\(U(\d+)-")


def _segment_min_distance(
    a0: Point, a1: Point, b0: Point, b1: Point, samples: int = 24
) -> float:
    best = float("inf")
    for i in range(samples + 1):
        t = i / samples
        pa = (a0[0] + t * (a1[0] - a0[0]), a0[1] + t * (a1[1] - a0[1]))
        for j in range(samples + 1):
            u = j / samples
            pb = (b0[0] + u * (b1[0] - b0[0]), b0[1] + u * (b1[1] - b0[1]))
            best = min(best, math.hypot(pa[0] - pb[0], pa[1] - pb[1]))
    return best


def _nets_min_distance(segments_a: Sequence[Segment], segments_b: Sequence[Segment]) -> float:
    best = float("inf")
    for sa in segments_a:
        for sb in segments_b:
            best = min(
                best, _segment_min_distance(sa.start, sa.end, sb.start, sb.end)
            )
    return best


def _ic_pad_on_net(ic: Footprint, net_name: str):
    for pad in ic.pads:
        if pad.net_name == net_name:
            return pad
    return None


def _ic_sw_fb_pad_metrics(ic: Footprint, sw_net: str, fb_net: str) -> Dict[str, Optional[float]]:
    """Edge-to-edge and center-to-center distance of the IC's own SW and FB pads."""
    sw_pad = _ic_pad_on_net(ic, sw_net)
    fb_pad = _ic_pad_on_net(ic, fb_net)
    if sw_pad is None or fb_pad is None:
        return {"edge_mm": None, "center_mm": None}
    sw_g = pad_world_polygon(sw_pad)
    fb_g = pad_world_polygon(fb_pad)
    edge = None
    if sw_g is not None and fb_g is not None:
        edge = round(float(sw_g.distance(fb_g)), 4)
    center = round(
        math.hypot(sw_pad.at[0] - fb_pad.at[0], sw_pad.at[1] - fb_pad.at[1]), 4
    )
    return {"edge_mm": edge, "center_mm": center}


def _gap_near_ic(ic: Footprint, gap_ends: Optional[Sequence], radius_mm: float = SW_FB_IC_NEAR_MM) -> bool:
    if not gap_ends or len(gap_ends) < 2:
        return False
    cx, cy = ic.at
    for pt in gap_ends[:2]:
        if pt is None or len(pt) < 2:
            return False
        if math.hypot(float(pt[0]) - cx, float(pt[1]) - cy) > radius_mm:
            return False
    return True


def _is_capacitor_footprint(fp: Footprint) -> bool:
    val = (fp.value or "").lower()
    fp_name = (fp.footprint or "").lower()
    return bool(_CAP_VALUE_RE.search(val)) or "capacitor" in fp_name


def _ic_ref_from_net(net_name: str) -> Optional[str]:
    m = _IC_REF_FROM_NET_RE.search(net_name)
    return f"U{m.group(1)}" if m else None


def _find_buck_ics(model: PcbModel, region_refs: Optional[Sequence[str]] = None) -> List[Footprint]:
    return find_buck_ics(model, region_refs)


def _buck_nets(ic_ref: str, model: PcbModel) -> Dict[str, Optional[str]]:
    nets = resolve_buck_nets(model, ic_ref)
    return {
        "sw": nets.get("sw"),
        "fb": nets.get("fb"),
        "vin": nets.get("vin"),
        "boot": nets.get("boot"),
        "vout": nets.get("vout"),
        "en": nets.get("en"),
        "switch_alias": nets.get("switch_alias"),
    }


def _ic_vin_pad(ic: Footprint, vin_net: str) -> Optional[Point]:
    for pad in ic.pads:
        if pad.net_name == vin_net:
            return pad.at
    return None


def _nearest_vin_capacitor(
    model: PcbModel, ic: Footprint, vin_net: str, vin_pt: Point
) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    for fp in model.footprints:
        if fp.reference == ic.reference:
            continue
        if not _is_capacitor_footprint(fp):
            continue
        for pad in fp.pads:
            if pad.net_name != vin_net:
                continue
            d = math.hypot(pad.at[0] - vin_pt[0], pad.at[1] - vin_pt[1])
            if best is None or d < best["distance_mm"]:
                best = {
                    "reference": fp.reference,
                    "value": fp.value,
                    "pad": pad.number,
                    "pad_at_mm": list(pad.at),
                    "distance_mm": round(d, 3),
                }
    return best


def _status_from_threshold(
    value_mm: float, pass_max: float, warn_max: float, higher_is_better: bool = False
) -> Tuple[str, str]:
    """For distance checks where larger is better (SW-FB spacing)."""
    if higher_is_better:
        if value_mm >= pass_max:
            return "pass", f"{value_mm:.2f} mm >= {pass_max} mm"
        if value_mm >= warn_max:
            return "warn", f"{value_mm:.2f} mm (target >= {pass_max} mm)"
        return "fail", f"{value_mm:.2f} mm < {warn_max} mm minimum"
    # Smaller is better (cap distance)
    if value_mm <= pass_max:
        return "pass", f"{value_mm:.2f} mm <= {pass_max} mm"
    if value_mm <= warn_max:
        return "warn", f"{value_mm:.2f} mm (target <= {pass_max} mm)"
    return "fail", f"{value_mm:.2f} mm > {warn_max} mm"


def analyze_buck_power_layout(
    model: PcbModel,
    polygon: Optional[Sequence[Point]] = None,
    region_result: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Per-buck SW-FB spacing and nearest input capacitor distance."""
    region_refs: List[str] = []
    if region_result:
        for fp in region_result.get("footprints", []):
            val = f"{fp.get('value', '')} {fp.get('footprint', '')}"
            if any(m.lower() in val.lower() for m in BUCK_VALUE_MARKERS):
                region_refs.append(fp["reference"])
        for ns in region_result.get("nets_summary", []):
            ref = _ic_ref_from_net(ns.get("net_name", ""))
            if ref and ref not in region_refs:
                region_refs.append(ref)

    results: List[Dict[str, Any]] = []
    for ic in _find_buck_ics(model, region_refs or None):
        nets = _buck_nets(ic.reference, model)
        sw_segs = [s for s in model.segments if model.net_name(s.net_id) == nets["sw"]]
        fb_segs = [s for s in model.segments if model.net_name(s.net_id) == nets["fb"]]
        vin_pt = _ic_vin_pad(ic, nets["vin"] or "")

        clearance = sw_fb_clearance(model, ic.reference, fb_segs)
        sw_fb_mm = clearance["distance_mm"]
        shield = analyze_sw_fb_shield(model, ic.reference)
        sw_fb_status, sw_fb_notes = ("info", "no SW copper or FB segments")
        if sw_fb_mm is not None:
            sw_fb_status, sw_fb_notes = _status_from_threshold(
                sw_fb_mm, SW_FB_PASS_MM, SW_FB_WARN_MM, higher_is_better=True
            )
            sw_fb_notes = sw_fb_notes + f" via {clearance['source']}"
            leftover = clearance.get("leftover_copper") or {}
            n_res = leftover.get("residual_trace_count") or 0
            if n_res:
                sw_fb_notes = (
                    sw_fb_notes
                    + f"; excluded {n_res} leftover SW/FB trace(s) "
                    f"(inside pad/pour or <{leftover.get('short_stub_mm', 0.12)} mm stub)"
                )
            if clearance.get("leftover_would_have_driven_si"):
                sw_fb_notes = (
                    sw_fb_notes
                    + "; pre-fix SI would have used leftover centerline geometry"
                )
            if shield.get("same_layer_blocks_line_of_sight"):
                sw_fb_notes = (
                    sw_fb_notes
                    + f"; coplanar {shield.get('same_layer_shield_net')} "
                    f"{shield.get('same_layer_shield_kind')} blocks shortest path"
                )
            pour_path = shield.get("pour_path") or {}
            if pour_path.get("same_layer_blocks_line_of_sight") and not shield.get(
                "same_layer_blocks_line_of_sight"
            ):
                sw_fb_notes = (
                    sw_fb_notes
                    + f"; {pour_path.get('same_layer_shield_net')} guards SW-pour–FB "
                    f"{pour_path.get('gap_length_mm')} mm but not this shorter path; "
                    f"pass threshold still >= {SW_FB_PASS_MM} mm copper-to-copper"
                )

        pad_metrics = _ic_sw_fb_pad_metrics(ic, nets["sw"] or "", nets["fb"] or "")
        near_sw = clearance.get("nearest_sw_edge") or {}
        at_inductor = (
            near_sw.get("kind") == "pad"
            and near_sw.get("footprint")
            and near_sw.get("footprint") != ic.reference
        )
        gap_ends = clearance.get("gap_ends_mm") or shield.get("gap_ends_mm")
        ic_edge = pad_metrics.get("edge_mm")
        sw_fb_package_limited = bool(
            sw_fb_mm is not None
            and ic_edge is not None
            and not at_inductor
            and _gap_near_ic(ic, gap_ends)
            and sw_fb_mm + 0.15 >= ic_edge
        )
        if sw_fb_package_limited:
            sw_fb_notes = (
                sw_fb_notes
                + f"; package-limited: IC SW–FB pads {ic_edge:.2f} mm edge-to-edge "
                f"(centers {pad_metrics.get('center_mm')} mm). "
                f"The {SW_FB_PASS_MM:.1f} mm guideline is for FB routing away from "
                "the inductor/SW island, not IC pin pitch — do not try to beat the package."
            )
            if sw_fb_status in ("warn", "fail"):
                sw_fb_status = "pass"

        nearest_cap = None
        vin_cap_status, vin_cap_notes = ("info", "VIN pad not found")
        if vin_pt and nets["vin"]:
            nearest_cap = _nearest_vin_capacitor(model, ic, nets["vin"], vin_pt)
            if nearest_cap:
                vin_cap_status, vin_cap_notes = _status_from_threshold(
                    nearest_cap["distance_mm"],
                    VIN_CAP_PASS_MM,
                    VIN_CAP_WARN_MM,
                    higher_is_better=False,
                )
            else:
                vin_cap_status, vin_cap_notes = ("warn", "no capacitor on VIN net")

        in_region = True
        if polygon is not None:
            from .sw_copper import sw_zone_polygons

            in_region = (
                segment_intersects_polygon(ic.at, ic.at, polygon)
                or any(
                    segment_intersects_polygon(s.start, s.end, polygon)
                    for s in sw_segs + fb_segs
                )
                or any(
                    polygons_intersect(poly, polygon)
                    for poly in sw_zone_polygons(model, ic.reference)
                )
            )

        overall = "pass"
        for st in (sw_fb_status, vin_cap_status):
            if st == "fail":
                overall = "fail"
            elif st == "warn" and overall == "pass":
                overall = "warn"

        sw_cu = analyze_sw_copper(model, ic.reference)

        results.append(
            {
                "source": "toolchain",
                "ic_reference": ic.reference,
                "ic_value": ic.value,
                "ic_at_mm": list(ic.at),
                "in_region": in_region,
                "sw_net": nets["sw"],
                "fb_net": nets["fb"],
                "vin_net": nets["vin"],
                "switch_alias": nets.get("switch_alias") or "SW",
                "sw_copper_detected": sw_cu["sw_copper_detected"],
                "sw_copper_source": sw_cu["sw_copper_source"],
                "sw_zone_count": sw_cu["sw_zone_count"],
                "sw_wide_trace_count": sw_cu["sw_wide_trace_count"],
                "sw_copper_note": sw_cu["toolchain_note"],
                "sw_fb_min_distance_mm": round(sw_fb_mm, 3) if sw_fb_mm is not None else None,
                "sw_fb_status": sw_fb_status,
                "sw_fb_notes": sw_fb_notes,
                "sw_fb_source": clearance["source"],
                "sw_fb_package_limited": sw_fb_package_limited,
                "sw_fb_ic_pad_edge_mm": pad_metrics.get("edge_mm"),
                "sw_fb_ic_pad_center_mm": pad_metrics.get("center_mm"),
                "leftover_copper": clearance.get("leftover_copper"),
                "leftover_would_have_driven_si": clearance.get(
                    "leftover_would_have_driven_si"
                ),
                "sw_fb_thresholds_mm": {
                    "pass_min": SW_FB_PASS_MM,
                    "warn_min": SW_FB_WARN_MM,
                },
                "sw_fb_shield": shield,
                "nearest_vin_cap": nearest_cap,
                "vin_cap_status": vin_cap_status,
                "vin_cap_notes": vin_cap_notes,
                "vin_cap_thresholds_mm": {
                    "pass_max": VIN_CAP_PASS_MM,
                    "warn_max": VIN_CAP_WARN_MM,
                },
                "overall_status": overall,
            }
        )
    return results
