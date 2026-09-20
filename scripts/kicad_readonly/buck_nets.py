"""Resolve buck converter pin nets (SW/LX, FB, VIN, BOOT, VOUT).

KiCad auto-names the switch node `Net-(U3-SW)` on TPS56637 and
`Net-(U7-LX)` on SY8843. Downstream SW-FB / leftover / SI code should
call these helpers instead of hard-coding `-SW`.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .pcb import Footprint, PcbModel

_PIN_ROLE_RE = re.compile(r"^Net-\((U\d+)-([A-Za-z0-9_]+)\)$")
_RAIL_V_RE = re.compile(r"^\+?(\d+(?:\.\d+)?)\s*V$", re.IGNORECASE)
_RAIL_V_SEARCH_RE = re.compile(r"(\d+(?:\.\d+)?)\s*V", re.IGNORECASE)

BUCK_VALUE_MARKERS = ("TPS56637", "SY8843")

SWITCH_ALIASES = ("SW", "LX")
FB_ALIASES = ("FB",)
VIN_ALIASES = ("VIN", "IN", "PVIN", "VM")
BOOT_ALIASES = ("BOOT", "BST", "BOOTSTRAP")
VOUT_ALIASES = ("VOUT", "OUT", "VO")
EN_ALIASES = ("EN", "ENABLE")

GND_NETS = {"GND", "GNDA", "AGND", "PGND", "SGND"}

WAVEFORM_BY_MARKER = {
    "TPS56637": {
        "f_sw_hz": 500_000,
        "t_on_min_ns": 50,
        "v_in_assumed_v": 24.0,
        "v_out_assumed_v": 5.0,
        "i_out_assumed_a": 2.0,
        "l_inductor_uh": 3.3,
    },
    "SY8843": {
        "f_sw_hz": 1_500_000,
        "t_on_min_ns": 50,
        "v_in_assumed_v": 5.0,
        "v_out_assumed_v": 1.0,
        "i_out_assumed_a": 1.0,
        "l_inductor_uh": 1.5,
    },
}

_RESOLVE_CACHE: Dict[Tuple[int, str], Dict[str, Any]] = {}


def pin_role_nets(ic: Footprint) -> Dict[str, str]:
    """Map Net-(U#-ROLE) pad nets to ROLE."""
    roles: Dict[str, str] = {}
    for pad in ic.pads:
        n = pad.net_name or ""
        if not n:
            continue
        m = _PIN_ROLE_RE.match(n)
        if m and m.group(1) == ic.reference:
            roles.setdefault(m.group(2).upper(), n)
    return roles


def is_buck_footprint(fp: Footprint) -> bool:
    blob = f"{fp.value or ''} {fp.footprint or ''}"
    if any(m.lower() in blob.lower() for m in BUCK_VALUE_MARKERS):
        return True
    roles = pin_role_nets(fp)
    return (("SW" in roles) or ("LX" in roles)) and ("FB" in roles)


def find_buck_ics(
    model: PcbModel, region_refs: Optional[Sequence[str]] = None
) -> List[Footprint]:
    bucks = [fp for fp in model.footprints if is_buck_footprint(fp)]
    if region_refs:
        ref_set = set(region_refs)
        bucks = [fp for fp in bucks if fp.reference in ref_set]
    return sorted(bucks, key=lambda f: f.reference)


def infer_rail_voltage(net_name: Optional[str]) -> Optional[float]:
    if not net_name:
        return None
    n = net_name.strip()
    m = _RAIL_V_RE.match(n)
    if m:
        return float(m.group(1))
    m2 = _RAIL_V_SEARCH_RE.search(n)
    if m2:
        return float(m2.group(1))
    return None


def waveform_defaults(
    ic: Optional[Footprint],
    nets: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Any]:
    blob = f"{(ic.value if ic else '')} {(ic.footprint if ic else '')}"
    params = dict(WAVEFORM_BY_MARKER["TPS56637"])
    for marker, prof in WAVEFORM_BY_MARKER.items():
        if marker.lower() in blob.lower():
            params = dict(prof)
            break
    nets = nets or {}
    vin_v = infer_rail_voltage(nets.get("vin"))
    vout_v = infer_rail_voltage(nets.get("vout"))
    if vin_v is not None:
        params["v_in_assumed_v"] = vin_v
    if vout_v is not None:
        params["v_out_assumed_v"] = vout_v
    return params


def _vout_from_inductor(
    model: PcbModel,
    sw_net: Optional[str],
    ic: Footprint,
) -> Optional[str]:
    """TPS-style bucks have no VOUT pin: SW/LX → inductor → output rail."""
    if not sw_net:
        return None
    best_net = None
    best_d = 1e9
    for fp in model.footprints:
        ref = (fp.reference or "").upper()
        if not ref.startswith("L") or ref.startswith("LED"):
            continue
        nets = [p.net_name for p in fp.pads if p.net_name]
        if sw_net not in nets:
            continue
        other = [n for n in nets if n != sw_net and n not in GND_NETS]
        if len(other) != 1:
            continue
        d = math.hypot(fp.at[0] - ic.at[0], fp.at[1] - ic.at[1])
        if d < best_d:
            best_d = d
            best_net = other[0]
    return best_net


def _role_from_aliases(roles: Dict[str, str], aliases: Sequence[str]) -> Optional[str]:
    for alias in aliases:
        if alias in roles:
            return roles[alias]
    return None


def resolve_buck_nets(model: PcbModel, ic_ref: str) -> Dict[str, Any]:
    key = (id(model), ic_ref)
    cached = _RESOLVE_CACHE.get(key)
    if cached is not None:
        return cached

    fallback: Dict[str, Any] = {
        "sw": f"Net-({ic_ref}-SW)",
        "fb": f"Net-({ic_ref}-FB)",
        "vin": f"Net-({ic_ref}-VIN)",
        "boot": None,
        "vout": None,
        "en": None,
        "lx": None,
        "has_boot_pin": False,
        "switch_alias": "SW",
    }
    ic = next((f for f in model.footprints if f.reference == ic_ref), None)
    if ic is None:
        fallback["has_boot_pin"] = False
        _RESOLVE_CACHE[key] = fallback
        return fallback

    roles = pin_role_nets(ic)
    pads = {str(p.number): p for p in ic.pads}

    switch_alias = "SW"
    sw = _role_from_aliases(roles, SWITCH_ALIASES)
    if sw:
        for alias in SWITCH_ALIASES:
            if alias in roles:
                switch_alias = alias
                break
    else:
        sw = f"Net-({ic_ref}-SW)"

    fb = _role_from_aliases(roles, FB_ALIASES) or f"Net-({ic_ref}-FB)"
    boot = _role_from_aliases(roles, BOOT_ALIASES)
    vin = _role_from_aliases(roles, VIN_ALIASES)
    vout = _role_from_aliases(roles, VOUT_ALIASES)
    en = _role_from_aliases(roles, EN_ALIASES)

    # SY8843-class SOT-23-6 + EP: 1=FB 2=VOUT 4=EN 5=VIN 6=LX 7=GND
    lx_pad = pads.get("6")
    lx_net = (lx_pad.net_name or "") if lx_pad else ""
    if "LX" in lx_net.upper():
        if vin is None:
            p5 = pads.get("5")
            if p5 and p5.net_name and p5.net_name not in GND_NETS:
                vin = p5.net_name
        if vout is None:
            p2 = pads.get("2")
            if p2 and p2.net_name and p2.net_name not in GND_NETS:
                vout = p2.net_name
        if en is None:
            p4 = pads.get("4")
            n4 = (p4.net_name if p4 else "") or ""
            n4u = n4.upper()
            looks_pg = (
                n4u.endswith("_OK")
                or "PGOOD" in n4u
                or n4u in ("PG", "PGOOD", "PWRGD")
            )
            if p4 and n4 and n4 not in GND_NETS and not looks_pg:
                en = n4

    if vout is None:
        vout = _vout_from_inductor(model, sw, ic)

    out: Dict[str, Any] = {
        "sw": sw,
        "fb": fb,
        "vin": vin,
        "boot": boot,
        "vout": vout,
        "en": en,
        "lx": roles.get("LX"),
        "has_boot_pin": bool(boot),
        "switch_alias": switch_alias,
    }
    _RESOLVE_CACHE[key] = out
    return out


def has_boot_pin(model: PcbModel, ic_ref: str) -> bool:
    return bool(resolve_buck_nets(model, ic_ref).get("boot"))


def switch_net_name(model: PcbModel, ic_ref: str) -> str:
    return resolve_buck_nets(model, ic_ref)["sw"] or f"Net-({ic_ref}-SW)"


def fb_net_name(model: PcbModel, ic_ref: str) -> str:
    return resolve_buck_nets(model, ic_ref)["fb"] or f"Net-({ic_ref}-FB)"
