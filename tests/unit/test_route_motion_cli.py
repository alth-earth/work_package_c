from __future__ import annotations

import hashlib
import json

import pytest

from arctic_route_planning.motion.cli import _publish, build_parser


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
