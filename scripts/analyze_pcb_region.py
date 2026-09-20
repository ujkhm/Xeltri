#!/usr/bin/env python3
"""
One-shot PCB analysis: export board model + query region (read-only).

Region coordinates (KiCad mm):
  - 2 points (4 numbers): diagonal corners → axis-aligned rectangle
  - 3+ points (6+ numbers): polygon boundary vertices

Usage:
  python analyze_pcb_region.py --region 158,65,168,75
  python analyze_pcb_region.py --region 140,68,150,68,150,75,140,75 --layers F.Cu
  python analyze_pcb_region.py --region 158,65,168,75 --skip-board-export
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
ANALYSIS_DIR = REPO_ROOT / "analysis"

sys.path.insert(0, str(SCRIPT_DIR))
from kicad_readonly.pcb_paths import format_pcb_source_banner, resolve_pcb_path
from kicad_readonly.region_summary import build_conversation_summary


def _parse_region(s: str) -> tuple[str, str]:
    """Return (mode, coord_string) for query_region.py."""
    parts = [p.strip() for p in s.replace(";", ",").split(",") if p.strip()]
    if len(parts) < 4 or len(parts) % 2 != 0:
        raise ValueError(
            "Region needs at least 2 points (4 numbers) as x,y pairs, "
            "e.g. 158,65,168,75 or polygon 140,68,150,68,150,75,140,75"
        )
    coord_str = ",".join(parts)
    if len(parts) == 4:
        return "rect", coord_str
    return "polygon", coord_str


def _run(cmd: list[str]) -> None:
    print(f">>> {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=REPO_ROOT)
    if result.returncode != 0:
        raise SystemExit(result.returncode)


def _run_si_checks(region_out: Path, pcb_path: Path, full_wave: bool) -> None:
    """Run si_sim for each buck IC found in the region report and fold results
    into region_out's si_checks + regenerate conversation_summary."""
    region_result = json.loads(region_out.read_text(encoding="utf-8"))
    ic_refs = [
        pl["ic_reference"] for pl in region_result.get("power_layout_checks", [])
    ]
    if not ic_refs:
        print("No buck ICs in region_result.power_layout_checks — skipping SI check.")
        return

    si_checks = []
    for ref in ic_refs:
        cmd = [
            sys.executable,
            str(SCRIPT_DIR / "si_sim" / "run_si_check.py"),
            "--ic",
            ref,
            "--pcb",
            str(pcb_path),
        ]
        if full_wave:
            cmd.append("--full-wave")
        print(f">>> {' '.join(cmd)}")
        proc = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        print(proc.stdout)
        if proc.returncode != 0:
            print(f"  SI check failed for {ref}: {proc.stderr[-500:]}", file=sys.stderr)
            continue
        out_path = None
        for line in proc.stdout.splitlines():
            if line.startswith("Wrote: "):
                out_path = Path(line[len("Wrote: ") :].strip())
        if out_path and out_path.is_file():
            si_data = json.loads(out_path.read_text(encoding="utf-8"))
            a = si_data.get("analytical", {})
            row = {
                "source": "toolchain+ai_assisted",
                "ic_reference": ref,
                "net_name": a.get("net_name"),
                "method": a.get("method"),
                "headline_metric": a.get("headline_metric"),
                "status": a.get("status"),
                "detail_file": str(out_path),
                "shield_remaining_factor": a.get("shield_remaining_factor"),
            }
            fw = si_data.get("full_wave")
            if fw and fw.get("ok"):
                row["full_wave_induced_mv"] = fw.get("induced_voltage_estimate_mv")
                row["full_wave_elapsed_s"] = fw.get("elapsed_s")
            si_checks.append(row)

    region_result["si_checks"] = si_checks
    region_result["conversation_summary"] = build_conversation_summary(region_result)
    region_out.write_text(
        json.dumps(region_result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"Folded {len(si_checks)} si_checks into {region_out}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Export + region query (read-only)")
    parser.add_argument(
        "--region",
        required=True,
        help="x1,y1,x2,y2 (2-point diagonal rect) or polygon vertices",
    )
    parser.add_argument(
        "--pcb",
        type=Path,
        default=None,
        help="Path to .kicad_pcb (default: auto — portable KiCad copy if newer)",
    )
    parser.add_argument("--layers", type=str, default="")
    parser.add_argument("--skip-board-export", action="store_true")
    parser.add_argument("--nets-only", action="store_true")
    parser.add_argument(
        "--tag",
        type=str,
        default="",
        help="Output filename tag, e.g. fmc → region_fmc.json",
    )
    parser.add_argument(
        "--si",
        action="store_true",
        help=(
            "Run analytical SI pre-screen (SW->FB coupling, seconds/IC) for "
            "any buck IC found in region and fold results into si_checks"
        ),
    )
    parser.add_argument(
        "--si-full-wave",
        action="store_true",
        help="Also run the openEMS FDTD full-wave SI check (~15-25 min per IC)",
    )
    args = parser.parse_args()

    try:
        pcb_path, pcb_reason = resolve_pcb_path(args.pcb)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(format_pcb_source_banner(pcb_path, pcb_reason))
    if "portable" in pcb_reason.lower():
        print(
            "  NOTE: portable Temp copy is not git. After Save, run "
            "scripts/sync_portable_kicad_to_repo.ps1 then commit from E:\\Xeltri."
        )

    mode, coords = _parse_region(args.region)
    tag = f"_{args.tag}" if args.tag else ""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    region_out = ANALYSIS_DIR / f"region{tag}_{ts}.json"
    manifest_out = ANALYSIS_DIR / f"session{tag}_{ts}.json"

    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)

    if not args.skip_board_export:
        _run(
            [
                sys.executable,
                str(SCRIPT_DIR / "export_board_model.py"),
                "--pcb",
                str(pcb_path),
            ]
        )

    query_cmd = [
        sys.executable,
        str(SCRIPT_DIR / "query_region.py"),
        f"--{'rect' if mode == 'rect' else 'polygon'}",
        coords,
        "--pcb",
        str(pcb_path),
        "--out",
        str(region_out),
    ]
    if args.layers:
        query_cmd.extend(["--layers", args.layers])
    if args.nets_only:
        query_cmd.append("--nets-only")
    _run(query_cmd)

    if args.si or args.si_full_wave:
        _run_si_checks(region_out, pcb_path, full_wave=args.si_full_wave)

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pcb_file": str(pcb_path),
        "pcb_resolution": pcb_reason,
        "region_input": args.region,
        "region_mode": mode,
        "layers_filter": args.layers or None,
        "board_model": str(ANALYSIS_DIR / "board_model.json"),
        "region_report": str(region_out),
        "note": "User should Save in KiCad before running. Reports are read-only.",
    }
    manifest_out.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    # Also write latest pointers for agent convenience
    (ANALYSIS_DIR / "latest_session.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (ANALYSIS_DIR / "latest_region.json").write_text(
        region_out.read_text(encoding="utf-8"), encoding="utf-8"
    )

    print(f"\nSession manifest: {manifest_out}")
    print(f"Latest pointers: analysis/latest_session.json, analysis/latest_region.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
