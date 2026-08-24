"""Session LRU eviction behaviour for ``RiskSourcePlanningIngress``."""

from __future__ import annotations

from arctic_route_planning.config import load_configuration
from arctic_route_planning.contracts import InMemoryRiskSource
from arctic_route_planning.ingress import RiskSourcePlanningIngress

_CONFIG_ROOT = "configs"


def _build_ingress() -> RiskSourcePlanningIngress:
    configuration = load_configuration(
        _CONFIG_ROOT,
        "tromso_isfjorden_july_2026_retrospective_v1",
    )
    return RiskSourcePlanningIngress(InMemoryRiskSource(), configuration=configuration)


def test_session_lru_evicts_oldest(monkeypatch) -> None:
    """Once ``_MAX_SESSIONS`` is exceeded the oldest session is dropped."""

    ingress = _build_ingress()
    monkeypatch.setattr(
        "arctic_route_planning.ingress._MAX_SESSIONS", 2
    )

    ingress._session_for(run_id="run-a", scenario_id="s")
    ingress._session_for(run_id="run-b", scenario_id="s")
    # Accessing a third distinct run must evict ``first`` (the oldest).
    ingress._session_for(run_id="run-c", scenario_id="s")

    keys = list(ingress._sessions.keys())
    assert keys == [("run-b", "s"), ("run-c", "s")]
    assert ("run-a", "s") not in ingress._sessions


def test_session_lru_promotes_on_reuse(monkeypatch) -> None:
    """Reusing a session moves it to the most-recently-used end."""

    ingress = _build_ingress()
    monkeypatch.setattr(
        "arctic_route_planning.ingress._MAX_SESSIONS", 2
    )

    ingress._session_for(run_id="run-a", scenario_id="s")
    ingress._session_for(run_id="run-b", scenario_id="s")
    # Reuse ``run-a``; it must now be more recent than ``run-b``.
    ingress._session_for(run_id="run-a", scenario_id="s")
    ingress._session_for(run_id="run-c", scenario_id="s")

    keys = list(ingress._sessions.keys())
    assert keys == [("run-a", "s"), ("run-c", "s")]
