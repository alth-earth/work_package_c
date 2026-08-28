"""Tests for the proof-carrying graph-topological arrival bound."""

from __future__ import annotations

from arctic_route_planning.planners.temporal_topology_bounds import (
    qualify_topological_lower_bound,
)

NODES = ((0, 0), (0, 1), (1, 1), (1, 2))
EDGES = {
    (0, 0): ((0, 1),),
    (0, 1): ((1, 1),),
    (1, 1): ((1, 2),),
    (1, 2): (),
}


def _scope(**updates: str) -> dict[str, str]:
    return {"edge_evaluator_digest": "explicit:grid-edge-v1", **updates}


def _qualify(*, scope=None, neighbors=None, distance=None, speed=10.0):
    return qualify_topological_lower_bound(
        scope=scope or _scope(),
        universe_nodes=NODES,
        start=NODES[0],
        goal=NODES[-1],
        neighbors=neighbors or (lambda node: EDGES[node]),
        edge_distance_km=distance or (lambda _start, _end: 10.0),
        max_speed_km_per_hour=speed,
    )


def test_graph_lower_bounds_are_complete_and_adapt_to_corridor_evidence() -> None:
    evidence = _qualify()

    assert evidence.usable
    assert evidence.forward_map[NODES[-1]] < 3.0
    assert evidence.reverse_map[NODES[0]] < 3.0
    adapted = evidence.as_admissible_bound_evidence()
    assert adapted.usable(evidence.scope)
    assert evidence.digest
    assert evidence.proof_digest


def test_topology_uses_directed_reverse_distances() -> None:
    edges = {
        (0, 0): ((0, 1),),
        (0, 1): ((1, 1),),
        (1, 1): ((1, 2),),
        (1, 2): (),
    }
    evidence = _qualify(neighbors=lambda node: edges[node])

    assert evidence.usable
    assert evidence.forward_map[(0, 1)] < evidence.forward_map[(1, 1)]
    assert evidence.reverse_map[(1, 1)] < evidence.reverse_map[(0, 1)]


def test_outside_adjacency_is_rejected_without_a_partial_bound() -> None:
    evidence = _qualify(neighbors=lambda node: ((99, 99),) if node == NODES[1] else EDGES[node])

    assert not evidence.usable
    assert evidence.reason == "adjacency_outside_universe"
    assert evidence.forward_lower_hours == ()
    assert evidence.reverse_lower_hours == ()


def test_evaluator_failure_and_unreachable_domain_fail_closed() -> None:
    failed = _qualify(
        distance=lambda _start, _end: (_ for _ in ()).throw(RuntimeError("edge failed"))
    )
    disconnected = _qualify(neighbors=lambda node: () if node == NODES[1] else EDGES[node])

    assert not failed.usable
    assert failed.reason == "evaluator_failure:RuntimeError"
    assert not disconnected.usable
    assert disconnected.reason == "unreachable_domain"


def test_unknown_scope_or_invalid_speed_cannot_authorize_bound() -> None:
    unknown = _qualify(scope=_scope(edge_evaluator_digest="unknown:mutable"))
    invalid_speed = _qualify(speed=float("nan"))

    assert not unknown.usable
    assert unknown.reason == "unknown_evaluator"
    assert not invalid_speed.usable
    assert invalid_speed.reason == "invalid_max_speed"
