# C → D：RoutePlan v2

Python 真源为 `arctic_route_planning.contracts.RoutePlan`，跨语言结构见
`schemas/route-plan-v2.schema.json`。序列化器可生成 JSON 和 GeoJSON。

## 关键规则

- `schema_version = "cd.route-plan.v2"`；单位明确为 km、h、m/s 和 UTC。
- 原样传播 `run_id`、共享 `config_digest`、B 的 `model_config_digest` 和 C 的
  `planner_config_digest`；D 必须按这些身份隔离展示缓存。
- 原样传播 RiskFrame 窗口的 `provenance`。`synthetic` 和 `legacy_unverified`
  路线可供开发展示，但不得显示或重标为 `formal`。
- `formal` 请求必须由暴露了可验证 `risk_identity` 的规划器执行；C 会核对
  其 run/scenario/corridor/vessel/digest/generation/provenance，缺失身份时 fail closed。
- `generation_id/planning_request_id/input_revision` 共同构成发布围栏，但不混入 digest。
- `corridor_id` 与 `plan_id` 分离；`objective_mode` 与 `plan_kind` 分离。
- 至少两个航点，ETA 严格递增且可复算；硬约束违规必须为 0，并引用真实
  `source_risk_ids`。

## CD latest

`CDLatestStore` 只有在上述全部发布身份与当前 `PublicationToken` 完全一致且未取消时才原子
发布。run、场景、代次或任一 digest 改变都会立即隐藏旧路线，防止 A/B/C 不同时间或
不同配置的结果混显。

旧 `cd.route-plan.v1` 只保留 Schema 作为审计材料；它缺少可证明的 run/model/planner
身份，不进入 v2 正式 latest。
