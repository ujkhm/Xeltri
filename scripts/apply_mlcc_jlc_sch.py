#!/usr/bin/env python3
"""Patch KiCad 9 schematic instances: Value + LCSC Part.

Default is dry-run. You run this (I will not write KiCad sources).

  python scripts/apply_mlcc_jlc_sch.py
  python scripts/apply_mlcc_jlc_sch.py --apply

Close the schematic in KiCad first, or File → Revert after running.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSIGN_PATH = REPO_ROOT / "analysis" / "mlcc_jlc_assignment.json"
SCH_DIR = REPO_ROOT / "KiCad" / "Xeltri"
BACKUP_DIR = REPO_ROOT / "analysis" / "backup_sch_mlcc"


def _sexpr_block_end(text: str, start: int) -> int:
    """Index after the matching close paren, ignoring parens in quotes."""
    i = start
    depth = 0
    in_str = False
    while i < len(text):
        ch = text[i]
        if in_str:
            if ch == "\\" and i + 1 < len(text):
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise ValueError("unbalanced s-expression")


def _find_instance_block(text: str, ref: str) -> Optional[Tuple[int, int]]:
    needle = f'(property "Reference" "{ref}"'
    idx = 0
    while True:
        i = text.find(needle, idx)
        if i < 0:
            return None
        start = text.rfind("(symbol", 0, i)
        if start < 0:
            idx = i + 1
            continue
        window = text[start:i]
        if '(lib_id "' not in window:
            idx = i + 1
            continue
        end = _sexpr_block_end(text, start)
        return start, end


def _replace_prop(block: str, name: str, value: str) -> Tuple[str, bool]:
    pat = re.compile(
        rf'(\(property "{re.escape(name)}" )"([^"]*)"',
        re.MULTILINE,
    )
    m = pat.search(block)
    if not m:
        return block, False
    return pat.sub(lambda mm: mm.group(1) + f'"{value}"', block, count=1), True


def _insert_lcsc(block: str, lcsc: str) -> str:
    if '(property "LCSC Part"' in block:
        block, _ = _replace_prop(block, "LCSC Part", lcsc)
        return block
    insert = (
        '\t\t(property "LCSC Part" "'
        + lcsc
        + '"\n'
        + "\t\t\t(at 0 0 0)\n"
        + "\t\t\t(effects\n"
        + "\t\t\t\t(font\n"
        + "\t\t\t\t\t(size 1.27 1.27)\n"
        + "\t\t\t\t)\n"
        + "\t\t\t\t(hide yes)\n"
        + "\t\t\t)\n"
        + "\t\t)\n"
    )
    for anchor in ('(property "Description"', '(property "Datasheet"', '(property "Value"'):
        pos = block.find(anchor)
        if pos < 0:
            continue
        end = _sexpr_block_end(block, pos)
        return block[:end] + "\n" + insert + block[end:]
    return block


def _patch_file(text: str, by_ref: Dict[str, Dict]) -> Tuple[str, List[str]]:
    changed: List[str] = []
    for ref, row in sorted(by_ref.items(), key=lambda x: x[0]):
        loc = _find_instance_block(text, ref)
        if not loc:
            continue
        start, end = loc
        block = text[start:end]
        new_block, ok = _replace_prop(block, "Value", row["value"])
        if not ok:
            continue
        new_block = _insert_lcsc(new_block, row["lcsc"])
        if row.get("datasheet"):
            new_block, ds_ok = _replace_prop(new_block, "Datasheet", row["datasheet"])
            if not ds_ok:
                pass
        if not row.get("keep_footprint", True) and row.get("footprint"):
            new_block, fp_ok = _replace_prop(new_block, "Footprint", row["footprint"])
            if not fp_ok:
                print(f"  no Footprint field on {ref}")
        if new_block != block:
            text = text[:start] + new_block + text[end:]
            changed.append(ref)
    return text, changed


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch schematic LCSC Part / Value")
    parser.add_argument("--apply", action="store_true", help="Write .kicad_sch files")
    parser.add_argument("--assignment", type=Path, default=ASSIGN_PATH)
    args = parser.parse_args()

    data = json.loads(args.assignment.read_text(encoding="utf-8"))
    by_ref = {a["reference"]: a for a in data.get("assignments") or []}
    sch_files = sorted(
        p
        for p in SCH_DIR.glob("*.kicad_sch")
        if not p.name.startswith("_autosave") and "pretty" not in p.parts
    )

    all_changed: Dict[str, List[str]] = {}
    missing = set(by_ref)
    for path in sch_files:
        text = path.read_text(encoding="utf-8")
        new_text, refs = _patch_file(text, by_ref)
        if not refs:
            continue
        all_changed[str(path)] = refs
        missing -= set(refs)
        if args.apply:
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            bak = BACKUP_DIR / path.name
            if not bak.exists():
                shutil.copy2(path, bak)
            path.write_text(new_text, encoding="utf-8", newline="\n")
            print(f"Wrote {path} ({len(refs)} symbols)")
        else:
            print(f"[dry-run] {path.name}: {', '.join(refs)}")

    if missing:
        print("Not found in schematic:", ", ".join(sorted(missing)))
    if not args.apply:
        print("Dry-run only. Re-run with --apply after closing the schematic in KiCad.")
        print(f"Backups will go to {BACKUP_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
