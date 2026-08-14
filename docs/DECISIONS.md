> [!NOTE]
> **文档治理声明**
>
> - 文件角色：工作包 C 0.4.0 的稳定架构与安全决策真源。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文件去向：`DECISIONS.archive-20260814-pre-governance.md`。
> - 改造原因：只保留长期技术决策，将日历排期交还顶层冲刺文档，并明确 provenance/calibration 双维语义。

# 工作包 C 决策记录

> 适用基线：C `0.4.0`、BC v2、CD v3（v2 兼容）。
> 本系统仅用于科研演示，不得用于真实航行安全决策。

## 包与数据边界

1. A 发布规范环境帧和不可变 DatasetBundle；B 负责环境时间处理、预测和风险融合；C 不直接读 A 数据库、目录或缓存。
2. C 不导入 A/B 实现模块；只通过 `arctic_route_contracts`、版本化 Schema 和结构化 protocol 集成。
3. 场景、航区、船舶事实和 `RunContext` 的唯一正式来源是相邻 `arctic_route_contracts`；C 本地只拥有船舶性能、规划器和重规划参数。
4. 公共 `config_digest` 绑定共享 Scenario/Corridor/Vessel 和 A DatasetBundle；B 发布 `model_config_digest`；C 发布 `planner_config_digest`。`generation_id` 不进入 digest。

## 时间、身份与来源

5. `issue_time` 是可见性门禁，`valid_time` 是环境/风险时间轴，`ingest_time` 只用于审计。C 不从文件名、mtime 或相近时间猜测语义。
6. `generation_id` 隔离 seek/reset；`planning_request_id` 和 `input_revision` 阻止同代次旧请求迟到覆盖。
7. 数据 provenance 只包含 `formal`、`synthetic`、`legacy_unverified`。`formal` 表示身份、时间和来源链合格，不表示模型已校准。
8. 船模/算法 calibration status 独立表达 `demo_unvalidated` 或 `calibrated`；不得用 formal provenance 掩盖 demo 参数。

## B→C 与风险采样

9. 正式 `RiskFrame v2` 必须提供 risk、hard mask、confidence 和 `environment_speed_factor`。B 不发布最终船速。
10. C 只在两个已发布、身份与网格完全兼容的风险帧之间做 ETA 采样；不外推、不跨上下文插值。
11. 软风险按时间线性插值；hard mask 取逻辑 OR；confidence 和环境速度因子取保守最小值；`risk_level` 由插值后 risk 重算。
12. 正式 C ingress 只消费 canonical、完整逐小时闭区间的 `CommittedRiskWindow`；普通 `get_window()` 结果不得冒充 commit。
13. prepare 只生成可审计输入；execute 必须持有 source execution lease、复核 commit，并从 canonical 私有快照重建规划组件。

## 端点、航速与成本

14. 正式调用方只能在 Corridor allowed region 内选择首帧 hard mask 可通航节点，同时满足显式距离上限和连通性；规划核心不暗中改坐标。
15. B 提供环境因子；C 将其应用到版本化船型，检查最低安全因子和操舵速度，计算最终航速、边耗时和 ETA。
16. C 不从 `risk_score` 或 `confidence` 再推导物理减速。风险是政策成本，不是第二套船速模型。
17. 当前使用 8 邻接规则网格、Haversine 距离、边内采样和等价小时成本。A* 启发式只使用可证明下界。
18. 当前不允许等待动作；风险时域必须覆盖实际 ETA。若未来引入等待或非 FIFO 成本，必须升级状态/算法和合同。

## v2/v3 、四层与发布

19. v2 保留历史读取、兼容回归和显式基线运行；v3 用于原子四层整组。一次正式运行只选一个输出合同，不双写。
20. v3 四层不是四次独立运行；它们共享同一 B commit/lease、运行身份、全航程推荐线和发布令牌。
21. 每层恰好三目标；完整 12 条路线才能原子发布。任一层失败、取消、过期或冲突都不得留部分整组。
22. 主通道、滚动和可执行层分别以全航程推荐线在 72/24/6 h 前的最后非起点航点为目标；无锚点则整组失败。
23. 重规划保留 generation/request/revision 围栏、取消、最小间隔、收益门槛和迟滞。新整组成功时原子替换旧整组。

## 默认值、运行时域与延期项

- 共享 `nordic_odyssey_reference_v1` 是事实参考，不等于 C 性能模型已校准。C 的航速、操舵、转弯、净空和阈值仍为 `demo_unvalidated`。
- 水深接口保留，但核心 bathymetry 硬约束当前关闭。
- 主线/测试线的设计窗和 216/144 h 航区上限是运行时事实；开发日程不得改变它们。
- 真实数据验收、真船校准、D 消费、0–2/2–4/4–6 h 科学可信度分级、等待、D* Lite 和 MPC 均不属于 0.4.0 已验收范围。

当前日历安排见 [`ABC_10_DAY_SPRINT.md`](../../ABC_10_DAY_SPRINT.md)；进度状态见
[`STATUS_AND_TODO.md`](STATUS_AND_TODO.md)。
