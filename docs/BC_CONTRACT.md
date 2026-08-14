# B → C：RiskFrame v2

Python 真源为 `arctic_route_planning.contracts.RiskFrame`，跨语言结构见
`schemas/risk-frame-v2.schema.json`。v2 Schema 已约束完整 payload、来源项、摘要格式和
formal 必需字段；时间先后、二维数组等形状、坐标单调性、有限值与内容身份仍必须通过
Python 的规范语义验证器，不能只凭 JSON Schema 宣布正式验收。旧 v1 Schema 仅为审计留档。

规范 codec 位于 `arctic_route_planning.contracts`：

- `risk_frame_to_document(frame)`：Python/xarray → JSON-compatible document；只把未知
  `risk_score` 的 NumPy NaN 转成 JSON `null`，其他非有限值一律拒绝。
- `risk_frame_from_document(document)`：严格拒绝额外顶层字段、额外 payload 变量、非 Z
  时间和 bool/浮点 generation，并执行全部 Python 语义验证。
- `canonical_risk_frame_bytes(...)`：UTF-8、排序键、无空白、禁止 NaN 的确定性 JSON。
- `risk_frame_content_digest(...)`：排除 `risk_id`，包含其余全部规范字段的 SHA-256。
- 正式 ID 必须是 `risk-sha256-<risk_frame_content_digest>`；合成和明确隔离的旧帧可保留
  可读 ID。`validate_canonical_risk_id`、解码器和正式窗口入口都会重算验证。

## 运行身份

- `schema_version = "bc.risk-frame.v2"`。
- `run_id` 指向共享 `RunContext`；`scenario_id/corridor_id/vessel_profile_id/config_digest`
  必须与其完全一致。
- 公共 `config_digest` 只绑定共享 Scenario（含 Corridor）、Vessel 和 A DatasetBundle，
  不包含 B/C 算法参数。
- `model_config_digest` 只绑定 B 的模型、风险规则和权重；`generation_id` 隔离模拟时钟
  seek/reset，不进入 digest。
- `run_id` 必须是 `run-<UUID>`；场景/航区/船型 ID 与 JSON Schema 使用同一受限字符集；
  `generation_id` 必须是非负 Python/JSON 整数，`bool` 不作为整数接受。
- C 会按共享配置重算 Scenario/Corridor/Vessel 内容摘要及公共 `config_digest`；同名同版本但内容不同也拒绝。RiskFrame 的完整有效时窗必须位于 `RunContext` 模拟时窗内，CLI 不做静默裁剪。

`valid_time`、`as_of_time`、`generated_at` 全部为 UTC；正式帧的每个来源必须同时携带
非空 `data_id`、UTC `issue_time`、UTC `valid_time` 和小写 SHA-256 `checksum`，且
`issue_time` 不得晚于 `as_of_time`。缺任一项都不能标为 `formal`。
历史最佳估计允许知识截止晚于模拟/出发时刻；冻结预报仍禁止使用出发后知识，且请求的
`as_of_time` 不得早于所消费风险帧的知识截止。

## payload 与 ETA 采样

必需变量仍为 `risk_score`、`risk_level`、`hard_mask`、`confidence`；正式帧还必须提供
`environment_speed_factor`。C 只在两帧已发布且所有运行身份、模型、来源级别、网格都一致时插值；
超出窗口、缺测、帧间隔超限或身份不一致均拒绝。

v2 不允许 B 自定义另一套 `risk_level` 阈值。每个有限单元必须满足：

```text
risk_level = min(5, floor(risk_score * 5) + 1)
```

未知 `risk_score` 必须表示为内存 NaN/传输 `null`，并同时由 `hard_mask=true` 或
`confidence=0` 显式防止当作安全；其 `risk_level` 固定为 5。payload 只允许上述四个变量
以及传输结构中可选的 `environment_speed_factor`；但 `provenance=formal` 时该变量必需。
`route_cost`、最终船速或任意未版本化扩展都会拒绝。

## 原子窗口与正式 C 入口

B 正式存储结构化实现 `CommittedRiskSource.get_committed_window(RiskWindowQuery)` 和
`lease_committed_window(RiskWindowQuery)`，无需继承 C 类。查询键包含：

```text
start/end/interval + run_id/scenario_id/corridor_id/generation_id
+ vessel_profile_id/config_digest/model_config_digest/as_of
```

返回的 `CommittedRiskWindow` 直接携带 `start/end/interval/count`、上述完整身份、frames、
`content_digest` 和 `commit_id=risk-window-sha256-<content_digest>`。摘要绑定窗口元数据和
每个完整规范帧文档；同一查询不能提交不同内容。

`RiskSourcePlanningIngress` 只请求 60 min 严格闭区间，要求首尾、数量和每个有效时刻完全
匹配，验证正式帧 canonical ID、知识截止、网格和起终点 Node 后，才构造现有
`RiskSampler`、`RegularGrid`、`VesselPerformanceModel`、`TimeDependentAStar` 与
规划服务。`PreparedRiskPlanning.execute()`、`replan_if_needed()`、
`execute_four_layer()` 和 `replan_four_layer_if_needed()` 都必须在 execution lease 内再次验证同一
query、`commit_id` 和 `content_digest`，把当前帧经 canonical encode→decode 形成私有
快照并从该快照重建 sampler/planner，且让租约保持到 RoutePlan 发布完成；因此 source
必须让同一 run 的共享执行租约与 generation 独占切换互斥，同时允许同代次新修订进入以
触发取消、并允许不同 run 并发。入口接收完整 `PlanningConfiguration`，从实际 vessel
model、planner 和 replanning 对象重算 `planner_config_digest`，不信任调用方单独声明的
摘要。入口对同一
`(run_id, scenario_id)` 共享一个 `PlanningCoordinator`，使新代次/新修订取消旧任务并拒绝
迟到发布；不同 run 使用独立 coordinator。该入口不修改规划核心算法，也不接受普通未提交
`get_window()` 结果。

`PreparedRiskPlanning` 只暴露 query/window 等审计信息和上述安全执行入口；它不暴露可直接
调用的 prepare 阶段 `PlanningService`，避免调用者绕过租约与内容复核旁路发布。v3 四层
必须在同一租约中完整生成；C 不为每一层重新获取或混用 B 窗口。

端点经纬度到网格 Node 的解析由 C 公共 `map_corridor_endpoints(...)` 完成：候选节点必须在
共享 Corridor 的对应 allowed region 内、首帧 hard mask 可通航、位于同一连通分量且不超过
显式距离上限。调用方不得用简单四舍五入或无限距离吸附绕开该边界。

## v1 与旧 B

v1 缺少 `run_id` 和独立模型 digest，不能升级为 formal。唯一入口是显式
`adapt_risk_frame_v1(..., acknowledge_legacy_unverified=True)` 或
`LegacyBArchiveAdapter`；输出永久标记 `legacy_unverified`，不得进入正式历史回放。
