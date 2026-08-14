# C → D：RoutePlan v3 与 v2 兼容

## 正式新输出：v3 原子四层整组

Python 真源是：

- `arctic_route_planning.contracts.RoutePlanV3`
- `arctic_route_planning.contracts.FourLayerRoutePlanSet`

跨语言结构见：

- `schemas/route-plan-v3.schema.json`
- `schemas/route-plan-v3.geojson.schema.json`
- `schemas/four-layer-route-plan-set-v3.schema.json`
- `schemas/four-layer-route-plan-set-v3.geojson.schema.json`

`RoutePlanV3` 继承 v2 的完整运行身份、来源、航点和指标，并新增：

- `planning_layer`；
- `layer_set_id`；
- `focus_start_time/focus_end_time`；
- 下层的 `reference_plan_id`；
- `layer_goal_reached` 与 `destination_reached`。

`FourLayerRoutePlanSet` 必须按固定顺序原子包含：

1. `full_voyage`；
2. `main_corridor_24_72h`；
3. `rolling_0_24h`；
4. `executable_0_6h`。

每层恰好包含 `fastest`、`low_risk`、`recommended`，整组共 12 条路线。全航程推荐线是
其他三层唯一参考计划；下层终点分别取其 72/24/6 h 截止时刻及之前的最后一个非起点
航点。全航程提前结束时使用业务终点；无可物化锚点时整组拒绝。

## 身份与内容完整性

- 路线 `schema_version = "cd.route-plan.v3"`；整组
  `schema_version = "cd.four-layer-route-plan-set.v3"`。
- 原样传播 `run_id`、场景、航区、船型、公共 `config_digest`、B
  `model_config_digest`、C `planner_config_digest`、provenance 和 generation。
- 12 条路线必须共享同一个 planning request、input revision、生成时刻、知识截止、开始
  时刻、plan kind 和 replan reasons。
- 规范路线身份为 `route-v3-sha256-<64hex>`，整组身份为
  `layer-set-sha256-<64hex>`；codec 与 store 都会重算并拒绝篡改。
- 至少两个航点，ETA 严格递增且可复算；硬约束违规必须为 0，并引用实际
  `source_risk_ids`。
- D 必须按完整运行身份和 `layer_set_id` 隔离缓存，不能跨 run、generation 或摘要混显。

## 原子 layered latest

`LayeredRoutePlanLatestStore` 只在完整四层、12 条路线和当前 publication token 全部一致且
未取消时发布。任何一层失败、generation/revision 过期、canonical ID 篡改或发布冲突都
不会留下部分结果。重规划成功时，新整组原子替换旧整组；被取消或迟到的旧任务不能覆盖。

## provenance

路线原样传播 B 窗口的 provenance。`synthetic` 和 `legacy_unverified` 可以用于开发展示，
不得显示或重标为 `formal`。正式请求必须由带可验证 `risk_identity` 的规划器执行，C 会
核对 run/scenario/corridor/vessel/digest/generation/provenance。

## v2 兼容策略

`arctic_route_planning.contracts.RoutePlan` 与
`schemas/route-plan-v2.schema.json` 继续保留，供历史结果读取、兼容回归和 Day 7 三目标
基线使用。正式 ingress 的 v2 初始/重规划 API 也保留用于该门槛。

v3 通过完整门槛后，新正式运行应显式选择 v3；同一次运行不得同时发布 v2 和 v3。v2
历史结果不会自动升级为 v3，也不能拼接成四层整组。

旧 `cd.route-plan.v1` 只保留 Schema 作为审计材料；它缺少完整 run/model/planner 身份，
不进入正式 latest。
