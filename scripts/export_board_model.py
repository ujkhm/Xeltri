#!/usr/bin/env python3
"""
Export read-only board model from KiCad .kicad_pcb (does NOT modify KiCad files).

Usage:
  python export_board_model.py
  python export_board_model.py --pcb KiCad/Xeltri/Xeltri.kicad_pcb --out analysis/board_model.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from kicad_readonly.pcb import board_summary, load_pcb, zones_summary
from kicad_readonly.pcb_paths import format_pcb_source_banner, resolve_pcb_path
from kicad_readonly.sch_index import index_schematic_nets
DEFAULT_SCH_DIR = SCRIPT_DIR.parent / "KiCad" / "Xeltri"
DEFAULT_OUT = SCRIPT_DIR.parent / "analysis" / "board_model.json"
DEFAULT_ZONES_OUT = SCRIPT_DIR.parent / "analysis" / "zones_geometry.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="Export KiCad PCB read-only model")
    parser.add_argument(
        "--pcb",
        type=Path,
        default=None,
        help="Path to .kicad_pcb (default: auto — portable KiCad copy if newer)",
    )
    parser.add_argument("--sch-dir", type=Path, default=DEFAULT_SCH_DIR, help="Schematic directory")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Output JSON path")
    parser.add_argument("--no-sch", action="store_true", help="Skip schematic net index")
    parser.add_argument(
        "--zones-out",
        type=Path,
        default=DEFAULT_ZONES_OUT,
        help="Full zone/copper pour geometry JSON",
    )
    parser.add_argument(
        "--no-zones-geometry",
        action="store_true",
        help="Skip writing zones_geometry.json",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Force re-parse PCB (ignore pickle cache)",
    )
    args = parser.parse_args()

    try:
        pcb_path, pcb_reason = resolve_pcb_path(args.pcb)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(format_pcb_source_banner(pcb_path, pcb_reason))
    import time

    t0 = time.perf_counter()
    model = load_pcb(pcb_path, use_cache=not args.no_cache)
    elapsed = time.perf_counter() - t0
    cache_note = "cache hit" if elapsed < 2.0 and not args.no_cache else "parsed"
    print(f"  loaded in {elapsed:.2f}s ({cache_note})")
    summary = board_summary(model)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pcb_file": str(pcb_path),
        "pcb_resolution": pcb_reason,
        "board": summary,
    }

    if not args.no_sch and args.sch_dir.is_dir():
        print(f"Indexing schematics: {args.sch_dir}")
        output["schematic_net_index"] = index_schematic_nets(args.sch_dir)
        output["schematic_net_index_note"] = (
            "Label/global_label/power Value → sheet names. Not full connectivity."
        )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    zones_path = None
    if not args.no_zones_geometry:
        zones_payload = {
            "generated_at": output["generated_at"],
            "pcb_file": output["pcb_file"],
            "zones": zones_summary(model, include_geometry=True),
        }
        args.zones_out.parent.mkdir(parents=True, exist_ok=True)
        args.zones_out.write_text(
            json.dumps(zones_payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        zones_path = args.zones_out
        (args.out.parent / "latest_zones_geometry.json").write_text(
            args.zones_out.read_text(encoding="utf-8"), encoding="utf-8"
        )

    print(f"Wrote: {args.out}")
    if zones_path:
        print(f"Wrote: {zones_path}")
    print(
        f"  footprints={summary['footprint_count']} "
        f"segments={summary['segment_count']} "
        f"vias={summary['via_count']} "
        f"zones={summary['zone_count']} "
        f"routed_nets={summary['routed_net_count']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
