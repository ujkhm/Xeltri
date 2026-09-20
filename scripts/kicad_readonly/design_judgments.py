"""Design inferences for PCB region reports.

Factual detections stay in toolchain fields; interpretive conclusions are
emitted only under ``ai_assisted_judgments`` with explicit provenance.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .datasheet_ref import crossref_footprint
from .pcb import Footprint, PcbModel

Point = Tuple[float, float]

TOOLCHAIN_GENERATED_FIELDS = [
    "polygon_mm",
    "bbox_mm",
    "region_area_mm2",
    "segment_count",
    "via_count",
    "zone_count",
    "keepout_count",
    "footprint_count",
    "pad_count",
    "copper_planes_in_region",
    "return_path_checks",
    "reference_plane_mode",
    "stackup_reference_map",
    "coverage_method",
    "split_ground_bridges",
    "power_layout_checks",
    "mlcc_dc_bias",
    "circuit_neighborhoods",
    "conversation_summary",
    "nets_summary",
    "zones",
    "keepouts",
    "segments",
    "vias",
    "footprints",
]

AI_ASSISTED_FIELDS = ["ai_assisted_judgments"]

_ZERO_OHM_RE = re.compile(r"^0\s*(r|ohm|Ω)?$", re.IGNORECASE)


def _point_in_bbox(pt: Point, bbox: Dict[str, float]) -> bool:
    return (
        bbox["min_x"] <= pt[0] <= bbox["max_x"]
        and bbox["min_y"] <= pt[1] <= bbox["max_y"]
    )


def _distance(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _is_zero_ohm(value: str) -> bool:
    v = (value or "").strip()
    return bool(_ZERO_OHM_RE.match(v)) or v in ("0", "0R", "0Ω", "0ohm")


def _ground_net_names() -> Tuple[str, ...]:
    return ("GND", "GNDA", "AGND")


def find_split_ground_bridges(model: PcbModel) -> List[Dict[str, Any]]:
    """Toolchain: footprints that directly connect GND-like nets (e.g. 0R jumpers)."""
    bridges: List[Dict[str, Any]] = []
    for fp in model.footprints:
        # Only two-terminal passives (e.g. 0R) — not multi-pad ICs with both GND and GNDA.
        if len(fp.pads) != 2:
            continue
        pad_nets = {p.net_name for p in fp.pads if p.net_name}
        has_gnd = "GND" in pad_nets
        has_agnd = any(n in pad_nets for n in ("GNDA", "AGND"))
        if not (has_gnd and has_agnd):
            continue
        bridges.append(
            {
                "reference": fp.reference,
                "value": fp.value,
                "at_mm": list(fp.at),
                "layer": fp.layer,
                "is_zero_ohm": _is_zero_ohm(fp.value),
                "pads": [
                    {
                        "pad": p.number,
                        "net_name": p.net_name,
                        "at_mm": list(p.at),
                    }
                    for p in fp.pads
                ],
            }
        )
    return sorted(bridges, key=lambda b: b["reference"])


def _bridges_in_region(
    bridges: Sequence[Dict[str, Any]], bbox: Dict[str, float]
) -> List[Dict[str, Any]]:
    in_region = []
    for bridge in bridges:
        pt = tuple(bridge["at_mm"])
        if _point_in_bbox(pt, bbox):
            in_region.append(bridge)
    return in_region


def _nearest_bridge(
    bridges: Sequence[Dict[str, Any]], pt: Point
) -> Optional[Dict[str, Any]]:
    if not bridges:
        return None
    return min(bridges, key=lambda b: _distance(tuple(b["at_mm"]), pt))


_CONFIDENCE_BASE_SCORE = {"high": 85, "medium": 60, "low": 35}


def _finalize_judgment(j: Dict[str, Any]) -> Dict[str, Any]:
    """Attach a numeric confidence_score (0-100) + uncertainty_factors list on
    top of the existing high/medium/low label, derived from evidence
    completeness and any declared limitations/unverified sources."""
    base = _CONFIDENCE_BASE_SCORE.get(j.get("confidence", "medium"), 50)
    uncertainty: List[str] = []

    if not j.get("toolchain_evidence"):
        base -= 20
        uncertainty.append("No toolchain_evidence attached to this judgment.")

    limitations = j.get("limitations", [])
    if limitations:
        base -= min(15, 3 * len(limitations))
        uncertainty.extend(f"Limitation: {lim}" for lim in limitations)

    if j.pop("_unverified_datasheet", False):
        base -= 10
        uncertainty.append(
            "Datasheet profile is manually curated and not yet human-verified "
            "against the current datasheet revision — treat thresholds as "
            "indicative, confirm against the actual PDF before sign-off."
        )

    j["confidence_score"] = max(5, min(95, base))
    j["uncertainty_factors"] = uncertainty
    return j


def generate_datasheet_crossref_judgments(
    region_result: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Generic per-footprint datasheet/layout-guideline crossref.

    Works for ANY component with a matching profile in
    data/datasheet_refs.json — not hardcoded to one part number. As new ICs
    get profiles added, future analyses automatically pick them up here.
    """
    judgments: List[Dict[str, Any]] = []
    power_by_ref = {
        pl["ic_reference"]: pl for pl in region_result.get("power_layout_checks", [])
    }
    seen_refs: set = set()

    for fp in region_result.get("footprints", []):
        ref = fp.get("reference", "")
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        toolchain_values = dict(power_by_ref.get(ref, {}))
        bundle = crossref_footprint(fp.get("value", ""), fp.get("footprint", ""), toolchain_values)
        if not bundle:
            continue

        fail_evals = [e for e in bundle["evaluations"] if e["status"] == "fail"]
        warn_evals = [e for e in bundle["evaluations"] if e["status"] == "warn"]
        pass_evals = [e for e in bundle["evaluations"] if e["status"] == "pass"]
        info_evals = [e for e in bundle["evaluations"] if e["status"] == "info"]

        lines = []
        for e in bundle["evaluations"]:
            mark = {"pass": "OK", "warn": "!!", "fail": "XX", "info": "--"}.get(e["status"], "?")
            lines.append(f"[{mark}] {e['topic']}: {e['reason']} ({e['section']})")

        judgments.append(
            _finalize_judgment(
                {
                    "id": f"datasheet_crossref_{ref}_{bundle['profile_id']}",
                    "source": "ai_assisted",
                    "confidence": "medium" if (warn_evals or fail_evals) else "high",
                    "category": "datasheet_crossref",
                    "conclusion": (
                        f"{ref} ({bundle['display_name']}): {len(pass_evals)}/"
                        f"{len(bundle['evaluations'])} layout-guideline checks pass"
                        + (f", {len(fail_evals)} fail" if fail_evals else "")
                        + (f", {len(warn_evals)} need review" if warn_evals else "")
                        + f". Reference: {bundle['doc']}."
                    ),
                    "rationale": "\n".join(lines),
                    "toolchain_evidence": {
                        "ic_reference": ref,
                        "power_layout_checks": toolchain_values,
                        "datasheet_evaluations": bundle["evaluations"],
                    },
                    "limitations": [
                        "Guideline thresholds are generic best-practice, not the exact "
                        "datasheet numeric spec unless the section explicitly gives one.",
                        "Does not replace vendor EVM layout comparison or bench test.",
                    ]
                    + (
                        [f"{len(info_evals)} guideline(s) are qualitative only (no numeric threshold)."]
                        if info_evals
                        else []
                    ),
                    "_unverified_datasheet": not bundle["verified"],
                }
            )
        )
    return judgments


def generate_mlcc_dc_bias_judgments(
    region_result: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Interpret MLCC DC-bias bank results and JLC same-package swaps."""
    mlcc = region_result.get("mlcc_dc_bias") or {}
    banks = mlcc.get("banks") or []
    if not banks:
        return []
    short = [b for b in banks if b.get("status") in ("fail", "warn")]
    if not short:
        return [
            {
                "id": "mlcc_dc_bias_board",
                "source": "ai_assisted",
                "confidence": "medium",
                "category": "mlcc_dc_bias",
                "conclusion": (
                    "Buck VIN/VOUT ceramic banks meet datasheet minima after the "
                    "conservative Class-II DC-bias model."
                ),
                "rationale": (
                    "Model conservative_class2_v1; not a vendor C-V curve. "
                    "Vr assumed from case/Cnom when Value has no /16V suffix."
                ),
                "toolchain_evidence": {
                    "overall_status": mlcc.get("overall_status"),
                    "model_id": mlcc.get("model_id"),
                },
                "limitations": mlcc.get("limitations") or [],
            }
        ]
    lines = []
    for b in short:
        jlc = ", ".join(s.get("lcsc", "") for s in (b.get("jlc_suggestions") or [])[:3])
        lines.append(
            f"{b.get('ic_reference')} {b.get('role')} {b.get('net')}: "
            f"{b.get('c_eff_sum_uf')} uF eff vs {b.get('minimum_uf')} uF min"
            + (f"; JLC {jlc}" if jlc else "")
        )
    return [
        {
            "id": "mlcc_dc_bias_short",
            "source": "ai_assisted",
            "confidence": "medium",
            "category": "mlcc_dc_bias",
            "conclusion": (
                f"{len(short)} VIN/VOUT bank(s) short after DC-bias derating. "
                + " ".join(lines)
            ),
            "rationale": (
                "Same-package JLC suggestions come from the curated catalog "
                "(higher Vr or lower CV at the same footprint)."
            ),
            "toolchain_evidence": {"short_banks": short},
            "limitations": mlcc.get("limitations") or [],
        }
    ]


def generate_circuit_topology_judgments(
    model: PcbModel,
    region_result: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Interpret pin-neighborhood connectivity (BOOT/FB/VIN/EN/SW) around
    flagged or always-reviewed IC pins. Follows series 0Ω so KiCad auto-nets
    are not mistaken for open circuits."""
    from .circuit_graph import is_auto_named_net, is_ic_pin_net

    judgments: List[Dict[str, Any]] = []
    neighborhoods = region_result.get("circuit_neighborhoods") or []

    for nb in neighborhoods:
        ic = nb.get("ic_reference", "?")
        boot_path = nb.get("boot_path")
        pin_nets = nb.get("pin_nets") or {}
        has_boot_pin = bool(pin_nets.get("BOOT") or pin_nets.get("BST"))
        if boot_path:
            zrefs = boot_path.get("series_zero_ohm") or []
            auto = boot_path.get("intermediate_auto_nets") or []
            topo = boot_path.get("topology", "")
            if zrefs and auto:
                conclusion = (
                    f"{ic} BOOT bootstrap is {boot_path.get('reference')} "
                    f"({boot_path.get('value')}) in series with 0Ω "
                    f"{', '.join(zrefs)}. Intermediate net(s) "
                    f"{', '.join(auto)} are KiCad auto-names (no net label), "
                    f"which is expected when a 0Ω jumper sits between BOOT and SW "
                    f"— not an open circuit."
                )
            elif zrefs:
                conclusion = (
                    f"{ic} BOOT capacitor {boot_path.get('reference')} reaches SW "
                    f"through series 0Ω {', '.join(zrefs)}."
                )
            else:
                conclusion = (
                    f"{ic} BOOT capacitor {boot_path.get('reference')} "
                    f"({boot_path.get('value')}) connects BOOT and SW directly."
                )
            judgments.append(
                {
                    "id": f"boot_bootstrap_path_{ic}",
                    "source": "ai_assisted",
                    "confidence": "high",
                    "category": "circuit_topology",
                    "conclusion": conclusion,
                    "rationale": (
                        "Series 0Ω on a bootstrap path is a common DNP/option jumper. "
                        "KiCad then auto-generates Net-(Cx-Pady) on the unnamed node. "
                        f"Path: {boot_path.get('path_text', '')}"
                    ),
                    "toolchain_evidence": {"boot_path": boot_path, "topology": topo},
                    "limitations": [
                        "Does not verify the 0Ω is populated (BOM/DNP).",
                        "Does not measure bootstrap loop inductance.",
                    ],
                }
            )
        elif has_boot_pin:
            judgments.append(
                {
                    "id": f"boot_bootstrap_path_{ic}",
                    "source": "ai_assisted",
                    "confidence": "medium",
                    "category": "circuit_topology",
                    "conclusion": (
                        f"{ic}: no BOOT–capacitor–SW path found even after walking "
                        f"series 0Ω jumpers. Confirm schematic bootstrap cap."
                    ),
                    "rationale": (
                        "BOOT capacitor may be missing, on a differently named pin, "
                        "or isolated by a non-0Ω series part the walker does not cross."
                    ),
                    "toolchain_evidence": {"pin_nets": nb.get("pin_nets")},
                    "limitations": ["Walker only crosses capacitor + 0Ω, max 6 hops."],
                }
            )

        fb = nb.get("fb_divider") or {}
        gnd_leg = fb.get("gnd_leg") or []
        resistors = fb.get("resistors") or []
        caps = fb.get("capacitors") or []
        if resistors:
            gnd_bits = []
            for p in gnd_leg:
                gnd_net = (
                    p["net_a"]
                    if p["net_a"] in ("GND", "GNDA", "AGND") or str(p["net_a"]).startswith("GND")
                    else p["net_b"]
                )
                gnd_bits.append(f"{p['reference']}={p['value']} to {gnd_net}")
            gnd_txt = ", ".join(gnd_bits) if gnd_bits else "no resistor to GND/GNDA found"
            other_r = [p for p in resistors if p not in gnd_leg]
            upper_txt = (
                ", ".join(f"{p['reference']}={p['value']}" for p in other_r) or "none"
            )
            ff = ", ".join(f"{p['reference']}={p['value']}" for p in caps) or "none"
            auto = fb.get("auto_named_nets") or []
            auto_note = (
                f" Auto-nets on the divider ({', '.join(auto)}) are unnamed series nodes."
                if auto
                else ""
            )
            judgments.append(
                {
                    "id": f"fb_divider_topology_{ic}",
                    "source": "ai_assisted",
                    "confidence": "high" if gnd_leg else "medium",
                    "category": "circuit_topology",
                    "conclusion": (
                        f"{ic} FB neighborhood: lower/gnd leg {gnd_txt}; "
                        f"upper/other resistors {upper_txt}; feedforward/filter caps {ff}."
                        f"{auto_note}"
                    ),
                    "rationale": (
                        "Buck FB should have a resistor to local analog ground (GNDA) "
                        "and a resistor toward VOUT. Extra R/C on auto-named nets is a "
                        "feedforward or RC filter, not a missing connection."
                    ),
                    "toolchain_evidence": {"fb_divider": fb},
                    "limitations": [
                        "Does not compute Vout from divider ratio.",
                        "Does not confirm which resistor is upper vs lower without VOUT net name.",
                    ],
                }
            )

        pin_nb = nb.get("pin_neighborhoods") or {}
        for role in ("VIN", "EN", "SW"):
            block = pin_nb.get(role)
            if not block:
                continue
            parts = block.get("parts") or []
            if not parts:
                continue
            summary = ", ".join(
                f"{p['reference']}={p['value']}({p['kind']})" for p in parts[:8]
            )
            if len(parts) > 8:
                summary += f", … +{len(parts) - 8} more"
            auto = block.get("auto_named_nets") or []
            extra = (
                f" Intermediate auto-nets: {', '.join(auto)}."
                if auto
                else ""
            )
            role_hint = {
                "VIN": "expect bulk + HF caps to PGND and possibly an input fuse",
                "EN": "expect UVLO / enable divider (resistors to VIN and GND/GNDA)",
                "SW": "expect inductor to VOUT and the BOOT jumper/cap on the same node",
            }.get(role, "")
            judgments.append(
                {
                    "id": f"pin_neighborhood_{ic}_{role}",
                    "source": "ai_assisted",
                    "confidence": "medium",
                    "category": "circuit_topology",
                    "conclusion": (
                        f"{ic} {role} related circuit: {summary}.{extra}"
                    ),
                    "rationale": (
                        f"Parts on the {role} net plus one 2-pin hop "
                        f"(including 0Ω). {role_hint}."
                    ),
                    "toolchain_evidence": {
                        "role": role,
                        "seed_net": block.get("seed_net"),
                        "parts": parts,
                    },
                    "limitations": [
                        "One extra hop only — distant bulk caps may be omitted.",
                        "Classification uses footprint Value/Reference heuristics.",
                    ],
                }
            )

    # Region-wide: unnamed auto-nets that are only explained by 2-pin series parts
    auto_in_region = sorted(
        {
            n.get("net_name", "")
            for n in region_result.get("nets_summary", [])
            if is_auto_named_net(n.get("net_name", ""))
            and n.get("length_in_region_mm", 0) > 0
        }
    )
    explained = set()
    for n in auto_in_region:
        if is_ic_pin_net(n):
            explained.add(n)
    for nb in neighborhoods:
        bp = nb.get("boot_path") or {}
        explained.update(bp.get("intermediate_auto_nets") or [])
        fb = nb.get("fb_divider") or {}
        explained.update(fb.get("auto_named_nets") or [])
        for block in (nb.get("pin_neighborhoods") or {}).values():
            explained.update(block.get("auto_named_nets") or [])
    unexplained = [n for n in auto_in_region if n not in explained]
    if auto_in_region:
        judgments.append(
            {
                "id": "auto_named_nets_in_region",
                "source": "ai_assisted",
                "confidence": "high" if not unexplained else "medium",
                "category": "circuit_topology",
                "conclusion": (
                    f"Region has {len(auto_in_region)} KiCad auto-named net(s) "
                    f"(no user net label). "
                    + (
                        "All of them sit on series 2-pin paths (0Ω / divider / filter) "
                        "already walked from IC pins — treat as unnamed intermediate nodes, "
                        "not missing schematic connections."
                        if not unexplained
                        else (
                            f"Explained via pin-neighborhood walks: "
                            f"{', '.join(sorted(explained)) or 'none'}; "
                            f"not yet attached to a walked IC pin: "
                            f"{', '.join(unexplained)}."
                        )
                    )
                ),
                "rationale": (
                    "KiCad names unlabeled copper Net-(Ref-PadN). A 0Ω or extra "
                    "series R/C between two functional pins always creates such a net."
                ),
                "toolchain_evidence": {
                    "auto_named_in_region": auto_in_region,
                    "explained_by_pin_walk": sorted(explained),
                    "unexplained": unexplained,
                },
                "limitations": [
                    "Does not parse schematic wire graphics; uses PCB pad net names, "
                    "which match the realized schematic connectivity.",
                ],
            }
        )

    return judgments


def generate_sw_fb_shield_judgments(region_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Interpret coplanar GNDA/AGND/GND copper between SW and FB."""
    judgments: List[Dict[str, Any]] = []
    for pl in region_result.get("power_layout_checks", []):
        shield = pl.get("sw_fb_shield") or {}
        ic = pl.get("ic_reference", "?")
        if not shield:
            continue
        los = shield.get("same_layer_blocks_line_of_sight")
        net = shield.get("same_layer_shield_net")
        kind = shield.get("same_layer_shield_kind")
        factor = shield.get("coupling_reduction_factor", 1.0)
        inner = shield.get("inner_ref_gap_coverage_pct") or {}
        if los:
            conclusion = (
                f"{ic} SW–FB shortest path is crossed by {shield.get('signal_layer')} "
                f"{net} {kind} copper. This is a coplanar guard, not just inner AGND "
                f"under FB. SI remaining-coupling factor {factor:.2f}."
            )
        else:
            conclusion = (
                f"{ic} SW–FB shortest path has no same-layer GNDA/AGND/GND crossing. "
                "Inner analog ground under FB is a return plane already assumed in the "
                "coupled-line reference; it is not extra coplanar shielding."
            )
        limitations = [
            "Coupling reduction factor is a heuristic scale, not a 3D field solve.",
            "Inner-layer split (GND vs GNDA under the gap) is reported as coverage, "
            "not modeled as a split-plane in openEMS.",
        ]
        if inner:
            limitations.append(
                f"Inner-layer coverage along the gap: {inner}."
            )
        judgments.append(
            {
                "id": f"sw_fb_coplanar_guard_{ic}",
                "source": "ai_assisted",
                "confidence": "high" if los else "medium",
                "category": "circuit_topology",
                "conclusion": conclusion,
                "rationale": shield.get("notes", ""),
                "toolchain_evidence": {"sw_fb_shield": shield},
                "limitations": limitations,
            }
        )
    return judgments


def generate_ai_assisted_judgments(
    model: PcbModel,
    region_result: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Interpretive design review — not geometric proof; requires human validation."""
    judgments: List[Dict[str, Any]] = []
    bbox = region_result.get("bbox_mm", {})
    bridges = region_result.get("split_ground_bridges") or find_split_ground_bridges(
        model
    )
    bridges_in_region = _bridges_in_region(bridges, bbox) if bbox else []

    if bridges:
        refs = ", ".join(b["reference"] for b in bridges)
        zero_refs = [b["reference"] for b in bridges if b["is_zero_ohm"]]
        judgments.append(
            {
                "id": "split_ground_bridge_topology",
                "source": "ai_assisted",
                "confidence": "high" if zero_refs else "medium",
                "category": "split_ground",
                "conclusion": (
                    f"{refs} connect GNDA/AGND to GND on this board"
                    + (
                        f" via 0 ohm ({', '.join(zero_refs)})"
                        if zero_refs
                        else " (non-zero value — verify schematic intent)"
                    )
                    + "."
                ),
                "rationale": (
                    "Footprints with one pad on GND and one on GNDA/AGND are "
                    "interpreted as split-ground tie / bridge components."
                ),
                "toolchain_evidence": {
                    "split_ground_bridges": bridges,
                    "bridges_in_region": bridges_in_region,
                },
                "limitations": [
                    "Does not verify schematic net labels or BOM intent.",
                    "Does not measure bridge impedance or noise coupling.",
                    "0Ω classification is from footprint Value field only.",
                ],
            }
        )

    fb_checks = [
        r
        for r in region_result.get("return_path_checks", [])
        if "FB" in r.get("net_name", "")
    ]
    for fb in fb_checks:
        dom = fb.get("dominant_reference_net", "GND")
        status = fb.get("status", "unknown")
        net = fb["net_name"]
        if dom in ("GNDA", "AGND") and status == "pass":
            bridge = None
            if fb.get("segments"):
                mid = fb["segments"][0]["start"]
                bridge = _nearest_bridge(bridges, tuple(mid))
            judgments.append(
                {
                    "id": f"fb_analog_return_{net}",
                    "source": "ai_assisted",
                    "confidence": "high" if bridges else "medium",
                    "category": "return_path",
                    "conclusion": (
                        f"{net} return path over {dom} on "
                        f"{', '.join(fb.get('primary_ref_planes_used', []))} "
                        f"is consistent with analog feedback layout"
                        + (
                            f"; nearest GND-GNDA bridge: "
                            f"{bridge['reference']} at "
                            f"{_distance(tuple(bridge['at_mm']), tuple(mid)):.1f} mm"
                            if bridge and fb.get("segments")
                            else ""
                        )
                        + "."
                    ),
                    "rationale": (
                        "Buck FB should reference local analog ground. GNDA return "
                        "with board-level GND tie is a common acceptable topology."
                    ),
                    "toolchain_evidence": {
                        "return_path_check": fb,
                        "nearest_split_ground_bridge": bridge,
                    },
                    "limitations": [
                        "Geometric coverage only; not transient or load regulation.",
                        "Does not check FB loop area against SW node.",
                    ],
                }
            )
        elif dom in ("GNDA", "AGND") and status != "pass":
            judgments.append(
                {
                    "id": f"fb_analog_return_{net}",
                    "source": "ai_assisted",
                    "confidence": "medium",
                    "category": "return_path",
                    "conclusion": (
                        f"{net} uses {dom} but toolchain geometry reports "
                        f"{status}; review pour/clearance manually."
                    ),
                    "rationale": "Split-ground FB expected over GNDA pour.",
                    "toolchain_evidence": {"return_path_check": fb},
                    "limitations": ["May be false negative near tie points or pours."],
                }
            )

    from .buck_nets import BUCK_VALUE_MARKERS

    buck_fps = [
        fp
        for fp in region_result.get("footprints", [])
        if any(
            m.lower() in f"{fp.get('value', '')} {fp.get('footprint', '')}".lower()
            for m in BUCK_VALUE_MARKERS
        )
    ]
    if not buck_fps:
        seen = set()
        for pl in region_result.get("power_layout_checks") or []:
            ref = pl.get("ic_reference")
            if ref and ref not in seen:
                seen.add(ref)
                buck_fps.append(
                    {
                        "reference": ref,
                        "value": pl.get("ic_value", ""),
                        "footprint": "",
                    }
                )
    if buck_fps:
        refs = ", ".join(fp["reference"] for fp in buck_fps)
        fb_pass = all(r.get("status") == "pass" for r in fb_checks)
        judgments.append(
            {
                "id": "buck_region_qualification",
                "source": "ai_assisted",
                "confidence": "high" if fb_pass and bridges else "medium",
                "category": "power_design",
                "conclusion": (
                    f"Buck stage(s) {refs}: toolchain return-path checks "
                    f"{'pass' if fb_pass else 'need review'}; "
                    f"split-ground tie {'present' if bridges else 'not detected on board'}."
                ),
                "rationale": (
                    "Buck FB should sit over a quiet return (GNDA or local GND) "
                    "with a single-point tie if analog/power grounds are split."
                ),
                "toolchain_evidence": {
                    "buck_footprints": refs,
                    "fb_return_path_checks": fb_checks,
                    "split_ground_bridges": bridges,
                },
                "limitations": [
                    "Not a substitute for vendor layout guide, EMI test, or thermal review.",
                ],
            }
        )

    judgments.extend(generate_circuit_topology_judgments(model, region_result))
    judgments.extend(generate_datasheet_crossref_judgments(region_result))
    judgments.extend(generate_sw_fb_shield_judgments(region_result))
    judgments.extend(generate_mlcc_dc_bias_judgments(region_result))

    return [_finalize_judgment(j) for j in judgments]


def attach_analysis_provenance(region_result: Dict[str, Any]) -> Dict[str, Any]:
    region_result["analysis_provenance"] = {
        "policy_doc": ".cursor/rules/kicad-readonly.mdc",
        "toolchain_generated_fields": TOOLCHAIN_GENERATED_FIELDS,
        "ai_assisted_fields": AI_ASSISTED_FIELDS,
        "note": (
            "Fields listed in toolchain_generated_fields are deterministic parser/"
            "geometry output. ai_assisted_judgments are interpretive and require "
            "engineer validation."
        ),
    }
    return region_result
