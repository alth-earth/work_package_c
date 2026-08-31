# 表4-X 算法公平性与实验统一设置

> 文档性质：第四章"工作包 C 算法对比"实验公平性对照表。所有算法共享同一 RiskFrame、网格、船舶性能模型、边评估器、时间桶、硬约束与 fail-closed 语义，**仅搜索策略或目标函数不同**，故任何观测到的差异可归因于算法本身。

**关联文档**：`docs/ALGORITHM_COMPARISON_REPORT.md` §1 公平性设计、`docs/CHAPTER4_VISUAL_EVIDENCE_PLAN.md` §1 第 2 项

---

## 表4-X 算法公平性与实验统一设置

| 项目 | Proposed A* | Dijkstra | Static-field A* | Risk-blind |
|---|---|---|---|---|
| 起终点 | 一致 | 一致 | 一致 | 一致 |
| 网格 | 一致（真实 31×11 抽象索引 / 合成 5×7×7–17×29×37） | 一致 | 一致 | 一致 |
| 船舶性能模型 | 一致（`VesselPerformanceModel.from_configuration(fixture.vessel_config)`） | 一致 | 一致 | 一致 |
| 边评估器 | 一致（`EdgeEvaluator` 含 risk→speed→ETA 耦合） | 一致 | 一致 | 一致 |
| 时间离散桶 | 一致（`time_bucket_minutes`，真实 fixture 自带） | 一致 | 一致 | 一致 |
| 边采样数 | 一致（`edge_sample_count`，真实 fixture 自带） | 一致 | 一致 | 一致 |
| 最大风险帧间隙 | 一致（`max_risk_frame_gap_minutes`） | 一致 | 一致 | 一致 |
| 硬约束 | 一致（fail-closed：缺测/未来/过期/覆盖不足一律失败） | 一致 | 一致 | 一致 |
| 风险信息 | ✅ 全程使用冰情预报序列 | ✅ 同（同图上无信息） | ❌ 仅用出发时刻帧 | ❌ 权重置零 |
| 时变信息 | ✅ 时间展开状态图 | ✅ 同（同图） | ❌ 冻结为静态 | ⚠️ 权重仍含 time 项，但 risk 项取消后无可比性 |
| 启发式 | ✅ admissible heuristic | ❌ `use_heuristic=False` | ✅ | ✅ |
| 目标函数 | 一致（三目标 fastest / low_risk / recommended） | 一致 | 一致 | **改**（risk/uncertainty 置零，其余权重保持） |
| 硬件 | 统一（同容器、同 Python 3.13、同 uv 环境） | 统一 | 统一 | 统一 |
| 数据集 | 同源（真实 Winter 145 帧 + 合成 4 档） | 同源 | 同源 | 同源 |
| 终止条件 | 一致（max_expansions=250 000 或到达目标） | 一致 | 一致 | 一致 |
| Repetitions | 真实 24h ×3、合成 small/medium/large ×5、stress ×3 | 同 | 同 | 同 |
| Warmup | 1 次预热（不计入中位数） | 同 | 同 | 同 |
| Schema 版本 | `c.algorithm-comparison.v2`（含 per-step 字段） | v2 | v2 | v2 |

**关键公平性论据**：

1. **Dijkstra 与本文 A\* 跑在同一时间展开状态图**，且 Dijkstra 不含启发式。因此 Dijkstra 与本文 A\* 的代价必须严格相同（cost_identical），**任何差异只能归因于启发式加速**。
2. **Static-field 与本文 A\* 用相同规划器、相同启发式、相同目标**，仅风险场被冻结为出发时刻帧。差异归因于"使用时变预报"这一设计选择。
3. **Risk-blind 是目标函数消融**：取 recommended 目标自身权重，仅把 `risk` 和 `uncertainty` 置零，其余（travel_time / distance / turn）保持不变。这是**最小消融**——避免把时间/距离/转向权衡也改掉导致混淆。它的差异归因于"使用风险场"这一件事。
4. **risk_blind 不输出搜索效率指标**（扩展数/加速比）作为优势——因为目标函数不同时搜索效率不可比。只比较风险/时间/航程权衡。

---

## 局限性

- 真实窗口仅 2 个（holdout 2026-02-22、development 2026-03-22），网格 31×11 单一走廊。统计样本量小。
- development 窗口 recommended 目标下 `risk_blind` 与 `recommended` 路线完全相同——risk 权重在该窗口不起决定性作用。这是**真实数据特征**，非公平性问题。
- 硬件为单容器离线运行，未做跨硬件复现。

完整证据链见 `docs/CHAPTER4_VISUAL_EVIDENCE_PLAN.md`；公平性论据的源代码实现见 `scripts/benchmark_algorithm_comparison.py`。