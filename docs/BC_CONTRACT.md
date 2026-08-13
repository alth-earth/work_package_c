# B → C：RiskFrame v2

Python 真源为 `arctic_route_planning.contracts.RiskFrame`，跨语言结构见
`schemas/risk-frame-v2.schema.json`。v2 Schema 已约束完整 payload、来源项、摘要格式和
formal 必需字段；时间先后、二维数组等形状、坐标单调性、有限值与内容身份仍必须通过
Python 的规范语义验证器，不能只凭 JSON Schema 宣布正式验收。旧 v1 Schema 仅为审计留档。

## 运行身份

- `schema_version = "bc.risk-frame.v2"`。
- `run_id` 指向共享 `RunContext`；`scenario_id/corridor_id/vessel_profile_id/config_digest`
  必须与其完全一致。
- 公共 `config_digest` 只绑定共享 Scenario（含 Corridor）、Vessel 和 A DatasetBundle，
  不包含 B/C 算法参数。
- `model_config_digest` 只绑定 B 的模型、风险规则和权重；`generation_id` 隔离模拟时钟
  seek/reset，不进入 digest。
- `RiskSource.get_window()` 在读窗口时就校验 `corridor_id`，不允许等到规划发布才发现串线。
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

## v1 与旧 B

v1 缺少 `run_id` 和独立模型 digest，不能升级为 formal。唯一入口是显式
`adapt_risk_frame_v1(..., acknowledge_legacy_unverified=True)` 或
`LegacyBArchiveAdapter`；输出永久标记 `legacy_unverified`，不得进入正式历史回放。
