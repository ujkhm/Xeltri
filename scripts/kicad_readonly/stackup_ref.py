"""Stackup-adjacent reference plane assignment for return-path checks."""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

Point = Tuple[float, float]


def primary_reference_plane(signal_layer: str, copper_layers: Sequence[str]) -> str:
    """
    Nearest inner reference plane for microstrip on outer layers.

    6-layer example F.Cu/In1/.../In4/B.Cu:
      F.Cu -> In1.Cu
      B.Cu -> In4.Cu
    """
    if not copper_layers:
        return signal_layer
    if signal_layer not in copper_layers:
        return copper_layers[min(1, len(copper_layers) - 1)]
    i = copper_layers.index(signal_layer)
    n = len(copper_layers)
    if i == 0:
        return copper_layers[1]
    if i == n - 1:
        return copper_layers[n - 2]
    # Inner signal: prefer adjacent plane toward board center
    if i <= n // 2:
        return copper_layers[i + 1]
    return copper_layers[i - 1]


def reference_planes_for_layer(
    signal_layer: str,
    copper_layers: Sequence[str],
) -> Dict[str, str]:
    """
    Return reference roles for a signal layer.

    - primary_ref: inner plane directly below (top) or above (bottom) the signal
    - coplanar_ref: same copper layer (GND pour on F.Cu / B.Cu)
    """
    primary = primary_reference_plane(signal_layer, copper_layers)
    return {
        "signal_layer": signal_layer,
        "primary_ref": primary,
        "coplanar_ref": signal_layer,
    }


def build_stackup_reference_map(copper_layers: Sequence[str]) -> Dict[str, Dict[str, str]]:
    return {
        layer: reference_planes_for_layer(layer, copper_layers)
        for layer in copper_layers
    }
