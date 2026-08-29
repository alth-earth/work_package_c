"""Fail-closed stale-entry compaction for the C temporal research path.

Queue compaction is a memory optimisation, not a search rule.  A queue entry
is removable only when the session's current exact-state label mapping proves
that the entry is obsolete.  Live labels, predecessors, and labels with a
different exact arrival are never touched.  The policy is disabled by default
and is intentionally not part of the production planner contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from .temporal_qualification import canonical_digest

QUEUE_COMPACTION_SCHEMA = "c.p0.2-temporal-queue-compaction.v1"
QUEUE_COMPACTION_METHOD = "live-label-equality-v1"
QUEUE_COMPACTION_DISABLED_DIGEST = "temporal-queue-compaction-disabled"


@dataclass(frozen=True, slots=True)
class TemporalQueueCompactionPolicy:
    """Explicit policy for removing only stale heap entries.

    ``live_only`` is the sole enabled mode.  Its proof obligation is the
    exact equality check between a queued cost and the session's current
    ``labels[state]`` value.  Thresholds affect only when the proven-safe
    rebuild is attempted, never which entries are considered live.
    """

    mode: str = "disabled"
    check_interval: int = 128
    min_stale_entries: int = 16
    min_stale_fraction: float = 0.25
    schema_version: str = QUEUE_COMPACTION_SCHEMA
    method: str = QUEUE_COMPACTION_METHOD

    def __post_init__(self) -> None:
        if self.mode not in {"disabled", "live_only"}:
            raise ValueError("unsupported queue compaction mode")
        if self.schema_version != QUEUE_COMPACTION_SCHEMA:
            raise ValueError("unsupported queue compaction schema")
        if self.method != QUEUE_COMPACTION_METHOD:
            raise ValueError("unsupported queue compaction method")
        for name in ("check_interval", "min_stale_entries"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        if (
            isinstance(self.min_stale_fraction, bool)
            or not isfinite(float(self.min_stale_fraction))
            or not 0.0 < float(self.min_stale_fraction) <= 1.0
        ):
            raise ValueError("min_stale_fraction must be in (0, 1]")

    @classmethod
    def disabled(cls) -> TemporalQueueCompactionPolicy:
        """Return the stable default-off policy."""

        return cls(mode="disabled")

    @classmethod
    def live_only(
        cls,
        *,
        check_interval: int = 128,
        min_stale_entries: int = 16,
        min_stale_fraction: float = 0.25,
    ) -> TemporalQueueCompactionPolicy:
        """Enable equality-proven stale-entry compaction for research."""

        return cls(
            mode="live_only",
            check_interval=check_interval,
            min_stale_entries=min_stale_entries,
            min_stale_fraction=min_stale_fraction,
        )

    @property
    def enabled(self) -> bool:
        return self.mode == "live_only"

    @property
    def usable(self) -> bool:
        return self.enabled and self.method == QUEUE_COMPACTION_METHOD

    @property
    def digest(self) -> str:
        if not self.enabled:
            return QUEUE_COMPACTION_DISABLED_DIGEST
        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "method": self.method,
                "mode": self.mode,
                "check_interval": self.check_interval,
                "min_stale_entries": self.min_stale_entries,
                "min_stale_fraction": self.min_stale_fraction,
            }
        )

    def should_check(self, event_count: int, *, force: bool = False) -> bool:
        """Return whether a queue scan is due at a push event."""

        if not self.usable:
            return False
        if isinstance(event_count, bool) or not isinstance(event_count, int) or event_count < 1:
            return False
        return force or event_count % self.check_interval == 0

    def qualifies(self, stale_entries: int, queue_size: int) -> bool:
        """Apply thresholds after stale/live classification."""

        if not self.usable or queue_size < 1:
            return False
        if isinstance(stale_entries, bool) or not isinstance(stale_entries, int):
            return False
        if stale_entries < self.min_stale_entries:
            return False
        return stale_entries / queue_size >= self.min_stale_fraction


def is_well_formed_queue_entry(entry: Any) -> bool:
    """Validate only the structural fields needed for safe compaction."""

    if not isinstance(entry, tuple) or len(entry) != 10:
        return False
    queued_cost = entry[1]
    if isinstance(queued_cost, bool) or not isinstance(queued_cost, (int, float)):
        return False
    return isfinite(float(queued_cost)) and queued_cost >= 0.0


__all__ = [
    "QUEUE_COMPACTION_DISABLED_DIGEST",
    "QUEUE_COMPACTION_METHOD",
    "QUEUE_COMPACTION_SCHEMA",
    "TemporalQueueCompactionPolicy",
    "is_well_formed_queue_entry",
]
