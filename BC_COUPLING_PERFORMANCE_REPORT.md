---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
Document Role: SUPPORTING
Scope: C planning cost over B formal fixed-grid experiment frames
Canonical/Supporting: Supporting experimental evidence; production planning configuration and contracts remain canonical elsewhere
Branch: research-validation-system
Last Verified: 2026-08-22
---

# B-C Coupling Performance Report

## Round3 optimization evidence（2026-08-22 02:34 +08:00）

The same medium RiskFrame document was used to compare C exact sample-cache
off versus a default-off 50,000-entry bounded LRU. Three independent runs per
mode produced one complete semantic route digest. Median planning time improved
from 76.281 s to 65.012 s (14.77%) while sampled RSS increased from 122,416 KiB
to 161,988 KiB. Shadow profiling found 242,992 exact repeats among 705,469
requests (34.444%).

This is `EXPERIMENTAL / REAL_B_FRAME_PASS`, not a production cache claim. The
input SHA-256, endpoint mapping, objective and planner configuration were fixed;
production ingress and contracts were unchanged. Full methods, equality scope
and limitations are in
[`C_RISK_SAMPLE_CACHE_EXPERIMENT.md`](C_RISK_SAMPLE_CACHE_EXPERIMENT.md).

## Verdict（2026-08-22 01:11 +08:00）

The real C `recommended` time-dependent A* completed on both the 112-node and
341-node RiskFrame grids. Medium resolution is still feasible for a bounded
single route, but it is no longer cheap: 3.04× nodes produced 7.14× planning
time and 6.83× expanded states. This is the current evidence boundary for B grid
refinement; no C optimization or search-semantic change was made.

Status: `EXPERIMENTAL / REAL_B_FRAMES + REAL_C_SEARCH_PASS`. The input frames
were produced from A public data through the formal B builder, then passed to the
benchmark as decoded `bc.risk-frame.v2` documents. They were not committed to a
production B store, so this is not a formal ingress or full integration claim.

## Experiment identity（2026-08-22 01:11 +08:00）

| Field | Value |
|---|---|
| Scenario | `tromso_isfjorden_august_2026_demo_v1` |
| Risk window | 78 hourly formal frames, 2026-08-15 10:00Z → 2026-08-18 15:00Z |
| Contract consumed | `bc.risk-frame.v2` |
| C objective | `recommended` |
| Planner/vessel configuration | current Tromsø C configuration |
| Endpoint gate | allowed region, hard mask, connectivity, max snap 30 km |
| Execution | baseline then medium, sequential, max 250,000 expansions |
| Publication | none |

## Measured results（2026-08-22 01:11 +08:00）

| Risk grid | C nodes | Planning time | Sampled RSS peak | Expansions | Generated | Queue peak | Route nodes | Result |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| baseline 16×7 | 112 | 10.506 s | 97,336 KiB | 2,247 | 4,232 | 1,927 | 11 | SUCCESS |
| medium 31×11 | 341 | 75.001 s | 122,420 KiB | 15,349 | 20,839 | 4,728 | 22 | SUCCESS |

The two searches resolved endpoints independently on their own grids, as the
formal C endpoint mapper requires. Therefore their route geometry, distance,
ETA and digest are expected to differ and must not be used as an algorithmic
determinism comparison across grids.

| Output | baseline | medium |
|---|---:|---:|
| Start adjustment | 22.239 km | 23.784 km |
| Goal adjustment | 26.358 km | 18.916 km |
| Distance | 867.249 km | 909.721 km |
| Travel time | 48.092 h | 50.454 h |
| Average risk | 0.043524 | 0.044191 |
| Maximum risk | 0.064355 | 0.066089 |

## Scaling interpretation（2026-08-22 01:11 +08:00）

| Ratio medium / baseline | Value |
|---|---:|
| Grid nodes | 3.045× |
| Planning time | 7.139× |
| Sampled RSS peak | 1.258× |
| Expanded states | 6.831× |
| Generated states | 4.924× |

Fine contains 1,260 nodes, 3.70× medium and 11.25× baseline. No fine C run was
started because the measured growth is already super-linear and the user
requested bounded memory/time use. A runtime value for fine would be speculation;
only its grid-size factor is recorded. Before running it, add an explicit wall
time/expansion budget and preserve partial metrics on controlled termination.

## Cache observations（2026-08-22 01:11 +08:00）

| Grid | Edge geometry hits | Misses / entries | Hit fraction |
|---|---:|---:|---:|
| baseline | 35,111 | 532 | 98.51% |
| medium | 248,720 | 1,783 | 99.29% |

These are observational counters on the existing geometry cache. They confirm
substantial reuse but do not identify equivalent risk samples, because risk
sampling also depends on ETA and valid-time selection. No new cache was enabled.

## Performance boundary（2026-08-22 01:11 +08:00）

The complete two-profile command took 86.29 s wall time and 121,080 KiB maximum
RSS with no swap. The in-process sampled peaks differ slightly from `/usr/bin/time`
because sampling points are not identical. This is an engineering observation,
not a professional benchmark; repeat runs and variance bounds are still needed
before setting an acceptance SLA.

## Reproduction（2026-08-22 01:11 +08:00）

```bash
cd /root/my_project/work_package_c
uv run python scripts/benchmark_bc_coupling.py \
  --profile baseline=/root/my_project/.runtime/experiments/b-formal-grid-round2/baseline/risk-frames.json \
  --profile medium=/root/my_project/.runtime/experiments/b-formal-grid-round2/medium/risk-frames.json \
  --c-config-root configs \
  --contracts-config-root /root/my_project/arctic_route_contracts/configs \
  --scenario-id tromso_isfjorden_august_2026_demo_v1 \
  --max-snap-km 30 \
  --max-expansions 250000 \
  --output /root/my_project/.runtime/experiments/bc-coupling-round2.json
```
