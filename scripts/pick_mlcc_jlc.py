#!/usr/bin/env python3
"""Pick JLCPCB/LCSC MLCCs for the whole board (read-only).

Writes analysis/mlcc_jlc_assignment.json. Does not modify KiCad sources.
"""

from __future__ import annotations

import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

from kicad_readonly.buck_nets import (  # noqa: E402
    find_buck_ics,
    infer_rail_voltage,
    resolve_buck_nets,
    waveform_defaults,
)
from kicad_readonly.mlcc_dc_bias import (  # noqa: E402
    _is_gnd_decoupling,
    _load_data,
    _power_net,
    _profile_c_limits,
    case_from_footprint,
    parse_cap_value,
    remaining_fraction,
)
from kicad_readonly.pcb import load_pcb  # noqa: E402
from kicad_readonly.pcb_paths import resolve_pcb_path  # noqa: E402

OUT_PATH = REPO_ROOT / "analysis" / "mlcc_jlc_assignment.json"
RADIUS_MM = 16.0


def _pretty_c(uf: float) -> str:
    if uf >= 1:
        return f"{uf:g}uF"
    if uf >= 0.001:
        n = uf * 1000.0
        return f"{n:g}nF"
    return f"{uf * 1e6:g}pF"


def _value_with_vr(uf: float, vr: float) -> str:
    v = int(vr) if float(vr).is_integer() else vr
    return f"{_pretty_c(uf)}/{v}V"


def _jlc_url(part: Dict[str, Any]) -> str:
    lcsc = part["lcsc"]
    mpn = (part.get("mpn") or "").replace(" ", "")
    mfr = (part.get("mfr") or "").replace(" ", "")
    if mpn:
        return f"https://jlcpcb.com/partdetail/{mfr}-{mpn}/{lcsc}"
    return f"https://jlcpcb.com/partdetail/{lcsc}"


FOOTPRINT_BY_CASE = {
    "0402": "Capacitor_SMD:C_0402_1005Metric",
    "0603": "Capacitor_SMD:C_0603_1608Metric",
    "0805": "Capacitor_SMD:C_0805_2012Metric",
    "1206": "Capacitor_SMD:C_1206_3216Metric",
    "1210": "Capacitor_SMD:C_1210_3225Metric",
}


def pick_catalog_part(
    catalog: List[Dict[str, Any]],
    c_nom_uf: float,
    package: str,
) -> Optional[Dict[str, Any]]:
    cands = []
    for part in catalog:
        if part.get("package") != package:
            continue
        if abs(float(part["c_nom_uf"]) - c_nom_uf) > max(0.02 * c_nom_uf, 1e-8):
            continue
        cands.append(part)
    if not cands:
        return None
    cands.sort(
        key=lambda p: (
            0 if p.get("jlc_library") == "basic" else 1,
            -float(p.get("v_rated") or 0),
        )
    )
    return cands[0]


def pick_prefer_basic(
    catalog: List[Dict[str, Any]],
    c_nom_uf: float,
    package: str,
    vdc: float = 0.0,
) -> tuple[Optional[Dict[str, Any]], str, bool]:
    """Basic library first; Vr must be >= 1.5×Vdc. If same package cannot,
    pick the basic alt with most remaining C at Vdc."""
    min_vr = 1.5 * float(vdc or 0.0)
    scored = []
    for part in catalog:
        if abs(float(part["c_nom_uf"]) - c_nom_uf) > max(0.02 * c_nom_uf, 1e-8):
            continue
        vr = float(part.get("v_rated") or 0)
        if vr + 1e-9 < min_vr:
            continue
        lib_pen = 0 if part.get("jlc_library") == "basic" else 1
        same_pen = 0 if part.get("package") == package else 1
        ceff = _part_ceff(part, float(vdc or 0.0))
        scored.append(
            (
                lib_pen,
                same_pen,
                vr if same_pen == 0 else 1e9,
                -ceff,
                part,
            )
        )
    if not scored:
        return None, package, True
    scored.sort()
    part = scored[0][4]
    pkg = str(part["package"])
    return part, pkg, pkg == package


def _part_ceff(part: Dict[str, Any], vdc: float) -> float:
    frac = remaining_fraction(
        float(part["c_nom_uf"]),
        part.get("package"),
        float(part["v_rated"]),
        vdc,
        part.get("dielectric") or "X5R",
    )
    return round(float(part["c_nom_uf"]) * frac, 4)


def main() -> int:
    pcb, resolution = resolve_pcb_path()
    model = load_pcb(pcb)
    catalog = _load_data().get("jlc_catalog") or []
    radius = float(_load_data().get("local_radius_mm") or RADIUS_MM)

    assignments: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []

    fp_by_ref = {f.reference: f for f in model.footprints}
    ic_vin: Dict[str, float] = {}
    for ic in find_buck_ics(model):
        nets = resolve_buck_nets(model, ic.reference)
        wave = waveform_defaults(ic, nets)
        labeled = infer_rail_voltage(nets.get("vin"))
        ic_vin[ic.reference] = float(
            labeled if labeled is not None else (wave.get("v_in_assumed_v") or 0)
        )

    def estimate_vdc(net: Optional[str]) -> float:
        labeled = infer_rail_voltage(net)
        if labeled is not None:
            return float(labeled)
        m = re.match(r"^Net-\((U\d+)-", net or "")
        if m:
            return ic_vin.get(m.group(1), 0.0)
        return 0.0

    for fp in model.footprints:
        if not (fp.reference or "").startswith("C"):
            continue
        parsed = parse_cap_value(fp.value or "")
        case = case_from_footprint(fp.footprint or "")
        if parsed is None:
            skipped.append(
                {
                    "reference": fp.reference,
                    "reason": "value is not a capacitance",
                    "value": fp.value,
                }
            )
            continue
        if case == "electrolytic":
            skipped.append(
                {
                    "reference": fp.reference,
                    "reason": "electrolytic — not an MLCC",
                    "value": fp.value,
                    "footprint": fp.footprint,
                }
            )
            continue
        if not case:
            skipped.append(
                {
                    "reference": fp.reference,
                    "reason": "unknown package",
                    "footprint": fp.footprint,
                }
            )
            continue
        net = _power_net(fp)
        vdc = estimate_vdc(net)
        part, pkg, keep_fp = pick_prefer_basic(
            catalog, parsed["c_nom_uf"], case, vdc
        )
        if not part:
            missing.append(
                {
                    "reference": fp.reference,
                    "value": fp.value,
                    "package": case,
                    "c_nom_uf": parsed["c_nom_uf"],
                    "vdc_v": vdc,
                    "reason": "no JLC catalog hit with Vr >= 1.5 x Vdc",
                }
            )
            continue
        note = part.get("note")
        if not keep_fp:
            note = (
                f"Package {case} → {pkg} for JLC basic at {vdc:g} V. "
                "Change schematic Footprint then Update PCB from Schematic "
                "(replace footprints) and re-place the pads."
                + (f" {note}" if note else "")
            )
        assignments.append(
            {
                "reference": fp.reference,
                "old_value": fp.value,
                "value": _value_with_vr(part["c_nom_uf"], part["v_rated"]),
                "lcsc": part["lcsc"],
                "mpn": part.get("mpn"),
                "mfr": part.get("mfr"),
                "package": pkg,
                "old_package": case,
                "footprint": FOOTPRINT_BY_CASE.get(pkg, fp.footprint),
                "old_footprint": fp.footprint,
                "keep_footprint": keep_fp,
                "c_nom_uf": part["c_nom_uf"],
                "v_rated": part["v_rated"],
                "dielectric": part.get("dielectric"),
                "jlc_library": part.get("jlc_library"),
                "datasheet": _jlc_url(part),
                "net": net,
                "vdc_v": vdc,
                "gnd_decoupling": _is_gnd_decoupling(fp),
                "note": note,
            }
        )

    by_ref = {a["reference"]: a for a in assignments}
    part_by_lcsc = {p["lcsc"]: p for p in catalog}

    banks: List[Dict[str, Any]] = []
    extra_qty: List[Dict[str, Any]] = []
    for ic in find_buck_ics(model):
        nets = resolve_buck_nets(model, ic.reference)
        limits = _profile_c_limits(ic.value or "", ic.footprint or "")
        wave = waveform_defaults(ic, nets)
        vin_label = infer_rail_voltage(nets.get("vin"))
        vout_label = infer_rail_voltage(nets.get("vout"))
        role_vdc = {
            "VIN": vin_label if vin_label is not None else wave.get("v_in_assumed_v"),
            "VOUT": vout_label if vout_label is not None else wave.get("v_out_assumed_v"),
        }
        for role, net, minimum in (
            ("VIN", nets.get("vin"), limits.get("cin_min_uf")),
            ("VOUT", nets.get("vout"), limits.get("cout_min_uf")),
        ):
            if not net:
                continue
            vdc = float(role_vdc.get(role) or 0.0)
            local = []
            for a in assignments:
                if a.get("net") != net:
                    continue
                fp = fp_by_ref.get(a["reference"])
                if not fp:
                    continue
                d = math.hypot(fp.at[0] - ic.at[0], fp.at[1] - ic.at[1])
                if d > radius:
                    continue
                if (a.get("c_nom_uf") or 0) < 1.0:
                    continue
                part = part_by_lcsc.get(a["lcsc"])
                if not part:
                    continue
                ceff = _part_ceff(part, vdc)
                local.append({**a, "distance_mm": round(d, 3), "c_eff_uf": ceff})
            ceff_sum = round(sum(x["c_eff_uf"] for x in local), 4)
            status = "info"
            if minimum is not None:
                if ceff_sum + 1e-9 >= minimum:
                    status = "pass"
                elif ceff_sum >= 0.7 * minimum:
                    status = "warn"
                else:
                    status = "fail"
            banks.append(
                {
                    "ic": ic.reference,
                    "role": role,
                    "net": net,
                    "vdc_v": vdc,
                    "minimum_uf": minimum,
                    "c_eff_sum_uf": ceff_sum,
                    "status": status,
                    "local": [
                        {
                            "reference": x["reference"],
                            "lcsc": x["lcsc"],
                            "c_eff_uf": x["c_eff_uf"],
                        }
                        for x in sorted(local, key=lambda z: z["distance_mm"])
                    ],
                }
            )
            if status in ("fail", "warn") and minimum is not None and local:
                sample = part_by_lcsc[local[0]["lcsc"]]
                each = _part_ceff(sample, vdc)
                n_need = max(1, math.ceil(minimum / each - 1e-12)) if each > 0 else 99
                extra_qty.append(
                    {
                        "ic": ic.reference,
                        "role": role,
                        "net": net,
                        "present": len(local),
                        "need_qty": n_need,
                        "add_qty": max(0, n_need - len(local)),
                        "same_package_lcsc": sample["lcsc"],
                        "each_ceff_uf": each,
                        "package": sample.get("package"),
                        "alt_package": "0805 C45783 or 1206 C12891"
                        if sample.get("package") == "1210"
                        else "1206 C12891",
                    }
                )

    unique_parts: Dict[str, Dict[str, Any]] = {}
    for a in assignments:
        row = unique_parts.setdefault(
            a["lcsc"],
            {
                "lcsc": a["lcsc"],
                "mpn": a["mpn"],
                "mfr": a["mfr"],
                "value": a["value"],
                "package": a["package"],
                "jlc_library": a["jlc_library"],
                "qty": 0,
                "refs": [],
            },
        )
        row["qty"] += 1
        row["refs"].append(a["reference"])

    worst = "pass"
    for b in banks:
        if b["status"] == "fail":
            worst = "fail"
        elif b["status"] == "warn" and worst == "pass":
            worst = "warn"

    blob = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pcb_file": str(pcb),
        "pcb_resolution": resolution,
        "model_id": (_load_data().get("_meta") or {}).get("model_id"),
        "lcsc_field": "LCSC Part",
        "bank_overall_status": worst,
        "assignments": sorted(assignments, key=lambda a: a["reference"]),
        "skipped": skipped,
        "missing_catalog": missing,
        "unique_jlc_parts": list(unique_parts.values()),
        "banks_after_assignment": banks,
        "if_still_short": extra_qty,
        "limitations": [
            "Remaining C uses conservative_class2_v1, not a vendor C-V curve.",
            "1210 10uF has no JLCPCB basic part; C77100 is extended (extra feeder fee).",
            "Electrolytic C10 is not assigned.",
            "Footprints are not auto-moved on the PCB; keep_footprint=false rows "
            "need Update PCB from Schematic (replace footprints) then hand placement.",
        ],
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(blob, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {OUT_PATH}")
    print(f"  assigned {len(assignments)}  skipped {len(skipped)}  missing {len(missing)}")
    print(f"  unique LCSC {len(unique_parts)}  banks {worst}")
    for p in sorted(unique_parts.values(), key=lambda x: -x["qty"]):
        print(
            f"    {p['lcsc']:8} {p['value']:12} {p['package']} "
            f"{p['jlc_library']:9} x{p['qty']}"
        )
    for b in banks:
        if b["status"] != "pass":
            print(
                f"  !! {b['ic']} {b['role']} {b['net']} "
                f"eff {b['c_eff_sum_uf']} vs {b['minimum_uf']} [{b['status']}]"
            )
    for e in extra_qty:
        print(
            f"  add {e['add_qty']} x {e['same_package_lcsc']} "
            f"near {e['ic']} {e['role']} (have {e['present']}, need {e['need_qty']})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
