# 第 4 章视觉证据计划（Chapter 4 Visual Evidence Plan）

> 文档性质：第四章"工作包 C 算法对比"部分的视觉证据清单。说明**每条论文结论需要什么指标**、**当前数据是否支持**、**新增实验清单**、**推荐图表与表格**、**原始数据位置**。

**代码提交**：`a4d7d72`（HEAD），分支 `research-validation-system`
**生成日期**：2026-09-01
**关联主报告**：`docs/ALGORITHM_COMPARISON_REPORT.md`（11 章，含 §6 创新性、§6.6 反向论证）

---

## 1. 论文结论 → 所需指标矩阵

| # | 论文结论 | 维度 | 所需指标 | 当前数据是否支持 | 推荐图表/表 |
|---|---|---|---|---|---|
| 1 | 算法框架完整闭环 | 整体性 | 多源输入→规划→验证的全链路 | ✅ 不依赖数据 | **图4-1 总体技术框架** |
| 2 | 比较公平 | 公平性 | 各算法输入/目标/约束一致 | ✅ fixture 配置齐全 | **表4-X 算法公平性表** |
| 3 | 效率优势随规模放大 | 效率 × 可扩展性 | 运行时间 vs 规模 + 加速比 | ✅ 4 档合成 + 真实窗口 | **图4-2 runtime-scale（log-log）** |
| 4 | 效率提升不牺牲解质量 | 效率 × 质量 | 代价/风险 vs 运行时间 | ✅ real 24h raw 记录 | **图4-3 runtime-cost、图4-4 runtime-risk 散点** |
| 5 | 风险降低不只是均值 | 质量 × 稳定性 | per-step 风险分布 | ✅ v2 schema 含 step 字段 | **图4-5 时序、图4-6 箱线** |
| 6 | 改进候选未超越当前 | 稳定性 × 鲁棒性 | 候选全部 FAIL | ✅ SSOT §3 成熟度表 | （见主报告 §6.6 漏斗图） |
| 7 | "未被超越"≠"性能最优" | 边界 | 红线声明 | ✅ 已声明 | （见主报告 §8 注意点 12） |
| 8 | 消融：每个模块都贡献 | 必要性 | full/risk_blind/no_heuristic/no_temporal | ✅ 4 档可严格定义；no_replanning 缺失 | **表4-Y 消融研究** |
| 9 | 性能随规模严格单调 | 可扩展性 | 绝对扩展差值 | ✅ fastest 32→247→1352→3755 | （见主报告 §5） |
| 10 | 真实 145 帧下的不一致性 | 边界 | `REAL_INPUT_FIFO_VIOLATED`/ETA 不动点不存在 | ✅ 真实数据 | （见主报告 §6.3） |

---

## 2. 推荐图表

所有图：PNG+SVG、中文主版本（Noto Sans CJK SC）、白底、无 3D、坐标轴/单位/图例完整、同一算法跨图视觉一致（A*=蓝、Dijkstra=橙、Static=绿、Risk-blind=红）、有重复实验显示中位数、n=1 显式标注。

| 图号 | 文件（中/英） | 论文结论 | 100-200 字分析 | 数据来源 | 局限性 | 建议插入 |
|---|---|---|---|---|---|---|
| 图4-1 | `fig-framework.png`/`framework.png` | 整体框架 | 7 主链路节点 + 3 反馈环节点，蓝实线主链、红虚线反馈环。 | 描述性，无数据依赖 | 仅框架示意，不证明任何性能 | §4.1 引言后 |
| 图4-2 | `fig-runtime-scale-log.png`/`runtime-scale-log.png` | 效率随规模放大 | 4 档合成 log-log：A*（实线）与 Dijkstra（虚线）随格点数对数线性增长；加速 fastest 5.67×→17.58×。增长趋势稳定且 ours 始终位于左下方。 | `summary-data.csv` synthetic 4 档 | 4 档规模较粗 | §4.3.2 效率对比 |
| 图4-3 | `fig-runtime-cost.png`/`runtime-cost.png` | 更快 ≠ 牺牲代价 | 真实 24h 上 A* 与 Dijkstra 点几乎重合在 cost 轴；横向拉开 1 个数量级。证明启发式加速不牺牲解代价。 | raw JSON cost+wall_ms | n=2 真实窗口 | §4.3.3 |
| 图4-4 | `fig-runtime-risk.png`/`runtime-risk.png` | 更快 ≠ 更高风险 | A* 在两窗口 6 单元中风险均低于 Dijkstra；dev 上 risk_blind ≡ recommended（n=1 标注）。效率优势与风险降低同向。 | `summary-data.csv` wall_ms+max_risk | n=2、dev 上 risk_blind 与 recommended 等价 | §4.3.3 |
| 图4-5 | `fig-risk-timeseries.png`/`risk-timeseries.png` | 风险降低不只是均值 | 真实 24h recommended：holdout（左）/dev（右）逐段风险序列：A*（蓝）始终位于绿/红线下方，峰值被显著压制；dev 上蓝/橙/红几乎重合（窗口风险可压低空间小）。 | raw JSON `step_edge_risk_score` | n=1 推荐目标每窗口 | §4.4.1 |
| 图4-6 | `fig-risk-distribution.png`/`risk-distribution.png` | 多数场景下都更好 | holdout：A* 与 Dijkstra（低风险箱子窄小）位于下方；static 与 risk_blind（红）箱子更宽、上移。极端风险（须须）被静态规划抬升。 | 同图4-5 | n=1 | §4.4.2 |

**配套表**：

| 表号 | 文件 | 论文结论 | 建议插入 |
|---|---|---|---|
| 表4-X | `docs/CHAPTER4_FAIRNESS_TABLE.md` | 比较公平 | §4.2.1 |
| 表4-Y | `docs/CHAPTER4_ABLATION.md` | 消融研究 | §4.5.1 |

---

## 3. 原始数据位置索引

| 图表 | 原始数据 | 落盘路径 |
|---|---|---|
| 图4-2 runtime-scale log | `summary-data.csv` synthetic 4 档 | `.runtime/experiments/c-algorithm-comparison-synthetic-{small,medium,large,stress}/comparison.json` |
| 图4-3 runtime-cost | raw JSON real 24h wall_ms + total_cost_hours | `.runtime/experiments/c-algorithm-comparison-{holdout,development}-24h/comparison.json` |
| 图4-4 runtime-risk | `summary-data.csv` real 24h wall_ms + max_risk | 同上 |
| 图4-5 risk-timeseries | raw JSON recommended `step_edge_risk_score` | 同上（v2 schema） |
| 图4-6 risk-distribution | 同图4-5 | 同上（v2 schema） |
| 表4-X fairness | fixture + 算法名清单 | `work_package_c/scripts/benchmark_algorithm_comparison.py` 内置 |
| 表4-Y ablation | 4 档从既有数据派生 | 同图4-4/4-5 |

---

## 4. 缺失实验与说明（明确不出图）

以下三项**严格未实现**，仅在本节声明缺失原因。**绝不伪造实验、不画虚假误差棒**。

### 4.1 风险场空间热力底图与路径地理空间叠加图

- **现状**：真实 Winter 输入的 risk frame 仅在 commit 内以 digest 索引，独立 zst 文件不含网格坐标到经纬度的投影矩阵。网格为 31×11 抽象索引，路径节点仅记录 `(row, col)`。
- **缺失原因**：将风险场复原到真实经纬度需要反归一化 zst 帧、已知海冰预报网格的投影矩阵、路径节点从网格索引到经纬度的映射，三者**均未暴露**。强行画会涉及未做做的坐标变换，违反"禁止编造不存在的数据"。
- **替代**：图4-5 时序图用 `step_edge_risk_score` 逐段呈现风险沿航线的变化；图4-6 分布图直接呈现风险统计特征。**第四章不画风险场热力图**。

### 4.2 `no_replanning` / `no_correction` 独立消融档

- **现状**：正式 `plan()` 默认就是**单次规划**（replanning baseline 是单独验证产物，不在 `plan()` 默认路径上）。"再剥一层"不构成新档。
- **缺失原因**：4 档可严格定义（full / risk_blind / no_heuristic / no_temporal）；"no_replanning" 在正式 control 下无独立语义。
- **替代**：消融表4-Y 严格只列 4 档，备注"重规划 baseline 与 `plan()` 默认路径正交"。详见 `docs/CHAPTER4_ABLATION.md`。

### 4.3 参数敏感性研究

- **现状**：`planner_config.time_bucket_minutes / edge_sample_count / max_risk_frame_gap_minutes` 等参数有配置开关，但**无系统化扫参实验数据**，SSOT 也未记录 sweep 矩阵。
- **缺失原因**：构建敏感性需 4-5 个参数笛卡尔积扫描，每组合保留 v2 raw 数据，汇总到 sensitivity matrix，预算与时间超出本轮 12h 截止线。
- **替代**：在主报告 §2 声明"实验设置一致"。**第四章不画敏感性图**。

### 4.4 development 24h 上 `recommended` 目标下 `risk_blind` 与本文完全一致

- **现状**：`c-algorithm-comparison-development-24h/comparison.json` 中 `risk_blind` 与 `recommended` 在同一 departure 下三条路线完全相同。
- **缺失原因**：development 窗口 risk 权重不起决定性作用，**真实数据特征**，非缺失。
- **替代**：图4-4/4-6 **明确标注 `dev/risk_blind ≡ recommended`**，避免读者误判 n=1 偏差。

---

## 5. 复现方式

```bash
cd /root/my_project/work_package_c

# 1) 重新生成 24h 构件（v2 schema，含 per-step 字段）
for win in holdout development; do
  COMMIT=$(ls /root/my_project/.runtime/experiments/winter-b-validation-$win-total-*/risk-store/commits/risk-window-sha256-*.json | head -1)
  ROUTE=$(ls /root/my_project/.runtime/experiments/winter-c-validation-$win-total-*/winter-four-layer-route-plan-set-v3.json | head -1)
  uv run python scripts/benchmark_algorithm_comparison.py \
    --real-commit $COMMIT --real-route-plan-set $ROUTE \
    --real-segment rolling_0_24h --repetitions 3 --warmup 1 \
    --algorithm time_dependent_astar --algorithm dijkstra \
    --algorithm static_field --algorithm risk_blind \
    --output-dir /root/my_project/.runtime/experiments/c-algorithm-comparison-$win-24h
done

# 2) 4 档合成
for p in small medium large stress; do
  uv run python scripts/benchmark_algorithm_comparison.py \
    --synthetic-profile $p --repetitions 5 --warmup 1 \
    --output-dir /root/my_project/.runtime/experiments/c-algorithm-comparison-synthetic-$p
done

# 3) 汇总 + 渲染全部图（含新增 4 类）
uv run python scripts/summarize_algorithm_comparison.py \
  --runs-root /root/my_project/.runtime/experiments \
  --output-dir /root/my_project/.runtime/experiments/c-algorithm-comparison-summary
uv run --with matplotlib python scripts/plot_algorithm_comparison.py \
  --csv /root/my_project/.runtime/experiments/c-algorithm-comparison-summary/summary-data.csv \
  --experiments-root /root/my_project/.runtime/experiments \
  --output-dir /root/my_project/.runtime/experiments/c-algorithm-comparison-summary/figures

# 4) 总体框架图
uv run --with matplotlib python scripts/plot_chapter4_architecture.py \
  --output-dir /root/my_project/.runtime/experiments/c-algorithm-comparison-summary/figures

# 5) 冒烟测试（11 项应保持通过）
uv run python -m pytest tests/unit/test_benchmark_algorithm_comparison_script.py -q
```

---

## 6. 与生产晋级门禁的关系

本章所有图均属**外部展示证据**，与生产晋级无关：

- 不修改 planner 实现、合同、`eta_refinement_policy` 默认值
- 不写 formal latest / replanning baseline / frozen artifact
- `uv.lock` SHA256 仍为 G0 冻结值 `8893cb83...`
- 漏斗图 6→0 仅用于说明正确性保守与候选筛选，**不得**作为性能最优证据

完整 SSOT 与诚实性边界见 `docs/ALGORITHM_COMPARISON_REPORT.md`。