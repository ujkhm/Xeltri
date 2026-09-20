"""Classify leftover (residual) copper so SI/geometry does not use it as the net.

KiCad often keeps thin tracks after a zone pour, or short stubs that terminate
inside a pad. Those traces are still in the file, so a naive min-distance over
*all* segments can:

- pick a leftover centerline as the SW/FB "nearest edge"
- use leftover width/length as the SI coupled-line proxy (optimistic)

This module splits each net's copper into:

- **cover**: filled zones + pads (the intended copper)
- **residual**: traces ≥ CONTAINED_FRAC inside cover, or shorter than SHORT_STUB_MM
- **functional**: traces that actually extend the copper outline

SI spacing uses pour + non-IC pads (e.g. inductor) + functional traces
(edge-to-edge). Residual is reported, not used as the aggressor/victim
geometry. The IC's own SW/FB pins are omitted from the SI edge so QFN
pitch does not always fail the clearance check.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .geometry import _shapely_polygon
from .pcb import Pad, PcbModel, Segment, pad_on_layer, pad_world_polygon

Point = Tuple[float, float]

CONTAINED_FRAC = 0.85
SHORT_STUB_MM = 0.12


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


def _unary_union(geoms):
    from shapely.ops import unary_union

    valid = [g for g in geoms if g is not None and not getattr(g, "is_empty", False)]
    if not valid:
        return None
    return unary_union(valid)


def _buffered_segment(start: Point, end: Point, width_mm: float):
    from shapely.geometry import LineString

    line = LineString([start, end])
    if width_mm <= 0:
        return line
    return line.buffer(width_mm / 2.0, cap_style=2)


def zone_polygons_on_net(
    model: PcbModel,
    net_name: str,
    layer: Optional[str] = None,
) -> List[List[Point]]:
    polys: List[List[Point]] = []
    for z in model.zones:
        if z.is_keepout or z.net_name != net_name:
            continue
        for geom in z.geometry_by_layer:
            if layer and geom.layer and geom.layer != layer:
                continue
            if geom.points and len(geom.points) >= 3:
                polys.append(list(geom.points))
    return polys


def pads_on_net(
    model: PcbModel,
    net_name: str,
    layer: Optional[str] = None,
    exclude_refs: Optional[Sequence[str]] = None,
) -> List[Tuple[str, Pad]]:
    skip = set(exclude_refs or ())
    out: List[Tuple[str, Pad]] = []
    for fp in model.footprints:
        if fp.reference in skip:
            continue
        for pad in fp.pads:
            if pad.net_name != net_name:
                continue
            if layer and not pad_on_layer(pad, layer):
                continue
            out.append((fp.reference, pad))
    return out


def cover_union(
    model: PcbModel,
    net_name: str,
    layer: Optional[str] = None,
    exclude_refs: Optional[Sequence[str]] = None,
):
    """Pads + filled zones on a net (the copper leftover traces sit inside)."""
    geoms = []
    for pts in zone_polygons_on_net(model, net_name, layer):
        poly = _shapely_polygon(pts)
        if poly is not None:
            geoms.append(poly)
    for _ref, pad in pads_on_net(model, net_name, layer, exclude_refs=exclude_refs):
        g = pad_world_polygon(pad)
        if g is not None:
            geoms.append(g)
    return _unary_union(geoms)


def classify_segment(seg: Segment, cover) -> Dict[str, Any]:
    buf = _buffered_segment(seg.start, seg.end, seg.width_mm)
    area = float(buf.area) if buf is not None else 0.0
    contained = 0.0
    if cover is not None and area > 1e-12:
        contained = float(buf.intersection(cover).area) / area
    elif cover is not None and area <= 1e-12:
        contained = 1.0 if cover.intersects(buf) else 0.0

    length = float(seg.length_mm)
    if length < SHORT_STUB_MM:
        cls = "residual_stub"
    elif contained >= CONTAINED_FRAC:
        cls = "residual_in_pad_or_pour"
    else:
        cls = "functional"
    return {
        "class": cls,
        "contained_frac": round(contained, 3),
        "length_mm": round(length, 4),
        "width_mm": seg.width_mm,
        "layer": seg.layer,
        "kind": getattr(seg, "kind", "segment"),
        "start": list(seg.start),
        "end": list(seg.end),
        "uuid": seg.uuid,
    }


def net_segments(model: PcbModel, net_name: str, layer: Optional[str] = None) -> List[Segment]:
    out: List[Segment] = []
    for s in model.segments:
        if model.net_name(s.net_id) != net_name:
            continue
        if layer and s.layer != layer:
            continue
        out.append(s)
    return out


def classify_net(
    model: PcbModel,
    net_name: str,
    layer: Optional[str] = None,
) -> Dict[str, Any]:
    cover = cover_union(model, net_name, layer)
    segs = net_segments(model, net_name, layer)
    rows = [classify_segment(s, cover) for s in segs]
    residual = [r for r in rows if r["class"].startswith("residual")]
    functional = [r for r in rows if r["class"] == "functional"]
    functional_segs = [s for s, r in zip(segs, rows) if r["class"] == "functional"]
    residual_segs = [s for s, r in zip(segs, rows) if r["class"].startswith("residual")]
    return {
        "net": net_name,
        "layer": layer,
        "cover_area_mm2": round(float(cover.area), 4) if cover is not None else 0.0,
        "segment_count": len(segs),
        "residual_count": len(residual),
        "functional_count": len(functional),
        "residual_length_mm": round(sum(r["length_mm"] for r in residual), 4),
        "functional_length_mm": round(sum(r["length_mm"] for r in functional), 4),
        "residual": residual,
        "functional": functional,
        "functional_segments": functional_segs,
        "residual_segments": residual_segs,
        "cover": cover,
    }


def _functional_union(
    classified: Dict[str, Any],
    si_cover=None,
):
    geoms = []
    cover = si_cover if si_cover is not None else classified.get("cover")
    if cover is not None:
        geoms.append(cover)
    for s in classified.get("functional_segments") or []:
        geoms.append(_buffered_segment(s.start, s.end, s.width_mm))
    return _unary_union(geoms)


def primary_sw_union(model: PcbModel, ic_ref: str, layer: str = "F.Cu"):
    """SW copper for SI: pour + inductor/passive pads + functional traces.

    The IC's own SW pad is omitted: QFN pin pitch would always fail SW–FB.
    Leftover-in-pad detection still uses the IC pad (see classify_net cover).
    """
    cls = classify_net(model, sw_net_name(ic_ref, model), layer)
    si_cover = cover_union(model, sw_net_name(ic_ref, model), layer, exclude_refs=(ic_ref,))
    return _functional_union(cls, si_cover=si_cover)


def primary_fb_union(model: PcbModel, ic_ref: str, layer: str = "F.Cu"):
    cls = classify_net(model, fb_net_name(ic_ref, model), layer)
    si_cover = cover_union(model, fb_net_name(ic_ref, model), layer, exclude_refs=(ic_ref,))
    return _functional_union(cls, si_cover=si_cover)


def _legacy_centerline_distance(
    model: PcbModel,
    ic_ref: str,
    fb_segments: Optional[Sequence[Segment]] = None,
) -> Dict[str, Any]:
    """Old SI path: min of ALL SW segment centerlines + zone polygons vs FB centerlines."""
    from .geometry import min_distance_polygon_to_segment, min_distance_segment_to_segment

    sw_net = sw_net_name(ic_ref, model)
    fb_net = fb_net_name(ic_ref, model)
    sw_segs = net_segments(model, sw_net)
    fb_segs = list(fb_segments) if fb_segments is not None else net_segments(model, fb_net)
    best = float("inf")
    source = "none"
    nearest_kind = None
    for sa in sw_segs:
        for sb in fb_segs:
            d = min_distance_segment_to_segment(sa.start, sa.end, sb.start, sb.end)
            if d < best:
                best = d
                source = "segments"
                nearest_kind = getattr(sa, "kind", "segment")
    for poly in zone_polygons_on_net(model, sw_net):
        for sb in fb_segs:
            d = min_distance_polygon_to_segment(poly, sb.start, sb.end)
            if d < best:
                best = d
                source = "zone_to_fb_segment"
                nearest_kind = "zone"
    return {
        "distance_mm": round(best, 4) if best < float("inf") else None,
        "source": source,
        "nearest_kind": nearest_kind,
    }


def _residual_won_legacy_min(
    model: PcbModel,
    ic_ref: str,
    sw_cls: Dict[str, Any],
    fb_cls: Dict[str, Any],
) -> bool:
    """True if a residual centerline was strictly closer than pad/pour/functional copper."""
    from .geometry import min_distance_polygon_to_segment, min_distance_segment_to_segment

    sw_res = set(id(s) for s in (sw_cls.get("residual_segments") or []))
    fb_res = set(id(s) for s in (fb_cls.get("residual_segments") or []))
    sw_all = net_segments(model, sw_net_name(ic_ref, model))
    fb_all = net_segments(model, fb_net_name(ic_ref, model))
    best_any = float("inf")
    best_clean = float("inf")
    for sa in sw_all:
        for sb in fb_all:
            d = min_distance_segment_to_segment(sa.start, sa.end, sb.start, sb.end)
            best_any = min(best_any, d)
            if id(sa) not in sw_res and id(sb) not in fb_res:
                best_clean = min(best_clean, d)
    for poly in zone_polygons_on_net(model, sw_net_name(ic_ref, model)):
        for sb in fb_all:
            d = min_distance_polygon_to_segment(poly, sb.start, sb.end)
            best_any = min(best_any, d)
            if id(sb) not in fb_res:
                best_clean = min(best_clean, d)
    if best_any == float("inf"):
        return False
    return best_any + 0.02 < best_clean


def leftover_report(
    model: PcbModel,
    ic_ref: str,
    layer: str = "F.Cu",
) -> Dict[str, Any]:
    sw = classify_net(model, sw_net_name(ic_ref, model), layer)
    fb = classify_net(model, fb_net_name(ic_ref, model), layer)
    residual_n = sw["residual_count"] + fb["residual_count"]
    note_bits = []
    if residual_n:
        note_bits.append(
            f"Excluded {sw['residual_count']} SW + {fb['residual_count']} FB leftover "
            f"trace(s) inside pads/pour or shorter than {SHORT_STUB_MM} mm from SI geometry."
        )
    else:
        note_bits.append("No leftover SW/FB traces inside pads/pour.")
    if sw["functional_count"]:
        note_bits.append(
            f"Kept {sw['functional_count']} SW functional trace(s) that extend the copper outline."
        )
    return {
        "source": "toolchain",
        "ic_reference": ic_ref,
        "layer": layer,
        "contained_frac_threshold": CONTAINED_FRAC,
        "short_stub_mm": SHORT_STUB_MM,
        "sw": {k: v for k, v in sw.items() if k not in ("cover", "functional_segments", "residual_segments")},
        "fb": {k: v for k, v in fb.items() if k not in ("cover", "functional_segments", "residual_segments")},
        "residual_trace_count": residual_n,
        "notes": " ".join(note_bits),
        "_sw": sw,
        "_fb": fb,
    }


def primary_clearance(
    model: PcbModel,
    ic_ref: str,
    layer: str = "F.Cu",
    fb_segments: Optional[Sequence[Segment]] = None,
) -> Dict[str, Any]:
    """Edge-to-edge SW–FB distance on primary copper.

    Primary = filled pour + non-IC SW pads (e.g. inductor) + functional traces.
    Residual tracks inside pads/pours and the IC's own pins are not the SI edge.
    """
    from shapely.ops import nearest_points

    report = leftover_report(model, ic_ref, layer)
    sw_cls = report["_sw"]
    fb_cls = report["_fb"]
    # Optional caller filter: still drop residual even if a FB list is passed.
    if fb_segments is not None:
        cover = fb_cls["cover"]
        kept = []
        for s in fb_segments:
            row = classify_segment(s, cover)
            if row["class"] == "functional":
                kept.append(s)
        fb_cls = dict(fb_cls)
        fb_cls["functional_segments"] = kept

    sw_si_cover = cover_union(
        model, sw_net_name(ic_ref, model), layer, exclude_refs=(ic_ref,)
    )
    fb_si_cover = cover_union(
        model, fb_net_name(ic_ref, model), layer, exclude_refs=(ic_ref,)
    )
    sw_u = _functional_union(sw_cls, si_cover=sw_si_cover)
    fb_u = _functional_union(fb_cls, si_cover=fb_si_cover)
    legacy = _legacy_centerline_distance(model, ic_ref, fb_segments)

    if sw_u is None or fb_u is None or sw_u.is_empty or fb_u.is_empty:
        return {
            "distance_mm": None,
            "source": "none",
            "nearest_sw_edge": None,
            "nearest_fb_segment": None,
            "leftover": {k: v for k, v in report.items() if not k.startswith("_")},
            "legacy_centerline": legacy,
            "leftover_would_have_driven_si": False,
        }

    dist = float(sw_u.distance(fb_u))
    p_sw, p_fb = nearest_points(sw_u, fb_u)
    sw_xy = (float(p_sw.x), float(p_sw.y))
    fb_xy = (float(p_fb.x), float(p_fb.y))

    winner_kind = "primary_copper"
    winner_width: Optional[float] = None
    winner_meta: Dict[str, Any] = {}
    best_hit = 1e9
    for pts in zone_polygons_on_net(model, sw_net_name(ic_ref, model), layer):
        g = _shapely_polygon(pts)
        if g is None:
            continue
        dhit = float(g.distance(p_sw))
        if dhit < best_hit:
            best_hit = dhit
            winner_kind = "zone"
            winner_meta = {"kind": "zone"}
    for ref, pad in pads_on_net(
        model, sw_net_name(ic_ref, model), layer, exclude_refs=(ic_ref,)
    ):
        g = pad_world_polygon(pad)
        if g is None:
            continue
        dhit = float(g.distance(p_sw))
        if dhit < best_hit:
            best_hit = dhit
            winner_kind = "pad"
            winner_width = min(pad.size) if pad.size else None
            winner_meta = {
                "kind": "pad",
                "footprint": ref,
                "pad": pad.number,
                "size_mm": list(pad.size),
            }
    for s in sw_cls.get("functional_segments") or []:
        g = _buffered_segment(s.start, s.end, s.width_mm)
        dhit = float(g.distance(p_sw))
        if dhit < best_hit:
            best_hit = dhit
            winner_kind = "functional_trace"
            winner_width = s.width_mm
            winner_meta = {
                "kind": "functional_trace",
                "start": list(s.start),
                "end": list(s.end),
                "width_mm": s.width_mm,
            }

    fb_winner = None
    fb_best = 1e9
    for s in fb_cls.get("functional_segments") or []:
        g = _buffered_segment(s.start, s.end, s.width_mm)
        dhit = float(g.distance(p_fb))
        if dhit < fb_best:
            fb_best = dhit
            fb_winner = s

    leftover_driven = _residual_won_legacy_min(model, ic_ref, sw_cls, fb_cls)

    pour_only = _unary_union(
        [
            g
            for g in (
                _shapely_polygon(pts)
                for pts in zone_polygons_on_net(model, sw_net_name(ic_ref, model), layer)
            )
            if g is not None
        ]
        + [
            _buffered_segment(s.start, s.end, s.width_mm)
            for s in (sw_cls.get("functional_segments") or [])
        ]
    )
    pour_only_mm = (
        round(float(pour_only.distance(fb_u)), 4)
        if pour_only is not None and not pour_only.is_empty
        else None
    )

    nearest_sw = {
        "start": list(sw_xy),
        "end": list(fb_xy),
        "width_mm": winner_width,
        "kind": winner_kind,
        **{k: v for k, v in winner_meta.items() if k != "kind"},
    }
    nearest_fb = None
    if fb_winner is not None:
        nearest_fb = {
            "start": list(fb_winner.start),
            "end": list(fb_winner.end),
            "width_mm": fb_winner.width_mm,
            "length_mm": round(fb_winner.length_mm, 4),
            "kind": getattr(fb_winner, "kind", "segment"),
        }
    else:
        nearest_fb = {
            "start": list(fb_xy),
            "end": list(fb_xy),
            "width_mm": 0.2,
            "length_mm": round(max(float(fb_u.length) if hasattr(fb_u, "length") else 0.5, 0.5), 4),
            "kind": "pad",
        }

    public = {k: v for k, v in report.items() if not k.startswith("_")}
    public["leftover_would_have_driven_si"] = leftover_driven
    public["legacy_centerline_distance_mm"] = legacy["distance_mm"]
    public["legacy_centerline_source"] = legacy["source"]
    public["pour_only_distance_mm"] = pour_only_mm

    return {
        "distance_mm": round(dist, 4),
        "source": f"primary_edge:{winner_kind}",
        "nearest_sw_edge": nearest_sw,
        "nearest_fb_segment": nearest_fb,
        "gap_ends_mm": [list(sw_xy), list(fb_xy)],
        "leftover": public,
        "legacy_centerline": legacy,
        "leftover_would_have_driven_si": leftover_driven,
    }


def _self_test_classify() -> None:
    from shapely.geometry import box

    cover = box(-1.0, -1.0, 1.0, 1.0)
    inside = Segment(
        start=(-0.2, 0.0),
        end=(0.2, 0.0),
        width_mm=0.2,
        layer="F.Cu",
        net_id=1,
        uuid="in",
    )
    stub = Segment(
        start=(10.0, 10.0),
        end=(10.05, 10.0),
        width_mm=0.2,
        layer="F.Cu",
        net_id=1,
        uuid="stub",
    )
    outside = Segment(
        start=(5.0, 0.0),
        end=(8.0, 0.0),
        width_mm=0.2,
        layer="F.Cu",
        net_id=1,
        uuid="out",
    )
    assert classify_segment(inside, cover)["class"] == "residual_in_pad_or_pour"
    assert classify_segment(stub, cover)["class"] == "residual_stub"
    assert classify_segment(outside, cover)["class"] == "functional"


if __name__ == "__main__":
    _self_test_classify()
    print("leftover_copper classify self-test ok")

