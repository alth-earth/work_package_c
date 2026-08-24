---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - IN_PROGRESS
Document Role: SUPPORTING
Scope: exact risk-sample reuse and bounded LRU evidence for C
Canonical/Supporting: Supporting experiment; production planner behavior remains canonical elsewhere
Branch: research-validation-system
Last Verified: 2026-08-22
---

# C Risk Sample Cache Experiment

## Verdict（2026-08-22 02:34 +08:00）

The medium-grid recommended search has material exact sample reuse and a bounded
50,000-entry LRU improved median planning time by **14.77%** while preserving the
complete planning semantic digest. The implementation is `EXPERIMENTAL`, is
used only by the BC benchmark, and remains default-off. Production ingress,
A*, route semantics, contracts and planner configuration were not changed.

Qualification: `IMPLEMENTED / UNIT_VALIDATED / REAL_B_FRAME_EXPERIMENT_PASS`.
It is not production approval because only one objective and the direct
RiskFrame benchmark path were measured; committed-window ingress and the four
layer × three objective matrix remain untested with the cache.

## Experiment boundary（2026-08-22 02:34 +08:00）

| Field | Value |
|---|---|
| Input | 78 real B-built `bc.risk-frame.v2` frames |
| Risk grid | medium 31×11, 341 nodes |
| Risk window | 2026-08-15 10:00Z → 2026-08-18 15:00Z |
| Objective | `recommended` |
| Input document SHA-256 | `13052ab4306e99fcdcee196e0f003be2243c30701f67041d1b99266f42161cd4` |
| Risk ID sequence SHA-256 | `28b84c4447d739b415206d1fb71a292e80e9c71e43b42341cf6df952dce469df` |
| Cache capacity | 50,000 successful samples |
| Execution | sequential independent processes; no concurrent heavy workload |
| Publication | none |

The exact key contains the risk-window fingerprint, explicit risk layer,
full UTC requested time, and IEEE-754 longitude/latitude bits. It performs no
time bucketing, coordinate rounding or approximate equivalence. Each sampler
instance owns an isolated cache; only successful immutable `SampledRisk` values
are retained.

## Reuse evidence（2026-08-22 02:34 +08:00）

Shadow mode always called the canonical sampler and retained keys only:

| Metric | Result |
|---|---:|
| Total requests | 705,469 |
| Exact unique requests | 462,477 |
| Exact repeated requests | 242,992 |
| Exact reuse ratio | 34.444% |
| Planning time | 77.163 s |
| Sampled RSS peak | 241,264 KiB |

The shadow RSS is intentionally not a cache deployment estimate: its unbounded
exact-unique set exists only to measure the reuse ceiling.

## Bounded LRU performance（2026-08-22 02:34 +08:00）

| Run | Cache off | Bounded LRU |
|---|---:|---:|
| 1 | 75.671 s | 65.242 s |
| 2 | 76.281 s | 64.833 s |
| 3 | 76.884 s | 65.012 s |
| Median | **76.281 s** | **65.012 s** |
| Median sampled RSS | 122,416 KiB | 161,988 KiB |

Median latency decreased by 11.269 s, or 14.77%; median sampled RSS increased
by 39,572 KiB (about 38.6 MiB). Every LRU run recorded 232,261 hits, 473,208
underlying samples, 423,208 evictions and exactly 50,000 retained entries. No
run used swap or encountered a major page fault.

## Equality gate（2026-08-22 02:34 +08:00）

All six measured runs produced semantic digest
`47c3af1c9f87d2cc76d1922223e93008fc67463234cbff3d18ec9b5b1f213cad`.
The digest covers objective, every route step and ETA, speed, edge risk,
confidence, cost breakdown, source risk IDs, aggregate route metrics and search
metrics except `compute_ms`.

The following values were identical in all runs:

| Metric | Value |
|---|---:|
| Expanded / generated states | 15,349 / 20,839 |
| Queue peak | 4,728 |
| Route nodes | 22 |
| Distance | 909.721252542322 km |
| Travel time | 50.454492238619 h |
| Average / maximum risk | 0.044191127981 / 0.066088972219 |

Unit tests additionally prove default-off delegation, exact microsecond and
IEEE-float key separation, bounded eviction, failed-sample non-retention, and
route digest equality on a deterministic fixture.

## Limits and next gate（2026-08-22 02:34 +08:00）

- Results are engineering measurements, not a professional benchmark SLA.
- The input is a real B experiment document but was not obtained through a
  production committed-window lease.
- Only `recommended` on the full medium fixture was performance-measured.
- The LRU must remain experimental/default-off until three-objective,
  four-layer, hard/unavailable, replan-window and formal-ingress equality tests
  pass.
- A capacity sweep may reduce the 423,208 evictions, but it must stay bounded
  and preserve the current memory gate.

Rollback is removal of the benchmark option and adapter; no artifact migration
or schema change is required.
