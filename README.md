> [!NOTE]
> **文档治理声明**
> - 文件角色：工作包 C 当前首读入口与最小运行指南。
> - 改造时间：2026-08-15（Asia/Shanghai）。
> - 原文件去向：[README_归档_20260815.md](README_归档_20260815.md)。
> - 改造原因：同步挑战杯工程演示验收、双运行模式和非阻塞科学接口。

# 北极航线工作包 C

C 消费 B 的逐小时风险窗，按 ETA 采样风险，运行时间依赖规划，并输出三目标路线、可选 v3
四层整组和重规划结果。

## 当前口径

| 项目 | 状态 |
|---|---|
| 版本/工程 | 0.4.0；2026-08-14 为 138 passed |
| 挑战杯主线 | v3 四层 × 三目标（12 路线整组）+ 重规划；v2 三目标为强制后备 |
| RC1 实源状态 | PASS（2026-08-16）：mur/dikson v3 四层 + 6h 重规划经 orchestrator r6/r7 跑通；单目标 144h ≈96s |
| 稳定演示 | 读取冻结本地数据，按 simulation time/generation 运行 |
| 历史回放 | 保持发布时间门禁，不使用未来信息 |
| 科学状态 | `demo_unvalidated`；保留接口，不阻塞工程演示 |
| 使用边界 | 禁止真实导航和安全决策 |

补充口径（源自：README_归档_20260815.md）：

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
cd /root/my_project/work_package_c
UV_OFFLINE=1 make check
make demo
```

`make demo` 是 synthetic 工程 smoke；比赛主线还需冻结 A/B 输入和 D 可视化。

正式运行入口（源自：README_归档_20260815.md）：系统级正式运行由
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
- [系统权威](../ARCTIC_ROUTE_SYSTEM.md)
- [十日计划](../ABC_10_DAY_SPRINT.md)
