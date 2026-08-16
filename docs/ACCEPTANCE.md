> [!NOTE]
> **文档治理声明**
> - 文件角色：工作包 C 当前挑战杯工程演示验收真源。
> - 改造时间：2026-08-15（Asia/Shanghai）。
> - 原文件去向：[ACCEPTANCE_归档_20260815.md](ACCEPTANCE_归档_20260815.md)。
> - 改造原因：把工程演示验收与未来科学接口分开，避免科学门槛阻塞比赛闭环。

# 工作包 C 验收清单

## A. 工程门禁

```bash
cd /root/my_project/work_package_c
UV_OFFLINE=1 make check
git diff --check
```

Ruff、pytest、lock/sync 和 CLI 必须通过。当前快照为 138 passed。

## B. 身份与时间

- 同一 RunContext、scenario、corridor、vessel、generation 和摘要贯穿输入输出；
- 历史回放保持 `issue_time <= as_of_time <= simulation_time`；
- 稳定演示读取冻结本地数据，但不跳过版本/代次围栏；
- `valid_time` 覆盖路线实际 ETA，不把一张风险图用于全航程。

补充（源自：ACCEPTANCE_归档_20260815.md）：

- `ScenarioDefinition`、`CorridorDefinition`、`VesselProfile` 和 `RunContext.v2` 来自
  `arctic_route_contracts`，不使用 C 的 legacy 场景/船型夹具；
- `RunContext` 的 ID、版本、内容摘要、时间窗和公共 `config_digest` 必须与实际共享配置一致；
- 冻结预报不得使用出发后知识；历史最佳估计必须明确声明知识截止；
- C 请求时域不得超过 `RunContext.simulation_end` 或 C 216 h 硬上限。

## C. B→C 输入

- RiskFrame 包含 risk、level、hard mask、confidence 和 environment speed factor；
- 窗口时序连续、网格和上下文一致，未知风险不当安全；
- canonical commit 与 execution lease 在执行期间稳定；
- 演示降级输入必须明确标 `demo_unvalidated`，不得伪装 scientific/calibrated。

补充（源自：ACCEPTANCE_归档_20260815.md）：

- 只接受 `bc.risk-frame.v2`、`provenance=formal` 的 canonical RiskFrame；
- 每个正式来源项都有 `data_id`、UTC `issue_time/valid_time` 和小写 SHA-256 checksum；
- payload 含 `risk_score`、`risk_level`、`hard_mask`、`confidence` 和
  `environment_speed_factor`；未知风险不能当安全；
- B store 对完整身份查询返回 canonical、内容寻址的 `CommittedRiskWindow`；
- 窗口按 60 min 严格覆盖闭区间，无缺帧、重复、错位或超窗外推；
- prepare 与 execute 之间的 commit ID/content digest 一致，execution lease 持续到发布结束。

## D. 端点与路线合理性

- 端点修正只发生在 corridor allowed region，记录距离与理由；
- 路线不穿陆地/硬掩膜，起终点不合并；
- ETA 严格递增，距离、风险和目标指标可复算；
- `fastest`、`low_risk`、`recommended` 三目标完整；
- 地图上无明显经纬翻转、跨陆直线、极端锯齿或无解释绕行；
- 风险变化后路线变化方向能用风险图和指标解释。

补充（源自：ACCEPTANCE_归档_20260815.md）：

- 正式调用方使用 `map_corridor_endpoints(...)`，不自行四舍五入经纬度；
- 节点位于起点/终点 allowed region 内，未被首帧 hard mask 阻断，且属于同一可通航连通分量；
- 调整距离不超过调用方显式给定的上限；请求/解析坐标、距离和连通性证据可审计；
- allowed region 无网格点、无可航点、无同分量点、超距离或起终点合并时明确失败。

## E. 重规划与发布

- 至少保存一次初始规划和一次模拟时间/风险更新后的重规划；
- 旧 generation/request/revision 不能覆盖新结果；
- CD latest 原子替换，不留下部分路线集；
- D 读取发布制品，不直接调用内部 planner。

补充（源自：ACCEPTANCE_归档_20260815.md）：

- 发布令牌同时绑定 run/scenario/generation/request/revision 和三类 digest；
- 同 run 新 generation 或更新 revision 能取消旧任务；不同 run 相互隔离；
- 正式重规划前已有同代次发布计划；`input_revision` 严格递增；
- `observed_at` 和 `risk_valid_time` 等于新请求 `start_time`；`risk_revision` 等于当前 B
  窗口 commit ID；
- 成功 v3 重规划以新完整整组原子替换旧整组。

## F. D 展示

- 显示当前风险、未来风险帧、三目标路线和 ETA/距离/风险等指标；
- 明确当前 simulation/valid time、场景和 generation；
- 规划计算期间保留上一完整结果并显示状态，不冻结界面；
- 显示“挑战杯演示、参数未科学校准、禁止真实导航”警示。

## G. v3 主线整组闸门（2026-08-15 确认 v3 为演示主线）

四层各三目标、共 12 条路线必须整组成功后才发布。若性能不足，v2 三目标 + 重规划仍可满足
挑战杯后备路径，不能发布不完整 v3。v2 仅作为强制后备，不作为首选验收结论。

补充（源自：ACCEPTANCE_归档_20260815.md）：

- 四层固定为 `full_voyage`、`main_corridor_24_72h`、`rolling_0_24h`、
  `executable_0_6h`；每层恰好三目标；
- 四层共享同一 RunContext、B committed window/execution lease、generation、revision、
  三类配置摘要和全航程推荐线；
- 下层终点为全航程推荐线在 72/24/6 h 截止时刻及之前的最后一个非起点航点；
- 全航程提前结束时使用业务终点；无可物化锚点时整组拒绝；
- JSON/GeoJSON codec 重算 canonical 路线和整组 ID，拒绝额外字段和篡改；
- 任一层失败、取消、过期代次/修订或发布冲突时，layered latest 不留部分整组。

## H. 科学接口（非阻塞）

海洋/气象、冰情、船舶、航运/法规、数据/模型接口和 calibration status 保留。真船标定、专业
签字、概率误差和适航认证不是工程演示通过条件。参数优先公开典型值，次选拟合，并标明来源。

## I. 验收结论

当 A 冻结数据 → B 风险 → C 三路线/重规划 → D 展示能无网稳定重复，且路线与风险没有明显
荒谬结果时，挑战杯工程演示可判定通过。默认验收路径为 v3 四层整组 + 重规划；若 v3 因性能
或数据原因无法整组发布，按后备规则以 v2 三目标 + 重规划验收，并如实标注。该结论不授予
真实导航或科学准确性。

## J. 禁止的验收替代（源自：ACCEPTANCE_归档_20260815.md）

- 不得用 `make demo` 代替正式 orchestrator/ingress 验收；
- 不得用 `synthetic`、`legacy_unverified` 或 test source snapshot 冒充实源；
- 不得把 `formal` provenance 解释为风险模型或船模已 calibrated；
- 不得用测试全绿代替净水深、法规区、真船参数或真实航次证据；
- 不得把默认 5×5 粗网格 smoke 当作路线质量或性能基线。

验收报告至少记录：输入 provenance、calibration status、RunContext/config/model/planner
digests、B commit ID、端点调整、帧数/时域、路线指标、重规划原因、测试结果及未关闭闸门。
