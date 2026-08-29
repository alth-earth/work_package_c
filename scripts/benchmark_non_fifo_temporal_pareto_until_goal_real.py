#!/usr/bin/env python3
"""Research-only real diagnostic for heuristic incumbent seeding.

The audited M27 runner is reused for fixture, manifest, and resource handling;
this wrapper selects ``until_goal`` so the certified heuristic is active only
until the first terminal label, after which the historical queue order is
restored.  The path remains a C-internal ordering diagnostic.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "c.p0.2-nonfifo-pareto-until-goal-real.v1"
os.environ.setdefault("C_PARETO_SCHEMA_VERSION", SCHEMA_VERSION)
os.environ.setdefault("C_PARETO_MILESTONE", "P0.2-M29")


def _load_m27() -> Any:
    path = Path(__file__).with_name("benchmark_non_fifo_temporal_pareto_heuristic_real.py")
    spec = importlib.util.spec_from_file_location("c_m29_m27_heuristic_runner", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load audited M27 heuristic runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_M27 = _load_m27()
_M27.IMPLEMENTATION_FILES = tuple(
    dict.fromkeys(
        (
            "scripts/benchmark_non_fifo_temporal_pareto_until_goal_real.py",
            *_M27.IMPLEMENTATION_FILES,
        )
    )
)


def main() -> int:
    argv = sys.argv[1:]
    if "--heuristic-ordering" not in argv:
        sys.argv.extend(("--heuristic-ordering", "until_goal"))
    return _M27.main()


if __name__ == "__main__":
    raise SystemExit(main())
