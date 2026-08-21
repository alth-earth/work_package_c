---
Overall Status: DRAFT
Content Status:
  - COMPLETED
  - PLANNED
Document Role: CANONICAL
Scope: bounded low-risk performance experiments for C
Canonical/Supporting: Canonical proposal only; it is not implementation approval
Branch: research-validation-system
Last Verified: 2026-08-22
---

# C Optimization Proposal

## Decision boundary（2026-08-22 01:11 +08:00）

Do not redesign A*, change its heuristic, share searches across objectives, or
introduce incremental planning in the next step. First measure exact repeated
risk-sample identities in the real medium-grid search. Any cache experiment must
be bounded, default-off, and prove byte-for-byte equivalent route semantics.

Current state:

- edge geometry cache statistics: `IMPLEMENTED / UNIT_PASS`;
- B→C baseline/medium benchmark fixture: `IMPLEMENTED / EXPERIMENTAL_PASS`;
- exact sample observability: `IMPLEMENTED / REAL_B_FRAME_EXPERIMENT_PASS`;
- bounded risk-sample cache: `IMPLEMENTED / EXPERIMENTAL_DEFAULT_OFF`;
- shared multi-objective search: `PLANNED, OUT OF SCOPE`;
- incremental replanning: `PLANNED, OUT OF SCOPE`.

## Evidence（2026-08-22 01:11 +08:00）

The medium grid completed one recommended route in 75.001 s with 15,349
expansions. Existing edge geometry reused 248,720 lookups from only 1,783 cached
entries. The component profiler separately shows risk sampling nested inside
edge traversal, but a geometry hit does not prove a risk-sample hit because ETA
changes the sample time.

## Phase 0 observability（2026-08-22 01:11 +08:00）

Implemented counters expose `{hits, misses, entries}` for the existing private
edge-geometry cache and include them in profiling/BC benchmark outputs. They do
not change keys, eviction, route selection, costs, or public contracts.

The Round3 experiment now records exact sample requests without changing the
canonical sampler result:

```text
(RiskSampler window fingerprint, risk layer, sampled_at UTC,
 longitude IEEE-754 bits, latitude IEEE-754 bits)
```

The real medium search recorded 705,469 requests, 462,477 exact unique keys and
242,992 repeats (34.444%). Coordinates and time are not rounded. Detailed
evidence is in [`C_RISK_SAMPLE_CACHE_EXPERIMENT.md`](C_RISK_SAMPLE_CACHE_EXPERIMENT.md).

## Phase 1 shadow cache experiment（2026-08-22 01:11 +08:00）

Shadow mode was run and always delegated to the canonical sampler. Its complete
route digest matched cache-off and bounded-LRU results. Acceptance remains:

1. identical route nodes and route digest;
2. identical distance, ETA, average/max/integrated risk and source RiskFrame IDs;
3. identical expanded/generated states and failure reason;
4. bounded additional RSS under an explicit key cap;
5. no stale value across sampler/window/generation identity.

## Phase 2 bounded opt-in cache（2026-08-22 01:11 +08:00）

The 50,000-entry per-sampler LRU is now implemented only in the experimental BC
benchmark and remains default disabled. Three independent medium-grid runs
reduced median planning time from 76.281 s to 65.012 s (14.77%) with about
38.6 MiB additional sampled RSS. Eviction affects performance only; complete
route semantics remained identical. Cache lifetime cannot cross a sampler or
risk window.

Do not cache whole `_EdgeTraversal` results: they also depend on departure time,
heading, objective cost inputs, vessel state and sampled risk sequence. The
smaller exact sample boundary is easier to validate and bound.

## Acceptance and rollback（2026-08-22 01:11 +08:00）

- unit tests cover hit/miss, eviction, sampler isolation, failed-sample behavior
  and exact time/coordinate identity;
- golden route tests cover three objectives and hard/unavailable failures;
- the medium benchmark has three independent runs per mode and reports median
  plus spread;
- production remains default-off until focused formal ingress regression passes;
- rollback removes the opt-in configuration and cache object without artifact
  migration because no public schema changes.
