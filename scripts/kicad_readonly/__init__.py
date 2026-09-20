"""Read-only KiCad PCB/schematic analysis (no file modification)."""

from .pcb import load_pcb
from .geometry import point_in_polygon, polygons_intersect, segment_intersects_polygon

__all__ = [
    "load_pcb",
    "point_in_polygon",
    "polygons_intersect",
    "segment_intersects_polygon",
]
