"""Reference-plane continuity and return-path checks using zone geometry."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from shapely.geometry import Point as ShapelyPoint
from shapely.prepared import prep

from .geometry import segment_length
from .pcb import PcbModel, Segment
from .stackup_ref import build_stackup_reference_map, reference_planes_for_layer

Point = Tuple[float, float]


def _point_distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _point_near_any(pt: Point, points: Sequence[Point], radius_mm: float) -> bool:
    return any(_point_distance(pt, p) <= radius_mm for p in points)


def discover_return_reference_nets(model: PcbModel) -> List[str]:
    """Ground-like nets that can act as return-current references."""
    names = sorted(set(model.nets.values()))
    refs: List[str] = []
    for preferred in ("GND", "GNDA", "AGND", "DGND", "PGND", "VSS", "VSSA"):
        if preferred in names:
            refs.append(preferred)
    for name in names:
        if name in refs:
            continue
        upper = name.upper()
        if upper.startswith("GND") or upper in ("VSS", "VSSA"):
            refs.append(name)
    return refs or ["GND"]


def _via_exclusion_points(model: PcbModel, net_id: int) -> Tuple[List[Point], float]:
    """Return via centers and exclusion radius for return-path sampling."""
    points: List[Point] = []
    max_radius = 0.35
    for via in model.vias:
        if via.net_id != net_id:
            continue
        points.append(via.at)
        max_radius = max(max_radius, 0.35 + via.size_mm / 2.0)
    return points, max_radius


@dataclass
class PlaneIndex:
    """Spatial index of filled copper zones per (layer, net_name)."""

    model: PcbModel
    _prepared: Dict[Tuple[str, str], List] = field(default_factory=dict)
    _geoms: Dict[Tuple[str, str], List] = field(default_factory=dict)

    def __post_init__(self) -> None:
        from shapely.geometry import Polygon

        buckets: Dict[Tuple[str, str], List] = {}
        for zone in self.model.zones:
            if zone.is_keepout or not zone.fill:
                continue
            for fp in zone.geometry_by_layer:
                if not fp.layer or not fp.points or len(fp.points) < 3:
                    continue
                key = (fp.layer, zone.net_name)
                try:
                    poly = Polygon(fp.points)
                    if not poly.is_valid:
                        poly = poly.buffer(0)
                    if poly.is_empty:
                        continue
                    buckets.setdefault(key, []).append(poly)
                except Exception:
                    continue
        for key, polys in buckets.items():
            self._geoms[key] = polys
            self._prepared[key] = [prep(p) for p in polys]

    def has_copper(self, layer: str, net_name: str, pt: Point) -> bool:
        key = (layer, net_name)
        prepared = self._prepared.get(key)
        if not prepared:
            return False
        p = ShapelyPoint(pt[0], pt[1])
        return any(pp.contains(p) or pp.touches(p) for pp in prepared)

    def copper_coverage_along_line(
        self,
        start: Point,
        end: Point,
        layer: str,
        net_name: str,
        sample_mm: float = 0.25,
        exclude_near: Optional[Sequence[Point]] = None,
        exclude_radius_mm: float = 0.0,
    ) -> float:
        """Fraction of line length over specified copper (0..1).

        Samples within ``exclude_radius_mm`` of any point in ``exclude_near``
        are omitted (e.g. layer-change vias where plane anti-pads are expected).
        """
        length = segment_length(start, end)
        if length < 1e-6:
            pt = start
            if exclude_near and exclude_radius_mm > 0:
                if _point_near_any(pt, exclude_near, exclude_radius_mm):
                    return 1.0
            return 1.0 if self.has_copper(layer, net_name, start) else 0.0
        steps = max(4, int(math.ceil(length / sample_mm)) + 1)
        hits = 0
        counted = 0
        for i in range(steps):
            t = i / (steps - 1)
            pt = (
                start[0] + t * (end[0] - start[0]),
                start[1] + t * (end[1] - start[1]),
            )
            if exclude_near and exclude_radius_mm > 0:
                if _point_near_any(pt, exclude_near, exclude_radius_mm):
                    continue
            counted += 1
            if self.has_copper(layer, net_name, pt):
                hits += 1
        if counted == 0:
            return 1.0
        return hits / counted


@dataclass
class SegmentReturnPathResult:
    net_name: str
    layer: str
    start: Point
    end: Point
    length_mm: float
    primary_ref_plane: str
    coplanar_ref_plane: str
    primary_ref_coverage_pct: float
    coplanar_ref_coverage_pct: float
    reference_net: str
    dominant_reference_net: str
    coverage_by_reference_net: Dict[str, float]
    coverage_pct: float
    status: str
    notes: str


def _status_from_coverage(
    primary_pct: float,
    coplanar_pct: float,
    min_primary_pct: float = 85.0,
) -> Tuple[str, str, float]:
    """
    Score using stackup-adjacent primary plane; coplanar GND is secondary.

    Pass when primary reference plane coverage is sufficient.
    """
    score = primary_pct
    if score >= min_primary_pct:
        return (
            "pass",
            f"Primary ref plane {primary_pct:.0f}% covered",
            score,
        )
    if score >= 50.0 or coplanar_pct >= 85.0:
        return (
            "warn",
            f"Primary ref {primary_pct:.0f}% (coplanar GND {coplanar_pct:.0f}%)",
            score,
        )
    return (
        "fail",
        f"Primary ref plane gap ({primary_pct:.0f}% under trace)",
        score,
    )


def _segment_reference_coverage(
    index: PlaneIndex,
    seg: Segment,
    model: PcbModel,
    primary: str,
    coplanar: str,
    reference_nets: Sequence[str],
) -> Tuple[Dict[str, float], Dict[str, float], str, float, float]:
    via_pts, via_radius = _via_exclusion_points(model, seg.net_id)
    primary_by_net: Dict[str, float] = {}
    coplanar_by_net: Dict[str, float] = {}
    for ref_net in reference_nets:
        primary_by_net[ref_net] = round(
            index.copper_coverage_along_line(
                seg.start,
                seg.end,
                primary,
                ref_net,
                exclude_near=via_pts,
                exclude_radius_mm=via_radius,
            )
            * 100.0,
            2,
        )
        coplanar_by_net[ref_net] = round(
            index.copper_coverage_along_line(
                seg.start,
                seg.end,
                coplanar,
                ref_net,
                exclude_near=via_pts,
                exclude_radius_mm=via_radius,
            )
            * 100.0,
            2,
        )
    dominant = max(reference_nets, key=lambda n: primary_by_net.get(n, 0.0))
    primary_pct = primary_by_net[dominant]
    coplanar_pct = coplanar_by_net.get(dominant, 0.0)
    return primary_by_net, coplanar_by_net, dominant, primary_pct, coplanar_pct


def _notes_for_reference_coverage(
    dominant_net: str,
    primary_by_net: Dict[str, float],
    primary_pct: float,
) -> str:
    gnd_pct = primary_by_net.get("GND", 0.0)
    if dominant_net != "GND" and gnd_pct < 50.0 and primary_pct >= 85.0:
        return (
            f"Primary ref {primary_pct:.0f}% via {dominant_net} "
            f"(GND only {gnd_pct:.0f}%; split-ground return)"
        )
    return f"Primary ref plane {primary_pct:.0f}% covered ({dominant_net})"


def analyze_segment_return_path(
    index: PlaneIndex,
    seg: Segment,
    model: PcbModel,
    reference_nets: Optional[Sequence[str]] = None,
    min_primary_pct: float = 85.0,
) -> SegmentReturnPathResult:
    refs = reference_planes_for_layer(seg.layer, model.copper_layers)
    primary = refs["primary_ref"]
    coplanar = refs["coplanar_ref"]
    ref_nets = list(reference_nets or discover_return_reference_nets(model))

    primary_by_net, coplanar_by_net, dominant, primary_pct, coplanar_pct = (
        _segment_reference_coverage(index, seg, model, primary, coplanar, ref_nets)
    )

    status, _, score = _status_from_coverage(
        primary_pct, coplanar_pct, min_primary_pct
    )
    notes = _notes_for_reference_coverage(dominant, primary_by_net, primary_pct)

    return SegmentReturnPathResult(
        net_name=model.net_name(seg.net_id),
        layer=seg.layer,
        start=seg.start,
        end=seg.end,
        length_mm=round(seg.length_mm, 4),
        primary_ref_plane=primary,
        coplanar_ref_plane=coplanar,
        primary_ref_coverage_pct=round(primary_pct, 2),
        coplanar_ref_coverage_pct=round(coplanar_pct, 2),
        reference_net=dominant,
        dominant_reference_net=dominant,
        coverage_by_reference_net=primary_by_net,
        coverage_pct=round(score, 2),
        status=status,
        notes=notes,
    )


def analyze_region_return_paths(
    model: PcbModel,
    polygon: Sequence[Point],
    index: Optional[PlaneIndex] = None,
    reference_nets: Optional[Sequence[str]] = None,
    signal_net_patterns: Sequence[str] = ("LX", "FB", "CLK", "USB", "FMC", "ULPI"),
) -> Tuple[List[Dict], Dict[str, Dict[str, str]]]:
    from .geometry import segment_intersects_polygon

    idx = index or PlaneIndex(model)
    ref_map = build_stackup_reference_map(model.copper_layers)
    ref_nets = list(reference_nets or discover_return_reference_nets(model))
    ref_net_set = set(ref_nets)
    by_net: Dict[str, Dict] = {}
    status_rank = {"fail": 0, "warn": 1, "pass": 2}

    for seg in model.segments:
        net_name = model.net_name(seg.net_id)
        if net_name in ref_net_set or net_name in ("", "+5V"):
            continue
        if not any(pat in net_name for pat in signal_net_patterns):
            continue
        if not segment_intersects_polygon(seg.start, seg.end, polygon):
            continue

        r = analyze_segment_return_path(idx, seg, model, ref_nets)
        via_pts, via_radius = _via_exclusion_points(model, seg.net_id)
        via_adjacent = (
            _point_near_any(seg.start, via_pts, via_radius)
            or _point_near_any(seg.end, via_pts, via_radius)
        )
        seg_detail = {
            "layer": r.layer,
            "length_mm": r.length_mm,
            "start": list(r.start),
            "end": list(r.end),
            "primary_ref_plane": r.primary_ref_plane,
            "coplanar_ref_plane": r.coplanar_ref_plane,
            "dominant_reference_net": r.dominant_reference_net,
            "coverage_by_reference_net": r.coverage_by_reference_net,
            "primary_ref_coverage_pct": r.primary_ref_coverage_pct,
            "coplanar_ref_coverage_pct": r.coplanar_ref_coverage_pct,
            "via_adjacent": via_adjacent,
            "status": r.status,
            "notes": r.notes,
        }

        prev = by_net.get(net_name)
        segments = list((prev or {}).get("segments", []))
        segments.append(seg_detail)

        total_len = sum(s["length_mm"] for s in segments)
        weighted_cov = (
            sum(s["length_mm"] * s["primary_ref_coverage_pct"] for s in segments)
            / total_len
            if total_len > 0
            else 0.0
        )
        worst_seg = min(
            segments,
            key=lambda s: status_rank.get(s["status"], 9),
        )
        layers_used = sorted({s["layer"] for s in segments})
        primary_refs_used = sorted({s["primary_ref_plane"] for s in segments})
        dominant_counts: Dict[str, float] = {}
        for s in segments:
            dominant_counts[s["dominant_reference_net"]] = (
                dominant_counts.get(s["dominant_reference_net"], 0.0) + s["length_mm"]
            )
        net_dominant = max(dominant_counts, key=dominant_counts.get)
        net_status, net_notes = _net_return_path_status(segments, weighted_cov)
        if net_dominant != "GND" and net_status == "pass":
            gnd_weighted = (
                sum(
                    s["length_mm"] * s["coverage_by_reference_net"].get("GND", 0.0)
                    for s in segments
                )
                / total_len
                if total_len > 0
                else 0.0
            )
            if gnd_weighted < 50.0:
                net_notes = (
                    f"Length-weighted {weighted_cov:.0f}% via {net_dominant} "
                    f"(GND only {gnd_weighted:.0f}%; split-ground return)"
                )

        by_net[net_name] = {
            "net_name": net_name,
            "layers_used": layers_used,
            "primary_ref_planes_used": primary_refs_used,
            "total_length_mm": round(total_len, 4),
            "reference_nets_checked": ref_nets,
            "dominant_reference_net": net_dominant,
            "coverage_pct": round(weighted_cov, 2),
            "worst_segment_coverage_pct": worst_seg["primary_ref_coverage_pct"],
            "status": net_status,
            "notes": net_notes,
            "segments": segments,
            "routing_pattern": _describe_routing_pattern(segments),
        }

    results = sorted(
        by_net.values(),
        key=lambda x: (status_rank.get(x["status"], 9), x["net_name"]),
    )
    return results, ref_map


def _net_return_path_status(
    segments: List[Dict],
    weighted_cov: float,
    min_weighted_pct: float = 85.0,
) -> Tuple[str, str]:
    """Net-level pass uses length-weighted primary ref coverage."""
    status_rank = {"fail": 0, "warn": 1, "pass": 2}
    if weighted_cov >= min_weighted_pct:
        return "pass", f"Length-weighted primary ref {weighted_cov:.0f}% covered"
    worst = min(segments, key=lambda s: status_rank.get(s["status"], 9))
    if worst["status"] == "warn":
        return "warn", worst["notes"]
    return "fail", worst["notes"]


def _describe_routing_pattern(segments: List[Dict]) -> str:
    """Human-readable layer hop summary, e.g. F.Cu→In1(GNDA) -> B.Cu→In4(GND)."""
    hops = []
    for s in segments:
        ref_net = s.get("dominant_reference_net", "GND")
        hops.append(f"{s['layer']}→ref {s['primary_ref_plane']}({ref_net})")
    return " → ".join(hops) if hops else ""
