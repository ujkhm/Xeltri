"""Build a lightweight net-name index from KiCad schematic files (read-only)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set


def index_schematic_nets(sch_dir: Path) -> Dict[str, List[str]]:
    """
    Map net label / power symbol names to schematic sheets where they appear.
    Not a full connectivity model — use for cross-reference hints only.
    """
    index: Dict[str, Set[str]] = {}
    sch_files = sorted(sch_dir.glob("*.kicad_sch"))

    label_re = re.compile(r'\(label\s+"([^"]+)"')
    global_re = re.compile(r'\(global_label\s+"([^"]+)"')
    power_re = re.compile(r'\(symbol\s+"[^"]*power:[^"]*"\s.*?\(property\s+"Value"\s+"([^"]+)"', re.DOTALL)
    # Simpler: power value on same line block
    power_value_re = re.compile(
        r'\(symbol\s+"[^"]*power:[^"]*".*?\(property\s+"Value"\s+"([^"]+)"',
        re.DOTALL,
    )

    for sch in sch_files:
        text = sch.read_text(encoding="utf-8")
        sheet = sch.name
        for pattern in (label_re, global_re):
            for m in pattern.finditer(text):
                index.setdefault(m.group(1), set()).add(sheet)
        for m in power_value_re.finditer(text):
            index.setdefault(m.group(1), set()).add(sheet)

    return {k: sorted(v) for k, v in sorted(index.items())}
