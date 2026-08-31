# 表4-Y 消融研究（Ablation Study）

> 文档性质：第四章"工作包 C 算法对比"消融对照表。回答"本文性能提升到底是哪一部分带来的"。

**关联文档**：`docs/ALGORITHM_COMPARISON_REPORT.md` §6 创新性、`docs/CHAPTER4_VISUAL_EVIDENCE_PLAN.md` §1 第 8 项

---

## 表4-Y 消融研究

| Variant | 描述 | Runtime | Expanded | Cost | Mean Risk | Max Risk | Sample |
|---|---|---|---|---|---|---|---|
| Full | 完整模型（本文 `time_dependent_astar`） | 2 012.3 ms (dev) / 2 147.5 ms (hold) | 567 (dev) / 653 (hold) | 一致 | 0.15119 / 0.07552 | 0.18733 / 0.09007 | n=2 真实窗口 |
| No risk（risk_blind） | 最小消融：risk+uncertainty 权重置零，其余保持 | 与 Full 接近（详见 raw JSON） | 同图 | dev: 0.0% 差异；hold: 风险 +10.4%/+15.4%，时间 -0.5% | 高 | 高 | n=1 有效（dev 与 Full 路线相同） |
| No heuristic（Dijkstra） | 同图零启发式 | 14 818.9 ms (dev) / 16 839.5 ms (hold) | 4 185 (dev) / 4 864 (hold) | **严格一致** | 同 Full | 同 Full | n=2 真实窗口 |
| No temporal（Static-field） | 风险场冻结为出发时刻帧 | 与 Full 接近 | 接近 | dev: 路线不同；hold: 航程同 397.4 km 但风险高 15.3%/29.8% | 高 | 高 | n=2 真实窗口 |

---

## 分析（100-200 字）

- **No risk**：development 上与 Full 完全相同（路线重合），说明 risk 权重在该窗口不起决定性作用；holdout 上风险 +10.4%/+15.4%、时间 -0.5%——证明"使用风险场"在该窗口带来 15.4% 峰值风险降低而仅多 0.5% 时间。
- **No heuristic（Dijkstra）**：扩展数增加 6-7 倍、耗时增加 7-8 倍，但**代价严格相同**——证明启发式加速不牺牲最优性，是工程实现正确性验证。
- **No temporal（Static-field）**：holdout 上航程与 Full 完全相同（397.4 km）但风险高 15.3%/29.8%——证明"使用时变预报"的价值在于**在同一条走廊上选出更安全的通行时机**，而非绕行。

---

## 缺失档说明

**`no_replanning` / `no_correction` 独立档**未列入本表，原因：

- 正式 `plan()` 默认就是**单次规划**（无重规划），重规划 baseline 是单独验证产物，与 `plan()` 默认路径正交。
- 在正式 control 下不存在"再剥一层重规划"的独立语义，强行添加会引入与生产路径不一致的实验。
- 若需对比"是否重规划"，应使用 `replanning baseline` 产物对照，而非在 `plan()` 默认路径上构造消融。

详见 `docs/CHAPTER4_VISUAL_EVIDENCE_PLAN.md` §4.2。

---

## 局限性

- 消融样本 n=2 真实窗口，其中 development 上 risk_blind 与 recommended 路线完全相同，故有效样本 n=1（holdout）。
- Dijkstra 行的 cost_identical=True 来自同图最优性证明，不构成性能优势。
- Static-field 是构造基线，非某一公开算法复现，优势应表述为"设计选择带来的收益"。

完整诚实性边界见 `docs/ALGORITHM_COMPARISON_REPORT.md` §8。