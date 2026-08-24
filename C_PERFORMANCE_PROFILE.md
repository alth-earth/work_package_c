---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - PLANNED
Document Role: SUPPORTING
Scope: C planner component profiling framework and initial synthetic result
Canonical Current State: NO
Branch: research-validation-system
Last Verified: 2026-08-22
---

# C Performance Profile

## Status and method（2026-08-22 00:08）

Status: `EXPERIMENTAL + UNIT_VALIDATED`.

`arctic_route_planning.profiling` executes the real `TimeDependentAStar`,
`RiskSampler`, vessel model and cost model on an explicitly synthetic fixture.
It does not modify planner logic, output contracts, cache behavior or route
semantics. The resulting route is not authoritative.

Fixture: 9×13 regular grid, 13 hourly RiskFrames, three objectives, no hard
cells, deterministic risk/speed arrays. Timings come from Python `cProfile`.

## Initial profile（2026-08-22 00:08）

Command:

```bash
uv run python scripts/profile_planner_components.py \
  --output ${ARCTIC_ROUTE_ROOT}/.runtime/test-logs/c-component-profile-20260822.json
```

| Component boundary | Calls | Self time | Inclusive time | Inclusive / total |
|---|---:|---:|---:|---:|
| total three-objective planner | — | — | 1.338443 s | 100% |
| edge traversal | 1,623 | 0.018991 s | 1.322514 s | 98.81% |
| risk sampling | 9,741 | 0.036443 s | 1.233091 s | 92.12% |
| heuristic | 972 | 0.000596 s | 0.002908 s | 0.22% |
| objective calculation | 2,595 | 0.003077 s | 0.009706 s | 0.73% |

Inclusive timings overlap: risk sampling is nested inside edge traversal, and
cost lower bounds are called from heuristic. Rows must not be summed.

Each evaluated edge produced exactly six sample calls on this fixture, matching
the current two ETA/speed refinements × three edge sample points. This is direct
evidence that sampling/traversal is the first optimization target; heuristic and
scalar objective calculation are not current bottlenecks.

## Result identity（2026-08-22 00:08）

All objectives returned the same physical route on this intentionally smooth
fixture, with stable digest
`21356fed2b6660abf70861ee48dbcddd59ff95f4c152ef070e5c5c50463b7ae1`.
Search effort differed:

| Objective | Expanded | Generated | Nodes |
|---|---:|---:|---:|
| fastest | 29 | 180 | 13 |
| low_risk | 138 | 524 | 13 |
| recommended | 49 | 265 | 13 |

Repeated unit execution verifies route-digest stability; timings are not treated
as deterministic values.

## Optimization decision（2026-08-22 00:08）

One low-risk correctness hardening was implemented: the existing edge geometry
cache key now includes `minimum_samples`. Previously the same planner could
reuse three-point geometry for a later five-point request. A focused regression
verifies separate cache entries; route/search semantics are otherwise unchanged.

No new sampling or traversal optimization was implemented. A naive sample cache
would be unbounded and could increase the historical multi-GiB planning peak.
Before memoization, a complete key and memory policy must bind at least:

```text
risk identity/content digest
grid and planner configuration
generation/input revision
sampled UTC time
longitude/latitude or edge/fraction identity
edge sample count
vessel model and speed policy
hard/maximum-risk/minimum-confidence policy
```

Recommended sequence:

1. run the same profiler on one bounded formal fixture and one frozen artifact;
2. count exact repeated sample keys across three objectives;
3. estimate bounded-LRU hit rate, bytes and eviction effect without changing results;
4. prototype objective-independent edge traversal caching behind an experimental flag;
5. require all three route business digests to match the uncached baseline;
6. measure wall time and peak RSS before considering default enablement.

Shared labels/open queues, incremental search, D* Lite and LPA* remain PLANNED.

## Validation（2026-08-22 00:08）

```text
targeted Ruff: PASS
targeted pytest: 2 passed
three objectives present: PASS
repeated route digests equal: PASS
component boundaries reported: PASS
authoritative semantics changed: NO
edge sample-count cache identity: FIXED + UNIT_PASS
```
