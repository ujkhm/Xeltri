#!/usr/bin/env python3
"""
Query routing/objects inside a polygon region on KiCad PCB (read-only).

Coordinates are KiCad PCB editor mm values.

Usage:
  python query_region.py --rect 158,65 168,75
  python query_region.py --polygon 140,68 150,68 150,75 140,75 --layers F.Cu,In2.Cu
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from kicad_readonly.geometry import parse_polygon
from kicad_readonly.pcb import load_pcb, query_region
from kicad_readonly.pcb_paths import format_pcb_source_banner, resolve_pcb_path
from kicad_readonly.region_summary import format_conversation_summary_text
from kicad_readonly.sch_index import index_schematic_nets
DEFAULT_SCH_DIR = SCRIPT_DIR.parent / "KiCad" / "Xeltri"
DEFAULT_OUT = SCRIPT_DIR.parent / "analysis" / "region_query.json"


def _parse_coords_string(s: str) -> list[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Query KiCad PCB region (read-only)")
    parser.add_argument(
        "--pcb",
        type=Path,
        default=None,
        help="Path to .kicad_pcb (default: auto — portable KiCad copy if newer)",
    )
    parser.add_argument("--sch-dir", type=Path, default=DEFAULT_SCH_DIR)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--polygon",
        type=str,
        help="Polygon as comma-separated x,y pairs: x1,y1,x2,y2,x3,y3,...",
    )
    parser.add_argument(
        "--rect",
        type=str,
        help="Axis-aligned rectangle: x1,y1,x2,y2 (any two opposite corners)",
    )
    parser.add_argument(
        "--layers",
        type=str,
        default="",
        help="Comma-separated layer filter, e.g. F.Cu,In2.Cu (default: all)",
    )
    parser.add_argument("--nets-only", action="store_true", help="Omit segment/via detail lists")
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args()

    if bool(args.polygon) == bool(args.rect):
        print("ERROR: specify exactly one of --polygon or --rect", file=sys.stderr)
        return 1

    try:
        pcb_path, pcb_reason = resolve_pcb_path(args.pcb)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.rect:
        coords = _parse_coords_string(args.rect)
        if len(coords) != 4:
            print("ERROR: --rect needs 4 numbers: x1,y1,x2,y2", file=sys.stderr)
            return 1
        x1, y1, x2, y2 = coords
        coords = [x1, y1, x2, y1, x2, y2, x1, y2]
    else:
        coords = _parse_coords_string(args.polygon)

    polygon = parse_polygon(coords)
    layer_filter = [x.strip() for x in args.layers.split(",") if x.strip()] or None

    import time

    print(format_pcb_source_banner(pcb_path, pcb_reason))
    t0 = time.perf_counter()
    model = load_pcb(pcb_path, use_cache=not args.no_cache)
    print(f"  loaded in {time.perf_counter() - t0:.2f}s")

    result = query_region(model, polygon, layers=layer_filter)
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    result["pcb_file"] = str(pcb_path)
    result["pcb_resolution"] = pcb_reason
    result["board_thickness_mm"] = model.board_thickness_mm
    result["copper_layers"] = model.copper_layers
    result["stackup"] = [
        {
            "name": ly.name,
            "type": ly.type,
            "thickness_mm": ly.thickness_mm,
            "epsilon_r": ly.epsilon_r,
        }
        for ly in model.stackup
    ]

    if args.sch_dir.is_dir():
        sch_index = index_schematic_nets(args.sch_dir)
        for net in result["nets_summary"]:
            name = net["net_name"]
            if name in sch_index:
                net["schematic_sheets"] = sch_index[name]

    if args.nets_only:
        result.pop("segments", None)
        result.pop("vias", None)
        result.pop("footprints", None)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"Wrote: {args.out}")
    print(
        f"  segments={result['segment_count']} vias={result['via_count']} "
        f"zones={result['zone_count']} footprints={result['footprint_count']} "
        f"nets={len(result['nets_summary'])}"
    )
    if result.get("copper_planes_in_region"):
        print("  Copper planes in region (exact area):")
        for plane in result["copper_planes_in_region"]:
            nets = ", ".join(
                f"{n['net_name']} {n['coverage_in_region_pct']:.0f}%"
                for n in plane["nets"][:5]
            )
            print(f"    {plane['layer']}: {nets}")
    if result.get("return_path_checks"):
        mode = result.get("reference_plane_mode", "legacy")
        fails = [r for r in result["return_path_checks"] if r["status"] != "pass"]
        print(
            f"  Return-path ({mode}): "
            f"{len(result['return_path_checks'])} nets, "
            f"{sum(1 for r in result['return_path_checks'] if r['status']=='pass')} pass"
        )
        for r in result["return_path_checks"][:8]:
            pat = r.get("routing_pattern", "")
            worst = r.get("worst_segment_coverage_pct", r["coverage_pct"])
            dom = r.get("dominant_reference_net", "GND")
            refs = ",".join(r.get("reference_nets_checked", ["GND"]))
            print(
                f"    [{r['status']}] {r['net_name']}: "
                f"weighted {r['coverage_pct']:.0f}% via {dom} "
                f"(worst seg {worst:.0f}%) — {pat}"
            )
            if dom != "GND":
                print(f"      ref nets checked: {refs}; {r.get('notes', '')}")
        for r in fails[:4]:
            if r["status"] != "pass":
                print(f"      ! {r['notes']}")
    if result.get("split_ground_bridges"):
        in_region = [
            b
            for b in result["split_ground_bridges"]
            if result.get("bbox_mm")
            and b["at_mm"][0] >= result["bbox_mm"]["min_x"]
            and b["at_mm"][0] <= result["bbox_mm"]["max_x"]
            and b["at_mm"][1] >= result["bbox_mm"]["min_y"]
            and b["at_mm"][1] <= result["bbox_mm"]["max_y"]
        ]
        refs = ", ".join(b["reference"] for b in result["split_ground_bridges"])
        print(f"  [toolchain] GND<->GNDA bridges on board: {refs}")
        if in_region:
            print(
                f"    in region: {', '.join(b['reference'] for b in in_region)}"
            )
    if result.get("conversation_summary"):
        block = format_conversation_summary_text(result["conversation_summary"])
        print(block.encode("ascii", "replace").decode("ascii"))
    if result.get("ai_assisted_judgments"):
        print(
            f"  [ai_assisted] {len(result['ai_assisted_judgments'])} judgment(s):"
        )
        for j in result["ai_assisted_judgments"][:6]:
            conf = j.get("confidence", "?")
            text = j.get("conclusion", "").encode("ascii", "replace").decode("ascii")
            print(f"    ({conf}) {text}")
    if result.get("keepout_count"):
        print(f"  Keepouts in region: {result['keepout_count']}")
    if result["nets_summary"]:
        print("  Top nets in region:")
        for net in sorted(
            result["nets_summary"],
            key=lambda n: n["length_in_region_mm"],
            reverse=True,
        )[:8]:
            print(
                f"    {net['net_name']}: "
                f"{net['length_in_region_mm']:.2f} mm, "
                f"{net['segment_count']} seg, "
                f"{net['via_count']} via"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
