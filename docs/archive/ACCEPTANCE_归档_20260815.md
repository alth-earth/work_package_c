> [!NOTE]
> **二次文档治理归档声明**
> - 本文件角色：2026-08-15 改造前的 C 验收清单快照，仅供历史追溯。
> - 归档时间：2026-08-15（Asia/Shanghai）。
> - 现行文件：[ACCEPTANCE.md](ACCEPTANCE.md)。
> - 归档原因：把挑战杯工程演示验收与未来科学接口明确拆分，避免科学门槛阻塞演示。

<!-- ORIGINAL CONTENT START -->

> [!NOTE]
> **文档治理声明**
>
> - 文件角色：工作包 C 的稳定验收真源。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文件去向：`ACCEPTANCE.archive-20260814-pre-governance.md`。
> - 改造原因：使用可重复的技术闸门代替逐日排期；实际日程统一由顶层冲刺文档管理。

# 工作包 C 验收清单

本文定义“什么证据才能宣称某项能力已通过”。排期与人员安排以
[`ABC_10_DAY_SPRINT.md`](../../ABC_10_DAY_SPRINT.md) 为准。

## A. 工程基线闸门

```bash
cd /root/my_project/work_package_c
make env-create
make lock
make sync
UV_OFFLINE=1 make check
git diff --check
```

`make check` 必须同时通过 Ruff、pytest、`uv lock --check`、`uv sync --check` 和 CLI help。
当前已复验：`138 passed`。已退役依赖旧 B `交付包.zip` 固定外部路径的可选回归；legacy
隔离边界继续由仓库内可重复测试验证。

## B. 共享上下文闸门

- `ScenarioDefinition`、`CorridorDefinition`、`VesselProfile` 和 `RunContext.v2` 来自
  `arctic_route_contracts`，不使用 C 的 legacy 场景/船型夹具。
- `RunContext` 的 ID、版本、内容摘要、时间窗和公共 `config_digest` 必须与实际共享配置一致。
- 冻结预报不得使用出发后知识；历史最佳估计必须明确声明其知识截止。
- C 请求时域不得超过 `RunContext.simulation_end` 或 C 216 h 硬上限。

## C. 正式 B→C 输入闸门

- 只接受 `bc.risk-frame.v2`、`provenance=formal` 的 canonical RiskFrame。
- 每个正式来源项都有 `data_id`、UTC `issue_time/valid_time` 和小写 SHA-256 checksum。
- payload 含 `risk_score`、`risk_level`、`hard_mask`、`confidence` 和
  `environment_speed_factor`；未知风险不能当安全。
- B store 对完整身份查询返回 canonical、内容寻址的 `CommittedRiskWindow`。
- 窗口按 60 min 严格覆盖闭区间，无缺帧、重复、错位或超窗外推。
- prepare 与 execute 之间的 commit ID/content digest 一致，execution lease 持续到发布结束。

## D. 端点映射闸门

- 正式调用方使用 `map_corridor_endpoints(...)`，不自行四舍五入经纬度。
- 节点位于起点/终点 allowed region 内，未被首帧 hard mask 阻断，且属于同一可通航连通分量。
- 调整距离不超过调用方显式给定的上限；请求/解析坐标、距离和连通性证据可审计。
- allowed region 无网格点、无可航点、无同分量点、超距离或起终点合并时明确失败。

## E. v2 三目标闸门

- 同一请求返回 `fastest`、`low_risk`、`recommended`。
- ETA 严格递增，硬约束违规数为 0，距离、ETA、风险和 `source_risk_ids` 可复算。
- 小网格时间依赖 A* 与零启发 Dijkstra 成本一致。
- 路线随 ETA 时刻的风险变化，而不是对一张风险图做静态路径。
- v2 只用于兼容读取、回归和显式选择的基线运行；不与 v3 在同一运行双写。

## F. v3 四层整组闸门

- 四层固定为 `full_voyage`、`main_corridor_24_72h`、`rolling_0_24h`、
  `executable_0_6h`；每层恰好三目标，共 12 条路线。
- 四层共享同一 RunContext、B committed window/execution lease、generation、revision、
  三类配置摘要和全航程推荐线。
- 下层终点为全航程推荐线在 72/24/6 h 截止时刻及之前的最后一个非起点航点。
- 全航程提前结束时使用业务终点；无可物化锚点时整组拒绝。
- JSON/GeoJSON codec 重算 canonical 路线和整组 ID，拒绝额外字段和篡改。
- 任一层失败、取消、过期代次/修订或发布冲突时，layered latest 不留部分整组。

## G. 重规划与发布闸门

- 发布令牌同时绑定 run/scenario/generation/request/revision 和三类 digest。
- 同 run 新 generation 或更新 revision 能取消旧任务；不同 run 相互隔离。
- 正式重规划前已有同代次发布计划；`input_revision` 严格递增。
- `observed_at` 和 `risk_valid_time` 等于新请求 `start_time`；`risk_revision` 等于当前 B 窗口 commit ID。
- 成功 v3 重规划以新完整整组原子替换旧整组。

## H. 实源、科学和 D 验收闸门

只有同时满足以下条件，才能把工程结果提升为相应的实源/科学主张：

- 指定主航区的真实 A `DatasetBundle v2` 与可重放 `RunContext.v2` 通过 A 验收；
- B 对该上下文发布完整 formal canonical 窗口；
- C 的 v2/v3 输出和至少一次可解释重规划可断网重复；
- 风险、船型、冰级、航速、操舵、转弯、净空、权重和阈值具有真实标签/航次校准证据；
- D 按完整运行身份只读消费 v3 原子整组，不读取部分或过期结果。

截至 2026-08-14，上述实源、科学校准和 D 闸门尚未在 C 交付证据中关闭。

## 禁止的验收替代

- 不得用 `make demo` 代替正式 orchestrator/ingress 验收。
- 不得用 `synthetic`、`legacy_unverified` 或 test source snapshot 冒充实源。
- 不得把 `formal` provenance 解释为风险模型或船模已 calibrated。
- 不得用测试全绿代替净水深、法规区、真船参数或真实航次证据。
- 不得把默认 5×5 粗网格 smoke 当作路线质量或性能基线。

验收报告至少记录：输入 provenance、calibration status、RunContext/config/model/planner
digests、B commit ID、端点调整、帧数/时域、路线指标、重规划原因、测试结果及未关闭闸门。
