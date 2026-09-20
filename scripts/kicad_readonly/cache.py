"""PCB parse cache keyed by file mtime + size."""

from __future__ import annotations

import hashlib
import pickle
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .pcb import PcbModel

CACHE_VERSION = 6
DEFAULT_CACHE_DIR = Path(__file__).resolve().parents[2] / "analysis" / ".cache"


def _file_fingerprint(path: Path) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|v{CACHE_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def cache_path_for(pcb_path: Path, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"pcb_{_file_fingerprint(pcb_path)}.pkl"


def load_cached_pcb(pcb_path: Path, cache_dir: Path = DEFAULT_CACHE_DIR) -> Optional["PcbModel"]:
    path = cache_path_for(pcb_path, cache_dir)
    if not path.is_file():
        return None
    if pcb_path.stat().st_mtime_ns > path.stat().st_mtime_ns:
        return None
    try:
        with path.open("rb") as fh:
            payload = pickle.load(fh)
        if payload.get("version") != CACHE_VERSION:
            return None
        return payload["model"]
    except Exception:
        return None


def save_cached_pcb(model: "PcbModel", pcb_path: Path, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    path = cache_path_for(pcb_path, cache_dir)
    with path.open("wb") as fh:
        pickle.dump({"version": CACHE_VERSION, "model": model}, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return path
