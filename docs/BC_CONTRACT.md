# B → C：RiskFrame v1

Python 真源为 `arctic_route_planning.contracts.RiskFrame`，跨语言结构见
`schemas/risk-frame-v1.schema.json`。

## 顶层语义

- `schema_version = "bc.risk-frame.v1"`。
- `scenario_id/corridor_id/vessel_profile_id/config_digest/generation_id`
  共同冻结一次规划上下文。
- `valid_time` 是风险描述的时刻；`as_of_time` 是本次计算允许使用信息的截止时刻；
  `generated_at` 是算法墙钟完成时间。全部为 UTC。
- 正式来源的每个 `source_summary.issue_time` 必须存在且不晚于
  `as_of_time`。
- `provenance` 明确区分 `formal`、`legacy_unverified` 和 `synthetic`。

## payload

v1 只接受 EPSG:4326 上严格递增的一维 `latitude`/`longitude` 坐标，每个变量都是
`(latitude, longitude)` 二维网格。

| 变量 | 必需 | 范围/语义 |
|---|---:|---|
| `risk_score` | 是 | `[0,1]` 连续软风险；缺测只能由 `hard_mask` 或 `confidence=0` 显式表达 |
| `risk_level` | 是 | `1..5` 整数，用于展示/解释 |
| `hard_mask` | 是 | bool；`True` 表示不得扩展 |
| `confidence` | 是 | `[0,1]` 有限值 |
| `environment_speed_factor` | 正式帧必需 | B 声明的 `(0,1]` 综合环境影响；C 用它计算最终有效航速 |

C 不从 `risk_score` 反推速度损失。正式 B 若认为没有速度影响，仍应给出明确的中性系数 1.0 和来源/质量说明，不应让 C 猜测。合成夹具可省略该可选物理效应，但输出必须标记为开发演示。

## 窗口和 ETA 采样

`RiskSource.get_window()` 按场景、代次、船型、配置和 `as_of` 筛选。同一
`valid_time` 只返回当时可见的最新版本。`RiskSampler` 的规则为：

- `risk_score`：空间双线性、时间线性；
- `hard_mask`：所有参与单元和两个时间边界取 OR；
- `confidence` 和 `environment_speed_factor`：取参与值的保守最小值；
- `risk_level`：由采样后的 `risk_score` 重新分级；
- 超出窗口、帧间隔超限或上下文不一致：拒绝，绝不当作安全。

## 旧 B 适配

`LegacyBArchiveAdapter` 是显式开发模式：仅读嵌套的综合风险 NetCDF，不读
`route_cost_grid`，不从文件名/mtime 猜 `issue_time`。旧 `sea_mask` 只能得到“陆地硬约束”，置信度限制为 `<=0.40`，速度系数为带警告的中性 1.0。这些帧永远是 `legacy_unverified`，不是正式 B 输出。
