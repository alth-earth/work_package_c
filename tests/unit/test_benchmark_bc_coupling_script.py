from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from arctic_route_planning.cost import CostBreakdown

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "benchmark_bc_coupling.py"
_SPEC = importlib.util.spec_from_file_location("c_benchmark_bc_coupling", _SCRIPT_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("unable to load B/C coupling benchmark script")
_SCRIPT = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _SCRIPT
_SPEC.loader.exec_module(_SCRIPT)


def test_route_digest_semantic_value_serializes_cost_breakdown() -> None:
    value = CostBreakdown(
        travel_hours=1.0,
        risk_exposure_hours=0.2,
        distance_equivalent_hours=0.5,
        turn_equivalent_hours=0.0,
        deviation_equivalent_hours=0.0,
        low_confidence_hours=0.1,
        total_equivalent_hours=1.8,
    )

    document = _SCRIPT._semantic_value(value)

    assert document["total_equivalent_hours"] == 1.8
    assert json.loads(json.dumps(document)) == document
