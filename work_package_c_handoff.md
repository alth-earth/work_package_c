> [!NOTE]
> **文档治理声明**
>
> - 文件角色：工作包 C 的唯一详细交接入口，供人和 AI 判断现状、边界、依赖与接手顺序。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文件去向：旧综合指南已归档为
>   `工作包C项目整体认识与继续开发指南.archive-20260814-pre-governance.md`；旧 README 已归档为
>   `README.archive-20260814-pre-governance.md`。
> - 改造原因：把历史审计、当前状态、架构说明和操作步骤分责，消除旧 v1/B 原型叙述与
>   C 0.4.0 现状混杂的问题。

# 工作包 C 交接说明

> 状态快照：2026-08-14；包版本 `0.4.0`；整体状态 **进行中**。
> 工程合同与规划主线已具备，真实来源闭环、科学校准和 D 消费验收尚未完成。

## 1. 项目目标与边界

工作包 C 把 B 发布的时空风险窗转换为可审计路线计划。核心链路是：端点映射 → 风险窗
准备 → 按候选边 ETA 采样 → 时间依赖 A* → 航速/成本/指标计算 → 原子发布 → 按事件
重规划。

C 负责：

- 将 B 的 `environment_speed_factor` 应用于版本化船模，计算最终航速、ETA 和成本；
- 生成 v2 三目标路线，或 v3 四层 × 三目标共 12 条路线的原子整组；
- 校验上下文、时间窗、身份与来源，并阻止旧 generation/request/revision 覆盖新结果；
- 提供重规划协调、序列化、Schema 和 latest store。

C 不负责：

- A 的数据下载、规范化、归档和回放；
- B 的预测、风险融合和模型权重；
- D 的可视化、交互和业务展示；
- 航海安全认证或科学参数校准。

正式集成不得读取 A/B 私有数据库、缓存或实现模块，只能使用
[`arctic_route_contracts`](../arctic_route_contracts/) 和版本化公共接口。

## 2. 当前状态

| 能力域 | 状态 | 当前口径 |
|---|---|---|
| 包、环境与工程检查 | 已完成 | Python 3.13、Mamba + uv、锁文件和 CLI 检查可复现 |
| 端点映射与公共上下文 | 已完成 | `map_corridor_endpoints(...)`；共享 `RunContext.v2` 与配置摘要校验 |
| B→C 正式入口 | 已完成 | `CommittedRiskSource`、逐小时 canonical window、execution lease |
| v2 三目标规划 | 已完成 | 最短时间、最低风险、综合成本；时间依赖 A* |
| v3 四层规划 | 已完成 | 四层共享同一输入快照和身份，12 条路线整组原子发布 |
| 滚动重规划与发布围栏 | 已完成 | generation/request/revision/cancel fencing |
| v1/旧 B 支持 | 已冻结 | 只允许显式 `legacy_unverified` 审计/迁移，不进正式 latest |
| 当前主航区真实 A→B→C 验收 | 待执行 | 尚无可据以宣称实源闭环通过的 C 侧验收制品 |
| 风险模型与船舶性能科学校准 | 待执行 | 当前校准状态为 `demo_unvalidated` |
| D 端消费/展示验收 | 待执行 | 需由 D 对稳定 v2/v3 合同完成消费者测试 |

来源合法性和科学可信度是两个独立维度：

- `formal` provenance：来源、身份、时间和合同链合格；
- `demo_unvalidated` calibration：模型/船舶参数尚未经科学或真船校准。

因此正式来源可以同时是未校准模型，任何文档或界面都不得把 `formal` 写成“科学有效”。

## 3. 已完成清单

| 功能 | 关键路径 | 主要验证 |
|---|---|---|
| 公共请求、风险与路线模型 | `src/arctic_route_planning/contracts/` | `tests/contract/` |
| 风险窗完整性和 committed source 协议 | `contracts/windows.py`、`contracts/sources.py` | `test_risk_sources.py`、`test_formal_ingress.py` |
| 正式规划入口及私有快照 | `ingress.py` | `test_formal_ingress.py` |
| 有界端点映射 | `endpoints.py` | `test_endpoints.py` |
| 时空风险采样 | `risk/sampler.py` | `test_risk_sampler.py` |
| 时间依赖 A* | `planners/time_dependent_astar.py` | `test_time_dependent_astar.py` |
| 最终船速和成本 | `cost/vessel.py`、`cost/model.py` | `test_grid_and_cost.py` |
| v2 规划服务 | `service.py` | `test_service.py` |
| v3 四层编排 | `layered.py`、`contracts/layered.py` | `test_layered_planning.py` |
| 重规划 | `replanning/` | `test_replanning.py` |
| 原子 latest 与序列化 | `publishing/` | `test_publishing.py` |
| JSON/GeoJSON 合同 | `schemas/` | `test_schemas.py` |
| 显式旧制品隔离 | `adapters/legacy_b.py` | `test_legacy_adapter.py` |

当前本地复验命令 `UV_OFFLINE=1 make check` 的结果为 `138 passed, 1 skipped`。唯一跳过项
依赖一个未提供的、路径写死为 `/mnt/c/.../交付包.zip` 的可选旧制品；它不影响正式 v2/v3
主线，但报告时必须保留 skipped 事实。

## 4. 未完成与待办

| 优先级 | 任务 | 前置依赖 | 完成证据 |
|---|---|---|---|
| P0 | 用当前 A 的真实、完整目标时窗经当前 B 生成并提交 RiskFrame v2 | A/B handoff 均确认同一 RunContext、时域、变量和 provenance | 保存 A bundle、B commit、C request 与 C 输出的身份/摘要；C 正式入口通过 |
| P0 | 验证真实窗口覆盖路线实际 ETA，拒绝未来、陈旧、缺帧或上下文不匹配输入 | 上一项 | 通过用例与各类拒绝用例均有日志/测试制品 |
| P0 | 与 D 完成 v2/v3 消费合同验收 | D 明确选择 v2 或 v3；确认原子 latest 读取方式 | D 消费者测试、Schema 校验和失败语义记录 |
| P1 | 校准风险、环境速度因子和船舶性能参数 | 可追溯观测/真船数据、评估协议与领域评审 | 参数版本、数据版本、误差指标和评审结论齐全后才标 `calibrated` |
| P1 | 固化系统级实源回放/重规划验收 | P0 实源链路 | 同一 generation 的初始计划、`+6 h` 或业务事件重规划及围栏证据 |
| P2 | 按 D/演示需求扩展可观测性和制品保留策略 | D 需求稳定 | 不破坏公共合同的指标、日志和保留策略 |

具体日期和人员只在 [`ABC_10_DAY_SPRINT.md`](../ABC_10_DAY_SPRINT.md) 维护，本文件不复制
逐日排期。

## 5. 技术架构要点与关键决策

```text
RunContext.v2 + committed RiskFrame v2 window
                  │
                  ▼
endpoint mapping ── RiskSourcePlanningIngress.prepare()
                  │  validates identity, hourly coverage, canonical commit
                  │  retains execution lease + private snapshot
                  ▼
RiskSampler ── time-dependent A* ── vessel speed/cost
                  │
                  ├─ execute()             → RoutePlan v2 × 3
                  └─ execute_four_layer()  → FourLayerRoutePlanSet v3 × 12
                                                   │
                                                   ▼
                                        fenced atomic latest → D
```

与架构蓝本一致之处：A/B/C/D 分责、预测驱动动态规划、滚动更新、版本化中间结果和多目标
路线仍是主干。当前实现对蓝本作了可验证的工程化收敛：

- 共享上下文集中到 `arctic_route_contracts`，不靠同名文件或私有模块隐式对齐；
- BC 正式边界是逐小时、canonical、原子 committed window 和 execution lease，不是松散静态文件；
- C→D 已从旧 v1 演进为 v2 兼容基线与 v3 原子四层整组；
- generation/request/revision、内容摘要和 publication token 形成并发/回放围栏；
- 端点由 orchestrator 显式映射并留下审计结果，C 不静默吸附到任意网格节点。

尚未达到蓝本最终目标的是：真实来源闭环、科学校准和 D 应用验收。详细决策见
[`ARCHITECTURE_AND_DECISIONS.md`](docs/ARCHITECTURE_AND_DECISIONS.md) 和
[`DECISIONS.md`](docs/DECISIONS.md)。

## 6. 已知问题、坑与风险

1. **证据等级混淆**：synthetic 通过、formal provenance 和 calibrated 三者不能互相替代。
2. **时域不足**：C 不等待也不外推；B 窗口必须覆盖搜索实际 ETA，缺一小时也会拒绝。
3. **缺测误判安全**：未知风险必须由 hard mask 或 `confidence=0` 保守处理，不能补零。
4. **重复减速**：B 提供环境因子，C 计算最终速度；不得再从 risk/confidence 推导减速。
5. **跨代覆盖**：任何自定义发布器若绕开 token/identity 校验，都可能让旧结果覆盖新结果。
6. **v2/v3 双写**：同一运行只能显式选择一种发布路径，不能自动双写或拼接历史结果。
7. **旧制品诱导**：v1 和旧 B ZIP 缺少当前身份/时域证据，只能标 `legacy_unverified`。
8. **端点沉默吸附**：调用方必须保存映射距离/理由并遵守阈值；不得绕过 orchestrator 映射。
9. **文档漂移**：归档综合指南含 74 tests、B 未工程化、v1 正式等历史说法，不可回填现状。

## 7. 数据、模型与输出位置

| 内容 | 位置/责任 | 说明 |
|---|---|---|
| 共享场景、航区、船舶和 RunContext | `../arctic_route_contracts/` | 正式公共事实来源 |
| B 风险模型与权重 | 工作包 B | C 不存储、不训练、不直接加载 |
| C 船舶性能模型 | `configs/vessel_models/`、`cost/vessel.py` | 当前为版本化演示参数，未校准 |
| C 规划/重规划参数 | `configs/planner/`、`configs/replanning/` | 进入 planner digest |
| C 机器合同 | `schemas/` 与 `contracts/` | Python 模型与 Schema 必须同步 |
| synthetic smoke 输出 | `output/demo/` | 可再生临时制品，不是正式验收依据 |
| 正式输入/输出 | 由 orchestrator 和 latest store 管理 | 不以手工复制 JSON 作为集成方式 |

`configs/scenarios/` 与 `configs/vessels/` 只保留旧夹具说明；正式共享配置不得从这些目录
重新分叉。

## 8. 下一步接手顺序

1. 先阅读本 handoff、[`BC_CONTRACT.md`](docs/BC_CONTRACT.md)、
   [`CD_CONTRACT.md`](docs/CD_CONTRACT.md) 和 [`ACCEPTANCE.md`](docs/ACCEPTANCE.md)。
2. 分别核对 A、B、contracts、orchestrator 的当前 handoff，确认同一 RunContext 与合同版本。
3. 在不改 C 算法的前提下完成真实 committed window → formal ingress → v2/v3 输出验收。
4. 与 D 固定读取、Schema、原子 latest 和失败语义；再决定持久制品/可视化扩展。
5. 最后开展科学校准；证据不足时继续保留 `demo_unvalidated`。

修改 C 时按 [`DEVELOPMENT_GUIDE.md`](docs/DEVELOPMENT_GUIDE.md) 执行，并在提交前运行
`UV_OFFLINE=1 make check` 与 `git diff --check`。

## 9. 相关文档索引

- 首读入口：[`README.md`](README.md)
- 概述：[`docs/PROJECT_OVERVIEW.md`](docs/PROJECT_OVERVIEW.md)
- 状态与待办：[`docs/STATUS_AND_TODO.md`](docs/STATUS_AND_TODO.md)
- 架构与决策：[`docs/ARCHITECTURE_AND_DECISIONS.md`](docs/ARCHITECTURE_AND_DECISIONS.md)
- 开发指南：[`docs/DEVELOPMENT_GUIDE.md`](docs/DEVELOPMENT_GUIDE.md)
- 系统架构：[`ARCTIC_ROUTE_SYSTEM.md`](../ARCTIC_ROUTE_SYSTEM.md)
- 系统排期：[`ABC_10_DAY_SPRINT.md`](../ABC_10_DAY_SPRINT.md)
- 版本历史：[`CHANGELOG.md`](CHANGELOG.md)

## 10. 需要人工确认的决策

1. D 的下一轮主消费合同是 v2 三目标还是 v3 四层整组；C 不替 D 默认选择。
2. 哪一套真实主航区 A bundle/B commit 被指定为系统验收基准，并由谁保管证据制品。
3. 科学校准的数据集、指标阈值、评审人和“允许标 calibrated”的批准流程。
4. 旧 `/mnt/c/.../交付包.zip` 是否仍需保留回归；若不再需要，应显式退役该可选测试，
   而不是把 skipped 隐去。
