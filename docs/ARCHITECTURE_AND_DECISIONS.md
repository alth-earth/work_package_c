> [!NOTE]
> **文档治理声明**
>
> - 文件角色：工作包 C 的架构视图、关键不变量与架构蓝本差异说明。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文件去向：旧综合指南已归档为
>   `../工作包C项目整体认识与继续开发指南.archive-20260814-pre-governance.md`。
> - 改造原因：从历史审计和操作步骤中分离当前 0.4.0 架构，并以实现/测试为最终校验依据。

# 工作包 C 架构与决策

## 1. 模块关系

```text
orchestrator
  ├─ map_corridor_endpoints() ───────────────────────────────┐
  └─ ServicePlanningRequest + CommittedRiskSource            │
                              │                               │
                              ▼                               │
                 RiskSourcePlanningIngress                    │
                 ├─ context/digest validation                 │
                 ├─ hourly coverage + canonical commit        │
                 └─ execution lease + private snapshot        │
                              │                               │
                              ▼                               │
      RiskSampler ── TimeDependentAStar ── Vessel/Cost ◀──────┘
                              │
                  ┌───────────┴────────────┐
                  ▼                        ▼
          PlanningService          FourLayerPlanningService
          RoutePlan v2 × 3         v3 four layers × 3
                  │                        │
                  ▼                        ▼
          RoutePlanLatestStore     LayeredRoutePlanLatestStore
                  └─────────── fenced, atomic ───────────▶ D
```

## 2. 公共边界

- 共享事实：相邻 [`arctic_route_contracts`](../../arctic_route_contracts/) 中的
  Scenario/Corridor/Vessel、DatasetBundle/RunContext 和公共摘要算法。
- B→C：`CommittedRiskSource`/`CommittedRiskWindow` 与 `bc.risk-frame.v2`。
- C 公共入口：`map_corridor_endpoints`、`ServicePlanningRequest`、
  `RiskSourcePlanningIngress`。
- C→D：`cd.route-plan.v2`，或 `cd.four-layer-route-plan-set.v3`；单次运行显式选一条。
- 机器真源：Python 不可变模型与 `schemas/`；Markdown 解释语义但不能覆盖机器合同。

正式调用由 orchestrator 先做端点映射，再构造请求进入 ingress。C CLI 只有 synthetic/legacy
工具，没有正式 A→B→C 子命令。

## 3. 正式 ingress 与快照一致性

`RiskSourcePlanningIngress.prepare()` 执行以下闸门：

1. 请求 RunContext、共享配置、模型/规划摘要和 generation 身份一致；
2. 来源为 formal 所需版本且 `issue_time <= simulation_time`；
3. requested interval 使用严格 60 分钟步长并闭区间完整覆盖；
4. 所有帧网格、context、commit 和时间序列一致；
5. source 返回 canonical commit 并授予整个执行期的 lease；
6. C 创建私有 planner/sampler 快照，执行结束前再次核对 commit。

这样避免“prepare 后 B 切换窗口”或“同名配置原地修改”导致搜索使用混合代次数据。

## 4. 时间依赖规划与速度责任

候选边成本取决于到达该边时的 `valid_time`，所以搜索状态包含时间，而非在整条路线只用
一帧静态代价。C 不等待、不外推；风险窗必须覆盖实际 ETA。

B 只发布环境影响（包括 `environment_speed_factor`）。C 将它与船型性能和下限规则结合，
得到最终航速，再计算时间、风险、燃耗/成本。禁止从融合风险或置信度再次推导减速，避免
同一物理效应被计算两次。稳定公式见 [`COST_MODEL.md`](COST_MODEL.md)。

## 5. v2、v3 与四层原子性

- v2：同一请求产生最短时间、最低风险、综合成本三条兼容路线。
- v3：四个固定层级各含三目标，共 12 条 `RoutePlanV3`。
- 四层共享同一 RunContext、RiskWindow、lease、generation/request/revision 和全航程锚点。
- 先在内存中完整构造、验证 canonical IDs，再一次原子发布；任一层失败不发布部分集合。
- 历史 v2 不自动升级为 v3，历史结果也不能拼成当前四层集合。

详细字段与兼容规则见 [`CD_CONTRACT.md`](CD_CONTRACT.md)。

## 6. 重规划与发布围栏

重规划策略根据时间推进、偏航、风险变化等结构化 observation 决策。发布时重新校验：

- `generation_id`：seek/reset 后旧代次失效；
- `planning_request_id`：隔离不同业务请求；
- `input_revision`：较旧输入不得覆盖较新输入；
- cancellation/publication token：已取消或迟到任务不得写 latest；
- canonical digest：序列化内容与声明身份一致。

围栏是安全不变量，不能为“让一次集成通过”而关闭。

## 7. 来源、校准和失败语义

数据 provenance：`formal`、`synthetic`、`legacy_unverified`。模型/船舶 calibration：
`demo_unvalidated`、`calibrated`。它们必须同时表达，不能相互推导。

以下情况必须失败而非降级：未来信息、陈旧/缺帧时窗、网格/上下文/摘要不匹配、未知风险
被当安全、端点超过允许映射阈值、层无法物化、lease/commit 变化、旧任务尝试覆盖新结果。
任何未来的“保守 fallback”也必须有显式合同、可见标记和测试。

## 8. 与总体架构蓝本的异同

一致部分：

- A 负责环境数据，B 负责预测/风险，C 负责动态路线，D 负责消费展示；
- 以时间变化风险驱动多目标规划，并支持滚动更新；
- 用缓存/版本和明确接口降低模块耦合。

当前实现的工程化补充或调整：

- 共享 `RunContext.v2`、内容摘要和版本化 contracts 成为跨包唯一语义边界；
- BC 不再是松散文件交付，而是原子 committed window + execution lease；
- C→D 当前正式能力为 v2/v3，而旧 v1 只保留审计用途；
- 四层十二路线和原子发布已经实现，不再是“待开发”项；
- orchestrator 负责端点映射和跨包组装，C 保持纯公共接口依赖；
- 真实来源、科学校准和 D 消费仍未完成，因此目标架构尚不能宣称全链路验收。

若历史架构附件与当前实现冲突，裁决顺序是当前代码/合同/测试与各包 handoff 优先；冲突
材料仅保存在归档中。系统级权威图与规划见
[`ARCTIC_ROUTE_SYSTEM.md`](../../ARCTIC_ROUTE_SYSTEM.md)。

## 9. 决策与证据导航

- 稳定决策：[`DECISIONS.md`](DECISIONS.md)
- B→C 字段/时域：[`BC_CONTRACT.md`](BC_CONTRACT.md)
- C→D 字段/兼容：[`CD_CONTRACT.md`](CD_CONTRACT.md)
- 成本与船速：[`COST_MODEL.md`](COST_MODEL.md)
- 需求到代码：[`ARCHITECTURE_TRACE.md`](ARCHITECTURE_TRACE.md)
- 可执行验收：[`ACCEPTANCE.md`](ACCEPTANCE.md)
