# KiCad 9 PCB Editor → Tools → Scripting Console
# With Xeltri.kicad_pcb open, run:
#   exec(open(r"E:\Xeltri\scripts\pcbnew_apply_mlcc_jlc.py", encoding="utf-8").read())
#
# Sets footprint Value and the "LCSC Part" field from analysis/mlcc_jlc_assignment.json.
# Does not change footprints / packages. Save the board afterwards.

from __future__ import annotations

import json
from pathlib import Path

import pcbnew

ASSIGN = Path(r"E:\Xeltri\analysis\mlcc_jlc_assignment.json")


def _set_field(fp, name: str, text: str) -> None:
    fld = None
    getter = getattr(fp, "GetFieldByName", None)
    if callable(getter):
        fld = getter(name)
    if fld is None:
        setter = getattr(fp, "SetField", None)
        if callable(setter):
            try:
                setter(name, text)
            except Exception:
                pass
            if callable(getter):
                fld = getter(name)
    if fld is None:
        try:
            fld = pcbnew.PCB_FIELD(fp, fp.GetFieldCount(), name)
            fld.SetName(name)
            fp.AddField(fld)
        except Exception as exc:
            print("  field fail", fp.GetReference(), name, exc)
            return
    fld.SetText(text)
    try:
        fld.SetVisible(False)
    except Exception:
        pass
    try:
        fld.SetLayer(pcbnew.F_Fab)
    except Exception:
        pass


def apply_mlcc_jlc() -> None:
    data = json.loads(ASSIGN.read_text(encoding="utf-8"))
    by_ref = {a["reference"]: a for a in data.get("assignments") or []}
    board = pcbnew.GetBoard()
    n = 0
    missing = []
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        row = by_ref.get(ref)
        if not row:
            continue
        fp.SetValue(row["value"])
        try:
            val = fp.GetFieldByName("Value")
            if val is not None:
                val.SetVisible(False)
                val.SetLayer(pcbnew.F_Fab)
        except Exception:
            pass
        _set_field(fp, "LCSC Part", row["lcsc"])
        if row.get("datasheet"):
            try:
                ds = fp.GetFieldByName("Datasheet")
                if ds is not None:
                    ds.SetText(row["datasheet"])
            except Exception:
                pass
        n += 1
    seen = {fp.GetReference() for fp in board.GetFootprints()}
    missing = sorted(set(by_ref) - seen)
    pcbnew.Refresh()
    print(f"Updated {n} footprints (Value + LCSC Part)")
    if missing:
        print("Not on this PCB:", ", ".join(missing))
    print("Save the board (Ctrl+S). Then run Update Schematic from PCB if you want fields copied back.")


apply_mlcc_jlc()
