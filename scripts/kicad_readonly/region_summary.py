"""Structured pass/fail summary for region reports and chat replies."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

STATUS_RANK = {"fail": 0, "warn": 1, "pass": 2, "info": 3}

VERDICT_ZH = {
    "pass": "合格",
    "warn": "待改善",
    "fail": "不合格",
}


def _sw_nets_in_region(region_result: Dict[str, Any]) -> List[str]:
    return sorted(
        {
            n["net_name"]
            for n in region_result.get("nets_summary", [])
            if "SW" in n.get("net_name", "") and n.get("length_in_region_mm", 0) > 0
        }
    )


def _worst_return_path_status(region_result: Dict[str, Any]) -> str:
    checks = region_result.get("return_path_checks", [])
    if not checks:
        return "pass"
    return min(checks, key=lambda c: STATUS_RANK.get(c.get("status", "pass"), 9))[
        "status"
    ]


def _worst_power_layout_status(region_result: Dict[str, Any]) -> str:
    checks = region_result.get("power_layout_checks", [])
    if not checks:
        return "pass"
    return min(checks, key=lambda c: STATUS_RANK.get(c.get("overall_status", "pass"), 9))[
        "overall_status"
    ]


def _worst_mlcc_status(region_result: Dict[str, Any]) -> str:
    mlcc = region_result.get("mlcc_dc_bias") or {}
    banks = mlcc.get("banks") or []
    if not banks:
        return "pass"
    return min(banks, key=lambda b: STATUS_RANK.get(b.get("status", "info"), 9)).get(
        "status", "pass"
    )


def _overall_verdict(region_result: Dict[str, Any]) -> str:
    for st in (
        _worst_return_path_status(region_result),
        _worst_power_layout_status(region_result),
        _worst_mlcc_status(region_result),
    ):
        if st == "fail":
            return "fail"
    for st in (
        _worst_return_path_status(region_result),
        _worst_power_layout_status(region_result),
        _worst_mlcc_status(region_result),
    ):
        if st == "warn":
            return "warn"
    for j in region_result.get("ai_assisted_judgments", []):
        if j.get("category") == "return_path" and "need review" in j.get(
            "conclusion", ""
        ):
            return "warn"
    return "pass"


def build_conversation_summary(region_result: Dict[str, Any]) -> Dict[str, Any]:
    """Build structured summary for JSON export and chat formatting."""
    bbox = region_result.get("bbox_mm", {})
    bbox_str = (
        f"({bbox.get('min_x')}, {bbox.get('min_y')})-"
        f"({bbox.get('max_x')}, {bbox.get('max_y')})"
        if bbox
        else "unknown"
    )

    toolchain_checks: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    needs_strengthening: List[Dict[str, Any]] = []
    recommended_actions: List[Dict[str, Any]] = []
    follow_up: List[Dict[str, Any]] = []

    for rp in region_result.get("return_path_checks", []):
        entry = {
            "source": "toolchain",
            "category": "return_path",
            "item": rp.get("net_name", ""),
            "status": rp.get("status", "unknown"),
            "detail": (
                f"weighted {rp.get('coverage_pct', 0):.0f}% via "
                f"{rp.get('dominant_reference_net', 'GND')}; "
                f"{rp.get('routing_pattern', '')}"
            ),
        }
        toolchain_checks.append(entry)
        if rp.get("status") == "fail":
            failed.append(
                {
                    "source": "toolchain",
                    "item": rp.get("net_name"),
                    "reason": rp.get("notes", "Primary reference plane gap"),
                    "where": (
                        f"layers {rp.get('layers_used')}, "
                        f"refs {rp.get('primary_ref_planes_used')}"
                    ),
                }
            )
            recommended_actions.append(
                {
                    "source": "toolchain",
                    "priority": "high",
                    "for_item": rp.get("net_name"),
                    "action": (
                        "Extend GND/GNDA pour or reroute so the signal runs over "
                        "a continuous reference plane on the stackup-adjacent inner layer."
                    ),
                }
            )
        elif rp.get("status") == "warn":
            needs_strengthening.append(
                {
                    "source": "toolchain",
                    "item": rp.get("net_name"),
                    "reason": rp.get("notes", "Partial reference coverage"),
                }
            )

    bridges = region_result.get("split_ground_bridges", [])
    fb_uses_gnda = any(
        rp.get("dominant_reference_net") in ("GNDA", "AGND")
        for rp in region_result.get("return_path_checks", [])
        if "FB" in rp.get("net_name", "")
    )
    toolchain_checks.append(
        {
            "source": "toolchain",
            "category": "split_ground",
            "item": "GND<->GNDA bridges",
            "status": "pass" if bridges else ("warn" if fb_uses_gnda else "info"),
            "detail": ", ".join(b["reference"] for b in bridges) or "none detected",
        }
    )
    if fb_uses_gnda and not bridges:
        needs_strengthening.append(
            {
                "source": "toolchain",
                "item": "split_ground_bridge",
                "reason": "FB references GNDA but no GND<->GNDA bridge found on board",
            }
        )
        recommended_actions.append(
            {
                "source": "toolchain",
                "priority": "high",
                "for_item": "GNDA return path",
                "action": (
                    "Add a single-point GNDA-GND tie (e.g. 0 ohm) per schematic "
                    "near the analog power section."
                ),
            }
        )

    if region_result.get("keepout_count", 0) > 0:
        toolchain_checks.append(
            {
                "source": "toolchain",
                "category": "keepout",
                "item": "footprint keepouts",
                "status": "info",
                "detail": f"{region_result['keepout_count']} in region",
            }
        )

    for pl in region_result.get("power_layout_checks", []):
        ic = pl.get("ic_reference", "?")
        sw_fb = pl.get("sw_fb_min_distance_mm")
        sw_cu_ok = pl.get("sw_copper_detected", False)
        toolchain_checks.append(
            {
                "source": "toolchain",
                "category": "power_layout",
                "item": f"{ic} SW copper",
                "status": "pass" if sw_cu_ok else "warn",
                "detail": pl.get(
                    "sw_copper_note",
                    f"source={pl.get('sw_copper_source', 'unknown')}",
                ),
            }
        )
        toolchain_checks.append(
            {
                "source": "toolchain",
                "category": "power_layout",
                "item": f"{ic} SW-FB spacing",
                "status": pl.get("sw_fb_status", "info"),
                "detail": pl.get("sw_fb_notes", ""),
            }
        )
        shield = pl.get("sw_fb_shield") or {}
        if shield:
            los = shield.get("same_layer_blocks_line_of_sight")
            toolchain_checks.append(
                {
                    "source": "toolchain",
                    "category": "power_layout",
                    "item": f"{ic} SW-FB coplanar guard",
                    "status": "pass" if los else "info",
                    "detail": shield.get("notes", ""),
                }
            )
        cap = pl.get("nearest_vin_cap") or {}
        toolchain_checks.append(
            {
                "source": "toolchain",
                "category": "power_layout",
                "item": f"{ic} VIN cap proximity",
                "status": pl.get("vin_cap_status", "info"),
                "detail": (
                    f"{cap.get('reference', '?')} {cap.get('value', '')} "
                    f"@ {cap.get('distance_mm', '?')} mm; "
                    f"{pl.get('vin_cap_notes', '')}"
                ),
            }
        )
        if "boot_cap_status" in pl:
            boot_cap = pl.get("boot_cap") or {}
            toolchain_checks.append(
                {
                    "source": "toolchain",
                    "category": "power_layout",
                    "item": f"{ic} BOOT cap distance",
                    "status": pl.get("boot_cap_status", "info"),
                    "detail": (
                        f"{boot_cap.get('reference', '?')} {boot_cap.get('value', '')} "
                        f"@ {pl.get('boot_cap_distance_mm', '?')} mm"
                        + (
                            f"; 0Ω {', '.join(boot_cap.get('series_zero_ohm') or [])}"
                            if boot_cap.get("series_zero_ohm")
                            else ""
                        )
                        + f"; {pl.get('boot_cap_notes', '')}"
                    ),
                }
            )
        if "sw_length_status" in pl:
            toolchain_checks.append(
                {
                    "source": "toolchain",
                    "category": "power_layout",
                    "item": f"{ic} SW copper length",
                    "status": pl.get("sw_length_status", "info"),
                    "detail": (
                        f"{pl.get('sw_length_used_mm', '?')} mm; "
                        f"{pl.get('sw_length_notes', '')}"
                    ),
                }
            )
        if "thermal_via_status" in pl:
            toolchain_checks.append(
                {
                    "source": "toolchain",
                    "category": "power_layout",
                    "item": f"{ic} GND via near IC",
                    "status": pl.get("thermal_via_status", "info"),
                    "detail": pl.get("thermal_via_notes", ""),
                }
            )
        if pl.get("boot_cap_status") == "warn":
            needs_strengthening.append(
                {
                    "source": "toolchain",
                    "item": f"{ic} BOOT cap",
                    "reason": pl.get("boot_cap_notes", ""),
                }
            )
            if pl.get("boot_cap"):
                if pl.get("boot_package_limited") or pl.get("boot_drc_limited"):
                    recommended_actions.append(
                        {
                            "source": "toolchain",
                            "priority": "low",
                            "for_item": f"{ic} BOOT capacitor",
                            "action": (
                                "Do not shrink further: cap is already against the SW node "
                                "and/or on the opposite side of the IC from the BOOT pin. "
                                + (pl.get("boot_drc_notes") or "")
                            ),
                        }
                    )
                else:
                    recommended_actions.append(
                        {
                            "source": "toolchain",
                            "priority": "medium",
                            "for_item": f"{ic} BOOT capacitor",
                            "action": (
                                "Circuit path is connected"
                                + (
                                    f" (series 0Ω {', '.join((pl.get('boot_cap') or {}).get('series_zero_ohm') or [])})"
                                    if (pl.get("boot_cap") or {}).get("series_zero_ohm")
                                    else ""
                                )
                                + "; optionally move the capacitor closer to IC BOOT/SW pins "
                                "to shrink the bootstrap loop (distance above pass threshold)"
                                + (
                                    f". {pl.get('boot_drc_notes')}"
                                    if pl.get("boot_drc_notes")
                                    else "."
                                )
                            ),
                        }
                    )
        if pl.get("sw_length_status") == "warn":
            needs_strengthening.append(
                {
                    "source": "toolchain",
                    "item": f"{ic} SW copper length",
                    "reason": pl.get("sw_length_notes", ""),
                }
            )
            recommended_actions.append(
                {
                    "source": "toolchain",
                    "priority": "medium",
                    "for_item": f"{ic} SW node",
                    "action": (
                        "Shorten/shrink the SW-node copper between IC and inductor; "
                        "keep it just wide enough for current, not a large flood."
                    ),
                }
            )
        if pl.get("thermal_via_status") == "warn":
            needs_strengthening.append(
                {
                    "source": "toolchain",
                    "item": f"{ic} thermal/GND vias",
                    "reason": pl.get("thermal_via_notes", ""),
                }
            )
        if pl.get("sw_fb_status") == "fail":
            failed.append(
                {
                    "source": "toolchain",
                    "item": f"{ic} SW-FB",
                    "reason": pl.get("sw_fb_notes", ""),
                    "where": f"{pl.get('sw_net')} vs {pl.get('fb_net')}",
                }
            )
            recommended_actions.append(
                {
                    "source": "toolchain",
                    "priority": "high",
                    "for_item": f"{ic} SW/FB",
                    "action": (
                        "Increase clearance between SW and FB traces; "
                        "route FB away from switch node and inductor."
                    ),
                }
            )
        elif pl.get("sw_fb_status") == "warn":
            if pl.get("sw_fb_package_limited"):
                recommended_actions.append(
                    {
                        "source": "toolchain",
                        "priority": "low",
                        "for_item": f"{ic} SW/FB",
                        "action": (
                            "Do not try to beat IC pin pitch. The 2.0 mm guideline "
                            "applies to FB vs the inductor/SW island, not the QFN pads."
                        ),
                    }
                )
            else:
                shield = pl.get("sw_fb_shield") or {}
                reason = pl.get("sw_fb_notes", "")
                if shield.get("same_layer_blocks_line_of_sight"):
                    reason = (
                        reason
                        + " — same-layer guard present on the shortest path; SI uses "
                        f"coplanar-guard reduction (factor {shield.get('coupling_reduction_factor')}). "
                        "Do not treat this as unshielded coupling."
                    )
                elif (shield.get("pour_path") or {}).get("same_layer_blocks_line_of_sight"):
                    reason = (
                        reason
                        + " — GNDA/AGND guards the SW-pour–FB gap, not the shorter "
                        "inductor-pad–FB edge. Pass threshold is still copper-to-copper "
                        "(unchanged by the guard)."
                    )
                needs_strengthening.append(
                    {
                        "source": "toolchain",
                        "item": f"{ic} SW-FB",
                        "reason": reason,
                    }
                )
        if pl.get("vin_cap_status") == "fail":
            failed.append(
                {
                    "source": "toolchain",
                    "item": f"{ic} VIN input cap",
                    "reason": pl.get("vin_cap_notes", ""),
                    "where": f"nearest {cap.get('reference', '?')}",
                }
            )
            recommended_actions.append(
                {
                    "source": "toolchain",
                    "priority": "high",
                    "for_item": f"{ic} input capacitor",
                    "action": (
                        "Move bulk/hf input capacitors closer to IC VIN and GND pins."
                    ),
                }
            )
        elif pl.get("vin_cap_status") == "warn":
            needs_strengthening.append(
                {
                    "source": "toolchain",
                    "item": f"{ic} VIN cap",
                    "reason": pl.get("vin_cap_notes", ""),
                }
            )
        leftover = pl.get("leftover_copper") or {}
        n_res = leftover.get("residual_trace_count") or 0
        if n_res:
            toolchain_checks.append(
                {
                    "source": "toolchain",
                    "category": "power_layout",
                    "item": f"{ic} leftover SW/FB copper",
                    "status": "warn" if pl.get("leftover_would_have_driven_si") else "info",
                    "detail": leftover.get("notes", f"{n_res} leftover trace(s) excluded"),
                }
            )
            recommended_actions.append(
                {
                    "source": "toolchain",
                    "priority": "low",
                    "for_item": f"{ic} leftover copper",
                    "action": (
                        "In KiCad, delete leftover SW/FB tracks that sit inside pads or "
                        "the SW pour, and any sub-0.12 mm stubs. SI already ignores those "
                        "residuals; traces that stick out of the pour still count as real copper."
                    ),
                }
            )
            if pl.get("leftover_would_have_driven_si"):
                needs_strengthening.append(
                    {
                        "source": "toolchain",
                        "item": f"{ic} leftover copper vs SI",
                        "reason": (
                            "Pre-fix SI would have taken leftover centerline geometry; "
                            "current spacing uses pads + pour + functional traces only."
                        ),
                    }
                )

    mlcc = region_result.get("mlcc_dc_bias") or {}
    for bank in mlcc.get("banks") or []:
        ic = bank.get("ic_reference", "?")
        role = bank.get("role", "")
        item = f"{ic} {role} MLCC DC-bias"
        toolchain_checks.append(
            {
                "source": "toolchain",
                "category": "mlcc_dc_bias",
                "item": item,
                "status": bank.get("status", "info"),
                "detail": bank.get("notes", ""),
            }
        )
        st = bank.get("status")
        jlc = bank.get("jlc_suggestions") or []
        jlc_txt = (
            "; JLC "
            + ", ".join(
                f"{s.get('lcsc')} {s.get('c_nom_uf')}uF/{s.get('v_rated')}V "
                f"(eff {s.get('c_eff_at_vdc_uf')}uF)"
                for s in jlc[:3]
            )
            if jlc
            else ""
        )
        if st == "fail":
            failed.append(
                {
                    "source": "toolchain",
                    "item": item,
                    "reason": bank.get("notes", ""),
                    "where": bank.get("net", ""),
                }
            )
            recommended_actions.append(
                {
                    "source": "toolchain",
                    "priority": "high",
                    "for_item": item,
                    "action": (
                        "Replace same-package bulk MLCCs with higher-Vr / lower-CV "
                        "parts so derated C meets the datasheet min"
                        + (jlc_txt or " (no catalog hit; raise Vr or add another 0805).")
                    ),
                }
            )
        elif st == "warn":
            needs_strengthening.append(
                {
                    "source": "toolchain",
                    "item": item,
                    "reason": bank.get("notes", "") + jlc_txt,
                }
            )
            if jlc:
                recommended_actions.append(
                    {
                        "source": "toolchain",
                        "priority": "medium",
                        "for_item": item,
                        "action": "Consider JLC same-package upgrades" + jlc_txt,
                    }
                )

    datasheet_rows: List[Dict[str, Any]] = []
    circuit_rows: List[Dict[str, Any]] = []
    for j in region_result.get("ai_assisted_judgments", []):
        cat = j.get("category", "")
        conf = j.get("confidence", "medium")
        if cat == "power_design" and conf == "high":
            continue  # covered by overall pass
        if cat == "return_path" and j.get("confidence") != "high":
            needs_strengthening.append(
                {
                    "source": "ai_assisted",
                    "item": j.get("id", "return_path"),
                    "reason": j.get("conclusion", ""),
                }
            )
        if cat == "circuit_topology":
            circuit_rows.append(
                {
                    "id": j.get("id"),
                    "conclusion": j.get("conclusion", ""),
                    "confidence": conf,
                    "confidence_score": j.get("confidence_score"),
                }
            )
            if conf == "low" or (
                j.get("id", "").startswith("boot_bootstrap_path_")
                and "no BOOT" in j.get("conclusion", "")
            ):
                needs_strengthening.append(
                    {
                        "source": "ai_assisted",
                        "item": j.get("id"),
                        "reason": j.get("conclusion", ""),
                    }
                )
        if cat == "datasheet_crossref":
            datasheet_rows.append(
                {
                    "id": j.get("id"),
                    "conclusion": j.get("conclusion", ""),
                    "confidence": conf,
                    "confidence_score": j.get("confidence_score"),
                }
            )
            for ev in j.get("toolchain_evidence", {}).get("datasheet_evaluations", []):
                if ev.get("status") in ("warn", "fail"):
                    needs_strengthening.append(
                        {
                            "source": "ai_assisted",
                            "item": f"{j.get('id')}::{ev.get('fact_id')}",
                            "reason": f"{ev.get('topic')}: {ev.get('reason')}",
                        }
                    )

    follow_up.extend(
        [
            {
                "source": "ai_assisted",
                "topic": "validation",
                "suggestion": (
                    "Cross-check datasheet_crossref entries against the actual current-"
                    "revision PDF (profiles are manually curated, not yet human-verified); "
                    "run load/EMI test on bench."
                ),
            },
            {
                "source": "ai_assisted",
                "topic": "advanced",
                "suggestion": (
                    "Run SI estimate (analytical or openEMS, see si_checks) for critical "
                    "nets when toolchain warns; treat as pre-screen, not sign-off."
                ),
            },
        ]
    )

    verdict = _overall_verdict(region_result)
    buck_ai = [
        j
        for j in region_result.get("ai_assisted_judgments", [])
        if j.get("id") == "buck_region_qualification"
    ]
    headline = (
        buck_ai[0].get("conclusion", "")
        if buck_ai
        else f"Region return-path worst status: {_worst_return_path_status(region_result)}"
    )

    summary = {
        "bbox_mm": bbox_str,
        "region_area_mm2": region_result.get("region_area_mm2"),
        "overall_verdict": verdict,
        "overall_verdict_zh": VERDICT_ZH.get(verdict, verdict),
        "headline": headline,
        "toolchain_checks": toolchain_checks,
        "failed": failed,
        "needs_strengthening": needs_strengthening,
        "recommended_actions": recommended_actions,
        "follow_up_optimization": follow_up,
        "datasheet_crossref": datasheet_rows,
        "circuit_topology": circuit_rows,
        "counts": {
            "toolchain_checks": len(toolchain_checks),
            "failed": len(failed),
            "needs_strengthening": len(needs_strengthening),
            "recommended_actions": len(recommended_actions),
        },
    }
    summary["summary_tables"] = _build_summary_tables(summary, region_result)
    return summary


def _build_summary_tables(
    summary: Dict[str, Any], region_result: Dict[str, Any]
) -> Dict[str, Any]:
    """Markdown-ready table rows for chat replies."""
    verdict_row = [
        {
            "result": summary.get("overall_verdict_zh"),
            "verdict": summary.get("overall_verdict"),
            "area": summary.get("bbox_mm"),
            "headline": summary.get("headline", "")[:120],
        }
    ]
    checklist = []
    for c in summary.get("toolchain_checks", []):
        checklist.append(
            {
                "source": c.get("source", "toolchain"),
                "item": c.get("item"),
                "status": c.get("status"),
                "detail": (c.get("detail") or "")[:160],
            }
        )
    failed_rows = summary.get("failed") or [{"item": "-", "reason": "無", "where": "-"}]
    strengthen_rows = summary.get("needs_strengthening") or [
        {"source": "-", "item": "-", "reason": "無"}
    ]
    action_rows = summary.get("recommended_actions") or [
        {"source": "-", "priority": "-", "for_item": "-", "action": "維持現狀"}
    ]
    follow_rows = summary.get("follow_up_optimization", [])
    power_rows = []
    for pl in region_result.get("power_layout_checks", []):
        cap = pl.get("nearest_vin_cap") or {}
        boot_cap = pl.get("boot_cap") or {}
        power_rows.append(
            {
                "ic": pl.get("ic_reference"),
                "sw_fb_mm": pl.get("sw_fb_min_distance_mm"),
                "sw_fb": pl.get("sw_fb_status"),
                "vin_cap": cap.get("reference"),
                "vin_cap_mm": cap.get("distance_mm"),
                "vin_cap_status": pl.get("vin_cap_status"),
                "boot_cap": boot_cap.get("reference"),
                "boot_cap_mm": pl.get("boot_cap_distance_mm"),
                "boot_cap_status": pl.get("boot_cap_status"),
                "boot_topology": boot_cap.get("topology"),
                "boot_zero_ohm": ",".join(boot_cap.get("series_zero_ohm") or []),
                "sw_len_mm": pl.get("sw_length_used_mm"),
                "sw_len_status": pl.get("sw_length_status"),
                "gnd_vias": pl.get("gnd_via_count_near_ic"),
                "thermal_status": pl.get("thermal_via_status"),
                "sw_fb_guard": (
                    (pl.get("sw_fb_shield") or {}).get("same_layer_shield_net")
                    if (pl.get("sw_fb_shield") or {}).get("same_layer_blocks_line_of_sight")
                    else "none"
                ),
                "boot_drc_limited": pl.get("boot_drc_limited"),
                "leftover_traces": (pl.get("leftover_copper") or {}).get(
                    "residual_trace_count"
                ),
                "sw_fb_package_limited": pl.get("sw_fb_package_limited"),
                "vin_ceff_uf": pl.get("vin_ceff_uf"),
                "mlcc_vin_status": pl.get("mlcc_vin_status"),
                "vout_ceff_uf": pl.get("vout_ceff_uf"),
                "mlcc_vout_status": pl.get("mlcc_vout_status"),
            }
        )
    circuit_table = [
        {
            "id": d.get("id"),
            "conclusion": (d.get("conclusion") or "")[:200],
            "confidence": d.get("confidence"),
            "confidence_score": d.get("confidence_score"),
        }
        for d in summary.get("circuit_topology", [])
    ]
    datasheet_rows = [
        {
            "id": d.get("id"),
            "conclusion": (d.get("conclusion") or "")[:160],
            "confidence": d.get("confidence"),
            "confidence_score": d.get("confidence_score"),
        }
        for d in summary.get("datasheet_crossref", [])
    ]
    si_rows = [
        {
            "net": s.get("net_name"),
            "method": s.get("method"),
            "metric": s.get("headline_metric"),
            "status": s.get("status"),
            "shield_factor": s.get("shield_remaining_factor"),
        }
        for s in region_result.get("si_checks", [])
    ]
    mlcc_rows = []
    for b in (region_result.get("mlcc_dc_bias") or {}).get("banks") or []:
        jlc = b.get("jlc_suggestions") or []
        mlcc_rows.append(
            {
                "ic": b.get("ic_reference"),
                "role": b.get("role"),
                "net": b.get("net"),
                "vdc_v": b.get("vdc_v"),
                "c_nom_uf": b.get("c_nom_sum_uf"),
                "c_eff_uf": b.get("c_eff_sum_uf"),
                "minimum_uf": b.get("minimum_uf"),
                "status": b.get("status"),
                "jlc": ", ".join(s.get("lcsc", "") for s in jlc[:3]) or "-",
            }
        )
    return {
        "verdict": verdict_row,
        "toolchain_checklist": checklist,
        "failed": failed_rows,
        "needs_strengthening": strengthen_rows,
        "recommended_actions": action_rows,
        "power_layout": power_rows,
        "circuit_topology": circuit_table,
        "datasheet_crossref": datasheet_rows,
        "si_checks": si_rows,
        "mlcc_dc_bias": mlcc_rows,
        "follow_up": follow_rows,
    }


def format_conversation_summary_text(summary: Dict[str, Any]) -> str:
    """Plain-text block for terminal and agent chat (ASCII-safe)."""
    lines: List[str] = []
    v = summary.get("overall_verdict_zh", "?")
    lines.append("=== REGION VERDICT ===")
    lines.append(
        f"  Overall: {summary.get('overall_verdict', '').upper()} "
        f"(verdict_zh={v})"
    )
    lines.append(f"  Area: {summary.get('bbox_mm')}  {summary.get('region_area_mm2')} mm2")
    lines.append(f"  Headline: {summary.get('headline', '')}")

    failed = summary.get("failed", [])
    lines.append("")
    lines.append("--- FAILED [toolchain] ---")
    if failed:
        for f in failed:
            lines.append(f"  x {f.get('item')}: {f.get('reason')}")
            lines.append(f"    where: {f.get('where')}")
    else:
        lines.append("  (none)")

    strengthen = summary.get("needs_strengthening", [])
    lines.append("")
    lines.append("--- NEEDS STRENGTHENING ---")
    if strengthen:
        for s in strengthen:
            src = s.get("source", "?")
            lines.append(f"  ! [{src}] {s.get('item')}: {s.get('reason')}")
    else:
        lines.append("  (none)")

    actions = summary.get("recommended_actions", [])
    lines.append("")
    lines.append("--- HOW TO FIX ---")
    if actions:
        for a in actions:
            src = a.get("source", "?")
            pri = a.get("priority", "")
            lines.append(f"  > [{src}/{pri}] {a.get('for_item')}: {a.get('action')}")
    else:
        lines.append("  (no action required for current checks)")

    follow = summary.get("follow_up_optimization", [])
    lines.append("")
    lines.append("--- FOLLOW-UP OPTIMIZATION ---")
    for fu in follow[:5]:
        lines.append(f"  * [{fu.get('source')}] {fu.get('suggestion')}")

    checks = summary.get("toolchain_checks", [])
    lines.append("")
    lines.append("--- TOOLCHAIN CHECKLIST ---")
    for c in checks:
        st = c.get("status", "?")
        mark = "OK" if st == "pass" else ("!!" if st == "fail" else "--")
        lines.append(
            f"  [{mark}] {c.get('item')}: {c.get('detail', '')[:120]}"
        )

    circuit = summary.get("circuit_topology") or []
    if circuit:
        lines.append("")
        lines.append("--- CIRCUIT TOPOLOGY [ai_assisted] ---")
        for row in circuit[:8]:
            lines.append(
                f"  * ({row.get('confidence')}/{row.get('confidence_score')}) "
                f"{row.get('conclusion', '')[:160]}"
            )

    mlcc = (summary.get("summary_tables") or {}).get("mlcc_dc_bias") or []
    if mlcc:
        lines.append("")
        lines.append("--- MLCC DC-BIAS [toolchain] ---")
        for row in mlcc:
            lines.append(
                f"  [{row.get('status')}] {row.get('ic')} {row.get('role')} "
                f"{row.get('net')} @ {row.get('vdc_v')}V: "
                f"nom {row.get('c_nom_uf')} uF → eff {row.get('c_eff_uf')} uF "
                f"(min {row.get('minimum_uf')} uF) JLC {row.get('jlc')}"
            )

    return "\n".join(lines)
