> [!NOTE]
> **文档治理声明**
>
> - 文件角色：共享 RunContext 与 C v2/v3 输出的稳定迁移操作说明。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文件去向：`docs/archive/SHARED_CONTEXT_MIGRATION.archive-20260814-pre-governance.md`。
> - 改造原因：将技术迁移顺序与冲刺日历分离，并补充 orchestrator 和 provenance/calibration 边界。

# 共享配置与 v2/v3 合同迁移

## 前置条件

- A、B、C 使用同一份 `arctic_route_contracts` 共享 Scenario/Corridor/Vessel。
- A 已从不可变 `DatasetBundle v2` 创建 `RunContext.v2`。
- B 使用同一 RunContext，并发布独立 `model_config_digest`。
- C 使用同一 RunContext 和 C 本地 vessel/planner/replanning 配置，发布独立
  `planner_config_digest`。

## 系统级迁移顺序

1. A 物化共享场景时域，并发布 DatasetBundle + RunContext。
2. B 核对同一公共 `config_digest`，生成 canonical `bc.risk-frame.v2`。
3. B 对 C 需要的完整闭区间执行原子 commit，并提供 execution lease。
4. orchestrator 使用窗口首帧与 Corridor allowed regions 调用 `map_corridor_endpoints(...)`。
5. orchestrator 用映射结果构造 `ServicePlanningRequest`，并交给 `RiskSourcePlanningIngress`。
6. 单次运行显式选择一条 C→D 路径：
   - `execute()`：发布 `cd.route-plan.v2` 三目标兼容基线；
   - `execute_four_layer()`：发布原子 `cd.four-layer-route-plan-set.v3`。
7. D 按 run/scenario/generation 和完整 digest 只读消费所选合同的 latest。

## 重规划迁移

- 新请求从新 `start_time` 开始，只读取从该时刻到 RunContext 结束的 committed suffix。
- 请求保持同 run/scenario/generation，`input_revision` 严格递增。
- `observed_at` 和 `risk_valid_time` 等于新 `start_time`；`risk_revision` 等于 suffix commit ID。
- v2 使用 `replan_if_needed()`；v3 使用 `replan_four_layer_if_needed()`。
- v3 重规划成功时只发布新完整整组，不更新单层。

## 兼容与失败原则

- v2 保留历史读取和回归；不自动升级或拼接成 v3。
- v1 只能走显式 `legacy_unverified` 迁移；不得根据同名 scenario、相近时间或旧文件名猜测归属。
- 每次新演示都必须用同一时间窗重跑 A、B、C，不复用另一 RunContext 的结果。
- 缺帧、错位、digest 不一致、过时 generation、不可见知识、超时域或部分 v3 整组均明确失败。
- 主线/测试线当前设计窗为 168/96 h，航区上限为 216/144 h；实际请求以物化 RunContext 为准，C 不自行延长。

## 证据等级

- 迁移链的 `formal` 标记只证明来源、时间、身份和内容摘要通过合同。
- B 风险模型和 C 船模/规划参数的 calibration status 必须另行报告。
- synthetic 和 legacy 链可用于开发/审计，不得填补 formal 实源验收空缺。

系统运行命令和输出目录以 [`arctic_route_orchestrator`](../../arctic_route_orchestrator/) 的 handoff 为准；
C 本地操作见 [`DEVELOPMENT_GUIDE.md`](DEVELOPMENT_GUIDE.md)。
