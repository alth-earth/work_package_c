from __future__ import annotations

import hashlib
import json

import pytest

from arctic_route_planning.motion.cli import (
    _assert_all_curve_records,
    _publish,
    build_parser,
)


def test_route_motion_cli_requires_all_explicit_identity_inputs() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_route_motion_cli_publishes_immutable_checksummed_directory(tmp_path) -> None:
    target = tmp_path / "motion"
    documents = {
        "route-motion-set.json": {"schema_version": "cd.route-motion-set.v1"},
        "route-motion-vessel-profile.json": {
            "schema_version": "c.route-motion-vessel-profile.v1"
        },
    }

    _publish(target, documents)

    checksums = json.loads((target / "checksums.json").read_text(encoding="utf-8"))
    for name, document in documents.items():
        assert json.loads((target / name).read_text(encoding="utf-8")) == document
        assert checksums[name] == hashlib.sha256((target / name).read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="already exists"):
        _publish(target, documents)


def test_strict_publication_requires_four_recommended_and_three_candidate_curves() -> None:
    documents = {
        "route-motion-set.json": {
            "records": [{"mode": "CURVE", "plan_id": f"recommended-{index}"}
                        for index in range(4)]
        },
        "route-motion-candidate-set.json": {
            "records": [
                {"record": {"mode": "CURVE", "plan_id": f"candidate-{index}"}}
                for index in range(3)
            ]
        },
    }

    _assert_all_curve_records(documents)

    documents["route-motion-candidate-set.json"]["records"][1]["record"]["mode"] = (
        "RAW_PASSTHROUGH"
    )
    with pytest.raises(ValueError, match=r"candidate\[1\]=.*RAW_PASSTHROUGH"):
        _assert_all_curve_records(documents)
