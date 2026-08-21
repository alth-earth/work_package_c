#!/usr/bin/env python3
"""Profile C's real planner on an explicitly synthetic small fixture."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from arctic_route_planning.profiling import (
    SyntheticProfileConfig,
    profile_synthetic_three_objective_planning,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=9)
    parser.add_argument("--cols", type=int, default=13)
    parser.add_argument("--frames", type=int, default=13)
    parser.add_argument("--spacing-degrees", type=float, default=0.05)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = profile_synthetic_three_objective_planning(
        SyntheticProfileConfig(
            rows=args.rows,
            cols=args.cols,
            frame_count=args.frames,
            spacing_degrees=args.spacing_degrees,
        )
    )
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
