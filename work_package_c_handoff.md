---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - PLANNED
Document Role: SUPPORTING
Scope: work package C handoff
Branch: research-validation-system
Last Verified: 2026-08-21
---

> [!NOTE]
> **文档治理声明**
> - 文件角色：工作包 C 当前唯一详细交接入口，供人和 AI 判断边界、现状、演示目标与接手顺序。
> - 改造时间：2026-08-15（Asia/Shanghai）。
> - 原文件去向：[work_package_c_handoff_归档_20260815.md](docs/archive/work_package_c_handoff_归档_20260815.md)。
> - 改造原因：落实挑战杯工程演示优先、双运行模式、散货船参数和非阻塞科学接口。

# 工作包 C 交接说明

> Status: CURRENT — RC1（2026-08-16）。v3 四层 + 6h 重规划已跑通（orchestrator r6/r7）；
> 性能基线、心跳与 benchmark 见 `scripts/bench_initial_planning.py` 与执行记录。

## 1. 项目目标与边界

C 把 B 的时空风险窗口转换为候选航线、ETA、风险/成本指标和重规划结果。挑战杯目标是让路线
不穿陆地、没有明显荒谬绕行、随风险变化而合理更新，并向 D 提供稳定制品。

C 负责：按 ETA 采样风险、组合演示船型得到最终速度、时间依赖规划、三目标/v3 四层、发布
围栏和重规划。C 不负责 A 下载、B 风险模型、D 页面，也不要求科学或适航认证。

## 2. 挑战杯统一口径

- 默认稳定演示：读取 A/B 冻结本地制品，按模拟时钟和 generation 运行，可断网重演。
- 历史回放：严格保持 `issue_time <= as_of_time <= simulation_time`。
- 工程演示通过即成功；科学准确、真船校准和跨专业签字不是 C 完成条件。
- 必需船型/成本参数优先公开典型值，次选透明拟合，否则使用明确标注的演示默认值。
- 五类专业接口保留，但本轮及以后默认只维护替换点、来源、版本和状态字段。
- 项目负责人对 C 有完整决策权。

## 3. 当前状态

| 能力 | 状态 | 当前口径 |
|---|---|---|
| 环境与工程门禁 | 已完成 | Python 3.13、Mamba+uv、lock/CLI；2026-08-14 为 138 passed |
| 正式 B→C ingress | 已完成 | canonical hourly committed window + execution lease |
| v2 三目标 | 已完成 | `fastest`、`low_risk`、`recommended` |
| v3 四层十二路线 | 已完成 | 整组原子发布，单次运行显式选择 |
| 重规划与围栏 | 已完成 | generation/request/revision/cancel fencing |
| 比赛冻结场景验收 | 进行中 | 仍需对真实演示数据检查路线质量和性能 |
| D 消费 | 待完成 | 挑战杯展示闭环必需 |
| 科学校准 | 非阻塞 | 保持 `demo_unvalidated` 和接口 |

## 4. 已完成清单

| 功能 | 路径 |
|---|---|
| 公共模型/Schema | `src/arctic_route_planning/contracts/`、`schemas/` |
| 正式 ingress 与快照 | `ingress.py` |
| 有界端点映射 | `endpoints.py` |
| 时空风险采样 | `risk/sampler.py` |
| 时间依赖 A* | `planners/time_dependent_astar.py` |
| 船速和成本 | `cost/` |
| v2/v3 服务 | `service.py`、`layered.py` |
| 重规划 | `replanning/` |
| 原子 latest 与 codec | `publishing/` |
| legacy 隔离 | `adapters/legacy_b.py` |

关键实现明细（源自：[work_package_c_handoff_归档_20260815.md](docs/archive/work_package_c_handoff_归档_20260815.md)）：

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

当前本地复验命令 `UV_OFFLINE=1 make check` 的结果为 138 passed。

## 5. 两种运行模式与缓存

| 模式 | C 的行为 |
|---|---|
| 历史回放/验证 | 只消费模拟时刻已可见的风险，拒绝未来信息并保留比较指标 |
| 稳定演示 | 读取预置 BC 窗口，仍按 simulation time、版本、generation 规划并至少重规划一次 |

C 只消费按 `valid_time` 排序的 BC 风险窗口；CD 以原子 latest 发布当前路线与指标并保留候选，
D 不持有 C 计算锁。旧 generation/request/revision 不能覆盖当前结果。

与架构蓝本一致之处：A/B/C/D 分责、预测驱动动态规划、滚动更新、版本化中间结果和多目标
路线仍是主干。工程化收敛（源自：[work_package_c_handoff_归档_20260815.md](docs/archive/work_package_c_handoff_归档_20260815.md)）：

- 共享上下文集中到 `arctic_route_contracts`，不靠同名文件或私有模块隐式对齐；
- BC 正式边界是逐小时、canonical、原子 committed window 和 execution lease，不是松散静态
  文件；
- C→D 已从旧 v1 演进为 v2 兼容基线与 v3 原子四层整组；
- generation/request/revision、内容摘要和 publication token 形成并发/回放围栏；
- 端点由 orchestrator 显式映射并留下审计结果，C 不静默吸附到任意网格节点。

## 6. 航线与端点

1. 先完成 `offshore_murmansk_to_offshore_dikson`：69.15°N, 33.60°E →
   73.55°N, 80.40°E；端点修正必须留在各自允许区域。
2. 后迁移 `tromso_to_isfjorden_outer`：69.75°N, 19.00°E → 78.15°N, 13.00°E。
3. 朗伊尔城 78.22°N, 15.65°E 只用于 AIS 完整航次识别；峡湾内部不参与路线优化评价。

完整 allowed region 见 [系统权威](../ARCTIC_ROUTE_SYSTEM.md)与 contracts corridor 配置。旧
`tromso_to_svalbard`/朗伊尔城算法终点只用于历史审计。

## 7. 船型与参数

当前使用明确标注的“演示散货船参数集”。共享 `nordic_odyssey_reference_v1` 提供公开参考
尺寸、Ice Class 1A 和标称速度，但不是校准性能模型，Ice Class 1A 不等于 PC6。

C 仍需版本化表达经济/最大/最小速度、转向能力、风浪流相对航向性能、等待策略、吃水/净空等。
参数不足时使用公开典型值或透明演示值；不得把演示值写成真船结果。当前等待动作仍关闭。

## 8. 未完成与待办

### P0：挑战杯演示

1. 使用冻结 A/B 场景运行 v3 四层整组并保存输入/输出摘要（v3 为 2026-08-15 确认的演示
   主线）；v2 三目标作为强制后备回归。
2. 人工检查路线不穿陆、ETA 递增、端点合理、无明显绕行和风险错位。
3. 推进模拟时钟或更新风险，完成至少一次可解释重规划。
4. 向 D 提供原子 JSON/GeoJSON、路线指标、风险帧引用和重规划原因。
5. 与 orchestrator 完成两次断网演示和失败恢复。

### P1：工程增强

- 分段测量 RiskSampler/A* 性能，设置安全超时和阶段报告；
- 用同一冻结输入完成 v3 小窗验证与整组复验（8/20 门槛）；
- 完成第二走廊迁移 smoke；
- 增强 D 所需历史/候选制品保留，但不破坏合同。

### P2：仅保留接口

- 真船、风险、法规和科学校准；
- 等待、方向相关完整性能、净水深 hard mask；
- D* Lite/LPA*/MPC 等算法升级。

P2 不阻塞挑战杯完成。

## 9. 工程演示验收

详见 [ACCEPTANCE.md](docs/ACCEPTANCE.md)。核心不是“科学正确”，而是：合同全绿、路线逻辑无
明显错误、风险变化能驱动合理路线变化、重规划和 D 展示稳定、参数和局限清楚。

```bash
cd ${ARCTIC_ROUTE_ROOT}/work_package_c
UV_OFFLINE=1 make check
```

## 10. 已知风险

- 真实冻结场景的规划性能和路线质量尚未完整复验；
- 编排器集成长运行曾超过 24 分钟，缺阶段预算；
- 当前船型、风险和成本参数未科学校准；
- bathymetry/法规区不是正式 hard constraints；
- D 仍未实现；
- v2/v3 同时展示会增加性能和解释复杂度。

风险保持记录，但只有 P0 项进入当前挑战杯主线。

补充风险清单（源自：[work_package_c_handoff_归档_20260815.md](docs/archive/work_package_c_handoff_归档_20260815.md)）：

1. **证据等级混淆**：synthetic 通过、formal provenance 和 calibrated 三者不能互相替代；
2. **时域不足**：C 不等待也不外推；B 窗口必须覆盖搜索实际 ETA，缺一小时也会拒绝；
3. **缺测误判安全**：未知风险必须由 hard mask 或 `confidence=0` 保守处理，不能补零；
4. **重复减速**：B 提供环境因子，C 计算最终速度；不得再从 risk/confidence 推导减速；
5. **跨代覆盖**：自定义发布器若绕开 token/identity 校验，可能让旧结果覆盖新结果；
6. **v2/v3 双写**：同一运行只能显式选择一种发布路径，不能自动双写或拼接历史结果；
7. **旧制品诱导**：v1 和旧 B ZIP 缺少当前身份/时域证据，只能标 `legacy_unverified`；
8. **端点沉默吸附**：调用方必须保存映射距离/理由并遵守阈值，不得绕过 orchestrator 映射；
9. **文档漂移**：旧综合指南含 74 tests、B 未工程化、v1 正式等历史说法，不可回填现状。

## 10.1 数据、模型与输出位置（源自：[work_package_c_handoff_归档_20260815.md](docs/archive/work_package_c_handoff_归档_20260815.md)）

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

## 11. 相关入口

- [C README](README.md)
- [状态与待办](docs/STATUS_AND_TODO.md)
- [验收清单](docs/ACCEPTANCE.md)
- [决策记录](docs/DECISIONS.md)
- [B→C 合同](docs/BC_CONTRACT.md)
- [C→D 合同](docs/CD_CONTRACT.md)
- [系统权威](../ARCTIC_ROUTE_SYSTEM.md)
- [十日计划](../ABC_10_DAY_SPRINT.md)

Git 提交与同步由项目负责人在本会话结束后手动执行。
