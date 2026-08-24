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
> - 原文件去向：`CD_CONTRACT.archive-20260814-pre-governance.md`。
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
