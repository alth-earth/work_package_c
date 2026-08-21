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
- bounded risk-sample cache: `PLANNED`;
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

Next add counters around exact sample requests, without retaining values:

```text
(RiskSampler instance/window identity, sampled_at, latitude index, longitude index)
```

Record total requests, unique keys, repeated keys and per-search peak unique
keys. Do not round coordinates or time; approximate equivalence could change
the selected valid frame and is outside this proposal.

## Phase 1 shadow cache experiment（2026-08-22 01:11 +08:00）

If exact repeats are material, run a shadow-only dictionary that records whether
a lookup would hit while still calling the existing sampler. Acceptance requires:

1. identical route nodes and route digest;
2. identical distance, ETA, average/max/integrated risk and source RiskFrame IDs;
3. identical expanded/generated states and failure reason;
4. bounded additional RSS under an explicit key cap;
5. no stale value across sampler/window/generation identity.

## Phase 2 bounded opt-in cache（2026-08-22 01:11 +08:00）

Only after Phase 1 evidence, evaluate a per-planner bounded LRU with an explicit
entry limit and default disabled. Eviction affects performance only; it must not
affect results. Cache lifetime must not cross a `RiskSampler`, committed window,
generation, or replay seek boundary.

Do not cache whole `_EdgeTraversal` results: they also depend on departure time,
heading, objective cost inputs, vessel state and sampled risk sequence. The
smaller exact sample boundary is easier to validate and bound.

## Acceptance and rollback（2026-08-22 01:11 +08:00）

- unit tests cover hit/miss, eviction, sampler isolation and time-key identity;
- golden route tests cover three objectives and hard/unavailable failures;
- baseline/medium benchmark is repeated with fixed resources and reports median
  plus spread;
- production remains default-off until focused formal ingress regression passes;
- rollback removes the opt-in configuration and cache object without artifact
  migration because no public schema changes.
