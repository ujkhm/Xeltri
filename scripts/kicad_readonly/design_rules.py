"""Read KiCad board/netclass clearances from the sibling .kicad_pro (read-only).

Used to decide whether a BOOT/SW capacitor is already against the DRC floor
instead of treating every guideline-distance miss as a free layout change.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


def load_clearance_mm(pcb_path: Path) -> Dict[str, Any]:
    """Default netclass clearance and board min_clearance, millimetres."""
    pcb_path = Path(pcb_path)
    pro = pcb_path.with_suffix(".kicad_pro")
    result: Dict[str, Any] = {
        "source": "fallback",
        "netclass_default_mm": 0.2,
        "board_min_mm": 0.09,
        "project_file": str(pro) if pro.is_file() else None,
    }
    if not pro.is_file():
        return result
    try:
        data = json.loads(pro.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        result["source"] = "kicad_pro_unreadable"
        return result

    rules = (
        data.get("board", {})
        .get("design_settings", {})
        .get("rules", {})
    )
    board_min = rules.get("min_clearance")
    if isinstance(board_min, (int, float)):
        result["board_min_mm"] = float(board_min)

    classes = data.get("net_settings", {}).get("classes", []) or []
    default: Optional[float] = None
    for cls in classes:
        if str(cls.get("name", "")).lower() == "default":
            c = cls.get("clearance")
            if isinstance(c, (int, float)):
                default = float(c)
            break
    if default is None and classes:
        c = classes[0].get("clearance")
        if isinstance(c, (int, float)):
            default = float(c)
    if default is not None:
        result["netclass_default_mm"] = default
    result["source"] = "kicad_pro"
    return result
