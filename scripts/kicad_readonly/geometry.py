"""2D geometry helpers for PCB region queries (mm coordinates)."""

from __future__ import annotations

import math
from typing import List, Sequence, Tuple

Point = Tuple[float, float]


def _cross(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Ray casting; works for convex and simple concave polygons."""
    x, y = point
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        if ((y1 > y) != (y2 > y)) and (
            x < (x2 - x1) * (y - y1) / (y2 - y1 + 1e-15) + x1
        ):
            inside = not inside
    return inside


def _segments_intersect(p1: Point, p2: Point, q1: Point, q2: Point) -> bool:
    d1 = _cross(q1, q2, p1)
    d2 = _cross(q1, q2, p2)
    d3 = _cross(p1, p2, q1)
    d4 = _cross(p1, p2, q2)
    if (
        ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0))
        and ((d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0))
    ):
        return True
    eps = 1e-9
    if abs(d1) < eps and point_on_segment(p1, q1, q2):
        return True
    if abs(d2) < eps and point_on_segment(p2, q1, q2):
        return True
    if abs(d3) < eps and point_on_segment(q1, p1, p2):
        return True
    if abs(d4) < eps and point_on_segment(q2, p1, p2):
        return True
    return False


def point_on_segment(p: Point, a: Point, b: Point, eps: float = 1e-6) -> bool:
    if (
        min(a[0], b[0]) - eps <= p[0] <= max(a[0], b[0]) + eps
        and min(a[1], b[1]) - eps <= p[1] <= max(a[1], b[1]) + eps
    ):
        return abs(_cross(a, b, p)) < eps
    return False


def segment_intersects_polygon(
    start: Point, end: Point, polygon: Sequence[Point]
) -> bool:
    if point_in_polygon(start, polygon) or point_in_polygon(end, polygon):
        return True
    n = len(polygon)
    for i in range(n):
        q1 = polygon[i]
        q2 = polygon[(i + 1) % n]
        if _segments_intersect(start, end, q1, q2):
            return True
    return False


def segment_length(start: Point, end: Point) -> float:
    return math.hypot(end[0] - start[0], end[1] - start[1])


def clip_segment_to_polygon(
    start: Point, end: Point, polygon: Sequence[Point]
) -> float:
    """Approximate length of segment inside polygon via sampling."""
    if not segment_intersects_polygon(start, end, polygon):
        return 0.0
    if point_in_polygon(start, polygon) and point_in_polygon(end, polygon):
        return segment_length(start, end)

    samples = 64
    total = segment_length(start, end)
    if total < 1e-9:
        return total if point_in_polygon(start, polygon) else 0.0

    inside_len = 0.0
    prev_inside = point_in_polygon(start, polygon)
    for i in range(1, samples + 1):
        t = i / samples
        pt = (
            start[0] + t * (end[0] - start[0]),
            start[1] + t * (end[1] - start[1]),
        )
        cur_inside = point_in_polygon(pt, polygon)
        if cur_inside and prev_inside:
            inside_len += total / samples
        elif cur_inside != prev_inside:
            inside_len += total / samples * 0.5
        prev_inside = cur_inside
    return inside_len


def rotate_point(x: float, y: float, degrees: float) -> Point:
    rad = math.radians(degrees)
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)
    return (x * cos_r - y * sin_r, x * sin_r + y * cos_r)


def parse_polygon(coords: List[float]) -> List[Point]:
    if len(coords) < 6 or len(coords) % 2 != 0:
        raise ValueError("Polygon needs at least 3 points as x1,y1,x2,y2,... pairs")
    return [(coords[i], coords[i + 1]) for i in range(0, len(coords), 2)]


def polygon_bbox(polygon: Sequence[Point]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def bboxes_overlap(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def polygon_area(polygon: Sequence[Point]) -> float:
    if len(polygon) < 3:
        return 0.0
    area = 0.0
    for i in range(len(polygon)):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % len(polygon)]
        area += x1 * y2 - x2 * y1
    return abs(area) * 0.5


def polygons_intersect(a: Sequence[Point], b: Sequence[Point]) -> bool:
    if not a or not b:
        return False
    if bboxes_overlap(polygon_bbox(a), polygon_bbox(b)) is False:
        return False
    for poly_a, poly_b in ((a, b), (b, a)):
        for pt in poly_a:
            if point_in_polygon(pt, poly_b):
                return True
    n_a = len(a)
    n_b = len(b)
    for i in range(n_a):
        p1, p2 = a[i], a[(i + 1) % n_a]
        for j in range(n_b):
            q1, q2 = b[j], b[(j + 1) % n_b]
            if _segments_intersect(p1, p2, q1, q2):
                return True
    return False


def _shapely_polygon(points: Sequence[Point]):
    from shapely.geometry import Polygon

    if len(points) < 3:
        return None
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)
    return poly if not poly.is_empty else None


def min_distance_segment_to_segment(
    a0: Point, a1: Point, b0: Point, b1: Point
) -> float:
    """Exact 2D distance between two finite segments (Shapely)."""
    from shapely.geometry import LineString

    return float(LineString([a0, a1]).distance(LineString([b0, b1])))


def min_distance_polygon_to_segment(
    polygon: Sequence[Point], a0: Point, a1: Point
) -> float:
    """Exact 2D distance from a polygon (filled copper) to a segment."""
    from shapely.geometry import LineString

    poly = _shapely_polygon(polygon)
    if poly is None:
        return float("inf")
    return float(poly.distance(LineString([a0, a1])))


def exact_overlap_area_mm2(a: Sequence[Point], b: Sequence[Point]) -> float:
    """Exact intersection area (mm²) via Shapely."""
    pa = _shapely_polygon(a)
    pb = _shapely_polygon(b)
    if pa is None or pb is None:
        return 0.0
    inter = pa.intersection(pb)
    return float(inter.area) if not inter.is_empty else 0.0


def estimate_overlap_area_mm2(
    a: Sequence[Point],
    b: Sequence[Point],
    grid: int = 24,
) -> float:
    """Exact overlap when possible; grid fallback."""
    if not polygons_intersect(a, b):
        return 0.0
    try:
        return exact_overlap_area_mm2(a, b)
    except Exception:
        pass
    ax0, ay0, ax1, ay1 = polygon_bbox(a)
    bx0, by0, bx1, by1 = polygon_bbox(b)
    x0, y0 = max(ax0, bx0), max(ay0, by0)
    x1, y1 = min(ax1, bx1), min(ay1, by1)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    cell_area = ((x1 - x0) / grid) * ((y1 - y0) / grid)
    count = 0
    for i in range(grid):
        for j in range(grid):
            px = x0 + (i + 0.5) * (x1 - x0) / grid
            py = y0 + (j + 0.5) * (y1 - y0) / grid
            if point_in_polygon((px, py), a) and point_in_polygon((px, py), b):
                count += 1
    return cell_area * count


def estimate_region_coverage_pct(
    region: Sequence[Point],
    copper: Sequence[Point],
    grid: int = 24,
) -> float:
    """Percent of query region covered by a copper polygon (exact area)."""
    overlap = estimate_overlap_area_mm2(region, copper, grid=grid)
    region_area = polygon_area(region)
    if region_area < 1e-9:
        return 0.0
    return min(100.0, overlap / region_area * 100.0)
