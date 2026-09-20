"""MLCC DC-bias effective capacitance vs buck CIN/COUT minima.

Board capacitors have no MPN. This module:

- infers Vdc from the non-ground pad net
- infers case size from the footprint
- estimates remaining C with a conservative Class-II model
- sums local banks at each buck VIN/VOUT
- if a bank is short, looks up same-package JLCPCB/LCSC parts that
  model as having more remaining C at that Vdc

Not a manufacturer C-V curve. Catalog LCSC IDs are curated snapshots.
"""

from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .buck_nets import find_buck_ics, infer_rail_voltage, resolve_buck_nets, waveform_defaults
from .datasheet_ref import match_profile
from .pcb import Footprint, PcbModel

DATA_PATH = Path(__file__).resolve().parent / "data" / "mlcc_dc_bias.json"

GND_NETS = {"GND", "GNDA", "AGND", "PGND", "SGND"}
_CAP_RE = re.compile(
    r"(\d+\.?\d*)\s*(p|n|u|µ|μ|m)f(?:\s*/\s*(\d+\.?\d*)\s*v)?",
    re.IGNORECASE,
)
_CASE_RE = re.compile(r"(0402|0603|0805|1206|1210|1812)", re.IGNORECASE)

CASE_AREA_MM2 = {
    "0402": 0.5,
    "0603": 0.9,
    "0805": 1.6,
    "1206": 3.2,
    "1210": 4.0,
    "1812": 7.2,
}

PACKAGE_UPGRADE = {"0402": "0603", "0603": "0805", "0805": "1206", "1206": "1210"}


@lru_cache(maxsize=1)
def _load_data() -> Dict[str, Any]:
    if not DATA_PATH.is_file():
        return {"jlc_catalog": [], "local_radius_mm": 12.0, "_meta": {}}
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


def parse_cap_value(value: str) -> Optional[Dict[str, Any]]:
    m = _CAP_RE.search(value or "")
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2).lower().replace("µ", "u").replace("μ", "u")
    scale = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3}[unit]
    uf = n * scale / 1e-6
    vr = float(m.group(3)) if m.group(3) else None
    return {"c_nom_uf": uf, "v_rated_from_value": vr}


def case_from_footprint(footprint: str) -> Optional[str]:
    fp = footprint or ""
    if re.search(r"elec", fp, re.IGNORECASE):
        return "electrolytic"
    m = _CASE_RE.search(fp)
    return m.group(1) if m else None


def assume_v_rated(case: Optional[str], c_nom_uf: float, explicit: Optional[float]) -> float:
    if explicit:
        return explicit
    if case == "electrolytic":
        return 25.0
    if case == "0402":
        if c_nom_uf >= 10:
            return 6.3
        if c_nom_uf >= 4.7:
            return 10.0
        return 16.0
    if case == "0805":
        if c_nom_uf >= 22:
            return 16.0
        return 16.0
    if case in ("1206", "1210", "1812"):
        if c_nom_uf >= 47:
            return 16.0
        return 16.0
    return 16.0


def remaining_fraction(
    c_nom_uf: float,
    case: Optional[str],
    v_rated: float,
    vdc: float,
    dielectric: str = "X5R",
) -> float:
    """Conservative Class-II remaining C / Cnom. C0G ≈ 1.0. Electrolytic ≈ 1.0."""
    diel = (dielectric or "").upper()
    if case == "electrolytic" or diel in ("C0G", "NP0", "C0G/NP0"):
        return 1.0
    if c_nom_uf <= 0.001 and diel not in ("X5R", "X7R"):
        return 1.0
    if v_rated <= 0:
        return 0.5
    x = max(0.0, float(vdc) / float(v_rated))
    area = CASE_AREA_MM2.get(case or "", 1.6)
    density = (c_nom_uf * v_rated) / max(area, 0.2)
    d_norm = min(density / 80.0, 4.0)
    a, b = 0.6, 1.2
    frac = 1.0 / (1.0 + (a + b * d_norm) * (x**2))
    return max(0.08, min(1.0, frac))


def _power_net(fp: Footprint) -> Optional[str]:
    nets = [p.net_name for p in fp.pads if p.net_name]
    power = [n for n in nets if n not in GND_NETS]
    if len(power) == 1:
        return power[0]
    if not power:
        return None
    labeled = [n for n in power if not n.startswith("Net-(")]
    return labeled[0] if labeled else power[0]


def _is_gnd_decoupling(fp: Footprint) -> bool:
    nets = {p.net_name for p in fp.pads if p.net_name}
    return bool(nets & GND_NETS) and bool(nets - GND_NETS)


def _profile_c_limits(value: str, footprint: str) -> Dict[str, Optional[float]]:
    profile = match_profile(value, footprint) or {}
    cin = cout = None
    cin_sec = cout_sec = None
    for fact in profile.get("facts") or []:
        fid = fact.get("id") or ""
        if fid == "cin_effective":
            cin = fact.get("recommend_min_uf")
            cin_sec = fact.get("section")
        elif fid == "cout_effective":
            cout = fact.get("recommend_min_uf")
            cout_sec = fact.get("section")
    return {
        "cin_min_uf": cin,
        "cout_min_uf": cout,
        "cin_section": cin_sec,
        "cout_section": cout_sec,
        "profile_id": profile.get("id"),
        "doc": profile.get("doc"),
    }


def _estimate_one(fp: Footprint, vdc: float) -> Dict[str, Any]:
    parsed = parse_cap_value(fp.value or "")
    case = case_from_footprint(fp.footprint or "")
    if parsed is None:
        return {
            "reference": fp.reference,
            "value": fp.value,
            "kind": "unknown",
            "c_nom_uf": None,
        }
    c_nom = parsed["c_nom_uf"]
    kind = "electrolytic" if case == "electrolytic" else "mlcc"
    dielectric = "C0G" if c_nom <= 0.001 and kind == "mlcc" else "X5R"
    vr = assume_v_rated(case, c_nom, parsed.get("v_rated_from_value"))
    frac = remaining_fraction(c_nom, case, vr, vdc, dielectric)
    ceff = round(c_nom * frac, 4)
    return {
        "reference": fp.reference,
        "value": fp.value,
        "footprint": fp.footprint,
        "kind": kind,
        "package": case,
        "c_nom_uf": round(c_nom, 4),
        "v_rated_assumed_v": vr,
        "v_rated_from_value": parsed.get("v_rated_from_value"),
        "dielectric_assumed": dielectric,
        "vdc_v": vdc,
        "remaining_fraction": round(frac, 3),
        "c_eff_uf": ceff,
        "at_mm": list(fp.at),
    }


def _catalog_ceff(part: Dict[str, Any], vdc: float) -> float:
    frac = remaining_fraction(
        part["c_nom_uf"],
        part.get("package"),
        float(part["v_rated"]),
        vdc,
        part.get("dielectric") or "X5R",
    )
    return round(part["c_nom_uf"] * frac, 4)


def suggest_jlc(
    package: Optional[str],
    vdc: float,
    n_present: int,
    other_ceff_uf: float,
    minimum_uf: float,
) -> List[Dict[str, Any]]:
    """JLC parts that can meet the bank minimum at Vdc (swap or add qty)."""
    n_present = max(0, int(n_present))
    catalog = _load_data().get("jlc_catalog") or []
    need = max(0.0, float(minimum_uf) - float(other_ceff_uf))
    out: List[Dict[str, Any]] = []
    for allow_upgrade, pkg in ((False, package), (True, PACKAGE_UPGRADE.get(package or ""))):
        if not pkg:
            continue
        ranked = []
        for part in catalog:
            if part.get("package") != pkg:
                continue
            ceff = _catalog_ceff(part, vdc)
            if ceff <= 0:
                continue
            n_need = max(1, math.ceil(need / ceff - 1e-12))
            bank = other_ceff_uf + n_need * ceff
            ranked.append((n_need, ceff, bank, part))
        ranked.sort(
            key=lambda x: (
                0 if x[0] <= max(1, n_present) else 1,
                x[0],
                0 if x[3].get("jlc_library") == "basic" else 1,
                -x[1],
            )
        )
        for n_need, ceff, bank, part in ranked[:3]:
            out.append(
                {
                    "lcsc": part["lcsc"],
                    "mpn": part.get("mpn"),
                    "mfr": part.get("mfr"),
                    "package": part["package"],
                    "c_nom_uf": part["c_nom_uf"],
                    "v_rated": part["v_rated"],
                    "dielectric": part.get("dielectric"),
                    "jlc_library": part.get("jlc_library"),
                    "c_eff_at_vdc_uf": ceff,
                    "bank_ceff_if_replaced_uf": round(bank, 4),
                    "qty": n_need,
                    "qty_present": n_present,
                    "same_package": not allow_upgrade,
                    "action": (
                        "swap"
                        if n_present > 0 and n_need <= n_present
                        else f"place {n_need} pcs"
                    ),
                    "note": part.get("note"),
                }
            )
        if out:
            break
    return out


def _bank_status(ceff: float, minimum: Optional[float]) -> Tuple[str, str]:
    if minimum is None:
        return "info", f"{ceff:.2f} uF effective (no datasheet CIN/COUT min)"
    if ceff + 1e-9 >= minimum:
        return "pass", f"{ceff:.2f} uF effective >= {minimum} uF min"
    if ceff >= 0.7 * minimum:
        return "warn", f"{ceff:.2f} uF effective < {minimum} uF min (within 30%)"
    return "fail", f"{ceff:.2f} uF effective < {minimum} uF min"


def analyze_mlcc_dc_bias(model: PcbModel) -> Dict[str, Any]:
    """Whole-board MLCC DC-bias banks for every buck VIN/VOUT."""
    data = _load_data()
    radius = float(data.get("local_radius_mm") or 12.0)
    meta = data.get("_meta") or {}

    caps: List[Dict[str, Any]] = []
    for fp in model.footprints:
        if not fp.reference.startswith("C"):
            continue
        parsed = parse_cap_value(fp.value or "")
        if parsed is None:
            continue
        if not _is_gnd_decoupling(fp):
            continue
        net = _power_net(fp)
        if not net:
            continue
        row = _estimate_one(fp, infer_rail_voltage(net) or 0.0)
        row["net"] = net
        row["vdc_from_net"] = infer_rail_voltage(net)
        caps.append(row)

    fp_by_ref = {f.reference: f for f in model.footprints}
    bucks = find_buck_ics(model)
    banks: List[Dict[str, Any]] = []
    for ic in bucks:
        nets = resolve_buck_nets(model, ic.reference)
        limits = _profile_c_limits(ic.value or "", ic.footprint or "")
        wave = waveform_defaults(ic, nets)
        vin_label_v = infer_rail_voltage(nets.get("vin"))
        vout_label_v = infer_rail_voltage(nets.get("vout"))
        role_vdc = {
            "VIN": vin_label_v if vin_label_v is not None else wave.get("v_in_assumed_v"),
            "VOUT": vout_label_v if vout_label_v is not None else wave.get("v_out_assumed_v"),
        }
        role_vdc_source = {
            "VIN": "net_label" if vin_label_v is not None else "ic_waveform_default",
            "VOUT": "net_label" if vout_label_v is not None else "ic_waveform_default",
        }
        for role, net, minimum in (
            ("VIN", nets.get("vin"), limits.get("cin_min_uf")),
            ("VOUT", nets.get("vout"), limits.get("cout_min_uf")),
        ):
            if not net:
                continue
            vdc = float(role_vdc.get(role) or 0.0)
            local = []
            for cap in caps:
                if cap.get("net") != net:
                    continue
                at = cap.get("at_mm") or [0, 0]
                d = math.hypot(at[0] - ic.at[0], at[1] - ic.at[1])
                if d > radius:
                    continue
                fp = fp_by_ref.get(cap["reference"])
                if not fp:
                    continue
                est = _estimate_one(fp, vdc)
                local.append({**est, "net": net, "distance_to_ic_mm": round(d, 3)})
            local.sort(key=lambda c: c["distance_to_ic_mm"])
            ceramics = [c for c in local if c.get("kind") == "mlcc"]
            electros = [c for c in local if c.get("kind") == "electrolytic"]
            ceff = round(sum(c.get("c_eff_uf") or 0.0 for c in ceramics), 4)
            cnom = round(sum(c.get("c_nom_uf") or 0.0 for c in ceramics), 4)
            status, notes = _bank_status(ceff, minimum)
            if electros:
                e_nom = round(sum(c.get("c_nom_uf") or 0.0 for c in electros), 4)
                notes += (
                    f"; electrolytic {','.join(c['reference'] for c in electros)} "
                    f"({e_nom} uF nom) not counted toward ceramic CIN/COUT"
                )
            suggestions: List[Dict[str, Any]] = []
            if status in ("fail", "warn") and minimum is not None:
                bulk = [c for c in ceramics if (c.get("c_nom_uf") or 0) >= 1.0]
                pkg = (
                    bulk[0].get("package")
                    if bulk
                    else (ceramics[0].get("package") if ceramics else "0805")
                )
                n_replace = len(bulk) or len(ceramics)
                bulk_ceff = sum(c.get("c_eff_uf") or 0.0 for c in bulk)
                other_ceff = max(0.0, ceff - bulk_ceff)
                suggestions = suggest_jlc(pkg, vdc, n_replace, other_ceff, minimum)
                if suggestions:
                    notes = (
                        notes
                        + f"; JLC candidates @ {vdc:g} V: "
                        + ", ".join(
                            f"{s['lcsc']} {s['c_nom_uf']}uF/{s['v_rated']}V "
                            f"x{s['qty']} ({s['action']}) ->{s['c_eff_at_vdc_uf']}uF ea"
                            + ("" if s.get("same_package") else f" ({s['package']} upgrade)")
                            for s in suggestions[:3]
                        )
                    )
                else:
                    notes += (
                        f"; no catalog {pkg} part reaches {minimum} uF at {vdc:g} V"
                    )
            banks.append(
                {
                    "source": "toolchain",
                    "ic_reference": ic.reference,
                    "ic_value": ic.value,
                    "role": role,
                    "net": net,
                    "vdc_v": vdc,
                    "vdc_source": role_vdc_source.get(role),
                    "minimum_uf": minimum,
                    "c_nom_sum_uf": cnom,
                    "c_eff_sum_uf": ceff,
                    "electrolytic_nom_uf": round(
                        sum(c.get("c_nom_uf") or 0.0 for c in electros), 4
                    ),
                    "local_radius_mm": radius,
                    "local_caps": [
                        {
                            "reference": c["reference"],
                            "value": c["value"],
                            "kind": c.get("kind"),
                            "package": c.get("package"),
                            "c_nom_uf": c.get("c_nom_uf"),
                            "c_eff_uf": c.get("c_eff_uf"),
                            "remaining_fraction": c.get("remaining_fraction"),
                            "v_rated_assumed_v": c.get("v_rated_assumed_v"),
                            "distance_to_ic_mm": c["distance_to_ic_mm"],
                        }
                        for c in local
                    ],
                    "status": status,
                    "notes": notes,
                    "jlc_suggestions": suggestions,
                    "profile_id": limits.get("profile_id"),
                    "doc": limits.get("doc"),
                }
            )

    worst = "pass"
    for b in banks:
        st = b.get("status")
        if st == "fail":
            worst = "fail"
        elif st == "warn" and worst == "pass":
            worst = "warn"

    return {
        "source": "toolchain",
        "model_id": meta.get("model_id"),
        "catalog_verified": bool(meta.get("verified")),
        "limitations": [
            "Remaining C uses conservative_class2_v1 (package + Cnom + assumed Vr + Vdc), "
            "not a vendor C-V curve.",
            "Vr is assumed from case/Cnom unless Value contains /16V.",
            "JLC LCSC IDs are a curated snapshot; stock/basic status changes.",
            "CIN/COUT minima come from datasheet_refs.json (unverified until marked).",
            "Local bank radius is 16 mm from IC center (covers inductor-side COUT rows).",
        ],
        "overall_status": worst,
        "capacitor_count": len(caps),
        "capacitors": caps,
        "banks": banks,
    }


def apply_mlcc_to_power_checks(
    power_checks: Sequence[Dict[str, Any]],
    mlcc: Dict[str, Any],
) -> None:
    """Fold VIN/VOUT bank results into per-IC power_layout_checks."""
    by = {(b["ic_reference"], b["role"]): b for b in mlcc.get("banks") or []}
    rank = {"fail": 0, "warn": 1, "pass": 2, "info": 3}
    for pc in power_checks:
        ref = pc.get("ic_reference")
        vin = by.get((ref, "VIN"))
        vout = by.get((ref, "VOUT"))
        if vin:
            pc["vin_ceff_uf"] = vin["c_eff_sum_uf"]
            pc["vin_cnom_uf"] = vin["c_nom_sum_uf"]
            pc["mlcc_vin_status"] = vin["status"]
            pc["mlcc_vin_notes"] = vin["notes"]
            pc["mlcc_vin_jlc"] = vin.get("jlc_suggestions")
        if vout:
            pc["vout_ceff_uf"] = vout["c_eff_sum_uf"]
            pc["vout_cnom_uf"] = vout["c_nom_sum_uf"]
            pc["mlcc_vout_status"] = vout["status"]
            pc["mlcc_vout_notes"] = vout["notes"]
            pc["mlcc_vout_jlc"] = vout.get("jlc_suggestions")
        for st in (pc.get("mlcc_vin_status"), pc.get("mlcc_vout_status")):
            if not st:
                continue
            pc["overall_status"] = min(
                (pc.get("overall_status") or "pass", st),
                key=lambda s: rank.get(s, 9),
            )
