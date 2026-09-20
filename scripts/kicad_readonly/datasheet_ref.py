"""Generic datasheet/layout-guideline cross-reference lookup.

This is deliberately NOT hardcoded to one part number. `data/datasheet_refs.json`
holds one "profile" per IC family; new components get a new profile without any
code change. `match_profile()` is called for every footprint the toolchain
inspects (any region, any IC), so future analyses automatically pick up new
entries as they're added.

All facts here are manually curated from public datasheets/app notes and are
therefore `ai_assisted` provenance, not toolchain measurement — the numeric
`checks_against` values ARE toolchain measurements and get merged in by
design_judgments.py.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

DATA_PATH = Path(__file__).resolve().parent / "data" / "datasheet_refs.json"


@lru_cache(maxsize=1)
def _load_profiles() -> List[Dict[str, Any]]:
    if not DATA_PATH.is_file():
        return []
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return data.get("profiles", [])


def match_profile(value: str, footprint_name: str = "") -> Optional[Dict[str, Any]]:
    """Return the first profile whose regex matches footprint Value or name."""
    haystacks = [value or "", footprint_name or ""]
    for profile in _load_profiles():
        pattern = profile.get("match", "")
        if not pattern:
            continue
        try:
            rx = re.compile(pattern, re.IGNORECASE)
        except re.error:
            continue
        if any(rx.search(h) for h in haystacks):
            return profile
    return None


def _get_nested(d: Dict[str, Any], dotted_key: str) -> Any:
    cur: Any = d
    for part in dotted_key.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def evaluate_fact(fact: Dict[str, Any], toolchain_values: Dict[str, Any]) -> Dict[str, Any]:
    """Compare one datasheet fact's threshold against toolchain-measured value(s)."""
    checks = fact.get("checks_against", [])
    measured: Dict[str, Any] = {}
    for key in checks:
        measured[key] = _get_nested(toolchain_values, key)

    status = "info"
    reason = "No numeric threshold to auto-check; qualitative guidance only."
    primary_val = next((v for v in measured.values() if isinstance(v, (int, float))), None)

    if primary_val is None and fact.get("status_field"):
        fallback_status = toolchain_values.get(fact["status_field"])
        fallback_notes = toolchain_values.get(fact["status_field"].replace("_status", "_notes"))
        if fallback_status:
            return {
                "fact_id": fact.get("id"),
                "topic": fact.get("topic"),
                "guideline": fact.get("guideline"),
                "section": fact.get("section"),
                "measured": measured,
                "status": fallback_status,
                "reason": fallback_notes or f"toolchain status: {fallback_status} (no numeric value found)",
            }

    if primary_val is not None:
        if "recommend_max_mm" in fact:
            thr = fact["recommend_max_mm"]
            status = "pass" if primary_val <= thr else "warn"
            reason = f"{primary_val:.2f} mm vs guideline max {thr} mm"
        elif "recommend_min_mm" in fact:
            thr = fact["recommend_min_mm"]
            status = "pass" if primary_val >= thr else "warn"
            reason = f"{primary_val:.2f} mm vs guideline min {thr} mm"
        elif "recommend_min_count" in fact:
            thr = fact["recommend_min_count"]
            status = "pass" if primary_val >= thr else "warn"
            reason = f"{primary_val:.0f} vs guideline min {thr}"
        elif "recommend_min_uf" in fact:
            thr = fact["recommend_min_uf"]
            if primary_val + 1e-9 >= thr:
                status = "pass"
            elif primary_val >= 0.7 * thr:
                status = "warn"
            else:
                status = "fail"
            reason = f"{primary_val:.2f} uF effective vs guideline min {thr} uF"

    limited_field = fact.get("package_limited_field")
    if limited_field and toolchain_values.get(limited_field):
        st = toolchain_values.get(fact.get("status_field") or "")
        if st:
            status = st
            reason = f"{reason}; {limited_field}=true (do not apply pin-escape vs {fact.get('recommend_min_mm', 'guideline')} mm)"

    return {
        "fact_id": fact.get("id"),
        "topic": fact.get("topic"),
        "guideline": fact.get("guideline"),
        "section": fact.get("section"),
        "measured": measured,
        "status": status,
        "reason": reason,
    }


def crossref_footprint(
    value: str,
    footprint_name: str,
    toolchain_values: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Full datasheet crossref bundle for one footprint, or None if no profile matches."""
    profile = match_profile(value, footprint_name)
    if not profile:
        return None
    evaluations = [evaluate_fact(f, toolchain_values) for f in profile.get("facts", [])]
    return {
        "profile_id": profile.get("id"),
        "display_name": profile.get("display_name"),
        "doc": profile.get("doc"),
        "verified": profile.get("verified", False),
        "evaluations": evaluations,
    }


def known_profile_ids() -> List[str]:
    return [p.get("id", "") for p in _load_profiles()]
