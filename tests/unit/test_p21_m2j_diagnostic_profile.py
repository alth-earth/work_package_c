"""Unit assertions for the P2.1-M2J R2 diagnostic profile (`trace_release_only`).

These tests pin the opt-in shadow diagnostic profile added by the M2J measurement
protocol proposal (see ``docs/CORE_ALGORITHM_IMPROVEMENT_PLAN.md``). The profile is
the safe, within-process realization of R1 candidate isolation: after the
MAIN_CORRIDOR reuse decision (layer_index == 1), the retained ``ControlTrace``
payload is released (``self._full_traces.clear()`` + ``gc.collect()``) before the
ROLLING/EXECUTABLE cold units run, eliminating trace-memory pollution.

The candidate stays opt-in and non-published (controlled by ``candidate-mode``),
and the profile never changes the default ``baseline`` path or any B/C/D contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arctic_route_planning.ingress import (
    _TEMPORAL_SHADOW_DIAGNOSTIC_PROFILES,
    _normalize_temporal_shadow_diagnostic_profile,
)


def test_trace_release_only_is_registered_profile() -> None:
    """The new profile must be a valid member of the diagnostic frozenset."""
    assert "trace_release_only" in _TEMPORAL_SHADOW_DIAGNOSTIC_PROFILES
    # Existing profiles remain present; the set is additive only.
    for existing in ("baseline", "force_main_cold", "post_main_normalize"):
        assert existing in _TEMPORAL_SHADOW_DIAGNOSTIC_PROFILES


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("trace_release_only", "trace_release_only"),
        ("trace-release-only", "trace_release_only"),
        ("TRACE_RELEASE_ONLY", "trace_release_only"),
        ("  trace-release-only  ", "trace_release_only"),
    ],
)
def test_normalize_accepts_trace_release_only_variants(raw: str, expected: str) -> None:
    """Both underscore and hyphen spellings normalize to the canonical key."""
    assert _normalize_temporal_shadow_diagnostic_profile(raw) == expected


def test_normalize_rejects_unknown_profile() -> None:
    """Unknown profiles fail closed (ValueError), preserving the fail-closed rule."""
    with pytest.raises(ValueError):
        _normalize_temporal_shadow_diagnostic_profile("post_main_warmup")


def _read_orchestrator_script_source() -> str:
    """Read the orchestrator shadow script as source text.

    NOTE: the script currently cannot be imported at runtime because it still
    references ``arctic_route_planning.planners.control_trace_reuse`` (lines 55-60),
    which was archived to ``planners/_archive/control_trace_reuse.py`` during the
    codex P2.1 work.  That is a pre-existing breakage unrelated to the M2J changes
    (the live execution path is C-side ``ingress.py``).  We therefore assert on the
    source text so the M2J-added contract is pinned without depending on the broken
    import.  Repairing the archive import is a separate task.
    """
    script_path = (
        Path(__file__).resolve().parents[3]
        / "arctic_route_orchestrator"
        / "scripts"
        / "winter_p2_shadow.py"
    )
    if not script_path.exists():
        pytest.skip(f"orchestrator script not found at {script_path}")
    return script_path.read_text(encoding="utf-8")


def test_orchestrator_validates_trace_release_only() -> None:
    """The orchestrator CLI validator accepts the hyphenated profile spelling.

    Asserted on source text because the script is not importable (see
    ``_read_orchestrator_script_source`` for the pre-existing archive-import break).
    """
    source = _read_orchestrator_script_source()
    # The validator must accept the new profile value.
    assert '"trace-release-only"' in source
    assert "_DIAGNOSTIC_PROFILE_VALUES" in source
    # The validator error message enumerates the new profile (fail-closed wording).
    assert "post-main-normalize, or trace-release-only" in source


def test_orchestrator_isolation_values_registered() -> None:
    """The M2J `--isolation` flag enumerates the per-track / per-unit-phase modes.

    Asserted on source text (script not importable; see above).
    """
    source = _read_orchestrator_script_source()
    assert '_ISOLATION_VALUES = ("per-track", "per-unit-phase")' in source
    # The worker command must force trace-release-only on the candidate track under
    # per-unit-phase isolation (the within-process realization of R1).
    assert 'effective_diagnostic_profile = "trace-release-only"' in source
    assert 'if isolation == "per-unit-phase" and track == "candidate"' in source
