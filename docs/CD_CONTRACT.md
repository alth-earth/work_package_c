---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - PLANNED
Document Role: CANONICAL
Scope: C to D route artifact contract
Branch: research-validation-system
Last Verified: 2026-08-21
---

> [!NOTE]
> **文档治理声明**
>
> - 文件角色：当前 C→D 输出合同说明；Python 模型和 JSON Schema 仍是机器可执行真源。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文件去向：`docs/archive/CD_CONTRACT.archive-20260814-pre-governance.md`。
> - 改造原因：保留 v3/v2 稳定技术语义，移除冲刺日历职责。

# C → D：RoutePlan v3 与 v2 兼容

## 正式新输出：v3 原子四层整组

Python 真源：

- `arctic_route_planning.contracts.RoutePlanV3`；
- `arctic_route_planning.contracts.FourLayerRoutePlanSet`。

跨语言 Schema：

- `schemas/route-plan-v3.schema.json`；
- `schemas/route-plan-v3.geojson.schema.json`；
- `schemas/four-layer-route-plan-set-v3.schema.json`；
- `schemas/four-layer-route-plan-set-v3.geojson.schema.json`。

`RoutePlanV3` 继承 v2 的运行身份、来源、航点和指标，并新增：

- `planning_layer`、`layer_set_id`；
- `focus_start_time/focus_end_time`；
- 下层 `reference_plan_id`；
- `layer_goal_reached`、`destination_reached`。

## 四层语义

`FourLayerRoutePlanSet` 必须按固定顺序原子包含：

1. `full_voyage`；
2. `main_corridor_24_72h`；
3. `rolling_0_24h`；
4. `executable_0_6h`。

每层恰好包含 `fastest`、`low_risk`、`recommended`，整组共 12 条路线。全航程推荐线是
其他三层唯一参考计划；下层终点分别取该路线 72/24/6 h 截止时刻及之前的最后一个
非起点航点。全航程提前结束时使用业务终点；无可物化锚点时整组拒绝。

## 身份与内容完整性

- 路线 `schema_version = "cd.route-plan.v3"`；整组
  `schema_version = "cd.four-layer-route-plan-set.v3"`。
- 原样传播 `run_id`、scenario/corridor/vessel、公共 `config_digest`、B
  `model_config_digest`、C `planner_config_digest`、provenance 和 generation。
- 12 条路线共享 planning request、input revision、生成时刻、知识截止、开始时刻、
  plan kind 和 replan reasons。
- 规范路线身份为 `route-v3-sha256-<64hex>`，整组身份为
  `layer-set-sha256-<64hex>`；codec 和 store 都会重算。
- 路线至少两个航点，ETA 严格递增且可复算，硬约束违规数为 0，并引用实际
  `source_risk_ids`。
- D 必须按完整运行身份和 `layer_set_id` 隔离缓存，不得跨 run、generation 或 digest 混显。

## 原子 layered latest

`LayeredRoutePlanLatestStore` 只在完整四层、12 条路线、canonical ID 和当前 publication token
全部一致且未取消时发布。任一层失败、generation/revision 过期、ID 篡改或发布冲突都不会
留下部分结果。重规划成功时，新整组原子替换旧整组。

## provenance 与 calibration

路线原样传播 B 窗口 provenance。`synthetic` 和 `legacy_unverified` 可用于开发展示，不得显示或
重标为 `formal`。正式请求必须有可验证 `risk_identity`。

`formal` 仅证明运行身份和来源链通过合同；D 不得因此隐藏 B/C 模型仍为
`demo_unvalidated` 的科学限制。

## v2 兼容与 v1 历史

`arctic_route_planning.contracts.RoutePlan` 与 `schemas/route-plan-v2.schema.json` 继续保留，用于历史读取、
兼容回归和显式选择的 v2 三目标基线。

一次新运行必须显式选择 v2 或 v3，不得双写。v2 历史结果不会自动升级为 v3，也不得拼接成
四层整组。

`cd.route-plan.v1` 只保留 Schema 作为历史审计/显式迁移材料。

> ⚠️ 与现状不符：任何把 v1 描述为当前正式 C→D 合同的文档都是过时内容。

## 可选 sidecar：推荐选择理由（Selection Rationale v1）

> 跨包合约变更提案：[`CD_CONTRACT_SELECTION_RATIONALE_PROPOSAL.md`](CD_CONTRACT_SELECTION_RATIONALE_PROPOSAL.md)（状态 `APPROVED`，2026-08-24 C/D 负责方批准）。

`selection-rationale` 是**可选、独立**的 sidecar，解释 C 为何选择 `recommended` 路线而非
`fastest` 基线。它不进入任何路线或整组的内容身份/digest（保持 SSOT 与失败关闭语义）。

机器真源：

- `arctic_route_planning.publishing.SelectionRationale`；
- `schemas/selection-rationale-v1.schema.json`。

承载位置（均为可选字段，缺省 `None`）：

- v2：`PlanningBatch.selection_rationale`；
- v3：`FourLayerPlanningOutcome.selection_rationale`（由 `full_voyage` 层的 recommended 与 fastest 派生）；
- CLI 额外写出 `selection-rationale.json`，并在 `run-summary.json` 增加 `selection_rationale` 摘要段。

语义约束：

- `schema_version = "selection-rationale.v1"`；
- `baseline_objective` 必须为 `fastest`（Schema `const`），保证权衡基准恒定；
- `tradeoffs` 含 delta 距离/ETA/风险与平均/最大风险降低百分比；风险 delta 限定在 [-1, 1]；
- `selected_objective` ∈ `{fastest, low_risk, recommended}`；
- 无推荐/最快对偶时不产出 rationale（字段为 `None`，不写文件），主路线合同不受影响。

D 必须将 rationale 视为可选；旧 C 不产 rationale 时正常降级显示，不得因此阻塞路线消费。

## 正式兄弟合同：Route Motion Set v1（2026-08-31）

`cd.route-motion-set.v1` 是 `FourLayerRoutePlanSet` 的正式兄弟 artifact，不修改
`RoutePlanV3` waypoint、ETA、metrics 或身份算法。Python 真源、codec 和 Schema 分别为：

- `arctic_route_planning.contracts.RouteMotionSet`；
- `arctic_route_planning.publishing.route_motion_serialization`；
- `schemas/route-motion-set-v1.schema.json`。

正式 producer 入口为 `arctic-route-motion`。它严格绑定四层 plan set、RunContext、显式
RiskWindow commit 和声明 GEBCO raster，生成 `route-motion-set.json`、版本化 vessel
profile 与 `checksums.json`；输出目录已存在时拒绝覆盖。

每个集合按四层固定顺序恰好绑定四条 `recommended` 路线；每条记录使用包含坐标、ETA 和
`recommended_speed_mps` 的完整 waypoint digest。`CURVE` 记录的
`motion_samples` 是 D 绘线、船位、航向、航速、trail 和 completed-track 的唯一曲线源；
`RAW_PASSTHROUGH` 是合法的逐层回退。整组 `motion_set_id` 对除自身外的规范 JSON 求
SHA-256，任一记录缺失、顺序/plan/RiskWindow/vessel/generation 身份不符或 digest 篡改时，
Orchestrator 不把该集合注入 Viewer bundle，D 则回退原始 waypoint/timeline。

配套 `c.route-motion-vessel-profile.v1` 使用
`nordic_odyssey_formula_reference_v1`：`225 m × 32.31 m`、吃水 `14.08 m`、经济航速
`10 kn`、上界 `15.7 kn`，且
`R_min(v)=max(2000 m, v/0.15°s⁻¹, v²/0.02 m s⁻²)`。它明确标记
`FORMULA_DERIVED_ENGINEERING_REFERENCE`、`real_vessel_calibrated=false`。连续走廊证明也只
声明 `CONTINUOUS_IN_DECLARED_RASTER_MODEL`；`navigation_grade=false`、bathymetry/UKC 未启用。
因此本合同中的“生产”仅表示当前工程仿真正式消费路径，不表示实船校准、导航认证或适航证明。

### Route motion qualification evidence v1（2026-09-02）

`c.route-motion-qualification-evidence.v1` 是 C-owned、可选的 motion 证据 artifact，
服务于现有两个 motion wire shape。它不改变 `cd.route-motion-set.v1` 或
`cd.route-motion-candidate-set.v1`；RoutePlanV3、waypoint/ETA/metrics、plan identity 和
正式 motion record 继续保持权威。CLI 将证据写在 motion JSON 同目录，并纳入同目录的
`checksums.json`。

producer 以确定性顺序先尝试起终点大圆边，再尝试有界的非相邻 waypoint-index 捷径；
每条捷径都经过完整时域 envelope，候选端点只能是原始 RoutePlan waypoint。最终候选使用
route-level joint cubic B-spline；单角 trim 严格小于 `0.5` 个相邻航段，任一共享航段的
总 trim 不超过 `0.90` 个航段。被捷径跳过的 waypoint 仍是严格 ETA/弧长 anchor，但不写入
曲线几何。

资格门禁顺序固定为
`sea_land_hard_mask → temporal_risk_coverage → corridor_allowed_area → manoeuvring →
eta_speed → risk_non_degradation → adaptive_trust_deviation`。`hard_mask`、`LAND`、
`DATA_UNAVAILABLE`、unknown、缺帧/部分 RiskWindow、越界/外推、走廊不确定、anchor 非单调
及任一曲线/ETA 门禁失败均 fail-closed 到 `RAW_PASSTHROUGH`。adaptive trust 使用
candidate→raw、raw→candidate 双向偏离和 anchor 投影，并随已证明的 raster 净空收紧；无法
证明净空即回退。`RiskSampler.sample_interval` 与 swept-cell temporal envelope 是正式
时域 API，普通逐点采样不构成完整时域证明。

Orchestrator 校验证据的 identity、producer/RiskWindow digest、record cardinality、
plan/objective binding 和 `details_digest`；没有 sidecar 的旧 v1 motion 目录继续可读。
证据与曲线仍仅代表工程仿真：`real_vessel_calibrated=false`、`navigation_grade=false`，
且不启用 bathymetry/UKC。
