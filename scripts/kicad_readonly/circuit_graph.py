"""Two-terminal connectivity graph on the PCB (schematic-equivalent).

KiCad auto-names unnamed nets (``Net-(C4-Pad2)``) whenever a series part
(typically a 0Ω jumper) sits between two functional nodes. Direct
net-equality checks then miss valid circuits such as BOOT—C—0Ω—SW.

This module walks 2-pin footprints. 0Ω resistors are treated as DC net
bridges; capacitors / resistors / inductors are typed edges. Output is
deterministic toolchain geometry — interpretation lives in
``design_judgments.py``.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .pcb import Footprint, PcbModel

Point = Tuple[float, float]

_ZERO_OHM_RE = re.compile(r"^0\s*(r|ohm|Ω)?$", re.IGNORECASE)
_CAP_VALUE_RE = re.compile(r"(\d+\.?\d*)\s*(p|n|u|µ|μ|m)?f\b", re.IGNORECASE)
_IND_VALUE_RE = re.compile(r"(\d+\.?\d*)\s*(n|u|µ|μ|m)?h\b", re.IGNORECASE)
_AUTO_NET_RE = re.compile(r"^Net-\(([^)]+)\)$")

# Pin-name tokens on KiCad auto nets: Net-(U3-BOOT) → BOOT
_PLANE_NETS = {"GND", "GNDA", "AGND", "PGND"}
# Pin-name tokens on KiCad auto nets: Net-(U3-BOOT) → BOOT
_PIN_ROLE_RE = re.compile(r"^Net-\((U\d+)-([A-Za-z0-9_]+)\)$")


def is_plane_or_rail_net(net_name: str) -> bool:
    n = net_name or ""
    if n in _PLANE_NETS:
        return True
    if n.startswith("+") or n.startswith("-"):
        return True
    if n.startswith("/"):
        return True
    return False


def is_zero_ohm(value: str) -> bool:
    v = (value or "").strip()
    return bool(_ZERO_OHM_RE.match(v)) or v in ("0", "0R", "0Ω", "0ohm")


def is_auto_named_net(net_name: str) -> bool:
    """True for KiCad-generated names like Net-(C4-Pad2) / Net-(U3-BOOT)."""
    return bool(_AUTO_NET_RE.match(net_name or ""))


def is_ic_pin_net(net_name: str) -> bool:
    """True for Net-(U3-BOOT) style IC-pin auto nets (functional pins, not series nodes)."""
    return bool(_PIN_ROLE_RE.match(net_name or ""))


def classify_passive(fp: Footprint) -> Optional[str]:
    """Return a kind for 2-pin parts, else None."""
    if len(fp.pads) != 2:
        return None
    val = fp.value or ""
    name = (fp.footprint or "").lower()
    ref = (fp.reference or "").upper()
    if is_zero_ohm(val):
        return "zero_ohm"
    if _CAP_VALUE_RE.search(val) or "capacitor" in name or ref.startswith("C"):
        return "capacitor"
    if "ferrite" in name or ref.startswith("FB"):
        return "ferrite"
    if _IND_VALUE_RE.search(val) or "inductor" in name or ref.startswith("L"):
        return "inductor"
    if "fuse" in name or (ref.startswith("F") and not ref.startswith("FB")):
        return "fuse"
    if "diode" in name or ref.startswith("D"):
        return "diode"
    if "resistor" in name or ref.startswith("R"):
        return "resistor"
    return "other"


def _pad_nets(fp: Footprint) -> List[str]:
    return [p.net_name for p in fp.pads if p.net_name]


def two_pin_edges(model: PcbModel) -> List[Dict[str, Any]]:
    """All 2-pin passives as typed net-to-net edges."""
    edges: List[Dict[str, Any]] = []
    for fp in model.footprints:
        kind = classify_passive(fp)
        if kind is None:
            continue
        nets = _pad_nets(fp)
        if len(nets) != 2 or nets[0] == nets[1]:
            continue
        pads = [
            {"pad": p.number, "net_name": p.net_name, "at_mm": list(p.at)}
            for p in fp.pads
            if p.net_name
        ]
        edges.append(
            {
                "reference": fp.reference,
                "value": fp.value,
                "kind": kind,
                "net_a": nets[0],
                "net_b": nets[1],
                "at_mm": list(fp.at),
                "pads": pads,
            }
        )
    return edges


def _adjacency(edges: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    adj: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for e in edges:
        adj[e["net_a"]].append(e)
        adj[e["net_b"]].append(e)
    return adj


def other_net(edge: Dict[str, Any], net: str) -> Optional[str]:
    if net == edge["net_a"]:
        return edge["net_b"]
    if net == edge["net_b"]:
        return edge["net_a"]
    return None


def zero_ohm_equivalent_nets(model: PcbModel, seed: str) -> Set[str]:
    """Nets reachable from ``seed`` walking only 0Ω jumpers."""
    adj = _adjacency([e for e in two_pin_edges(model) if e["kind"] == "zero_ohm"])
    seen = {seed}
    q = deque([seed])
    while q:
        cur = q.popleft()
        for e in adj.get(cur, []):
            nxt = other_net(e, cur)
            if nxt and nxt not in seen:
                seen.add(nxt)
                q.append(nxt)
    return seen


def find_series_passive_path(
    model: PcbModel,
    start_net: str,
    end_net: str,
    *,
    required_kind: str = "capacitor",
    extra_kinds: Sequence[str] = ("zero_ohm",),
    max_hops: int = 6,
) -> Optional[Dict[str, Any]]:
    """Shortest path from start_net to end_net using required_kind once plus extra_kinds.

    Typical BOOT bootstrap: BOOT — capacitor — auto-net — 0Ω — SW.
    """
    if not start_net or not end_net:
        return None
    all_edges = two_pin_edges(model)
    allowed = {required_kind, *extra_kinds}
    adj = _adjacency([e for e in all_edges if e["kind"] in allowed])
    edges_by_ref = {e["reference"]: e for e in all_edges}
    # state: net, required_used, tuple of refs
    q: deque[Tuple[str, bool, Tuple[str, ...]]] = deque()
    q.append((start_net, False, ()))
    seen: Set[Tuple[str, bool, Tuple[str, ...]]] = {(start_net, False, ())}

    while q:
        net, used, refs = q.popleft()
        if net == end_net and used and refs:
            path_edges = [edges_by_ref[r] for r in refs if r in edges_by_ref]
            auto_nets = [
                n
                for n in _path_nets(start_net, path_edges)
                if n not in (start_net, end_net) and is_auto_named_net(n)
            ]
            return {
                "start_net": start_net,
                "end_net": end_net,
                "hops": len(path_edges),
                "edges": path_edges,
                "path_nets": _path_nets(start_net, path_edges),
                "path_text": _format_path(start_net, path_edges),
                "required_parts": [
                    e for e in path_edges if e["kind"] == required_kind
                ],
                "zero_ohm_parts": [e for e in path_edges if e["kind"] == "zero_ohm"],
                "intermediate_auto_nets": auto_nets,
                "topology": (
                    "direct_" + required_kind
                    if not any(e["kind"] == "zero_ohm" for e in path_edges)
                    else required_kind + "_plus_zero_ohm"
                ),
            }
        if len(refs) >= max_hops:
            continue
        for e in adj.get(net, []):
            if e["reference"] in refs:
                continue
            nxt = other_net(e, net)
            if not nxt:
                continue
            next_used = used
            if e["kind"] == required_kind:
                if used:
                    continue
                next_used = True
            elif e["kind"] not in extra_kinds:
                continue
            nxt_refs = refs + (e["reference"],)
            key = (nxt, next_used, nxt_refs)
            if key in seen:
                continue
            seen.add(key)
            q.append((nxt, next_used, nxt_refs))
    return None


def _path_nets(start: str, edges: Sequence[Dict[str, Any]]) -> List[str]:
    nets = [start]
    cur = start
    for e in edges:
        nxt = other_net(e, cur)
        if nxt is None:
            break
        nets.append(nxt)
        cur = nxt
    return nets


def _format_path(start: str, edges: Sequence[Dict[str, Any]]) -> str:
    parts = [start]
    cur = start
    for e in edges:
        nxt = other_net(e, cur)
        parts.append(f"{e['reference']}({e['value']}/{e['kind']})")
        if nxt:
            parts.append(nxt)
            cur = nxt
    return " — ".join(parts)


def _ic_pin_roles(ic: Footprint) -> Dict[str, str]:
    """Map functional pin roles to net names from IC pads."""
    roles: Dict[str, str] = {}
    for pad in ic.pads:
        n = pad.net_name or ""
        if not n:
            continue
        m = _PIN_ROLE_RE.match(n)
        if m and m.group(1) == ic.reference:
            roles.setdefault(m.group(2).upper(), n)
        elif n in ("GNDA", "AGND"):
            roles.setdefault("AGND", n)
        elif n == "GND":
            roles.setdefault("PGND", n)
    if "SW" not in roles and "LX" in roles:
        roles["SW"] = roles["LX"]
    return roles


def _parts_on_nets(edges: Sequence[Dict[str, Any]], nets: Iterable[str]) -> List[Dict[str, Any]]:
    want = set(nets)
    out = []
    for e in edges:
        if e["net_a"] in want or e["net_b"] in want:
            out.append(e)
    return out


def neighborhood_for_net(
    model: PcbModel,
    seed_net: str,
    extra_hops: int = 1,
) -> Dict[str, Any]:
    """2-pin parts on seed_net, plus companions on unnamed/series nets.

    Does **not** walk into GND / power-rail nets — otherwise hop-1 from a VIN
    decoupling cap would pull in every GND part on the board.
    """
    edges = two_pin_edges(model)
    adj = _adjacency(edges)
    seed_parts = list(adj.get(seed_net, []))
    companion_nets: Set[str] = set()
    for e in seed_parts:
        other = other_net(e, seed_net)
        if other and other != seed_net and not is_plane_or_rail_net(other):
            # Only follow unnamed series nodes, not other IC pin nets
            # (Net-(U3-EN) is auto-named but is a functional pin).
            if is_auto_named_net(other) and not _PIN_ROLE_RE.match(other):
                companion_nets.add(other)

    hop_parts = list(seed_parts)
    if extra_hops >= 1:
        for n in companion_nets:
            hop_parts.extend(adj.get(n, []))

    seen_refs = set()
    unique: List[Dict[str, Any]] = []
    for e in hop_parts:
        if e["reference"] in seen_refs:
            continue
        seen_refs.add(e["reference"])
        unique.append(e)
    reached = {seed_net} | companion_nets
    auto = sorted(n for n in reached if is_auto_named_net(n) and n != seed_net)
    return {
        "seed_net": seed_net,
        "reached_nets": sorted(reached),
        "auto_named_nets": auto,
        "parts": unique,
    }


def _min_pad_distance(fp_edge: Dict[str, Any], anchor: Point) -> float:
    best = float("inf")
    for p in fp_edge.get("pads", []):
        at = p.get("at_mm") or [0, 0]
        best = min(best, math.hypot(at[0] - anchor[0], at[1] - anchor[1]))
    if best == float("inf") and fp_edge.get("at_mm"):
        at = fp_edge["at_mm"]
        best = math.hypot(at[0] - anchor[0], at[1] - anchor[1])
    return best


def find_boot_cap_on_path(
    model: PcbModel,
    ic: Footprint,
    boot_net: str,
    sw_net: str,
    boot_pad: Optional[Point],
) -> Optional[Dict[str, Any]]:
    """Locate the BOOT bootstrap capacitor, allowing series 0Ω jumpers."""
    path = find_series_passive_path(
        model,
        boot_net,
        sw_net,
        required_kind="capacitor",
        extra_kinds=("zero_ohm",),
        max_hops=6,
    )
    if not path or not path["required_parts"]:
        return None
    cap = path["required_parts"][0]
    zero = path["zero_ohm_parts"]
    dist = None
    if boot_pad is not None:
        dist = round(_min_pad_distance(cap, boot_pad), 3)
    jumper_sw_mm = None
    if boot_pad is not None and zero:
        jumper_sw_mm = round(min(_min_pad_distance(z, boot_pad) for z in zero), 3)
    return {
        "reference": cap["reference"],
        "value": cap["value"],
        "kind": cap["kind"],
        "pad_at_mm": (cap["pads"][0]["at_mm"] if cap.get("pads") else cap["at_mm"]),
        "distance_mm": dist,
        "topology": path["topology"],
        "path_text": path["path_text"],
        "series_zero_ohm": [z["reference"] for z in zero],
        "series_zero_ohm_values": [z["value"] for z in zero],
        "intermediate_auto_nets": path["intermediate_auto_nets"],
        "jumper_distance_from_boot_pad_mm": jumper_sw_mm,
    }


def _fb_divider(model: PcbModel, fb_net: str) -> Dict[str, Any]:
    """Resistors/caps hanging off FB (and one extra hop) — typical divider + feedforward."""
    nb = neighborhood_for_net(model, fb_net, extra_hops=2)
    resistors = [p for p in nb["parts"] if p["kind"] == "resistor"]
    caps = [p for p in nb["parts"] if p["kind"] == "capacitor"]
    to_gnd = [
        p
        for p in resistors
        if "GND" in (p["net_a"], p["net_b"]) or "GNDA" in (p["net_a"], p["net_b"]) or "AGND" in (p["net_a"], p["net_b"])
    ]
    return {
        "fb_net": fb_net,
        "resistors": resistors,
        "capacitors": caps,
        "gnd_leg": to_gnd,
        "auto_named_nets": nb["auto_named_nets"],
    }


def analyze_ic_circuit(model: PcbModel, ic_ref: str) -> Dict[str, Any]:
    """Toolchain neighborhood around one IC's functional pins."""
    ic = next((f for f in model.footprints if f.reference == ic_ref), None)
    if ic is None:
        return {"ic_reference": ic_ref, "error": "footprint not found"}
    from .buck_nets import resolve_buck_nets

    roles = _ic_pin_roles(ic)
    nets = resolve_buck_nets(model, ic_ref)
    if nets.get("sw"):
        roles["SW"] = nets["sw"]
    if nets.get("lx"):
        roles["LX"] = nets["lx"]
    if nets.get("fb"):
        roles["FB"] = nets["fb"]
    if nets.get("vin"):
        roles["VIN"] = nets["vin"]
    if nets.get("vout"):
        roles["VOUT"] = nets["vout"]
    if nets.get("en"):
        roles["EN"] = nets["en"]
    if nets.get("boot"):
        roles["BOOT"] = nets["boot"]

    boot_net = nets.get("boot")
    sw_net = nets.get("sw") or roles.get("SW")
    fb_net = nets.get("fb") or roles.get("FB")
    vin_net = nets.get("vin") or roles.get("VIN")
    en_net = nets.get("en") or roles.get("EN")

    boot_pad = None
    if boot_net:
        for pad in ic.pads:
            if pad.net_name == boot_net:
                boot_pad = pad.at
                break

    boot_path = (
        find_boot_cap_on_path(model, ic, boot_net, sw_net, boot_pad)
        if boot_net and sw_net
        else None
    )

    pin_nb = {}
    for role, net in (
        ("BOOT", boot_net),
        ("SW", sw_net),
        ("FB", fb_net),
        ("VIN", vin_net),
        ("EN", en_net),
    ):
        if not net:
            continue
        pin_nb[role] = neighborhood_for_net(model, net, extra_hops=1)

    return {
        "source": "toolchain",
        "ic_reference": ic_ref,
        "ic_value": ic.value,
        "pin_nets": roles,
        "boot_path": boot_path,
        "fb_divider": _fb_divider(model, fb_net) if fb_net else None,
        "pin_neighborhoods": pin_nb,
    }
