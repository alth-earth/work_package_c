> [!NOTE]
> **文档治理声明**
>
> - 文件角色：工作包 C 的稳定整体认识与职责边界说明。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文件去向：旧综合指南已归档为
>   `../工作包C项目整体认识与继续开发指南.archive-20260814-pre-governance.md`。
> - 改造原因：从混合了历史审计、排期和操作说明的长文中提取不会随冲刺日历频繁变化的概述。

# 工作包 C 项目概述

## 一句话目标

工作包 C 在不偷看未来、不把缺测当安全、也不绕过来源证据的前提下，把 B 的逐小时风险
预测转换为带 ETA、成本、来源和版本身份的可发布航线计划。

## 系统中的位置

```text
A：环境事实与可回放数据
          ↓ DatasetBundle.v2 + RunContext.v2
B：时间处理、预测和风险融合
          ↓ committed bc.risk-frame.v2 window
C：端点、ETA 风险采样、航速/成本、规划、重规划和发布
          ↓ cd.route-plan.v2 或 cd.four-layer-route-plan-set.v3
D：只读消费、展示和交互
```

每个箭头都是版本化合同边界。C 不直接导入 A/B 私有实现，系统编排由
[`arctic_route_orchestrator`](../../arctic_route_orchestrator/) 完成。

## 输入

正式运行至少需要：

- 与 A/B 相同的 `RunContext.v2`；
- B 原子提交的完整逐小时 `RiskFrame v2` 窗口及 execution lease；
- 共享 Scenario/Corridor/Vessel 事实；
- C 本地版本化 vessel model、planner 和 replanning 参数；
- 经 `map_corridor_endpoints(...)` 生成并可审计的起终点映射。

风险帧提供风险分量、融合风险、硬约束、置信度和 `environment_speed_factor`。C 不从
`risk_score` 或 `confidence` 猜测物理减速。

## 核心处理

1. 校验 RunContext、公共/模型/规划摘要、generation/request/revision 和窗口 commit。
2. 要求风险窗为严格 60 分钟步长、闭区间完整覆盖，并在执行期间持有同一 lease。
3. 将输入复制为 canonical 私有快照，避免上游在搜索中途变更。
4. 时间依赖 A* 按船舶到达候选边的 ETA 采样风险，而不是只看出发时刻。
5. B 的环境速度因子与 C 的船型性能共同决定最终速度、ETA、燃料/成本和路线评分。
6. 结果通过并发围栏和原子 latest 发布；事件触发时使用同一规则重规划。

## 输出模式

| 模式 | 输出 | 用途 |
|---|---|---|
| v2 | 三种目标的 `cd.route-plan.v2` | 兼容基线 |
| v3 | 四层 × 三目标的 `cd.four-layer-route-plan-set.v3` | 当前四层能力 |
| legacy | 显式 v1 审计/迁移结果 | 永久 `legacy_unverified`，不得进入正式 latest |

v3 集合内的每条路线使用 `cd.route-plan.v3`；它不是顶层 planning contract。v3 的四层是
全航程、主通道、滚动和可执行视野。它们共享同一输入窗口、lease、运行身份
和全航程锚点；任一层失败不得留下部分整组。

## 关键时间概念

- `issue_time`：信息何时可见，是防未来信息泄漏的门禁；
- `valid_time`：环境或风险所描述的时刻，也是 ETA 采样的时间轴；
- `ingest_time`：系统何时接收制品，只用于审计；
- simulation/departure time：本次规划的决策时点与起航时点。

C 不从文件名、mtime 或“最接近的时间”推断这些语义，也不在风险窗外等待或外推。

## 来源与校准是两条轴

`formal` 只说明来源、身份、时间和合同证据完整；`synthetic` 是测试数据；
`legacy_unverified` 是旧制品隔离标记。另一方面，船模/算法的 calibration status 独立为
`demo_unvalidated` 或 `calibrated`。当前工程基线仍是 `demo_unvalidated`。

## 当前能力边界

当前 C `0.4.0` 已实现正式 ingress、v2/v3 规划、重规划和原子发布。尚不能宣称真实航行
可用，因为主航区真实 A→B→C 闭环、风险/船舶科学校准和 D 消费验收仍待完成。

当前证据和下一步见 [`STATUS_AND_TODO.md`](STATUS_AND_TODO.md)，详细交接见
[`../work_package_c_handoff.md`](../work_package_c_handoff.md)。
