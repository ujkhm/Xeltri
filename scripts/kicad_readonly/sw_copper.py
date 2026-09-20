"""Detect switch-node (SW) copper from KiCad PCB geometry.

KiCad may represent SW copper as filled zones *or* as wide routed traces.
Only counting zones misses valid board saves (e.g. 0.8 mm SW pours as segments).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .geometry import (
    polygon_area,
    polygon_bbox,
)
from .pcb import PcbModel, Segment, Zone

# Power-class SW trace width (mm). TPS56637 layout often uses 0.6–1.0 mm on F.Cu.
WIDE_SW_TRACE_MM = 0.6


def sw_net_name(ic_ref: str, model: Optional[PcbModel] = None) -> str:
    if model is None:
        return f"Net-({ic_ref}-SW)"
    from .buck_nets import switch_net_name

    return switch_net_name(model, ic_ref)


def fb_net_name(ic_ref: str, model: Optional[PcbModel] = None) -> str:
    if model is None:
        return f"Net-({ic_ref}-FB)"
    from .buck_nets import fb_net_name as _fb

    return _fb(model, ic_ref)


def sw_zone_records(model: PcbModel, ic_ref: str) -> List[Zone]:
    """All copper zone objects on the SW net (filled or outline-only)."""
    sw_net = sw_net_name(ic_ref, model)
    return [z for z in model.zones if z.net_name == sw_net and not z.is_keepout]


def sw_zones(model: PcbModel, ic_ref: str) -> List[Zone]:
    return [z for z in sw_zone_records(model, ic_ref) if z.fill]


def sw_segments(model: PcbModel, ic_ref: str) -> List[Segment]:
    sw_net = sw_net_name(ic_ref, model)
    return [s for s in model.segments if model.net_name(s.net_id) == sw_net]


def sw_wide_segments(
    model: PcbModel,
    ic_ref: str,
    min_width_mm: float = WIDE_SW_TRACE_MM,
) -> List[Segment]:
    return [s for s in sw_segments(model, ic_ref) if s.width_mm >= min_width_mm]


def sw_zone_polygons(model: PcbModel, ic_ref: str) -> List[List[Tuple[float, float]]]:
    polys: List[List[Tuple[float, float]]] = []
    for z in sw_zone_records(model, ic_ref):
        for geom in z.geometry_by_layer:
            if geom.points and len(geom.points) >= 3:
                polys.append(list(geom.points))
    return polys


def sw_zone_metrics(model: PcbModel, ic_ref: str) -> Dict[str, Any]:
    """BBox extent and area of SW pour(s) — used when there are no SW traces."""
    polys = sw_zone_polygons(model, ic_ref)
    if not polys:
        return {
            "extent_mm": None,
            "area_mm2": 0.0,
            "bbox_mm": None,
            "min_width_mm": None,
        }
    boxes = [polygon_bbox(p) for p in polys]
    min_x = min(b[0] for b in boxes)
    min_y = min(b[1] for b in boxes)
    max_x = max(b[2] for b in boxes)
    max_y = max(b[3] for b in boxes)
    w = max_x - min_x
    h = max_y - min_y
    area = sum(polygon_area(p) for p in polys)
    return {
        "extent_mm": round(max(w, h), 4),
        "area_mm2": round(area, 4),
        "bbox_mm": {
            "min_x": round(min_x, 4),
            "min_y": round(min_y, 4),
            "max_x": round(max_x, 4),
            "max_y": round(max_y, 4),
        },
        "min_width_mm": round(min(w, h), 4),
    }


def sw_fb_clearance(
    model: PcbModel,
    ic_ref: str,
    fb_segments: Optional[Sequence[Segment]] = None,
) -> Dict[str, Any]:
    """Min SW–FB distance on primary copper (pads + pour + functional traces).

    Leftover tracks inside pads/pours and sub-0.12 mm stubs are excluded so SI
    does not use residual centerlines as the aggressor/victim geometry.
    """
    from .leftover_copper import primary_clearance

    sw_net = sw_net_name(ic_ref, model)
    fb_net = fb_net_name(ic_ref, model)
    primary = primary_clearance(model, ic_ref, fb_segments=fb_segments)
    zm = sw_zone_metrics(model, ic_ref)

    nearest_sw_edge = primary.get("nearest_sw_edge")
    if nearest_sw_edge and nearest_sw_edge.get("width_mm") is None:
        nearest_sw_edge = dict(nearest_sw_edge)
        nearest_sw_edge["width_mm"] = zm.get("min_width_mm") or 0.8

    leftover = primary.get("leftover") or {}
    return {
        "sw_net": sw_net,
        "fb_net": fb_net,
        "distance_mm": primary.get("distance_mm"),
        "source": primary.get("source") or "none",
        "nearest_fb_segment": primary.get("nearest_fb_segment"),
        "nearest_sw_edge": nearest_sw_edge,
        "zone_metrics": zm,
        "gap_ends_mm": primary.get("gap_ends_mm"),
        "leftover_copper": leftover,
        "legacy_centerline_distance_mm": leftover.get("legacy_centerline_distance_mm"),
        "leftover_would_have_driven_si": bool(primary.get("leftover_would_have_driven_si")),
        "pour_only_distance_mm": leftover.get("pour_only_distance_mm"),
    }


def _copper_sources(zones: Sequence[Zone], wide_segments: Sequence[Segment]) -> List[str]:
    sources: List[str] = []
    if zones:
        sources.append("zone")
    if wide_segments:
        sources.append("wide_trace")
    return sources


def _pour_like_wide_segments(wide: Sequence[Segment]) -> bool:
    """Heuristic: uniform wide F.Cu segments often come from a filled pour."""
    if not wide:
        return False
    fc = [s for s in wide if s.layer == "F.Cu"]
    if len(fc) < 2:
        return False
    widths = {round(s.width_mm, 3) for s in fc}
    return len(widths) == 1 and fc[0].width_mm >= WIDE_SW_TRACE_MM


def analyze_sw_copper(
    model: PcbModel,
    ic_ref: str,
    min_wide_width_mm: float = WIDE_SW_TRACE_MM,
) -> Dict[str, Any]:
    """Summarize SW copper presence for one buck IC."""
    zone_records = sw_zone_records(model, ic_ref)
    zones = [z for z in zone_records if z.fill]
    segments = sw_segments(model, ic_ref)
    wide = [s for s in segments if s.width_mm >= min_wide_width_mm]
    pour_like = _pour_like_wide_segments(wide)
    sources = _copper_sources(zones, wide)
    if pour_like and not zones:
        sources = ["pour_segments"] + [s for s in sources if s != "wide_trace"]
    detected = bool(sources)

    filled_polys = sum(len(z.filled_polygons) for z in zone_records)
    if zones and wide:
        note = (
            f"SW copper: {len(zones)} zone(s) + {len(wide)} wide segment(s) "
            f"(>= {min_wide_width_mm} mm) in saved PCB"
        )
    elif zones:
        note = (
            f"SW copper zone(s): {len(zones)} "
            f"({filled_polys} filled_polygon in file)"
        )
    elif pour_like:
        w = wide[0].width_mm
        note = (
            f"SW copper detected as {len(wide)} uniform {w} mm F.Cu segment(s) "
            f"(pour-like geometry). Saved file has no (zone {sw_net_name(ic_ref, model)}) "
            f"record — KiCad UI may show a pour polygon that is not yet written "
            f"as a zone entry; Ctrl+S and Fill Zone, then re-run analysis."
        )
    elif wide:
        note = (
            f"SW copper as {len(wide)} wide segment(s) (>= {min_wide_width_mm} mm); "
            f"no zone record for {sw_net_name(ic_ref, model)} in saved PCB"
        )
    elif segments:
        note = (
            f"SW net has {len(segments)} narrow trace(s) only "
            f"(< {min_wide_width_mm} mm); no pour zone"
        )
    else:
        note = "No SW segments or zones in saved PCB"

    zm = sw_zone_metrics(model, ic_ref)
    if zones:
        note = note + f"; zone extent {zm['extent_mm']} mm, area {zm['area_mm2']} mm2"

    leftover = None
    try:
        from .leftover_copper import leftover_report

        leftover = leftover_report(model, ic_ref)
        leftover_public = {k: v for k, v in leftover.items() if not k.startswith("_")}
        n_res = leftover_public.get("residual_trace_count") or 0
        if n_res:
            note = note + f"; excluded {n_res} leftover SW/FB trace(s) from SI geometry"
    except Exception:
        leftover_public = None

    return {
        "ic_reference": ic_ref,
        "sw_net": sw_net_name(ic_ref, model),
        "sw_zone_count": len(zones),
        "sw_zone_record_count": len(zone_records),
        "sw_zone_filled_polygon_count": filled_polys,
        "sw_zone_extent_mm": zm["extent_mm"],
        "sw_zone_area_mm2": zm["area_mm2"],
        "sw_zone_bbox_mm": zm["bbox_mm"],
        "sw_segment_count": len(segments),
        "sw_wide_trace_count": len(wide),
        "sw_wide_trace_min_width_mm": min_wide_width_mm,
        "sw_pour_like_segments": pour_like,
        "sw_copper_detected": detected,
        "sw_copper_source": "+".join(sources) if sources else "none",
        "sw_total_trace_length_mm": round(sum(s.length_mm for s in segments), 4),
        "sw_wide_trace_length_mm": round(sum(s.length_mm for s in wide), 4),
        "leftover_copper": leftover_public,
        "toolchain_note": note,
        # Back-compat aliases used by openEMS export
        "sw_zones_detected": len(zones) > 0,
        "sw_zones_detected_legacy": len(zones) > 0,
    }


def segment_dict(s: Segment) -> Dict[str, Any]:
    return {
        "layer": s.layer,
        "width_mm": s.width_mm,
        "start_mm": list(s.start),
        "end_mm": list(s.end),
        "length_mm": round(s.length_mm, 4),
        "kind": getattr(s, "kind", "segment"),
    }
