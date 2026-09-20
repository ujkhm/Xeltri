"""Detect grounded copper that sits *between* SW and FB on the signal layer.

The SW–FB distance check is edge-to-edge. A coplanar GNDA/AGND/GND guard
trace or pour can block that line of sight and cut coupling even when the
numeric spacing is still < 2 mm. Inner-layer analog ground under FB is a
return plane, not a coplanar guard — those two cases are reported separately.

All outputs are toolchain geometry (Shapely). Coupling reduction factors are
heuristic scales applied later by the SI estimators, not field-solve results.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .geometry import _shapely_polygon
from .pcb import PcbModel, StackupLayer

Point = Tuple[float, float]

SHIELD_NETS_PREFERRED = ("GNDA", "AGND")
SHIELD_NETS_POWER = ("GND",)
ALL_SHIELD_NETS = SHIELD_NETS_PREFERRED + SHIELD_NETS_POWER

# Heuristic remaining coupling if a same-layer grounded guard crosses the gap.
# Stitched (via close to the gap) is more effective than a long un-via'd stub.
REDUCTION_VIA_LEQ_1MM = 0.20
REDUCTION_VIA_LEQ_2P5MM = 0.35
REDUCTION_UNSTITCHED = 0.50
REDUCTION_POUR = 0.15


def _unary_union(geoms):
    from shapely.ops import unary_union

    valid = [g for g in geoms if g is not None and not g.is_empty]
    if not valid:
        return None
    return unary_union(valid)


def _buffered_segment(start: Point, end: Point, width_mm: float):
    from shapely.geometry import LineString

    if width_mm <= 0:
        return LineString([start, end])
    return LineString([start, end]).buffer(width_mm / 2.0, cap_style=2)


def _net_copper(model: PcbModel, nets: Sequence[str], layer: str):
    net_set = set(nets)
    geoms = []
    for z in model.zones:
        if z.is_keepout or z.net_name not in net_set:
            continue
        for fp in z.geometry_by_layer:
            if fp.layer != layer:
                continue
            poly = _shapely_polygon(fp.points)
            if poly is not None:
                geoms.append(poly)
    for s in model.segments:
        if s.layer != layer:
            continue
        if model.net_name(s.net_id) not in net_set:
            continue
        geoms.append(_buffered_segment(s.start, s.end, s.width_mm))
    return _unary_union(geoms)


def _sw_copper(model: PcbModel, ic_ref: str, layer: str):
    from .leftover_copper import primary_sw_union

    return primary_sw_union(model, ic_ref, layer)


def _fb_copper(model: PcbModel, ic_ref: str, layer: str):
    from .leftover_copper import primary_fb_union

    return primary_fb_union(model, ic_ref, layer)


def _stackup_adjacent_inner(stackup: Sequence[StackupLayer], signal_layer: str) -> Optional[str]:
    names = [ly.name for ly in stackup if ly.type == "copper"]
    if signal_layer not in names:
        return "In1.Cu" if signal_layer == "F.Cu" else None
    idx = names.index(signal_layer)
    if idx + 1 < len(names):
        return names[idx + 1]
    return None


def _intersection_length(line, geom) -> float:
    if geom is None or line is None or line.length < 1e-12:
        return 0.0
    inter = line.intersection(geom)
    if inter.is_empty:
        return 0.0
    if hasattr(inter, "length"):
        return float(inter.length)
    return 0.0


def _shrink_line(line, inset_mm: float = 0.02):
    from shapely.geometry import LineString

    if line.length <= 2.0 * inset_mm + 1e-9:
        return line
    return LineString(
        [
            line.interpolate(inset_mm),
            line.interpolate(line.length - inset_mm),
        ]
    )


def _shield_kind(model: PcbModel, nets: Sequence[str], layer: str, gap_line) -> str:
    """Classify intersecting shield copper as pour, trace, or mixed."""
    net_set = set(nets)
    hit_zone = False
    hit_seg = False
    for z in model.zones:
        if z.is_keepout or z.net_name not in net_set:
            continue
        for fp in z.geometry_by_layer:
            if fp.layer != layer:
                continue
            poly = _shapely_polygon(fp.points)
            if poly is not None and poly.intersects(gap_line):
                hit_zone = True
    for s in model.segments:
        if s.layer != layer or model.net_name(s.net_id) not in net_set:
            continue
        buf = _buffered_segment(s.start, s.end, s.width_mm)
        if buf.intersects(gap_line):
            hit_seg = True
    if hit_zone and hit_seg:
        return "mixed"
    if hit_zone:
        return "pour"
    if hit_seg:
        return "trace"
    return "none"


def _intersecting_trace_width_mm(
    model: PcbModel, nets: Sequence[str], layer: str, gap_line
) -> Optional[float]:
    net_set = set(nets)
    widths: List[float] = []
    for s in model.segments:
        if s.layer != layer or model.net_name(s.net_id) not in net_set:
            continue
        buf = _buffered_segment(s.start, s.end, s.width_mm)
        if buf.intersects(gap_line):
            widths.append(s.width_mm)
    if not widths:
        return None
    widths.sort()
    return widths[len(widths) // 2]


def _nearest_via_mm(
    model: PcbModel, nets: Sequence[str], point: Point
) -> Tuple[Optional[float], Optional[str]]:
    import math

    net_set = set(nets)
    best: Optional[float] = None
    best_net: Optional[str] = None
    for via in model.vias:
        n = model.net_name(via.net_id)
        if n not in net_set:
            continue
        d = math.hypot(via.at[0] - point[0], via.at[1] - point[1])
        if best is None or d < best:
            best = d
            best_net = n
    return (round(best, 3) if best is not None else None, best_net)


def coupling_reduction_factor(
    blocks_los: bool,
    kind: str,
    nearest_via_mm: Optional[float],
) -> float:
    """Remaining coupling multiplier (1.0 = no extra shield credit)."""
    if not blocks_los:
        return 1.0
    if kind == "pour":
        return REDUCTION_POUR
    if nearest_via_mm is not None and nearest_via_mm <= 1.0:
        return REDUCTION_VIA_LEQ_1MM
    if nearest_via_mm is not None and nearest_via_mm <= 2.5:
        return REDUCTION_VIA_LEQ_2P5MM
    return REDUCTION_UNSTITCHED


def _primary_shield_net(model: PcbModel, chosen_nets: Sequence[str]) -> Optional[str]:
    if not chosen_nets:
        return None
    analog_hit = [
        n
        for n in SHIELD_NETS_PREFERRED
        if any(z.net_name == n for z in model.zones)
        or any(model.net_name(s.net_id) == n for s in model.segments)
    ]
    if tuple(chosen_nets) == SHIELD_NETS_PREFERRED:
        return analog_hit[0] if analog_hit else "GNDA"
    return "GND"


def _evaluate_gap(
    model: PcbModel,
    sw,
    fb,
    analog,
    power_gnd,
    signal_layer: str,
    inner_layer: Optional[str],
    path_id: str,
) -> Optional[Dict[str, Any]]:
    from shapely.geometry import LineString
    from shapely.ops import nearest_points

    if sw is None or fb is None or sw.is_empty or fb.is_empty:
        return None
    p_sw, p_fb = nearest_points(sw, fb)
    sw_xy = (float(p_sw.x), float(p_sw.y))
    fb_xy = (float(p_fb.x), float(p_fb.y))
    gap = LineString([sw_xy, fb_xy])
    gap_len = float(gap.length)
    gap_probe = _shrink_line(gap)

    chosen_nets: Tuple[str, ...] = ()
    chosen_geom = None
    if analog is not None and analog.intersects(gap_probe):
        chosen_nets = SHIELD_NETS_PREFERRED
        chosen_geom = analog
    elif power_gnd is not None and power_gnd.intersects(gap_probe):
        chosen_nets = SHIELD_NETS_POWER
        chosen_geom = power_gnd

    blocks = chosen_geom is not None
    crossing = _intersection_length(gap_probe, chosen_geom) if blocks else 0.0
    kind = _shield_kind(model, chosen_nets, signal_layer, gap_probe) if blocks else "none"
    trace_w = (
        _intersecting_trace_width_mm(model, chosen_nets, signal_layer, gap_probe)
        if blocks
        else None
    )
    d_sw = float(sw.distance(chosen_geom)) if blocks and chosen_geom is not None else None
    d_fb = float(fb.distance(chosen_geom)) if blocks and chosen_geom is not None else None
    mid = (0.5 * (sw_xy[0] + fb_xy[0]), 0.5 * (sw_xy[1] + fb_xy[1]))
    via_mm, via_net = _nearest_via_mm(model, chosen_nets or ALL_SHIELD_NETS, mid)
    factor = coupling_reduction_factor(blocks, kind, via_mm)
    primary_net = _primary_shield_net(model, chosen_nets) if blocks else None
    shield_width = trace_w if trace_w is not None else (round(crossing, 4) if crossing else None)

    inner_cov: Dict[str, float] = {}
    if inner_layer and gap_len > 1e-12:
        for net in ALL_SHIELD_NETS:
            g = _net_copper(model, (net,), inner_layer)
            if g is None:
                continue
            frac = _intersection_length(gap, g) / gap_len
            if frac > 0.005:
                inner_cov[net] = round(100.0 * frac, 1)

    return {
        "path_id": path_id,
        "gap_length_mm": round(gap_len, 4),
        "gap_ends_mm": [list(sw_xy), list(fb_xy)],
        "same_layer_blocks_line_of_sight": blocks,
        "same_layer_shield_net": primary_net,
        "same_layer_shield_kind": kind,
        "same_layer_crossing_width_mm": round(crossing, 4) if blocks else 0.0,
        "same_layer_shield_width_mm": round(shield_width, 4) if shield_width else None,
        "distance_sw_to_shield_mm": round(d_sw, 4) if d_sw is not None else None,
        "distance_fb_to_shield_mm": round(d_fb, 4) if d_fb is not None else None,
        "nearest_shield_via_mm": via_mm,
        "nearest_shield_via_net": via_net,
        "inner_ref_gap_coverage_pct": inner_cov,
        "coupling_reduction_factor": factor,
    }


def _pour_sw_copper(model: PcbModel, ic_ref: str, layer: str):
    from .leftover_copper import sw_net_name, zone_polygons_on_net

    geoms = []
    for pts in zone_polygons_on_net(model, sw_net_name(ic_ref, model), layer):
        g = _shapely_polygon(pts)
        if g is not None:
            geoms.append(g)
    return _unary_union(geoms)


def analyze_sw_fb_shield(
    model: PcbModel,
    ic_ref: str,
    signal_layer: str = "F.Cu",
) -> Dict[str, Any]:
    """Geometry of grounded copper between SW and FB for one buck IC.

    Two gaps are scored: the true nearest SW copper (often the inductor pad) and
    the SW pour / island. A guard on the pour–FB gap does not change the
    copper-to-copper pass threshold, and does not credit SI if a shorter
    unguarded pad–FB gap exists.
    """
    inner_layer = _stackup_adjacent_inner(model.stackup, signal_layer)
    sw = _sw_copper(model, ic_ref, signal_layer)
    fb = _fb_copper(model, ic_ref, signal_layer)
    empty: Dict[str, Any] = {
        "source": "toolchain",
        "ic_reference": ic_ref,
        "signal_layer": signal_layer,
        "inner_ref_layer": inner_layer,
        "same_layer_blocks_line_of_sight": False,
        "same_layer_shield_net": None,
        "same_layer_shield_kind": "none",
        "coupling_reduction_factor": 1.0,
        "notes": "SW or FB copper missing on signal layer; no guard check.",
    }
    if sw is None or fb is None or sw.is_empty or fb.is_empty:
        return empty

    analog = _net_copper(model, SHIELD_NETS_PREFERRED, signal_layer)
    power_gnd = _net_copper(model, SHIELD_NETS_POWER, signal_layer)
    nearest = _evaluate_gap(
        model, sw, fb, analog, power_gnd, signal_layer, inner_layer, "nearest_sw_copper"
    )
    pour = _evaluate_gap(
        model,
        _pour_sw_copper(model, ic_ref, signal_layer),
        fb,
        analog,
        power_gnd,
        signal_layer,
        inner_layer,
        "sw_pour",
    )
    if nearest is None:
        return empty

    notes_parts = []
    if nearest["same_layer_blocks_line_of_sight"]:
        notes_parts.append(
            f"{signal_layer} {nearest['same_layer_shield_net']} "
            f"{nearest['same_layer_shield_kind']} crosses the shortest SW–FB path "
            f"(crossing {nearest['same_layer_crossing_width_mm']:.3f} mm of "
            f"{nearest['gap_length_mm']:.3f} mm gap)."
        )
        notes_parts.append(
            f"SI remaining-coupling factor {nearest['coupling_reduction_factor']:.2f} "
            "(heuristic, not a field solve)."
        )
    else:
        notes_parts.append(
            f"No {signal_layer} GNDA/AGND/GND copper crosses the shortest SW–FB path "
            f"({nearest['gap_length_mm']:.3f} mm). "
            "Inner-layer analog ground under FB is a return plane, not a coplanar guard."
        )
    if pour and pour["same_layer_blocks_line_of_sight"]:
        notes_parts.append(
            f"Same-layer {pour['same_layer_shield_net']} {pour['same_layer_shield_kind']} "
            f"DOES cross the SW-pour–FB gap ({pour['gap_length_mm']:.3f} mm). "
            "That guard does not change the copper-to-copper pass threshold "
            f"(still measured on the shortest edge) and does not reduce SI if a "
            f"shorter unguarded path exists."
        )
    elif pour and not nearest["same_layer_blocks_line_of_sight"]:
        notes_parts.append(
            f"SW-pour–FB gap is {pour['gap_length_mm']:.3f} mm and is also unguarded."
        )
    inner_cov = nearest.get("inner_ref_gap_coverage_pct") or {}
    if inner_cov:
        bits = ", ".join(f"{n} {p:.0f}%" for n, p in inner_cov.items())
        notes_parts.append(f"{inner_layer} coverage along the shortest gap: {bits}.")

    result = {
        "source": "toolchain",
        "ic_reference": ic_ref,
        "signal_layer": signal_layer,
        "inner_ref_layer": inner_layer,
        **{k: v for k, v in nearest.items() if k != "path_id"},
        "si_path_id": nearest["path_id"],
        "pour_path": pour,
        "notes": " ".join(notes_parts),
    }
    return result
