"""Tests for the C-internal ETA interval qualification sidecar."""

from __future__ import annotations

import pytest

from arctic_route_planning.planners.eta_interval import (
    EtaInterval,
    EtaIntervalStatus,
    partition_eta_domain,
    qualify_eta_interval,
    qualify_eta_partition,
)


def test_unverified_interval_evaluator_never_becomes_a_root_certificate() -> None:
    certificate = qualify_eta_interval(
        EtaInterval(1.0, 2.0),
        lambda _domain: EtaInterval(1.2, 1.8),
        scope={"input": "fixture"},
        coverage_complete=True,
        evaluator_certified=False,
        contraction_bound=0.2,
    )

    assert certificate.status is EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
    assert not certificate.usable
    assert not certificate.permits({"input": "fixture"})


def test_certified_contraction_proves_unique_fixed_point() -> None:
    certificate = qualify_eta_interval(
        EtaInterval(1.0, 2.0),
        lambda segment: EtaInterval(
            segment.lower_hours + 0.4,
            segment.lower_hours + 0.6,
        ),
        scope={"input": "fixture"},
        coverage_complete=True,
        evaluator_certified=True,
        contraction_bound=0.5,
    )

    assert certificate.status is EtaIntervalStatus.ROOT_EXISTS_UNIQUE
    assert certificate.proves_fixed_point
    assert certificate.usable
    assert certificate.permits({"input": "fixture"})
    assert not certificate.permits({"input": "other"})


def test_disjoint_image_requires_certified_interval_extension_for_exclusion() -> None:
    unverified = qualify_eta_interval(
        EtaInterval(1.0, 2.0),
        lambda _domain: EtaInterval(3.0, 4.0),
        coverage_complete=True,
        evaluator_certified=False,
    )
    verified = qualify_eta_interval(
        EtaInterval(1.0, 2.0),
        lambda _domain: EtaInterval(3.0, 4.0),
        coverage_complete=True,
        evaluator_certified=True,
    )

    assert unverified.status is EtaIntervalStatus.UNCERTAIN_NO_INTERVAL_PROOF
    assert verified.status is EtaIntervalStatus.ROOT_EXCLUDED
    assert not verified.proves_fixed_point
    assert not verified.usable


def test_incomplete_coverage_is_uncertain_even_if_callback_would_contract() -> None:
    certificate = qualify_eta_interval(
        EtaInterval(1.0, 2.0),
        lambda _domain: EtaInterval(1.4, 1.6),
        coverage_complete=False,
        evaluator_certified=True,
        contraction_bound=0.2,
    )

    assert certificate.status is EtaIntervalStatus.UNCERTAIN_COVERAGE
    assert certificate.image is None
    assert not certificate.usable


def test_evaluator_failure_is_recorded_without_silent_fallback() -> None:
    def evaluate(_domain: EtaInterval) -> EtaInterval:
        raise RuntimeError("coverage gap")

    certificate = qualify_eta_interval(
        EtaInterval(1.0, 2.0),
        evaluate,
        coverage_complete=True,
        evaluator_certified=True,
    )

    assert certificate.status is EtaIntervalStatus.UNCERTAIN_EVALUATOR_FAILURE
    assert certificate.reason == "evaluation_failed:RuntimeError"


def test_continuity_and_endpoint_sign_change_prove_existence_without_uniqueness() -> None:
    certificate = qualify_eta_interval(
        EtaInterval(1.0, 2.0),
        lambda _domain: EtaInterval(1.2, 1.8),
        coverage_complete=True,
        evaluator_certified=True,
        continuity_certified=True,
        endpoint_residuals=(0.5, -0.25),
    )

    assert certificate.status is EtaIntervalStatus.ROOT_EXISTS_NONUNIQUE
    assert certificate.proves_fixed_point
    assert certificate.usable


@pytest.mark.parametrize(
    "domain",
    [EtaInterval(1.0, 1.0), EtaInterval(0.5, 3.0)],
)
def test_interval_digest_is_stable_for_valid_domains(domain: EtaInterval) -> None:
    first = qualify_eta_interval(
        domain,
        lambda value: value,
        scope={"input": "fixture"},
        coverage_complete=True,
        evaluator_certified=True,
        contraction_bound=0.0,
    )
    second = qualify_eta_interval(
        domain,
        lambda value: value,
        scope={"input": "fixture"},
        coverage_complete=True,
        evaluator_certified=True,
        contraction_bound=0.0,
    )

    assert first.digest == second.digest


def test_residual_interval_is_signed_and_cannot_hide_a_zero_crossing() -> None:
    certificate = qualify_eta_interval(
        EtaInterval(1.0, 2.0),
        lambda _domain: EtaInterval(1.4, 1.6),
        coverage_complete=True,
        evaluator_certified=True,
        contraction_bound=0.2,
    )

    assert certificate.residual_interval is not None
    assert certificate.residual_interval.lower == pytest.approx(-0.6)
    assert certificate.residual_interval.upper == pytest.approx(0.6)
    assert certificate.residual_interval.contains_zero


def test_partition_is_contiguous_and_includes_risk_boundary() -> None:
    segments = partition_eta_domain(EtaInterval(1.0, 3.0), (2.0,))

    assert segments == (EtaInterval(1.0, 2.0), EtaInterval(2.0, 3.0))


def test_unproven_partition_boundary_blocks_global_certificate() -> None:
    qualification = qualify_eta_partition(
        EtaInterval(1.0, 3.0),
        (2.0,),
        lambda _domain: EtaInterval(1.4, 1.6),
        coverage_complete=True,
        evaluator_certified=True,
        contraction_bound=0.2,
    )

    assert qualification.status is EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
    assert qualification.reason == "partition_boundary_continuity_unproven"
    assert not qualification.usable


def test_hard_mask_boundary_reason_is_retained_even_with_local_contraction() -> None:
    qualification = qualify_eta_partition(
        EtaInterval(1.0, 3.0),
        (2.0,),
        lambda _domain: EtaInterval(1.4, 1.6),
        coverage_complete=True,
        evaluator_certified=True,
        contraction_bound=0.2,
        boundary_continuity_certified=True,
        boundary_reasons=("hard_mask_discontinuity",),
    )

    assert qualification.status is EtaIntervalStatus.UNCERTAIN_DISCONTINUITY
    assert qualification.reason == "hard_mask_discontinuity"
    assert not qualification.usable


def test_complete_partition_can_be_authorized_only_with_boundary_proof() -> None:
    qualification = qualify_eta_partition(
        EtaInterval(1.0, 3.0),
        (2.0,),
        lambda segment: EtaInterval(
            segment.lower_hours + 0.4,
            segment.lower_hours + 0.6,
        ),
        scope={"input": "fixture", "edge_evaluator_digest": "explicit:test"},
        coverage_complete=True,
        evaluator_certified=True,
        contraction_bound=0.2,
        boundary_continuity_certified=True,
    )

    assert qualification.status is EtaIntervalStatus.ROOT_EXISTS_UNIQUE
    assert qualification.usable
    assert qualification.permits(qualification.scope)
    assert not qualification.permits({"input": "other"})


def test_partition_coverage_gap_is_fail_closed() -> None:
    qualification = qualify_eta_partition(
        EtaInterval(1.0, 3.0),
        (2.0,),
        lambda _domain: EtaInterval(1.4, 1.6),
        coverage_complete=False,
        evaluator_certified=True,
        contraction_bound=0.2,
        boundary_continuity_certified=True,
    )

    assert qualification.status is EtaIntervalStatus.UNCERTAIN_COVERAGE
    assert not qualification.usable
