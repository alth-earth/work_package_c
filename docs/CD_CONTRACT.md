# C → D：RoutePlan v1

Python 真源为 `arctic_route_planning.contracts.RoutePlan`，跨语言结构见
`schemas/route-plan-v1.schema.json`。序列化器可生成 JSON 和 GeoJSON。

## 关键规则

- `schema_version = "cd.route-plan.v1"`；单位明确为 km、h、m/s 和 UTC。
- `corridor_id` 与具体 `plan_id` 分离；`objective_mode`
  (`fastest/low_risk/recommended`) 与 `plan_kind` (`initial/replanned`) 分离。
- 至少两个航点，首航点 ETA 等于 `start_time`，后续 ETA 严格递增；
  `metrics.eta_hours` 必须与末航点 ETA 可复算。
- 发布路线的 `hard_constraint_violations` 必须为 0；必须引用实际
  `source_risk_ids`。
- `config_digest` 绑定场景、船型和算法配置；`generation_id`、
  `planning_request_id` 和 `input_revision` 共同提供发布围栏。

## CD latest

`CDLatestStore` 是线程安全的最新值缓存，保留当前路线、上一版和同批候选路线。
只有与当前 `PublicationToken` 完全匹配且未取消的结果才能原子发布。场景或代次切换时立即隔离旧路线；C 计算变慢/失败时，D 仍可读取同代次最近有效值。

GeoJSON 的第一个 Feature 是路线 `LineString`，后续 Feature 是航点；每个航点均携带 ETA 和推荐速度。

