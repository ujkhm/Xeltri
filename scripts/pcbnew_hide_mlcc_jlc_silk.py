# KiCad 9 PCB Editor → Tools → Scripting Console
# Hide the fields added by the MLCC LCSC assignment (they landed on silk).
# Does not delete LCSC Part / Value text — BOM still has them, they just
# will not print on F.SilkS.
#
#   exec(open(r"E:\Xeltri\scripts\pcbnew_hide_mlcc_jlc_silk.py", encoding="utf-8").read())

from __future__ import annotations

import json
from pathlib import Path

import pcbnew

ASSIGN = Path(r"E:\Xeltri\analysis\mlcc_jlc_assignment.json")

# Keep Reference silk (C1, C2, …). Hide everything else that the JLC
# assignment / Update-from-schematic typically dumps onto silk.
HIDE_NAMES = {
    "LCSC Part",
    "LCSC",
    "Value",
    "Datasheet",
    "Description",
    "Manufacturer",
    "MPN",
    "Mfr. #",
    "Mfr Part",
}


def _hide_fab(fld) -> None:
    try:
        fld.SetVisible(False)
    except Exception:
        try:
            fld.SetHidden(True)
        except Exception:
            pass
    try:
        fld.SetLayer(pcbnew.F_Fab)
    except Exception:
        pass


def hide_mlcc_jlc_silk() -> None:
    data = json.loads(ASSIGN.read_text(encoding="utf-8"))
    refs = {a["reference"] for a in data.get("assignments") or []}
    board = pcbnew.GetBoard()
    n_fp = 0
    n_fld = 0
    for fp in board.GetFootprints():
        if fp.GetReference() not in refs:
            continue
        n_fp += 1
        fields = []
        getter = getattr(fp, "GetFields", None)
        if callable(getter):
            fields = list(getter())
        else:
            for name in HIDE_NAMES:
                g = getattr(fp, "GetFieldByName", None)
                if callable(g):
                    fld = g(name)
                    if fld is not None:
                        fields.append(fld)
        for fld in fields:
            try:
                name = fld.GetName()
            except Exception:
                continue
            if name == "Reference":
                continue
            if name in HIDE_NAMES:
                _hide_fab(fld)
                n_fld += 1
                continue
            try:
                layer = fld.GetLayer()
            except Exception:
                continue
            if layer in (pcbnew.F_SilkS, pcbnew.B_SilkS) and name != "Reference":
                _hide_fab(fld)
                n_fld += 1
    pcbnew.Refresh()
    print(f"Hid {n_fld} fields on {n_fp} assigned MLCCs (kept Reference silk).")
    print("Save the board (Ctrl+S).")


hide_mlcc_jlc_silk()
