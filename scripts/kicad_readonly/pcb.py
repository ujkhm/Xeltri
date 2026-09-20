"""Load KiCad .kicad_pcb into a read-only analysis model."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .geometry import (
    bboxes_overlap,
    clip_segment_to_polygon,
    estimate_region_coverage_pct,
    point_in_polygon,
    polygon_area,
    polygon_bbox,
    polygons_intersect,
    rotate_point,
    segment_intersects_polygon,
    segment_length,
)
from .sexp import parse

Point = Tuple[float, float]


@dataclass
class StackupLayer:
    name: str
    type: str
    thickness_mm: Optional[float] = None
    material: Optional[str] = None
    epsilon_r: Optional[float] = None
    loss_tangent: Optional[float] = None


@dataclass
class Segment:
    start: Point
    end: Point
    width_mm: float
    layer: str
    net_id: int
    uuid: str
    kind: str = "segment"  # "segment" | "arc"

    @property
    def length_mm(self) -> float:
        return segment_length(self.start, self.end)


@dataclass
class Via:
    at: Point
    size_mm: float
    drill_mm: float
    layers: List[str]
    net_id: int
    uuid: str


@dataclass
class Pad:
    number: str
    at: Point
    size: Tuple[float, float]
    layers: List[str]
    net_id: int
    net_name: str
    rotation: float = 0.0  # world degrees (footprint rot + pad local rot)


@dataclass
class Footprint:
    reference: str
    value: str
    footprint: str
    at: Point
    rotation: float
    layer: str
    pads: List[Pad] = field(default_factory=list)
    uuid: str = ""


@dataclass
class FilledPolygon:
    layer: str
    points: List[Point]


@dataclass
class Zone:
    net_id: int
    net_name: str
    layers: List[str]
    uuid: str
    priority: int
    fill: bool
    outline: List[Point]
    filled_polygons: List[FilledPolygon]
    is_keepout: bool = False
    keepout_rules: Dict[str, str] = field(default_factory=dict)
    parent_reference: str = ""
    zone_kind: str = "copper"

    @property
    def layer(self) -> str:
        """Primary layer label (first of multi-layer zone)."""
        return self.layers[0] if self.layers else ""

    @property
    def geometry_by_layer(self) -> List[FilledPolygon]:
        if self.filled_polygons:
            return self.filled_polygons
        layer = self.layer or "unknown"
        return [FilledPolygon(layer=layer, points=self.outline)] if self.outline else []

    @property
    def bbox(self) -> Tuple[float, float, float, float]:
        polys = [fp.points for fp in self.geometry_by_layer]
        if not polys:
            return (0.0, 0.0, 0.0, 0.0)
        boxes = [polygon_bbox(p) for p in polys if p]
        return (
            min(b[0] for b in boxes),
            min(b[1] for b in boxes),
            max(b[2] for b in boxes),
            max(b[3] for b in boxes),
        )


@dataclass
class PcbModel:
    source: str
    board_thickness_mm: float
    copper_layers: List[str]
    stackup: List[StackupLayer]
    nets: Dict[int, str]
    segments: List[Segment]
    vias: List[Via]
    footprints: List[Footprint]
    zones: List[Zone]

    def net_name(self, net_id: int) -> str:
        return self.nets.get(net_id, f"<net {net_id}>")


def _atom(node: Any, idx: int, default: Any = None) -> Any:
    if isinstance(node, list) and len(node) > idx:
        return node[idx]
    return default


def _find_child(node: List[Any], tag: str) -> Optional[List[Any]]:
    for child in node[1:]:
        if isinstance(child, list) and child and child[0] == tag:
            return child
    return None


def _find_all_children(node: List[Any], tag: str) -> List[List[Any]]:
    return [
        child
        for child in node[1:]
        if isinstance(child, list) and child and child[0] == tag
    ]


def _parse_at(node: List[Any]) -> Tuple[float, float, float]:
    nums = [c for c in node[1:] if isinstance(c, (int, float))]
    if len(nums) >= 2:
        rot = float(nums[2]) if len(nums) >= 3 else 0.0
        return float(nums[0]), float(nums[1]), rot
    return 0.0, 0.0, 0.0


def _parse_net_ref(node: List[Any]) -> Tuple[int, str]:
    nums = [c for c in node[1:] if isinstance(c, (int, float))]
    strs = [c for c in node[1:] if isinstance(c, str)]
    net_id = int(nums[0]) if nums else 0
    net_name = strs[0] if strs else ""
    return net_id, net_name


def _parse_stackup(stackup_node: List[Any]) -> List[StackupLayer]:
    layers: List[StackupLayer] = []
    for child in stackup_node[1:]:
        if not isinstance(child, list) or not child or child[0] != "layer":
            continue
        name = _atom(child, 1, "")
        type_node = _find_child(child, "type")
        layer_type = _atom(type_node, 1, "") if type_node else ""
        thickness_node = _find_child(child, "thickness")
        material_node = _find_child(child, "material")
        eps_node = _find_child(child, "epsilon_r")
        loss_node = _find_child(child, "loss_tangent")
        layers.append(
            StackupLayer(
                name=str(name),
                type=str(layer_type),
                thickness_mm=float(_atom(thickness_node, 1)) if thickness_node else None,
                material=str(_atom(material_node, 1)) if material_node else None,
                epsilon_r=float(_atom(eps_node, 1)) if eps_node else None,
                loss_tangent=float(_atom(loss_node, 1)) if loss_node else None,
            )
        )
    return layers


def _transform_pad(fp_x: float, fp_y: float, fp_rot: float, pad_x: float, pad_y: float) -> Point:
    rx, ry = rotate_point(pad_x, pad_y, fp_rot)
    return (fp_x + rx, fp_y + ry)


def pad_world_polygon(pad: Pad):
    """Axis-aligned pad rectangle rotated by pad.rotation around pad.at (Shapely)."""
    from shapely.affinity import rotate
    from shapely.geometry import box

    w, h = pad.size if pad.size else (0.0, 0.0)
    if w <= 0.0 or h <= 0.0:
        return None
    x, y = pad.at
    geom = box(x - w / 2.0, y - h / 2.0, x + w / 2.0, y + h / 2.0)
    rot = float(pad.rotation or 0.0) % 360.0
    if rot < 1e-9 or abs(rot - 180.0) < 1e-9:
        return geom
    return rotate(geom, rot, origin=(x, y), use_radians=False)


def pad_on_layer(pad: Pad, layer: str) -> bool:
    if not pad.layers:
        return True
    if layer in pad.layers:
        return True
    return any(ly in ("*.Cu", "*") for ly in pad.layers)


def _ccw_delta(a: float, b: float) -> float:
    return (b - a) % (2.0 * math.pi)


def _circumcenter(a: Point, b: Point, c: Point) -> Optional[Point]:
    ax, ay = a
    bx, by = b
    cx, cy = c
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-18:
        return None
    ux = (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / d
    uy = (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / d
    return (ux, uy)


def tessellate_arc(
    start: Point,
    mid: Point,
    end: Point,
    n: int = 8,
) -> List[Tuple[Point, Point]]:
    """Approximate a KiCad (arc start/mid/end) as n chords through the mid point."""
    center = _circumcenter(start, mid, end)
    if center is None:
        return [(start, end)]
    a0 = math.atan2(start[1] - center[1], start[0] - center[0])
    am = math.atan2(mid[1] - center[1], mid[0] - center[0])
    a1 = math.atan2(end[1] - center[1], end[0] - center[0])
    ccw_via_mid = _ccw_delta(a0, am) + _ccw_delta(am, a1)
    cw_via_mid = _ccw_delta(am, a0) + _ccw_delta(a1, am)
    total = ccw_via_mid if ccw_via_mid <= cw_via_mid else -cw_via_mid
    radius = math.hypot(start[0] - center[0], start[1] - center[1])
    if radius < 1e-12 or abs(total) < 1e-12:
        return [(start, end)]
    points: List[Point] = [start]
    half = max(n // 2, 1)
    for i in range(1, half):
        ang = a0 + total * (i / n)
        points.append(
            (center[0] + radius * math.cos(ang), center[1] + radius * math.sin(ang))
        )
    points.append(mid)
    for i in range(half + 1, n):
        ang = a0 + total * (i / n)
        points.append(
            (center[0] + radius * math.cos(ang), center[1] + radius * math.sin(ang))
        )
    points.append(end)
    # drop consecutive duplicates
    cleaned: List[Point] = [points[0]]
    for pt in points[1:]:
        if math.hypot(pt[0] - cleaned[-1][0], pt[1] - cleaned[-1][1]) > 1e-9:
            cleaned.append(pt)
    return list(zip(cleaned[:-1], cleaned[1:]))


def _track_fields(node: List[Any]) -> Tuple[float, str, int, str]:
    width_node = _find_child(node, "width")
    layer_node = _find_child(node, "layer")
    net_node = _find_child(node, "net")
    uuid_node = _find_child(node, "uuid")
    return (
        float(_atom(width_node, 1, 0.2)),
        str(_atom(layer_node, 1, "")),
        int(_atom(net_node, 1, 0)),
        str(_atom(uuid_node, 1, "")),
    )


def _parse_pts_node(pts_node: List[Any]) -> List[Point]:
    points: List[Point] = []
    for child in pts_node[1:]:
        if isinstance(child, list) and child and child[0] == "xy":
            nums = [c for c in child[1:] if isinstance(c, (int, float))]
            if len(nums) >= 2:
                points.append((float(nums[0]), float(nums[1])))
    return points


def _parse_zone_polygon_container(node: List[Any]) -> List[Point]:
    pts_node = _find_child(node, "pts")
    return _parse_pts_node(pts_node) if pts_node else []


def _parse_zone(
    node: List[Any],
    nets: Dict[int, str],
    parent_reference: str = "",
) -> Optional[Zone]:
    net_node = _find_child(node, "net")
    net_id, _ = _parse_net_ref(net_node) if net_node else (0, "")
    net_name_node = _find_child(node, "net_name")
    net_name = str(_atom(net_name_node, 1, "")) if net_name_node else nets.get(net_id, "")
    layer_node = _find_child(node, "layer")
    layers_node = _find_child(node, "layers")
    layers: List[str] = []
    if layers_node:
        layers = [str(x) for x in layers_node[1:] if isinstance(x, str)]
    elif layer_node:
        layers = [str(_atom(layer_node, 1, ""))]
    uuid_node = _find_child(node, "uuid")
    uuid = str(_atom(uuid_node, 1, "")) if uuid_node else ""
    priority_node = _find_child(node, "priority")
    priority = int(_atom(priority_node, 1, 0)) if priority_node else 0

    keepout_node = _find_child(node, "keepout")
    keepout_rules: Dict[str, str] = {}
    if keepout_node:
        for child in keepout_node[1:]:
            if isinstance(child, list) and len(child) >= 2:
                keepout_rules[str(child[0])] = str(child[1])

    fill = True
    fill_node = _find_child(node, "fill")
    if fill_node:
        fill_flag = _atom(fill_node, 1, "yes")
        fill = str(fill_flag).lower() in ("yes", "true", "1")
    elif keepout_node:
        fill = False

    outline_node = _find_child(node, "polygon")
    outline = _parse_zone_polygon_container(outline_node) if outline_node else []

    filled_polygons: List[FilledPolygon] = []
    for fp_node in _find_all_children(node, "filled_polygon"):
        pts = _parse_zone_polygon_container(fp_node)
        if not pts:
            continue
        fp_layer_node = _find_child(fp_node, "layer")
        fp_layer = str(_atom(fp_layer_node, 1, "")) if fp_layer_node else (layers[0] if layers else "")
        filled_polygons.append(FilledPolygon(layer=fp_layer, points=pts))
        if fp_layer and fp_layer not in layers:
            layers.append(fp_layer)

    if not outline and not filled_polygons:
        return None

    zone_kind = "keepout" if keepout_node else "copper"
    if parent_reference:
        zone_kind = f"footprint_{zone_kind}"

    return Zone(
        net_id=net_id,
        net_name=net_name or nets.get(net_id, ""),
        layers=layers,
        uuid=uuid,
        priority=priority,
        fill=fill,
        outline=outline,
        filled_polygons=filled_polygons,
        is_keepout=bool(keepout_node),
        keepout_rules=keepout_rules,
        parent_reference=parent_reference,
        zone_kind=zone_kind,
    )


def _zone_intersects_polygon(zone: Zone, polygon: Sequence[Point]) -> bool:
    region_bbox = polygon_bbox(polygon)
    if not bboxes_overlap(zone.bbox, region_bbox):
        return False
    for fp in zone.geometry_by_layer:
        if polygons_intersect(fp.points, polygon):
            return True
    return False


def _zone_to_dict(zone: Zone, include_geometry: bool = True) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "net_id": zone.net_id,
        "net_name": zone.net_name,
        "layers": zone.layers,
        "layer": zone.layer,
        "uuid": zone.uuid,
        "priority": zone.priority,
        "fill": zone.fill,
        "bbox_mm": {
            "min_x": zone.bbox[0],
            "min_y": zone.bbox[1],
            "max_x": zone.bbox[2],
            "max_y": zone.bbox[3],
        },
        "outline_point_count": len(zone.outline),
        "filled_polygon_count": len(zone.filled_polygons),
        "uses_filled_geometry": bool(zone.filled_polygons),
        "is_keepout": zone.is_keepout,
        "keepout_rules": zone.keepout_rules,
        "parent_reference": zone.parent_reference or None,
        "zone_kind": zone.zone_kind,
    }
    if include_geometry:
        data["outline_mm"] = [list(p) for p in zone.outline]
        data["filled_polygons_mm"] = [
            {"layer": fp.layer, "points": [list(p) for p in fp.points]}
            for fp in zone.filled_polygons
        ]
    return data


def _parse_footprint(node: List[Any], nets: Dict[int, str]) -> Footprint:
    fp_name = str(_atom(node, 1, ""))
    at_node = _find_child(node, "at")
    fx, fy, rot = _parse_at(at_node) if at_node else (0.0, 0.0, 0.0)
    ref_node = _find_child(node, "property")
    reference = ""
    value = ""
    for prop in _find_all_children(node, "property"):
        key = _atom(prop, 1, "")
        if key == "Reference":
            reference = str(_atom(prop, 2, ""))
        elif key == "Value":
            value = str(_atom(prop, 2, ""))
    layer = "F.Cu"
    layer_child = _find_child(node, "layer")
    if layer_child:
        layer = str(_atom(layer_child, 1, "F.Cu"))

    pads: List[Pad] = []
    for pad_node in _find_all_children(node, "pad"):
        pad_num = str(_atom(pad_node, 1, "?"))
        at_p = _find_child(pad_node, "at")
        px, py, pad_rot = _parse_at(at_p) if at_p else (0.0, 0.0, 0.0)
        world = _transform_pad(fx, fy, rot, px, py)
        size_node = _find_child(pad_node, "size")
        w = float(_atom(size_node, 1, 0))
        h = float(_atom(size_node, 2, w))
        layers_node = _find_child(pad_node, "layers")
        pad_layers = [str(x) for x in layers_node[1:]] if layers_node else []
        net_node = _find_child(pad_node, "net")
        net_id, net_name_inline = _parse_net_ref(net_node) if net_node else (0, "")
        net_name = net_name_inline or nets.get(net_id, "")
        pads.append(
            Pad(
                number=pad_num,
                at=world,
                size=(w, h),
                layers=pad_layers,
                net_id=net_id,
                net_name=net_name,
                rotation=rot + pad_rot,
            )
        )

    uuid_node = _find_child(node, "uuid")
    uuid = str(_atom(uuid_node, 1, "")) if uuid_node else ""

    return Footprint(
        reference=reference,
        value=value,
        footprint=fp_name,
        at=(fx, fy),
        rotation=rot,
        layer=layer,
        pads=pads,
        uuid=uuid,
    )


def load_pcb(path: Path, use_cache: bool = True) -> PcbModel:
    from .cache import load_cached_pcb, save_cached_pcb

    if use_cache:
        cached = load_cached_pcb(path)
        if cached is not None:
            return cached

    text = path.read_text(encoding="utf-8")
    tree = parse(text)
    if not isinstance(tree, list) or tree[0] != "kicad_pcb":
        raise ValueError(f"Not a kicad_pcb file: {path}")

    board_thickness = 1.6
    general = _find_child(tree, "general")
    if general:
        thickness_node = _find_child(general, "thickness")
        if thickness_node:
            board_thickness = float(_atom(thickness_node, 1, board_thickness))

    stackup: List[StackupLayer] = []
    setup = _find_child(tree, "setup")
    if setup:
        stackup_node = _find_child(setup, "stackup")
        if stackup_node:
            stackup = _parse_stackup(stackup_node)

    copper_layers = [ly.name for ly in stackup if ly.type == "copper"]

    nets: Dict[int, str] = {}
    for child in tree[1:]:
        if isinstance(child, list) and child and child[0] == "net":
            net_id = int(_atom(child, 1, 0))
            net_name = str(_atom(child, 2, ""))
            nets[net_id] = net_name

    segments: List[Segment] = []
    vias: List[Via] = []
    footprints: List[Footprint] = []
    zones: List[Zone] = []

    for child in tree[1:]:
        if not isinstance(child, list) or not child:
            continue
        tag = child[0]
        if tag == "segment":
            start_node = _find_child(child, "start")
            end_node = _find_child(child, "end")
            if not start_node or not end_node:
                continue
            width_mm, layer, net_id, uuid = _track_fields(child)
            segments.append(
                Segment(
                    start=(float(start_node[1]), float(start_node[2])),
                    end=(float(end_node[1]), float(end_node[2])),
                    width_mm=width_mm,
                    layer=layer,
                    net_id=net_id,
                    uuid=uuid,
                    kind="segment",
                )
            )
        elif tag == "arc":
            start_node = _find_child(child, "start")
            mid_node = _find_child(child, "mid")
            end_node = _find_child(child, "end")
            if not start_node or not mid_node or not end_node:
                continue
            width_mm, layer, net_id, uuid = _track_fields(child)
            start = (float(start_node[1]), float(start_node[2]))
            mid = (float(mid_node[1]), float(mid_node[2]))
            end = (float(end_node[1]), float(end_node[2]))
            for a, b in tessellate_arc(start, mid, end):
                segments.append(
                    Segment(
                        start=a,
                        end=b,
                        width_mm=width_mm,
                        layer=layer,
                        net_id=net_id,
                        uuid=uuid,
                        kind="arc",
                    )
                )
        elif tag == "via":
            at_node = _find_child(child, "at")
            size_node = _find_child(child, "size")
            drill_node = _find_child(child, "drill")
            layers_node = _find_child(child, "layers")
            net_node = _find_child(child, "net")
            uuid_node = _find_child(child, "uuid")
            if not at_node:
                continue
            vx, vy, _ = _parse_at(at_node)
            via_layers = [str(x) for x in layers_node[1:]] if layers_node else []
            vias.append(
                Via(
                    at=(vx, vy),
                    size_mm=float(_atom(size_node, 1, 0.4)),
                    drill_mm=float(_atom(drill_node, 1, 0.3)),
                    layers=via_layers,
                    net_id=int(_atom(net_node, 1, 0)),
                    uuid=str(_atom(uuid_node, 1, "")),
                )
            )
        elif tag == "footprint":
            fp = _parse_footprint(child, nets)
            footprints.append(fp)
            for znode in _find_all_children(child, "zone"):
                z = _parse_zone(znode, nets, parent_reference=fp.reference)
                if z:
                    zones.append(z)
        elif tag == "zone":
            zone = _parse_zone(child, nets)
            if zone:
                zones.append(zone)

    model = PcbModel(
        source=str(path),
        board_thickness_mm=board_thickness,
        copper_layers=copper_layers,
        stackup=stackup,
        nets=nets,
        segments=segments,
        vias=vias,
        footprints=footprints,
        zones=zones,
    )
    if use_cache:
        save_cached_pcb(model, path)
    return model


def query_region(
    model: PcbModel,
    polygon: Sequence[Point],
    layers: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Return routing/objects intersecting a polygon (mm)."""
    from .plane_analysis import PlaneIndex, analyze_region_return_paths

    layer_filter = set(layers) if layers else None
    plane_index = PlaneIndex(model)

    seg_hits = []
    net_seg_stats: Dict[int, Dict[str, Any]] = {}

    for seg in model.segments:
        if layer_filter and seg.layer not in layer_filter:
            continue
        if not segment_intersects_polygon(seg.start, seg.end, polygon):
            continue
        clipped = clip_segment_to_polygon(seg.start, seg.end, polygon)
        partial = not (
            point_in_polygon(seg.start, polygon) and point_in_polygon(seg.end, polygon)
        )
        seg_hits.append(
            {
                "start": seg.start,
                "end": seg.end,
                "layer": seg.layer,
                "width_mm": seg.width_mm,
                "net_id": seg.net_id,
                "net_name": model.net_name(seg.net_id),
                "length_in_region_mm": round(clipped, 4),
                "full_length_mm": round(seg.length_mm, 4),
                "partial": partial,
                "uuid": seg.uuid,
            }
        )
        st = net_seg_stats.setdefault(
            seg.net_id,
            {"length_in_region_mm": 0.0, "segment_count": 0, "layers": set()},
        )
        st["length_in_region_mm"] += clipped
        st["segment_count"] += 1
        st["layers"].add(seg.layer)

    via_hits = []
    net_via_stats: Dict[int, int] = {}
    for via in model.vias:
        if not point_in_polygon(via.at, polygon):
            continue
        via_hits.append(
            {
                "at": via.at,
                "size_mm": via.size_mm,
                "drill_mm": via.drill_mm,
                "layers": via.layers,
                "net_id": via.net_id,
                "net_name": model.net_name(via.net_id),
                "uuid": via.uuid,
            }
        )
        net_via_stats[via.net_id] = net_via_stats.get(via.net_id, 0) + 1

    fp_hits = []
    pad_hits = []
    for fp in model.footprints:
        fp_inside = point_in_polygon(fp.at, polygon)
        pads_in = []
        for pad in fp.pads:
            if point_in_polygon(pad.at, polygon):
                pads_in.append(
                    {
                        "pad": pad.number,
                        "at": pad.at,
                        "net_id": pad.net_id,
                        "net_name": pad.net_name or model.net_name(pad.net_id),
                        "layers": pad.layers,
                    }
                )
        if fp_inside or pads_in:
            fp_hits.append(
                {
                    "reference": fp.reference,
                    "value": fp.value,
                    "footprint": fp.footprint,
                    "at": fp.at,
                    "rotation": fp.rotation,
                    "layer": fp.layer,
                    "center_in_region": fp_inside,
                    "pads_in_region": pads_in,
                }
            )
            pad_hits.extend(
                [{"reference": fp.reference, **p} for p in pads_in]
            )

    zone_hits = []
    keepout_hits = []
    layer_copper: Dict[str, Dict[str, float]] = {}
    for zone in model.zones:
        if layer_filter and not any(ly in layer_filter for ly in zone.layers):
            continue
        if not _zone_intersects_polygon(zone, polygon):
            continue
        if zone.is_keepout:
            keepout_hits.append(_zone_to_dict(zone, include_geometry=True))
            continue
        fp_coverages = []
        best_coverage = 0.0
        for fp in zone.geometry_by_layer:
            if layer_filter and fp.layer not in layer_filter:
                continue
            cov = estimate_region_coverage_pct(polygon, fp.points, grid=20)
            fp_coverages.append(
                {"layer": fp.layer, "coverage_in_region_pct": round(cov, 2)}
            )
            best_coverage = max(best_coverage, cov)
            layer_copper.setdefault(fp.layer, {})
            layer_copper[fp.layer][zone.net_name] = (
                layer_copper[fp.layer].get(zone.net_name, 0.0) + cov
            )
        zone_hits.append(
            {
                **{k: v for k, v in _zone_to_dict(zone, include_geometry=False).items()},
                "coverage_in_region_pct": round(best_coverage, 2),
                "filled_polygon_coverages": fp_coverages,
                "outline_mm": [list(p) for p in zone.outline],
                "filled_polygons_mm": [
                    {"layer": fp.layer, "points": [list(p) for p in fp.points]}
                    for fp in zone.filled_polygons
                ],
            }
        )

    copper_planes = []
    region_area = polygon_area(polygon)
    return_path_checks, stackup_ref_map = analyze_region_return_paths(
        model, polygon, index=plane_index
    )
    for layer in sorted(layer_copper):
        nets_cov = layer_copper[layer]
        copper_planes.append(
            {
                "layer": layer,
                "nets": [
                    {
                        "net_name": net_name,
                        "coverage_in_region_pct": round(cov, 2),
                    }
                    for net_name, cov in sorted(
                        nets_cov.items(), key=lambda x: -x[1]
                    )
                ],
            }
        )

    nets_summary = []
    all_net_ids = set(net_seg_stats) | set(net_via_stats)
    for net_id in sorted(all_net_ids):
        seg_st = net_seg_stats.get(net_id, {})
        nets_summary.append(
            {
                "net_id": net_id,
                "net_name": model.net_name(net_id),
                "segment_count": seg_st.get("segment_count", 0),
                "length_in_region_mm": round(seg_st.get("length_in_region_mm", 0.0), 4),
                "layers": sorted(seg_st.get("layers", set())),
                "via_count": net_via_stats.get(net_id, 0),
            }
        )

    from .design_judgments import (
        attach_analysis_provenance,
        find_split_ground_bridges,
        generate_ai_assisted_judgments,
    )
    from .region_summary import build_conversation_summary

    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    split_ground_bridges = find_split_ground_bridges(model)
    result = {
        "polygon_mm": [list(p) for p in polygon],
        "bbox_mm": {
            "min_x": min(xs),
            "max_x": max(xs),
            "min_y": min(ys),
            "max_y": max(ys),
        },
        "region_area_mm2": round(region_area, 4),
        "segment_count": len(seg_hits),
        "via_count": len(via_hits),
        "zone_count": len(zone_hits),
        "keepout_count": len(keepout_hits),
        "footprint_count": len(fp_hits),
        "pad_count": len(pad_hits),
        "copper_planes_in_region": copper_planes,
        "return_path_checks": return_path_checks,
        "reference_plane_mode": "stackup_adjacent_split_ground",
        "stackup_reference_map": stackup_ref_map,
        "coverage_method": "shapely_exact",
        "nets_summary": nets_summary,
        "zones": zone_hits,
        "keepouts": keepout_hits,
        "segments": seg_hits,
        "vias": via_hits,
        "footprints": fp_hits,
        "split_ground_bridges": split_ground_bridges,
    }
    from .buck_extra_checks import analyze_all_buck_extra_checks
    from .power_layout_checks import analyze_buck_power_layout

    power_checks = analyze_buck_power_layout(model, polygon=polygon, region_result=result)
    extra_by_ref = {
        c["ic_reference"]: c
        for c in analyze_all_buck_extra_checks(
            model, [pc["ic_reference"] for pc in power_checks]
        )
    }
    status_rank = {"fail": 0, "warn": 1, "pass": 2, "info": 3}
    for pc in power_checks:
        extra = extra_by_ref.get(pc["ic_reference"])
        if not extra:
            continue
        pc.update({k: v for k, v in extra.items() if k not in ("source", "ic_reference")})
        pc["overall_status"] = min(
            (pc.get("overall_status", "pass"), extra.get("overall_status", "pass")),
            key=lambda s: status_rank.get(s, 9),
        )
    result["power_layout_checks"] = power_checks

    from .mlcc_dc_bias import analyze_mlcc_dc_bias, apply_mlcc_to_power_checks

    mlcc = analyze_mlcc_dc_bias(model)
    apply_mlcc_to_power_checks(power_checks, mlcc)
    result["mlcc_dc_bias"] = mlcc

    from .circuit_graph import analyze_ic_circuit

    result["circuit_neighborhoods"] = [
        analyze_ic_circuit(model, pc["ic_reference"]) for pc in power_checks
    ]

    # ai_assisted_judgments must run AFTER power_layout_checks so datasheet
    # crossref judgments can see BOOT/SW-length/thermal-via toolchain values.
    result["ai_assisted_judgments"] = generate_ai_assisted_judgments(model, result)
    result["conversation_summary"] = build_conversation_summary(result)
    return attach_analysis_provenance(result)


def zones_summary(model: PcbModel, include_geometry: bool = False) -> Dict[str, Any]:
    by_layer: Dict[str, Dict[str, int]] = {}
    for zone in model.zones:
        layer_keys = zone.layers or ([zone.layer] if zone.layer else ["unknown"])
        for ly in layer_keys:
            by_layer.setdefault(ly, {})
            by_layer[ly][zone.net_name] = by_layer[ly].get(zone.net_name, 0) + 1

    keepouts = sum(1 for z in model.zones if z.is_keepout)
    footprint_zones = sum(1 for z in model.zones if z.parent_reference)
    return {
        "zone_count": len(model.zones),
        "keepout_count": keepouts,
        "footprint_zone_count": footprint_zones,
        "by_layer_net_count": {
            layer: dict(sorted(nets.items()))
            for layer, nets in sorted(by_layer.items())
        },
        "zones": [
            _zone_to_dict(zone, include_geometry=include_geometry)
            for zone in sorted(model.zones, key=lambda z: (z.layer, z.net_name, z.uuid))
        ],
    }


def board_summary(model: PcbModel) -> Dict[str, Any]:
    net_lengths: Dict[int, float] = {}
    net_layers: Dict[int, set] = {}
    for seg in model.segments:
        net_lengths[seg.net_id] = net_lengths.get(seg.net_id, 0.0) + seg.length_mm
        net_layers.setdefault(seg.net_id, set()).add(seg.layer)

    net_vias: Dict[int, int] = {}
    for via in model.vias:
        net_vias[via.net_id] = net_vias.get(via.net_id, 0) + 1

    routed_nets = sorted(
        [
            {
                "net_id": nid,
                "net_name": model.net_name(nid),
                "total_length_mm": round(net_lengths.get(nid, 0.0), 4),
                "segment_count": sum(1 for s in model.segments if s.net_id == nid),
                "via_count": net_vias.get(nid, 0),
                "layers": sorted(net_layers.get(nid, set())),
            }
            for nid in set(net_lengths) | set(net_vias)
            if nid != 0 and model.net_name(nid)
        ],
        key=lambda x: x["net_name"],
    )

    return {
        "source": model.source,
        "board_thickness_mm": model.board_thickness_mm,
        "copper_layers": model.copper_layers,
        "stackup": [
            {
                "name": ly.name,
                "type": ly.type,
                "thickness_mm": ly.thickness_mm,
                "material": ly.material,
                "epsilon_r": ly.epsilon_r,
                "loss_tangent": ly.loss_tangent,
            }
            for ly in model.stackup
        ],
        "net_count": len(model.nets),
        "routed_net_count": len(routed_nets),
        "segment_count": len(model.segments),
        "via_count": len(model.vias),
        "zone_count": len(model.zones),
        "keepout_count": sum(1 for z in model.zones if z.is_keepout),
        "footprint_zone_count": sum(1 for z in model.zones if z.parent_reference),
        "footprint_count": len(model.footprints),
        "zones_by_layer": zones_summary(model, include_geometry=False)["by_layer_net_count"],
        "nets": routed_nets,
        "footprints": [
            {
                "reference": fp.reference,
                "value": fp.value,
                "footprint": fp.footprint,
                "at": fp.at,
                "rotation": fp.rotation,
                "layer": fp.layer,
                "pad_count": len(fp.pads),
            }
            for fp in sorted(model.footprints, key=lambda f: f.reference)
        ],
    }
