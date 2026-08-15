# 工作包 C 变更记录

本文件记录工作包 C 的可见功能、跨包合同、兼容性和验证状态变化。项目用途、运行方法
与当前架构请先阅读 [README.md](README.md)；长期设计取舍见
[决策记录](docs/DECISIONS.md)。

## 记录规则

- 版本按时间倒序记录；`Unreleased` 表示已规划但尚未纳入当前版本的工作。
- “新增/变更/修复”只描述已经落入代码、配置、Schema、测试或文档的内容。
- “后续工作”不是完成声明，也不构成正式接口承诺。
- BC/CD 合同版本与 Python 包版本是不同概念。例如包版本 `0.4.0` 继续消费
  `bc.risk-frame.v2`，并提供 `cd.route-plan.v3` 整组输出。
- 当前系统仅用于科研演示；未经标定的船舶、风险和规划参数不得用于真实航行决策。

## Unreleased

### 变更

- 按用户确认退役 C 对旧 B `交付包.zip` 固定 `/mnt/c/...` 路径的外部制品回归及 pytest 标记；
  保留不读取该 ZIP 的显式 development-mode 门禁。当前 `UV_OFFLINE=1 make check` 为
  `138 passed`。

### 后续工作

- 以指定主航区的真实 12 类、168 h `DatasetBundle v2` 完成 A→B→C 实源联调，并验收
  B 的 169 个 formal canonical 风险帧、v2 三目标、v3 四层 12 条路线和一次 6 h 时间触发
  重规划。`synthetic`、`legacy_unverified` 和测试 source snapshot 不能冒充实源证据。
- 使用真实船舶与航次数据标定航速、操舵、转弯、净空、风险权重和重规划阈值，并建立
  主线冻结参数向测试线迁移的验收基线。
- 评估 D 对 v3 原子整组的只读消费；在完整迁移证据形成前，继续保留 v2 历史解析和 v1
  审计材料。

## 0.4.0 - 2026-08-14：公共端点映射与原子四层 RoutePlan v3

### 新增

- 新增公共 `map_corridor_endpoints(...)`：只在 Corridor 声明的 allowed region 内选择未被
  hard mask 阻断的节点，要求起终点位于同一可通航连通分量，并严格执行显式最大调整
  距离；返回可审计的请求/解析坐标、距离、网格和连通性证据。
- 新增不可变 `PlanLayer`、`RoutePlanV3`、`LayerRouteBundle` 与
  `FourLayerRoutePlanSet` 合同。整组按固定顺序包含四层，每层恰好包含最快、低风险和
  综合推荐三目标，共 12 条路线。
- 新增 v3 JSON/GeoJSON Schema、严格 codec 和规范内容身份。路线使用
  `route-v3-sha256-<64hex>`，整组使用 `layer-set-sha256-<64hex>`；解码与发布都会重算
  canonical ID 并拒绝篡改或额外字段。
- 新增 `FourLayerPlanningService`：全航程层到业务终点，主通道、滚动和可执行层分别到
  全航程推荐线 72/24/6 h 截止时刻及之前的最后一个非起点航点；提前到达时使用业务
  终点，无可物化锚点时整组拒绝。
- 新增 `LayeredRoutePlanLatestStore`，以 run/scenario/generation/request/revision 围栏原子
  发布完整整组；任何一层失败、任务取消、旧代次/修订或发布冲突都不会留下部分结果。
- 正式 `PreparedRiskPlanning` 与 `RiskSourcePlanningIngress` 新增
  `execute_four_layer()`、`replan_four_layer_if_needed()`；v3 初始规划和重规划与 v2 一样
  在同一个 B committed-window execution lease 内执行。

### 变更与兼容性

- 包版本提升到 `0.4.0`；`bc.risk-frame.v2` 不变。A*、风险采样、规则网格、成本和最终
  航速算法不变，四层能力位于合同、应用编排、ingress 和发布边界。
- v3 的三个下层都引用全航程推荐计划，并显式携带关注时间窗、分层目标是否到达以及
  是否到达业务终点。四层共享同一运行身份、B 窗口、generation、revision 和三类摘要。
- `cd.route-plan.v2` Schema/codec 和三目标入口继续用于历史读取、回归与 Day 7 稳定门槛；
  v3 推广后新正式运行选择 v3，不在同一次运行双写 v2。
- v1 仍仅用于审计/显式迁移，不得进入正式 latest。

### 验收边界

- 已加入端点 allowed-region/连通性、v3 Python/JSON/GeoJSON 往返、四层锚点、12 路线
  完整性、原子发布、失败不留部分结果、取消/迟到拒绝及 v3 重规划覆盖。
- 当前 `UV_OFFLINE=1 make check` 已通过 Ruff、uv lock/sync 与 CLI；pytest 为
  `138 passed, 1 skipped`，唯一跳过项仍是未提供的可选旧版外部归档。
- 真实主航区的 12 类、168 h A bundle 尚待交付；因此本版本不能宣称完成实源四层验收、
  科学调参或真实船舶校准。
- 当前最多 10 个自然日是开发冲刺期限，不改变主/测试航区 216/144 h 运行时上限；Day 7
  先冻结可重复的 v2 主线，Day 8–9 再推广 v3，Day 10 仅做验收或阻断修复。

## 0.3.0 - 2026-08-13：规范 BC codec、原子窗口与正式规划入口

### 新增

- 新增 `bc.risk-frame.v2` canonical JSON codec，明确内存 NaN ↔ 传输 `null`、严格字段、
  Z 时间、整数 generation、JSON 属性和确定性序列化规则。
- 新增排除 `risk_id`、包含其他全部传输字段的规范内容摘要；正式 ID 固定为
  `risk-sha256-<64hex>`，解码和正式入口均重算验证。
- 新增结构化 `CommittedRiskSource`、完整 `RiskWindowQuery` 与内容寻址
  `CommittedRiskWindow`。窗口直接声明闭区间、间隔、帧数、完整身份、知识截止和提交摘要。
- 新增公共 `RiskSourcePlanningIngress.prepare/execute`，从正式已提交 B 窗口装配既有 sampler、
  grid、vessel model、time-dependent A* 与 PlanningService，不改规划核心。
- `CommittedRiskSource` 新增 execution lease；执行时复核 prepare 所见的 query、commit ID 和
  content digest，并让 B generation fence 贯穿规划与最终 RoutePlan 发布。

### 变更与加固

- Python 与 JSON Schema 的 run/实体 ID、UTC、generation 和 payload 变量集合收紧为同一
  严格交集；正式 Schema 同时要求 canonical risk ID 形状。
- v2 `risk_level` 冻结为 `min(5, floor(risk_score*5)+1)`；未知风险固定等级 5，不能由 B
  使用未版本化业务阈值覆盖。
- 正式入口要求逐小时完整闭区间已经原子提交；缺帧、重复、错位、窗口摘要篡改、错误
  as-of/代次/配置/网格和不属于该网格的 Node 均在进入规划核心前失败。
- 同一个 `RiskSourcePlanningIngress` 对同一 run/scenario 复用一个
  `PlanningCoordinator`，不同 run 隔离；并发新修订会取消旧任务，generation 在 prepare
  后切换也会阻止旧快照开始执行或迟到发布。
- `execute()` 在 execution lease 内对当前帧执行 canonical encode→decode 私有快照，并从
  私有帧重建 sampler/planner；prepare 后替换暴露 xarray 变量不能污染实际规划输入。
- `PreparedRiskPlanning` 不再暴露可直接执行的 prepare 阶段 `PlanningService`，关闭绕过
  execution lease、commit 复核和私有快照的公共旁路。
- 正式入口改为接收完整 `PlanningConfiguration`，从实际 vessel model、planner 与
  replanning 对象重算并核对 `planner_config_digest`，同时用同一重规划配置构造运行策略；
  不再信任与执行对象脱离的摘要字符串。
- 上述变更全部位于合同、codec 与 ingress 边界；既有
  `risk/grid/cost/planners/replanning/service/publishing` 核心模块保持不变。

### 兼容性

- 包版本提升到 `0.3.0`。`bc.risk-frame.v2` 名称不变，但此前由宽松 Python 模型接受、
  JSON Schema 拒绝的文档现在会被 Python 同样拒绝；正式 B 应固定依赖 C `0.3.x` 公共合同。
- 合成和 `legacy_unverified` 帧仍可使用可读 risk ID；canonical ID 强制仅针对 formal。

### 验证记录

- 当前 `make check`：Ruff、uv lock/sync 和 CLI help 通过；pytest
  `126 passed, 1 skipped`。跳过项仅因用户可选旧版外部归档未提供，不影响正式 v2 边界。
- B 跨包门槛另验证 12 类、96/168/216 h、双走廊同模型摘要、A 归档重启与 formal
  RoutePlan；输入为可复核夹具，不是实源完成声明。

## 0.2.0 - 2026-08-13：共享运行上下文与 BC/CD v2 身份合同

### 新增

- 接入独立的 `arctic_route_contracts`，统一读取版本化的 `ScenarioDefinition`、
  `CorridorDefinition`、`VesselProfile` 和 `RunContext.v2`；C 不再维护这些共享事实的副本。
- 新增 `bc.risk-frame.v2` 与 `cd.route-plan.v2` Python 合同和 JSON Schema，并保留 v1
  Schema 作为只读审计材料。
- 新增统一的 `RunContext` 绑定验证：核对场景、航区、船型的 ID、版本、内容摘要，模拟
  起止时间以及公共 `config_digest`。
- `RiskIdentity` 和 `RoutePlan` 新增显式 `provenance`；正式来源必须提供非空
  `data_id`、UTC `issue_time`、UTC `valid_time` 和小写 SHA-256 `checksum`。
- 新增开发专用上下文与 v1 风险帧迁移适配器。合成和旧数据始终标为 `synthetic` 或
  `legacy_unverified`，不能升级或重标为 `formal`。
- CLI 支持共享场景、显式模拟开始时间、候选航线距离和外部 `RunContext`，使 A 与 C
  能按相同输入物化相同的动态航程时域。

### 变更

- C 本地配置只保留船舶性能模型、规划器和重规划算法参数；场景、航区和船舶事实迁至
  共享包。公共 `config_digest`、B 的 `model_config_digest` 与 C 的
  `planner_config_digest` 各自独立，不再混称。
- 主线明确为摩尔曼斯克外海—迪克森外海；测试线明确为特罗姆瑟外海—伊斯峡湾外部
  入口。朗伊尔城仅保留为 AIS 航次识别参考点，不作为规划终点。
- 全航程时域改为按候选路线距离、参考船速和缓冲动态物化，不再固定为 7 天或 9 天。
  C 不自行扩大 A 已冻结的时窗；超过场景或来源上限时返回
  `forecast_coverage_insufficient`。
- 冻结预测与事后最佳估计使用不同的知识时间规则：冻结预测禁止消费出发后知识，历史
  最佳估计允许明确记录的事后知识，但两者都必须满足 RiskFrame 自身的 `as_of_time`
  门禁。
- 正式 `RiskFrame` 必须包含 `environment_speed_factor`。B 负责环境影响，C 只将该因子
  应用于版本化船舶性能模型以计算最终速度，不从风险分数或置信度重复推导降速。
- `RoutePlan` 的 JSON、GeoJSON 和 CD latest 发布链路原样传播 run、模型、规划器、代次
  与来源身份；D 可据此隔离不同运行结果。
- 旧版嵌套 B 制品继续由隔离适配器读取，但需要显式确认未知发布时间/有效时间，并永久
  保持非正式来源等级。当前 `工作包B.zip` 不通过该路径包装成半正式输入。

### 修复与加固

- 修复仅比较共享对象 ID/版本、未比较内容摘要的问题；同名同版本但内容被原地修改时
  现在会拒绝运行。
- 修复规划开始时间、最大耗时或 RiskFrame 窗口可能越过 `RunContext` 结束时间的问题；
  CLI 和服务均明确失败，不静默裁剪或延长。
- 修复开发 planner 缺少风险身份时仍可能发布正式外观路线的问题；正式请求现在要求
  可验证且与请求完全一致的 `risk_identity`。
- 修复 formal 来源引用可以缺少数据 ID、有效时间或 checksum 的问题；Python 语义验证
  与 JSON Schema 均要求完整证据。
- 修复合成、旧版和正式风险窗口可能混合的问题；来源等级已纳入采样窗口身份和发布
  一致性检查。
- 修复 A 与 C 对动态时域分别重建场景而产生 ID、结束时间或摘要不一致的问题；相同
  起始时间和候选距离现在得到相同物化结果，超上限时也以相同错误拒绝。

### 验证记录

- 完整 `make check`：Ruff、锁文件/同步检查和 CLI 检查通过；pytest
  `107 passed, 1 skipped`。跳过项仅依赖未提供的可选旧版外部归档，不影响 v2 正式合同。
- A/C 动态时域一致性反例覆盖 168 h、120 h 和超过 216 h 上限的失败路径。
- 合同测试覆盖摘要篡改、同 ID 内容变更、时域越界、未来知识泄漏、来源字段缺失、来源
  等级混合及无风险身份发布等失败路径。
- 合成演示仍采用粗网格进行合同/流程冒烟；端点吸附距离会显式报告，结果不作为航线
  质量或真实安全能力证明。

### 兼容性说明

- `bc.risk-frame.v1` 和 `cd.route-plan.v1` 缺少完整的 run、模型、规划器与来源身份，不能
  直接进入 v2 正式链路。B、D 消费者需要按对应 v2 合同迁移。
- 本版本没有实现 B 的逐小时预测模型，也没有实现上述四层规划编排；这些内容仍属于
  `Unreleased`，不得从 v2 合同存在推断为模型已经交付。

## 0.1.0 - 2026-08-10：工作包 C 初始实现

### 新增

- 建立独立 Python 3.13、Mamba + uv 工程，提供锁文件、Makefile、CLI、Ruff 与 pytest
  验收入口。
- 实现 `RiskFrame v1`、`RiskSource`、严格时空 `RiskSampler` 和确定性合成风险源；采样
  超窗、缺测、网格或上下文不一致时明确失败，不把缺失风险解释为安全。
- 实现规则网格、显式有限距离端点映射、船舶性能模型和等价小时成本模型。
- 实现时间依赖 A* 及 Dijkstra oracle，支持最快、低风险和综合推荐三种目标；按船舶
  到达每条边的 ETA 采样风险和环境速度系数。
- 实现五类重规划触发、防抖、迟滞、取消和请求/修订/代次发布围栏。
- 实现 `RoutePlan v1` JSON/GeoJSON 序列化及 `CDLatestStore` 原子最新值发布。
- 实现旧版嵌套 B 交付物的隔离审计/规划适配器；未知 `issue_time`、稀疏时间帧和端点
  映射必须由调用者显式确认，不能冒充正式输入。
- 建立 BC/CD 合同、成本模型、验收清单、架构追踪和决策记录，并加入单元、合同与集成
  测试。

### 初始边界

- A → B → C → D 保持单向依赖；C 不读取 A 的数据库、目录或内部缓存，也不调用 B
  的模型内部实现。
- B 提供风险、硬约束、置信度和环境影响，C 拥有最终船速、ETA、成本与路线计算。
- 演示船舶、风险和规划参数标记为 `demo_unvalidated`，不得用于真实航行安全决策。
- v1 不支持等待动作；风险时间窗必须覆盖实际 ETA，禁止外推或用零值补齐缺帧。
