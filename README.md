---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - IN_PROGRESS
  - PLANNED
Document Role: CANONICAL
Scope: work package C entrypoint and public boundary
Branch: research-validation-system
Last Verified: 2026-09-02
---

> [!NOTE]
> **文档治理声明**
> - 文件角色：工作包 C 当前首读入口与最小运行指南。
> - 改造时间：2026-08-15（Asia/Shanghai）。
> - 原文件去向：[README_归档_20260815.md](docs/archive/README_归档_20260815.md)。
> - 改造原因：同步挑战杯工程演示验收、双运行模式和非阻塞科学接口。

> **路径约定（2026-08-24）**：本文件中 `${ARCTIC_ROUTE_ROOT}` 为工作区根占位符，
> 指向包含各工作包目录（`arctic_route_contracts/`、`work_package_a/` 等）的公共根。
> 解析优先级：环境变量 > 当前所在目录 > `$HOME`。完整定义见
> `arctic_route_governance/README.md` 的"路径约定"章节。

# 北极航线工作包 C

## 正式局部 B 样条与批量性能（2026-09-02）

受约束局部三次 B 样条已经通过正式兄弟合同 `cd.route-motion-set.v1` 接入，并非仅有
Viewer 本地绘制。它只替换满足转角、走廊、hard mask、连续风险、ETA/速度、曲率和操纵性
门禁的局部窗口；若整条候选失败，producer 会在保持 raw 路线全局合格的前提下逐角恢复
可安全采用的局部曲线。无合格转角时必须发布 `RAW_PASSTHROUGH`，不能为了连续显示而绕过
门禁。

当前默认 Winter 原始冻结链共有 9 个 revision。采用正式参数
`max_trim_fraction=0.49`、`sample_spacing_m=250.0` 后，R1–R4 发布 `CURVE`；R5–R7 因
`integrated_risk_increased` 回退权威折线，R8–R9 因 `no_eligible_corner` 回退。对 R5–R7
已经复核 `0.25/0.35/0.45/0.47/0.49`，失败原因不随 trim 改变，不能为了画面连续而绕过
风险门禁。R1 full-voyage 曲线为 982 个正式样本，最小曲率半径约 7,464 m、最大偏离约
723 m；曲线测地长度约 919.21 km，相对权威 RoutePlan 约 921.38 km 缩短约 2.17 km。

`max_trim_fraction` 控制转角两侧可参与局部平滑的窗口比例，直接影响可获得曲率半径；
`sample_spacing_m` 控制曲线离散采样密度，主要影响验证/显示分辨率和成本，而不是独立的
“目标转弯半径”旋钮。`minimum_radius_m` 是安全下限，不是半径选择器。性能主要消耗在连续
走廊、风险重采样和完整资格门禁，不是 Cox–de Boor 基函数求值；提速仍只允许同一调用内
复用 raw 风险采样和前置廉价门禁，不跨 identity 缓存、不减少安全检查。

## Research Validation 定位（2026-08-21 23:18）

C 的阶段角色为 Risk-aware Navigation Decision。四层 × 三目标 = 12 路线已实现并由
RC1 artifact 验证；正式默认执行仍是 12 次独立 A*。SMO-A* 共享遍历记忆化已作为
显式、默认关闭的 C 内部实验路径提供；增量 search 和 replay Viewer candidate publication
尚未实现。

核心算法研究线已完成 P0 exact-arrival-time 候选、P1 内部可恢复 session 骨架、P2 同目标
单调约束证书复用和 P2.1 control-trace equivalence；P3 SMO-A* 已完成初版实现，P3.1 已完成
内存/证据加固与 ARA* 的 synthetic M0 可行性骨架。P2.1 在显式、默认关闭、不发布的 shadow 路径中，对同 goal 收紧重复查询取得
可重复的 M0/M1 耗时优势；默认 control、正式合同与 frozen artifact 均未改变。该结论不外推到
不同 anchor、完整 Winter、全局最优或生产默认。当前状态、
证据、失败实验和后续门禁以
[`CORE_ALGORITHM_IMPROVEMENT_PLAN.md`](docs/CORE_ALGORITHM_IMPROVEMENT_PLAN.md) 为准。

The isolated component profiler and first synthetic measurements are documented
in [`C_PERFORMANCE_PROFILE.md`](docs/archive/performance/C_PERFORMANCE_PROFILE.md). No production search
or cache behavior changed.

The real B-frame/C-search comparison is documented in
[`BC_COUPLING_PERFORMANCE_REPORT.md`](docs/archive/performance/BC_COUPLING_PERFORMANCE_REPORT.md), and the
bounded next-step gate in [`C_OPTIMIZATION_PROPOSAL.md`](docs/archive/performance/C_OPTIMIZATION_PROPOSAL.md).
The planner exposes observational edge-geometry and opt-in traversal-cache
statistics. The default cache key, eviction policy, A* behavior, routes and
public contracts remain unchanged; SMO-A* is not enabled by formal callers.

C 消费 B 的逐小时风险窗，按 ETA 采样风险，运行时间依赖规划，并输出三目标路线、可选 v3
四层整组和重规划结果。

## 当前口径

| 项目 | 状态 |
|---|---|
| 版本/工程 | 0.4.0；当前研究工作树的最新检查结果以 `make check` 实际输出为准 |
| 挑战杯主线 | v3 四层 × 三目标（12 路线整组）+ 重规划；v2 三目标为强制后备 |
| RC1 实源状态 | PASS（2026-08-16）：mur/dikson v3 四层 + 6h 重规划经 orchestrator r6/r7 跑通；单目标 144h ≈96s |
| RC2 BC 扩展 | `bc.risk-frame.v2` 可选 `hard_reason`（NONE/LAND/DATA_UNAVAILABLE/OTHER），旧帧向后兼容（RC2 分支） |
| 稳定演示 | 读取冻结本地数据，按 simulation time/generation 运行 |
| 历史回放 | 保持发布时间门禁，不使用未来信息 |
| 科学状态 | `demo_unvalidated`；保留接口，不阻塞工程演示 |
| 使用边界 | 禁止真实导航和安全决策 |

补充口径（源自：[README_归档_20260815.md](docs/archive/README_归档_20260815.md)）：

- B→C：正式入口只接受完整、逐小时、canonical、原子提交的 `RiskFrame v2` 窗口；
- C→D：新运行可显式选择 v2 三目标或 v3 四层整组；同一运行禁止双写；
- `formal` 表示输入的来源、身份和时间证据通过合同，不等于风险模型或船舶参数已经科学校准；
  provenance 与 calibration status 必须分开报告。

## 责任边界

A 下载/预处理，B 生成风险，C 生成最终船速、ETA、路线和重规划，D 只读展示。C 不扫描 A/B
私有目录，不从风险/置信度重复推导环境减速。

```text
A DatasetBundle v2 + RunContext v2
                │
                ▼
B CommittedRiskWindow / RiskFrame v2
                │  同一 execution lease
                ▼
C endpoint mapping → ETA sampling → time-dependent A*
                │
                ├─ v2 三目标兼容基线
                └─ v3 四层 × 三目标 → atomic layered latest
                                      │
                                      ▼
                                  D 只读消费
```

- A 负责数据获取、规范化、归档和回放；C 不读 A 私有数据库或缓存；
- B 提供风险、硬约束、置信度和 `environment_speed_factor`；
- C 把环境因子应用到版本化船型，并计算最终航速、ETA、成本、路线与重规划；
- C 不从 `risk_score` 或 `confidence` 重复推导物理减速，不外推风险，不把缺测当安全。

## 快速检查

```bash
cd ${ARCTIC_ROUTE_ROOT}/work_package_c
UV_OFFLINE=1 make check
make demo
```

`make demo` 是 synthetic 工程 smoke；比赛主线还需冻结 A/B 输入和 D 可视化。

正式运行入口（源自：[README_归档_20260815.md](docs/archive/README_归档_20260815.md)）：系统级正式运行由
[`arctic_route_orchestrator`](../arctic_route_orchestrator/) 组装 A、B 和 C；C 也提供
Python 公共入口 `RiskSourcePlanningIngress`。正式调用方必须：

1. 使用 `map_corridor_endpoints(...)` 获得有界、可审计的起终点节点；
2. 用完整 `PlanningConfiguration` 和同一 `RunContext` 创建请求；
3. 通过 committed-window execution lease 执行；
4. 对单次运行显式选择 `execute()` 或 `execute_four_layer()`。

正式 C CLI 并不存在；不得用 `synthetic-demo` 或 `legacy-plan` 代替系统编排器。

## 文档入口

- [详细 handoff](work_package_c_handoff.md)
- [状态与待办](docs/STATUS_AND_TODO.md)
- [工程演示验收](docs/ACCEPTANCE.md)
- [稳定决策](docs/DECISIONS.md)
- [B→C 合同](docs/BC_CONTRACT.md)
- [C→D 合同](docs/CD_CONTRACT.md)
- [核心算法现状、改进方案与实施计划（首要参考）](docs/CORE_ALGORITHM_IMPROVEMENT_PLAN.md)
- [P0/P1/P2/P2.1 时间语义、会话与 control-trace 复用验证入口](scripts/validate_temporal_semantics.py)
- [系统权威](../arctic_route_governance/current/architecture/ARCTIC_ROUTE_SYSTEM.md)
- [当前路线图](../arctic_route_governance/current/CURRENT_ROADMAP.md)
