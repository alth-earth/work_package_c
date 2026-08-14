> [!NOTE]
> **文档治理声明**
>
> - 文件角色：工作包 C 的需求/决策到实现证据追踪表。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文件去向：`ARCHITECTURE_TRACE.archive-20260814-pre-governance.md`。
> - 改造原因：移除对混合历史指南和冲刺日历的依赖，只保留可复核的当前证据。

# 工作包 C 架构追踪

## 证据优先级

1. 当前 C 代码、Python 合同、Schema、配置和自动测试；
2. 当前 [`README`](../README.md)、[`handoff`](../work_package_c_handoff.md) 和本目录规范文档；
3. 顶层 [`ARCTIC_ROUTE_SYSTEM.md`](../../ARCTIC_ROUTE_SYSTEM.md) 与当前工作包 A/B/contracts/orchestrator；
4. 归档文档只用于历史审计，不作为现状依据。

## 需求与证据

| 需求/矛盾 | 当前处理 | 实现/验证证据 |
|---|---|---|
| C 不应耦合 A/B 私有实现 | 只依赖 `arctic_route_contracts`、`CommittedRiskSource` 和公共合同 | `config.py`、`contracts/sources.py`、`ingress.py` |
| 同名共享配置可能原地变化 | 重算 Scenario/Corridor/Vessel 内容摘要和公共 `config_digest` | `context_validation.py`、`tests/contract/test_configuration.py` |
| 配置摘要可能与实际执行参数脱离 | ingress 接收完整 `PlanningConfiguration` 并重算 C digest | `config.py`、`ingress.py`、`tests/integration/test_formal_ingress.py` |
| B 窗口可能在规划期间切换 | prepare 后执行仍持有 source lease，重验 commit 并使用 canonical 私有快照 | `contracts/windows.py`、`ingress.py` |
| 稀疏/部分风险可能被误当完整时域 | 正式入口要求 60 min 严格闭区间原子 commit，不外推 | `contracts/windows.py`、`risk/sampler.py`、`ingress.py` |
| 缺测可能被当作零风险 | 未知 risk 必须由 hard mask 或 confidence=0 防止当安全 | `contracts/models.py`、`risk/sampler.py` |
| 有效航速责任不清 | B 提供环境因子；C 应用版本化船模计算最终速度 | `contracts/models.py`、`cost/vessel.py`、`docs/COST_MODEL.md` |
| 经纬度取整可能落在限制区、陆地或断开海区 | 公共 endpoint mapping 检查 allowed region、首帧 hard mask、距离和连通性 | `endpoints.py`、`tests/unit/test_endpoints.py` |
| 四层可能被实现成四次无关运行 | 共享全航程推荐线、一次 lease、一个 request/revision 和整组身份 | `contracts/layered.py`、`layered.py`、`ingress.py` |
| 下层锚点或 `destination_reached` 可能模糊 | 锚点为全航程推荐线 72/24/6 h 前最后非起点；提前结束才标记业务终点 | `contracts/layered.py`、`layered.py` |
| 任一层失败仍可能留下部分结果 | 先在内存中生成四层 12 路线，再原子发布完整整组 | `layered.py`、`publishing/layered_store.py` |
| 旧任务可能迟到覆盖新结果 | generation/request/revision、取消状态和 digest 在发布时再验 | `replanning/`、`publishing/store.py`、`publishing/layered_store.py` |
| v2/v3 双写可能产生两个“正式最新值” | 一次运行由上层显式选择一个输出合同 | `ingress.py`、`docs/CD_CONTRACT.md` |
| 旧 B 制品可能被提升为正式输入 | 只经 legacy adapter，永久 `legacy_unverified`，不读 `route_cost_grid` | `adapters/legacy_b.py`、`tests/integration/test_legacy_adapter.py` |
| `formal` 可能被误解为科学校准 | provenance 只表示身份/来源合格；模型仍用独立 calibration status | `contracts/models.py`、`domain/models.py`、`configs/vessel_models/` |

## 与架构蓝本的当前差异

- 已落地：单向 A→B→C→D 合同、时间依赖规划、三目标、四层编排、重规划和原子发布。
- 工程落地但未实源/科学验收：正式 BC ingress、v3 四层、风险与船模参数。
- 尚未交付：D 正式消费、完整海事硬约束、方向相关风浪流性能、等待动作、科学置信度分级。
- 高级算法（LPA*/D* Lite/MPC）只能由真实网格性能证据触发，不属于 0.4.0 已验收能力。

未完成项的当前状态见 [`STATUS_AND_TODO.md`](STATUS_AND_TODO.md)，技术验收闸门见
[`ACCEPTANCE.md`](ACCEPTANCE.md)。
