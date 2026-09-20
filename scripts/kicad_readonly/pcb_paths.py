"""Resolve which .kicad_pcb file the toolchain should read.

KiCad portable editions often open/save under %LOCALAPPDATA%\\Temp\\PortableDev\\
(or PortableDir) while the git repo lives on E:\\Xeltri. Auto-pick the newer copy
unless the caller passes an explicit --pcb path or sets XELTRI_PCB_PATH.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PCB = REPO_ROOT / "KiCad" / "Xeltri" / "Xeltri.kicad_pcb"
ACTIVE_PCB_HINT = REPO_ROOT / "analysis" / ".active_pcb_path"


def _temp_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Temp"


def portable_pcb_candidates() -> List[Path]:
    """Known KiCad portable work-tree locations for this project."""
    roots = (_temp_root() / "PortableDev", _temp_root() / "PortableDir")
    rel = Path("work") / "Xeltri" / "KiCad" / "Xeltri" / "Xeltri.kicad_pcb"
    found: List[Path] = []
    for root in roots:
        p = root / rel
        if p.is_file():
            found.append(p.resolve())
    for p in _temp_root().glob("Portable*/work/Xeltri/KiCad/Xeltri/Xeltri.kicad_pcb"):
        rp = p.resolve()
        if rp.is_file() and rp not in found:
            found.append(rp)
    return found


def _read_hint_file() -> Optional[Path]:
    if not ACTIVE_PCB_HINT.is_file():
        return None
    raw = ACTIVE_PCB_HINT.read_text(encoding="utf-8").strip().splitlines()
    if not raw:
        return None
    p = Path(raw[0].strip())
    return p if p.is_file() else None


def resolve_pcb_path(explicit: Optional[Path] = None) -> Tuple[Path, str]:
    """
    Return (pcb_path, resolution_reason).

    Priority:
      1. explicit path (CLI --pcb)
      2. XELTRI_PCB_PATH environment variable
      3. analysis/.active_pcb_path one-line hint
      4. newest KiCad portable copy if newer than repo default
      5. repo default KiCad/Xeltri/Xeltri.kicad_pcb
    """
    if explicit is not None:
        p = explicit.expanduser().resolve()
        if not p.is_file():
            raise FileNotFoundError(f"PCB not found: {p}")
        return p, "explicit --pcb"

    env = os.environ.get("XELTRI_PCB_PATH", "").strip()
    if env:
        p = Path(env).expanduser().resolve()
        if p.is_file():
            return p, "XELTRI_PCB_PATH"
        raise FileNotFoundError(f"XELTRI_PCB_PATH not found: {p}")

    hinted = _read_hint_file()
    if hinted is not None:
        return hinted.resolve(), "analysis/.active_pcb_path"

    repo = DEFAULT_PCB.resolve()
    portable = portable_pcb_candidates()
    if portable and repo.is_file():
        newest_portable = max(portable, key=lambda p: p.stat().st_mtime_ns)
        if newest_portable.stat().st_mtime_ns > repo.stat().st_mtime_ns:
            return (
                newest_portable,
                f"portable KiCad copy newer than repo ({newest_portable.parent.parent.parent.parent.name})",
            )
    elif portable and not repo.is_file():
        newest_portable = max(portable, key=lambda p: p.stat().st_mtime_ns)
        return newest_portable, "repo PCB missing; using portable KiCad copy"

    if not repo.is_file():
        raise FileNotFoundError(f"Default PCB not found: {repo}")
    return repo, "repo default"


def _mtime_str(path: Path) -> str:
    if not path.is_file():
        return "missing"
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")


def format_pcb_source_banner(path: Path, reason: str) -> str:
    portable_root = _temp_root()
    is_portable = portable_root in path.parents
    tag = " [PORTABLE]" if is_portable else ""
    lines = [
        f"PCB source{tag}: {path}",
        f"  ({reason})",
        f"  this copy mtime: {_mtime_str(path)}",
        f"  git repo mtime:  {_mtime_str(DEFAULT_PCB)}  ({DEFAULT_PCB})",
    ]
    if is_portable:
        lines.append(
            "  GIT: this is KiCad portable Temp — Ctrl+S here does NOT update E:\\Xeltri. "
            "Copy before commit: powershell -File scripts/sync_portable_kicad_to_repo.ps1"
        )
    return "\n".join(lines)
