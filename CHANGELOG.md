---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - IN_PROGRESS
Document Role: SUPPORTING
Scope: work package C change history
Branch: research-validation-system
Last Verified: 2026-08-27
---

# 工作包 C 变更记录

本文件记录工作包 C 的可见功能、跨包合同、兼容性和验证状态变化。项目用途、运行方法
与当前架构请先阅读 [README.md](README.md)；长期设计取舍见
[决策记录](docs/DECISIONS.md)。

## Unreleased — P3.1 SMO-A* evidence hardening（2026-08-27）

- `TimeDependentAStar` 的显式 `shared_edge_evaluation=True` 路径现在把拒绝边保存为
  traceback-free 不可变 rejection record，并让最后一个 objective 只读已有缓存，避免缓存
  无后续消费者的条目；默认 `plan()`、非 shared 路径和正式调用链保持兼容。
- 新增 `traversal_cache_stats` 观察字段，区分 accepted/rejected hit/miss、当前/峰值条目，
  仅用于内部诊断，不进入 C→D 路线合同或业务 digest。
- `scripts/benchmark_smo_astar.py` 改为每 cell 独立 worker、control/candidate 交替、CPU
  绑定、进程 RSS/`VmSwap`、硬超时与完整路线业务语义 digest，并记录 Git/lock/runner/input identity。
- 新增未从 package root 导出的 `AnytimeRepairingAStar` 研究候选：固定
  `2.5→2.0→1.5→1.0` epsilon 修复序列，记录 incumbent/下界/gap，扩展预算耗尽时 fail-closed；
  仅通过 synthetic M0 单测，不进入 Winter 或正式入口。
- 本轮仍未启用 SMO、未修改 RiskFrame/RoutePlan 合同、未写入 formal latest 或 frozen artifact；
  P3.1 的双 Winter M1 与 ARA* 后备状态以 `CORE_ALGORITHM_IMPROVEMENT_PLAN.md` 为准。

## Unreleased — P2.1 control-trace equivalence（2026-08-25 02:32 +08:00）

- 新增内部 `control_trace_reuse.py` 与 `TimeDependentAStar._plan_traced()`：默认 `plan()` 不变；仅显式
  shadow 记录首次 goal pop 前成功写入的 rolling digest 与保守 elapsed/risk envelope。
- 复用只接受同 start/goal/departure/objective/input/config/model/evaluator identity 下收紧
  `maximum_elapsed`/`maximum_risk` 的查询；termination、取消、篡改、放宽、identity mismatch 与
  transient-label 越界均 fail-closed，不产生 `OPTIMAL` 声明。
- ingress 与 Winter runner 增加默认关闭、非发布的 `control_trace` 模式；只对 full→main 同 goal
  尝试真实 hit，其余 layer cold control，sidecar 区分 hit/miss/fallback/zero-search。
- clean M0 首批 r5 为 40/40 语义通过，但 5×7、R=4 trace overhead `5.98%` 超限；预声明增加
  20 次确认后，pooled 30 样本四个单元 overhead 为 `1.95%–4.93%`、total median 改善
  `46.67%–78.85%`。首批失败保留，不由确认批覆盖。
- clean M1 r4 为 10/10 通过；16×7 与 31×11 本地 B-grid profile 的 paired improvement median
  为 `48.86%/49.87%`，median RSS ratio 为 `1.000/0.989`。
- Winter runner 增加正式 ingress、单轨 scratch/非发布证明、隔离 worker timeout/RSS/swap、12-route
  跨算法业务 digest、trace overhead、零搜索 HIT 和真实证书门禁。screening r1 因正式 digest 保留算法
  标签产生假阴性，修复提交 `03479058` 后 r2 为 2/2 `PASS`；旧 r1 原样保留。后续 `3097271`
  将新 experiment identity 绑定 runner/C 实现 SHA 与两仓 commit，旧 artifacts 不重写。
- Winter formal r1 的 4/4 case、48/48 路线语义、确定性、复用矩阵、RSS/swap 和 trace overhead 均
  通过，总 wall-time median 改善 `47.86%`；但 `rolling_0_24h × fastest` median 回归
  `5.94% > 5%`，故 M2 总 verdict 严格为 `FAIL`，候选继续默认关闭、非发布。
- 明确保留并排除错误证据：M0 r1 为 trace 热路径 overhead FAIL；M1 r1 为 `CostBreakdown`
  序列化 harness FAIL；M1 r2 含 control-only RSS polling 计时不对称。未运行 P3、2.2.2 或默认启用，
  未修改 B/C、C/D 合同、正式 latest 或 frozen artifact。

## Unreleased — P2 monotonic certificate reuse and P4a shadow（2026-08-25 00:42 +08:00）

- 新增未公开导出的内部 `temporal_reuse.py`：从终态 session 独立重算 `U/LB/epsilon`、
  `OPEN_BOUND/OPEN_EMPTY`、state/route/certificate digest；只允许同一完整身份下收紧
  `maximum_elapsed` 和/或 `maximum_risk`，命中不推进搜索。
- 结果状态明确区分 `HIT_EXACT`、`HIT_MONOTONIC`、`MISS_INCOMPATIBLE`、
  `COLD_CANDIDATE` 和实际运行独立 control 后的 `FALLBACK_CONTROL`；取消直接传播，当前未定义的
  cumulative-risk 约束 fail-closed。
- `PreparedRiskPlanning.execute_four_layer_temporal_shadow()` 在同一 committed-window lease 内运行
  两套 scratch planner/coordinator/store，并明确 `production_published=false`；正式 `execute*()`、
  session baseline、latest、C→D schema/digest 与 frozen artifact 均不改变。
- Orchestrator 新增独立 `scripts/winter_p2_shadow.py`，正式 Winter runner 默认行为不变；shadow 只向
  新 experiment 目录写 control/candidate、certificate/reuse、integrity 和 comparison sidecar。
- `scripts/validate_temporal_semantics.py --p2-exact-goal-reuse` 的 10 次 synthetic 验证全部通过语义、
  证书、命中零搜索和显式 control fallback，但 candidate cold median 约 `722.410 ms`，control cold
  median 约 `135.373 ms`，未通过 M0 性能门禁。
- Winter 正式输入 prepare-only 通过；有效 paired shadow 在约 `674.463 s` 后因候选 queue 达到
  `50,000` 硬上限失败关闭，峰值 RSS `229176 KiB`，未生成或发布候选四层结果。因此 P2 仅为
  `UNIT_PASS`，P4a 工具为 `IMPLEMENTED` 但 M2 未通过，不声明性能优势或默认启用。
- 完整 C 检查为 258 项通过；Orchestrator 非 integration/real-artifact 测试为 98 项通过。

## Unreleased — P1 resumable temporal sessions（2026-08-24 23:33 +08:00）

- 新增未公开导出的内部 `TemporalSession`：每个 objective 独立保存 exact-arrival labels、OPEN、
  前驱、incumbent、启发式缓存和累计诊断；状态限定为 `READY`、`PAUSED` 与四类终态。
- 新增完整 session identity fence，绑定当前 sampler 内容、可选 committed-window 内容寻址身份、
  generation/input revision、风险/planner/model/config、请求、网格、ETA policy、搜索限制和
  edge evaluator；恢复会重新计算当前 planner/request 身份，拒绝过期或伪造 identity。
- checkpoint 使用不可变 tuple/frozen value，保留 stale heap entry 和微秒级 ETA，清除进程本地
  cancel callback，并校验 state digest；所有终态不可恢复，四类资源上限跨暂停/恢复累计。
- `TemporalLabelAStar.plan()` 改为 session 兼容包装，并提供内部 per-objective bundle；正式
  `TimeDependentAStar`、ingress/service、B/C 与 C/D 合同和 frozen artifact 均未接入或改变。
- `scripts/validate_temporal_semantics.py` 新增显式 `--session-slice-expansions` P1 模式，对 control、
  one-shot candidate 和逐片 checkpoint/restore candidate 做串行语义比较；默认 P0 行为保持不变。
- 聚焦 P0/P1 回归 63 项通过，完整 `UV_OFFLINE=1 make check` 为 238 项通过。P1 仍仅为
  `UNIT_PASS`，不宣称性能优势、正式集成或冻结基线。

## Unreleased — P0 temporal semantics validation（2026-08-24 22:27 +08:00）

- 新增 fail-closed `damped_fixed_point_v1` ETA 精化器：最多 12 次、1 秒/`1e-6` 容差、
  0.5 阻尼、周期/超迭代/终值不一致检测，并按最终 ETA 重采样。
- 新增未公开导出、未接 ingress/service 的 `TemporalLabelAStar` 实验候选：标签身份为
  `(node, heading, exact UTC arrival_time)`，禁止跨时间支配，使用 goal incumbent/OPEN 下界终止，
  并对 expansions/labels/queue/edge evaluations 设置显式硬上限。
- 新增 test-only、零启发式 exact-time Dijkstra oracle；其实现不导入生产规划器。静态三方差分、
  非 FIFO、同桶多 ETA、exact-state replacement、ETA failure 与资源上限均有聚焦回归。
- 新增 `scripts/validate_temporal_semantics.py`：在 5×7×7 synthetic 静态 fixture 上串行运行
  当前 `TimeDependentAStar` control 与实验性 `TemporalLabelAStar` candidate；默认重复 10 次，
  通过 `--repetitions` 参数化，并将 manifest/cases 写入调用方指定目录。
- 验证入口记录 Git SHA、`uv.lock` SHA256、Python/platform、ETA/搜索策略、离散搜索结果和耗时；
  不导入 test-only oracle、不接正式 ingress、不覆盖已有 `manifest.json`/`cases.jsonl`，也不写冻结构件。
- 新增 `tests/unit/test_validate_temporal_semantics.py`，覆盖 2 次重复运行的结构、确定性（忽略耗时）
  及构件覆盖保护。
- 干净基线的 10 次 P0 static 运行 semantic digest 全部一致，但 candidate median wall time 约比
  control 慢 50.0%；当前状态仅为正确性 `UNIT_PASS`，不宣称性能优势，不改变正式规划器或跨包合同。

## Unreleased — Version clutter cleanup（2026-08-24）

依据 `arctic_route_governance/reports/audits/C_D_VERSION_CLUTTER_AUDIT_AND_CLEANUP_PLAN_20260824.md` 执行版本/合同/旧文件清理，**不触及代码逻辑与 C→D 合约**：

- **文档归档收敛**：14 个 pre-governance / 2026-08-15 归档文件（`*.archive-20260814-pre-governance.md`、`*_归档_20260815.md`）从根目录与 `docs/` 移入 `docs/archive/`；5 份历史性能报告（C_OPTIMIZATION_PROPOSAL / C_PERFORMANCE_PROFILE / C_RISK_SAMPLE_CACHE_EXPERIMENT / BC_COUPLING_PERFORMANCE_REPORT / bench_cprofile_96.0h.pstats）移入 `docs/archive/performance/`。
- **链接更新**：11 个当前文档（README、handoff、STATUS_AND_TODO、ACCEPTANCE、DECISIONS、ARCHITECTURE_TRACE、CD_CONTRACT、ARCHITECTURE_AND_DECISIONS、PROJECT_OVERVIEW、DEVELOPMENT_GUIDE、SHARED_CONTEXT_MIGRATION）中的归档文件引用全部更新为 `docs/archive/...` 路径，按治理标准"目标：规范链接失效数 = 0"。
- **死 Schema 删除**：`schemas/route-plan-v1.schema.json` 无任何代码/测试引用，直接删除（`risk-frame-v1.schema.json` 仍被 legacy 适配器使用，保留）。
- **历史产物清理**：删除 `output/legacy-smoke/`。
- **保留项**：v2 路线 schema（selection-rationale 基准 + v3 投影后备）、v3 四层 schema、selection-rationale schema、legacy CLI 与适配器（仍服役）。

## Unreleased — Core algorithm audit fixes（2026-08-24）

依据 `docs/CORE_ALGORITHM_IMPROVEMENT_PLAN.md` 14 问题清单完成内部质量修复，**不触及 C→D 合约**（schema 文件、序列化格式、digest 语义、selection-rationale sidecar 均不变）：

- **P0 性能**：`_Counters` 改可变（去 `frozen`），A\* 热循环消除每迭代 15+ 次 `dataclasses.replace` 重建，循环结束仍快照为不可变 `SearchMetrics`。
- **P1 观测剥离**：`planners/time_dependent_astar.py` 移除顶层 `import os`/`import resource`；env 解析 `C_ASTAR_PROGRESS_SECONDS` 移至 `service.progress_interval_from_env()` 经 `PlanningRequest.progress_interval_seconds` 注入；28 行进度打印提取为 `_emit_progress`，RSS 采样 lazy import（非 Unix 输出 `rss=na`）。
- **P1 SSOT 收敛**：新增 `ROUTE_PLAN_V3_SCHEMA_VERSION` / `FOUR_LAYER_ROUTE_PLAN_SET_V3_SCHEMA_VERSION` 常量（`contracts/layered.py`），替换 7 处裸串；`service.py`/`layered.py` 的 v2 裸串收敛为已有 `ROUTE_PLAN_SCHEMA_VERSION`；层时间窗 72/24/6h 提取为 `MAIN_CORRIDOR_HOURS` 等命名常量（不加 `PlannerConfig` 字段以保护 digest）。
- **P2 异常统一与类型收紧**：`PlanningCancelled` 唯一定义于 `errors.py`，`planners/errors.py` 与 `replanning/coordinator.py` 改 re-export（两条导入路径零破坏）；`ingress.py` 两处 `assert isinstance` 改 `TypeError`；三处 `RuntimeError("maximum_elapsed was not resolved")` 改 `ContractError`；`layered.py` `planner_config: object` 收紧为 `PlannerConfig`；`1e-12` 与 `range(2)` 提取为 `_COST_EPSILON`/`_EDGE_REFINEMENT_ROUNDS` 常量。
- **P2 性能**：`grid/regular.py` `snap_to_navigable` 改 numpy 矢量化（meshgrid + 矢量 haversine + `np.lexsort`），tie-break `(distance, row, col)` 语义与原实现一致。
- **P3 健壮性/清理**：`ingress._sessions` 改 `OrderedDict` LRU + `_MAX_SESSIONS = 64`；`replanning/policy.py` UTC 校验统一为 `timedelta(0)`；`risk/sampler.py` `risk_score=1.0` 加保守占位注释；`publishing/serialization.py` 两处 `from_dict` 的 `schema_version` 缺失由静默回退改严格 `KeyError→ValueError`。
- 配套测试：`tests/unit/test_publishing.py` 加 2 个缺 `schema_version` 负例；`tests/unit/test_ingress_lru.py` 新增 LRU 驱逐与重用提升测试。

## Unreleased — Selection rationale sidecar（2026-08-24）

- 新增可选 `selection-rationale` sidecar（SelectionRationale 模型 + `selection-rationale.v1` Schema），
  解释推荐路线相对最快基线的权衡（距离/ETA/风险 delta 与风险降低百分比）。
  v2 `PlanningBatch` 与 v3 `FourLayerPlanningOutcome` 增加可选 `selection_rationale` 字段；
  CLI 输出 `selection-rationale.json` 并在 `run-summary.json` 增加摘要段。
- 跨包合约变更提案：`docs/CD_CONTRACT_SELECTION_RATIONALE_PROPOSAL.md`（DRAFT）；
  CD_CONTRACT.md 同步 selection-rationale 语义（可选 sidecar，不进入路线 digest）。
- 配套测试：`tests/contract/test_schemas.py`（schema 验证）、`tests/unit/test_publishing.py`、
  `tests/integration/test_service.py`、`tests/unit/test_layered_planning.py`。
- 跨包落地：提案升 APPROVED（C/D 负责方批准）；D 侧 `load_selection_rationale` 消费 +
  真实 synthetic-demo 产物 fixture 跨包回归 PASS（D 全量 100 passed / 3 skipped）。

## Unreleased — B-C coupling evidence（2026-08-22 01:11 +08:00）

- add a sequential benchmark that decodes experimental formal
  `bc.risk-frame.v2` documents and runs the existing endpoint mapper,
  `RiskSampler`, regular grid, vessel model and recommended A* search;
- expose observational edge-geometry cache hits/misses/entries in planner and
  profiler outputs without changing cache or search behavior;
- add unit coverage for the benchmark and counters;
- record baseline/medium scaling and a bounded, default-off optimization
  proposal; no fine search, A* redesign or public contract change.

## Unreleased — Research component profiling（2026-08-22 00:08）

- add an isolated synthetic profiler around the real three-objective planner;
- report overlapping risk-sampling, edge-traversal, heuristic and objective-cost
  call/time boundaries with deterministic route digests;
- include `minimum_samples` in the private edge-geometry cache key so requests
  with different edge sampling densities cannot share stale geometry;
- do not alter A* search, route, ETA, publication or public contract semantics.

## 记录规则

- 版本按时间倒序记录；`Unreleased` 表示已规划但尚未纳入当前版本的工作。
- “新增/变更/修复”只描述已经落入代码、配置、Schema、测试或文档的内容。
- “后续工作”不是完成声明，也不构成正式接口承诺。
- BC/CD 合同版本与 Python 包版本是不同概念。例如包版本 `0.4.0` 继续消费
  `bc.risk-frame.v2`，并提供顶层 `cd.four-layer-route-plan-set.v3` 整组输出；
  `cd.route-plan.v3` 是该整组内的单路线 schema。
- 当前系统仅用于科研演示；未经标定的船舶、风险和规划参数不得用于真实航行决策。

## Unreleased

### 2026-08-18（Strategy B Semantic Hardening）

- v3 four-layer main_corridor 合同边缘修复：当 layer anchor == destination
  （recommended full ETA < 层配置上限）时，layer 搜索 ceiling 改为
  `min(request horizon, layer ceiling)`，不再被 recommended ETA 截断；
  这允许 fastest/low_risk 在 causal risk 覆盖存在时合法晚于 recommended
  到达。中间 anchor 层保持原语义。
- 新增 `test_destination_anchor_layer_allows_objectives_beyond_recommended_eta`；
  97 项 unit/integration 测试通过；RC1 golden 与 RC2 144h frozen regression
  保持 PASS（frozen 业务结果不变）。

### 2026-08-17（RC2 development）

- `bc.risk-frame.v2` 新增可选 `payload.variables.hard_reason`（NONE / LAND /
  DATA_UNAVAILABLE / OTHER）：codec 往返、Python 语义校验与 JSON Schema 同步支持；
  旧帧不含该变量仍可往返（向后兼容）。校验不变量：非 hard 单元格必须 NONE、
  hard 单元格必须给出非 NONE 原因、取值必须来自白名单。
- 新增 codec 测试（round-trip、不一致原因拒绝、旧帧兼容）；C 141 项测试通过。
- Ruff 清理 `time_dependent_astar.py`（心跳 f-string 分行重构，无语义变化）；
  `ruff check` 全绿。

### 2026-08-16（RC1）

- `RiskSampler` 重构为构造期预计算每帧 numpy 数组 + bisect 时间 bracket + 直接数组索引
  （消除热循环中的 xarray transpose/.values），数值语义不变；
- `TimeDependentAStar` 增加边几何/启发式距离/平静航速缓存与搜索进度计数
  （`C_ASTAR_PROGRESS_SECONDS` 心跳）；单目标 144h 由 >1h 优化至 ≈96s；
- 隔离 benchmark 工具 `scripts/bench_initial_planning.py`（真实 r5 risk-store 输入）；
  C 61 项单元测试（含 Dijkstra 等价性）通过。

### 变更

- 按用户确认退役 C 对旧 B `交付包.zip` 固定 `/mnt/c/...` 路径的外部制品回归及 pytest 标记；
  保留不读取该 ZIP 的显式 development-mode 门禁。当前 `UV_OFFLINE=1 make check` 为
  `138 passed`。
- 确认 v3 四层 × 三目标（12 路线整组）+ 重规划为挑战杯演示主线，v2 三目标为强制后备
  （2026-08-15）；README、DECISIONS、ACCEPTANCE 与 handoff 的主线口径同步更新。

### 后续工作

- 以指定主航区的真实 12 类、168 h `DatasetBundle v2` 完成 A→B→C 实源联调，并验收
  B 的 169 个 formal canonical 风险帧、v2 三目标、v3 四层 12 条路线和一次 6 h 时间触发
  重规划。`synthetic`、`legacy_unverified` 和测试 source snapshot 不能冒充实源证据。
- 使用真实船舶与航次数据标定航速、操舵、转弯、净空、风险权重和重规划阈值，并建立
  主线冻结参数向测试线迁移的验收基线。
- 评估 D 对 v3 原子整组的只读消费；在完整迁移证据形成前，继续保留 v2 历史解析和 v1
  审计材料。

## 0.4.0 - 2026-08-14：公共端点映射与原子四层 RoutePlan v3

### 新增

- 新增公共 `map_corridor_endpoints(...)`：只在 Corridor 声明的 allowed region 内选择未被
  hard mask 阻断的节点，要求起终点位于同一可通航连通分量，并严格执行显式最大调整
  距离；返回可审计的请求/解析坐标、距离、网格和连通性证据。
- 新增不可变 `PlanLayer`、`RoutePlanV3`、`LayerRouteBundle` 与
  `FourLayerRoutePlanSet` 合同。整组按固定顺序包含四层，每层恰好包含最快、低风险和
  综合推荐三目标，共 12 条路线。
- 新增 v3 JSON/GeoJSON Schema、严格 codec 和规范内容身份。路线使用
  `route-v3-sha256-<64hex>`，整组使用 `layer-set-sha256-<64hex>`；解码与发布都会重算
  canonical ID 并拒绝篡改或额外字段。
- 新增 `FourLayerPlanningService`：全航程层到业务终点，主通道、滚动和可执行层分别到
  全航程推荐线 72/24/6 h 截止时刻及之前的最后一个非起点航点；提前到达时使用业务
  终点，无可物化锚点时整组拒绝。
- 新增 `LayeredRoutePlanLatestStore`，以 run/scenario/generation/request/revision 围栏原子
  发布完整整组；任何一层失败、任务取消、旧代次/修订或发布冲突都不会留下部分结果。
- 正式 `PreparedRiskPlanning` 与 `RiskSourcePlanningIngress` 新增
  `execute_four_layer()`、`replan_four_layer_if_needed()`；v3 初始规划和重规划与 v2 一样
  在同一个 B committed-window execution lease 内执行。

### 变更与兼容性

- 包版本提升到 `0.4.0`；`bc.risk-frame.v2` 不变。A*、风险采样、规则网格、成本和最终
  航速算法不变，四层能力位于合同、应用编排、ingress 和发布边界。
- v3 的三个下层都引用全航程推荐计划，并显式携带关注时间窗、分层目标是否到达以及
  是否到达业务终点。四层共享同一运行身份、B 窗口、generation、revision 和三类摘要。
- `cd.route-plan.v2` Schema/codec 和三目标入口继续用于历史读取、回归与 Day 7 稳定门槛；
  v3 推广后新正式运行选择 v3，不在同一次运行双写 v2。
- v1 仍仅用于审计/显式迁移，不得进入正式 latest。

### 验收边界

- 已加入端点 allowed-region/连通性、v3 Python/JSON/GeoJSON 往返、四层锚点、12 路线
  完整性、原子发布、失败不留部分结果、取消/迟到拒绝及 v3 重规划覆盖。
- 当前 `UV_OFFLINE=1 make check` 已通过 Ruff、uv lock/sync 与 CLI；pytest 为
  `138 passed, 1 skipped`，唯一跳过项仍是未提供的可选旧版外部归档。
- 真实主航区的 12 类、168 h A bundle 尚待交付；因此本版本不能宣称完成实源四层验收、
  科学调参或真实船舶校准。
- 当前最多 10 个自然日是开发冲刺期限，不改变主/测试航区 216/144 h 运行时上限；Day 7
  先冻结可重复的 v2 主线，Day 8–9 再推广 v3，Day 10 仅做验收或阻断修复。

## 0.3.0 - 2026-08-13：规范 BC codec、原子窗口与正式规划入口

### 新增

- 新增 `bc.risk-frame.v2` canonical JSON codec，明确内存 NaN ↔ 传输 `null`、严格字段、
  Z 时间、整数 generation、JSON 属性和确定性序列化规则。
- 新增排除 `risk_id`、包含其他全部传输字段的规范内容摘要；正式 ID 固定为
  `risk-sha256-<64hex>`，解码和正式入口均重算验证。
- 新增结构化 `CommittedRiskSource`、完整 `RiskWindowQuery` 与内容寻址
  `CommittedRiskWindow`。窗口直接声明闭区间、间隔、帧数、完整身份、知识截止和提交摘要。
- 新增公共 `RiskSourcePlanningIngress.prepare/execute`，从正式已提交 B 窗口装配既有 sampler、
  grid、vessel model、time-dependent A* 与 PlanningService，不改规划核心。
- `CommittedRiskSource` 新增 execution lease；执行时复核 prepare 所见的 query、commit ID 和
  content digest，并让 B generation fence 贯穿规划与最终 RoutePlan 发布。

### 变更与加固

- Python 与 JSON Schema 的 run/实体 ID、UTC、generation 和 payload 变量集合收紧为同一
  严格交集；正式 Schema 同时要求 canonical risk ID 形状。
- v2 `risk_level` 冻结为 `min(5, floor(risk_score*5)+1)`；未知风险固定等级 5，不能由 B
  使用未版本化业务阈值覆盖。
- 正式入口要求逐小时完整闭区间已经原子提交；缺帧、重复、错位、窗口摘要篡改、错误
  as-of/代次/配置/网格和不属于该网格的 Node 均在进入规划核心前失败。
- 同一个 `RiskSourcePlanningIngress` 对同一 run/scenario 复用一个
  `PlanningCoordinator`，不同 run 隔离；并发新修订会取消旧任务，generation 在 prepare
  后切换也会阻止旧快照开始执行或迟到发布。
- `execute()` 在 execution lease 内对当前帧执行 canonical encode→decode 私有快照，并从
  私有帧重建 sampler/planner；prepare 后替换暴露 xarray 变量不能污染实际规划输入。
- `PreparedRiskPlanning` 不再暴露可直接执行的 prepare 阶段 `PlanningService`，关闭绕过
  execution lease、commit 复核和私有快照的公共旁路。
- 正式入口改为接收完整 `PlanningConfiguration`，从实际 vessel model、planner 与
  replanning 对象重算并核对 `planner_config_digest`，同时用同一重规划配置构造运行策略；
  不再信任与执行对象脱离的摘要字符串。
- 上述变更全部位于合同、codec 与 ingress 边界；既有
  `risk/grid/cost/planners/replanning/service/publishing` 核心模块保持不变。

### 兼容性

- 包版本提升到 `0.3.0`。`bc.risk-frame.v2` 名称不变，但此前由宽松 Python 模型接受、
  JSON Schema 拒绝的文档现在会被 Python 同样拒绝；正式 B 应固定依赖 C `0.3.x` 公共合同。
- 合成和 `legacy_unverified` 帧仍可使用可读 risk ID；canonical ID 强制仅针对 formal。

### 验证记录

- 当前 `make check`：Ruff、uv lock/sync 和 CLI help 通过；pytest
  `126 passed, 1 skipped`。跳过项仅因用户可选旧版外部归档未提供，不影响正式 v2 边界。
- B 跨包门槛另验证 12 类、96/168/216 h、双走廊同模型摘要、A 归档重启与 formal
  RoutePlan；输入为可复核夹具，不是实源完成声明。

## 0.2.0 - 2026-08-13：共享运行上下文与 BC/CD v2 身份合同

### 新增

- 接入独立的 `arctic_route_contracts`，统一读取版本化的 `ScenarioDefinition`、
  `CorridorDefinition`、`VesselProfile` 和 `RunContext.v2`；C 不再维护这些共享事实的副本。
- 新增 `bc.risk-frame.v2` 与 `cd.route-plan.v2` Python 合同和 JSON Schema，并保留 v1
  Schema 作为只读审计材料。
- 新增统一的 `RunContext` 绑定验证：核对场景、航区、船型的 ID、版本、内容摘要，模拟
  起止时间以及公共 `config_digest`。
- `RiskIdentity` 和 `RoutePlan` 新增显式 `provenance`；正式来源必须提供非空
  `data_id`、UTC `issue_time`、UTC `valid_time` 和小写 SHA-256 `checksum`。
- 新增开发专用上下文与 v1 风险帧迁移适配器。合成和旧数据始终标为 `synthetic` 或
  `legacy_unverified`，不能升级或重标为 `formal`。
- CLI 支持共享场景、显式模拟开始时间、候选航线距离和外部 `RunContext`，使 A 与 C
  能按相同输入物化相同的动态航程时域。

### 变更

- C 本地配置只保留船舶性能模型、规划器和重规划算法参数；场景、航区和船舶事实迁至
  共享包。公共 `config_digest`、B 的 `model_config_digest` 与 C 的
  `planner_config_digest` 各自独立，不再混称。
- 主线明确为摩尔曼斯克外海—迪克森外海；测试线明确为特罗姆瑟外海—伊斯峡湾外部
  入口。朗伊尔城仅保留为 AIS 航次识别参考点，不作为规划终点。
- 全航程时域改为按候选路线距离、参考船速和缓冲动态物化，不再固定为 7 天或 9 天。
  C 不自行扩大 A 已冻结的时窗；超过场景或来源上限时返回
  `forecast_coverage_insufficient`。
- 冻结预测与事后最佳估计使用不同的知识时间规则：冻结预测禁止消费出发后知识，历史
  最佳估计允许明确记录的事后知识，但两者都必须满足 RiskFrame 自身的 `as_of_time`
  门禁。
- 正式 `RiskFrame` 必须包含 `environment_speed_factor`。B 负责环境影响，C 只将该因子
  应用于版本化船舶性能模型以计算最终速度，不从风险分数或置信度重复推导降速。
- `RoutePlan` 的 JSON、GeoJSON 和 CD latest 发布链路原样传播 run、模型、规划器、代次
  与来源身份；D 可据此隔离不同运行结果。
- 旧版嵌套 B 制品继续由隔离适配器读取，但需要显式确认未知发布时间/有效时间，并永久
  保持非正式来源等级。当前 `工作包B.zip` 不通过该路径包装成半正式输入。

### 修复与加固

- 修复仅比较共享对象 ID/版本、未比较内容摘要的问题；同名同版本但内容被原地修改时
  现在会拒绝运行。
- 修复规划开始时间、最大耗时或 RiskFrame 窗口可能越过 `RunContext` 结束时间的问题；
  CLI 和服务均明确失败，不静默裁剪或延长。
- 修复开发 planner 缺少风险身份时仍可能发布正式外观路线的问题；正式请求现在要求
  可验证且与请求完全一致的 `risk_identity`。
- 修复 formal 来源引用可以缺少数据 ID、有效时间或 checksum 的问题；Python 语义验证
  与 JSON Schema 均要求完整证据。
- 修复合成、旧版和正式风险窗口可能混合的问题；来源等级已纳入采样窗口身份和发布
  一致性检查。
- 修复 A 与 C 对动态时域分别重建场景而产生 ID、结束时间或摘要不一致的问题；相同
  起始时间和候选距离现在得到相同物化结果，超上限时也以相同错误拒绝。

### 验证记录

- 完整 `make check`：Ruff、锁文件/同步检查和 CLI 检查通过；pytest
  `107 passed, 1 skipped`。跳过项仅依赖未提供的可选旧版外部归档，不影响 v2 正式合同。
- A/C 动态时域一致性反例覆盖 168 h、120 h 和超过 216 h 上限的失败路径。
- 合同测试覆盖摘要篡改、同 ID 内容变更、时域越界、未来知识泄漏、来源字段缺失、来源
  等级混合及无风险身份发布等失败路径。
- 合成演示仍采用粗网格进行合同/流程冒烟；端点吸附距离会显式报告，结果不作为航线
  质量或真实安全能力证明。

### 兼容性说明

- `bc.risk-frame.v1` 和 `cd.route-plan.v1` 缺少完整的 run、模型、规划器与来源身份，不能
  直接进入 v2 正式链路。B、D 消费者需要按对应 v2 合同迁移。
- 本版本没有实现 B 的逐小时预测模型，也没有实现上述四层规划编排；这些内容仍属于
  `Unreleased`，不得从 v2 合同存在推断为模型已经交付。

## 0.1.0 - 2026-08-10：工作包 C 初始实现

### 新增

- 建立独立 Python 3.13、Mamba + uv 工程，提供锁文件、Makefile、CLI、Ruff 与 pytest
  验收入口。
- 实现 `RiskFrame v1`、`RiskSource`、严格时空 `RiskSampler` 和确定性合成风险源；采样
  超窗、缺测、网格或上下文不一致时明确失败，不把缺失风险解释为安全。
- 实现规则网格、显式有限距离端点映射、船舶性能模型和等价小时成本模型。
- 实现时间依赖 A* 及 Dijkstra oracle，支持最快、低风险和综合推荐三种目标；按船舶
  到达每条边的 ETA 采样风险和环境速度系数。
- 实现五类重规划触发、防抖、迟滞、取消和请求/修订/代次发布围栏。
- 实现 `RoutePlan v1` JSON/GeoJSON 序列化及 `CDLatestStore` 原子最新值发布。
- 实现旧版嵌套 B 交付物的隔离审计/规划适配器；未知 `issue_time`、稀疏时间帧和端点
  映射必须由调用者显式确认，不能冒充正式输入。
- 建立 BC/CD 合同、成本模型、验收清单、架构追踪和决策记录，并加入单元、合同与集成
  测试。

### 初始边界

- A → B → C → D 保持单向依赖；C 不读取 A 的数据库、目录或内部缓存，也不调用 B
  的模型内部实现。
- B 提供风险、硬约束、置信度和环境影响，C 拥有最终船速、ETA、成本与路线计算。
- 演示船舶、风险和规划参数标记为 `demo_unvalidated`，不得用于真实航行安全决策。
- v1 不支持等待动作；风险时间窗必须覆盖实际 ETA，禁止外推或用零值补齐缺帧。
