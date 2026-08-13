from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import PlanRequest
from arctic_route_planning.domain import GeoPoint, ObjectiveMode
from arctic_route_planning.errors import ContractError

CONFIG_ROOT = Path(__file__).parents[2] / "configs"
START = datetime(2026, 7, 15, tzinfo=UTC)


def _request(scenario_id: str, *, as_of_time: datetime) -> PlanRequest:
    configuration = load_configuration(
        CONFIG_ROOT,
        scenario_id,
        simulation_start=(START if "frozen_forecast" in scenario_id else None),
    )
    return PlanRequest(
        scenario=configuration.scenario,
        vessel=configuration.vessel,
        run_id="run-contract-test",
        config_digest="a" * 64,
        model_config_digest="b" * 64,
        planner_config_digest="c" * 64,
        generation_id=0,
        planning_request_id="request-1",
        input_revision=0,
        as_of_time=as_of_time,
        start_time=START,
        start=GeoPoint(19.0, 69.75),
        destination=GeoPoint(13.0, 78.15),
        objective_mode=ObjectiveMode.RECOMMENDED,
    )


def test_plan_request_allows_retrospective_knowledge_after_simulation_time() -> None:
    request = _request(
        "tromso_isfjorden_july_2026_retrospective_v1",
        as_of_time=START + timedelta(days=28),
    )

    assert request.as_of_time > request.start_time


def test_plan_request_rejects_frozen_forecast_knowledge_after_departure() -> None:
    with pytest.raises(ContractError, match="frozen_forecast"):
        _request(
            "tromso_isfjorden_frozen_forecast_template_v1",
            as_of_time=START + timedelta(seconds=1),
        )
