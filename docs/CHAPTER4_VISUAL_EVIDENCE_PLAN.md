# 第 4 章视觉证据计划（Chapter 4 Visual Evidence Plan）

> 文档性质：第四章"工作包 C 算法对比"部分的视觉证据清单。说明**每条论文结论需要什么指标**、**当前数据是否支持**、**新增实验清单**、**推荐图表与表格**、**原始数据位置**。

**代码基线提交**：`3db7a94`，分支 `research-validation-system`（本轮文档与绘图修改尚未提交）
**生成日期**：2026-09-01（初版）；2026-09-01 19:11 +08:00（扩样本修订，见 §2.1）；2026-09-03（动态重规划航线图修订）
**关联主报告**：`docs/ALGORITHM_COMPARISON_REPORT.md`（13 章，含 §6 创新性、§6.6 反向论证、§12 扩样本聚合）

---

## 1. 论文结论 → 所需指标矩阵

| # | 论文结论 | 维度 | 所需指标 | 当前数据是否支持 | 推荐图表/表 |
|---|---|---|---|---|---|
| 1 | 算法框架完整闭环 | 整体性 | 多源输入→规划→验证的全链路 | ✅ 不依赖数据 | **图4-1 总体技术框架** |
| 2 | 比较公平 | 公平性 | 各算法输入/目标/约束一致 | ✅ fixture 配置齐全 | **表4-X 算法公平性表** |
| 3 | 动态重规划保持航迹连续 | 空间性 × 可执行性 | 起终点、已航行轨迹、R2/R3、决策/生效点 | ✅ 冻结回顾性空间回放 | **图4-2 动态重规划航线图** |
| 4 | 效率优势随规模放大 | 效率 × 可扩展性 | 运行时间 vs 规模 + 加速比 | ✅ 4 档合成 + 104 算例扩样本 | **图4-3 runtime-scale、图4-9/4-10 sweep** |
| 5 | 效率提升不牺牲解质量 | 效率 × 质量 | 代价/风险 vs 运行时间 | ✅ real 24h raw 记录 | **图4-4 runtime-cost、图4-5 runtime-risk 散点** |
| 6 | 风险降低不只是均值 | 质量 × 稳定性 | per-step 风险分布 + 算例级聚合 | ✅ v2 schema + summary-sweep.csv | **图4-6 时序、图4-7 箱线、图4-11 风险-航程** |
| 7 | 改进候选未超越当前 | 稳定性 × 鲁棒性 | 候选全部 FAIL | ✅ SSOT §3 成熟度表 | **图4-8 门禁漏斗** |
| 8 | "未被超越"≠"性能最优" | 边界 | 红线声明 | ✅ 已声明 | （见主报告 §8 注意点 12） |
| 9 | 消融：每个模块都贡献 | 必要性 | full/risk_blind/no_heuristic/no_temporal | ✅ 4 档可严格定义；no_replanning 缺失 | **表4-Y 消融研究** |
| 10 | 性能随规模严格单调 | 可扩展性 | 绝对扩展差值 | ✅ fastest 32→247→1352→3755 | （见主报告 §5） |
| 11 | 真实 145 帧下的不一致性 | 边界 | `REAL_INPUT_FIFO_VIOLATED`/ETA 不动点不存在 | ✅ 真实数据 | （见主报告 §6.3） |
| 12 | 配对差异非偶发 | 效率 × 质量 × 鲁棒性 | 逐算例配对差异的分布与符号检验 | ✅ 104 算例 × 3 目标（246 对可比较） | **图4-9 分布/图4-10 胜负/图4-11 风险-航程** |

---

## 2. 推荐图表

全部算法对比图采用PNG+SVG及中英文版本；新增航线图采用PNG+SVG中文论文版。所有图均为白底、无3D、坐标轴/单位/图例完整，并保持跨图颜色语义一致。

**图4-4~4-7 已升级为扩样本版（2026-09-01 19:11 +08:00 起）**：原单算例版已替换为基于 104 算例 sweep 的版本。2026-09-03 在图4-1后新增图4-2动态重规划航线图，原性能图统一顺延一位。

| 图号 | 文件（中/英） | 论文结论 | 100-200 字分析 | 数据来源 | 局限性 | 建议插入 |
|---|---|---|---|---|---|---|
| 图4-1 | `fig-framework.png`/`framework.png` | 整体框架 | 7 主链路节点 + 3 反馈环节点，蓝实线主链、红虚线反馈环。 | 描述性，无数据依赖 | 仅框架示意，不证明任何性能 | §4.1 引言后 |
| 图4-2 | `fig-route-replanning-map.png`/`.svg` | 动态重规划保持航迹连续 | 12:00 UTC形成R3决策时R2仍在执行；14:17到达下一执行节点后R3生效。主图叠加决策时刻风险场、起终点和已航行轨迹，局部放大框展示R2继续向北与R3提前向西的分叉。 | 冻结Viewer bundle中的EPSG:4326风险帧、timeline、R2/R3 | 单场景回顾性动态回放，非实时因果或实船证据 | §4.5.2后 |
| 图4-3 | `fig-runtime-scale-log.png`/`runtime-scale-log.png` | 效率随规模放大 | 4 档合成 log-log：A*（实线）与 Dijkstra（虚线）随格点数对数线性增长；加速 fastest 5.67×→17.58×。增长趋势稳定且 ours 始终位于左下方。 | `summary-data.csv` synthetic 4 档 | 4 档规模较粗 | §4.7.3 |
| 图4-4 | `fig-runtime-cost.png`/`runtime-cost.png` | 更快 ≠ 牺牲代价 | **扩样本版**：246/239/246 个可行（算例,目标）单元上，A*/Dijkstra/静态场/风险无关四算法散点沿 cost 轴几乎重合，运行时间横向拉开 1~2 个数量级。 | sweep CSV wall_ms+cost_hours | n≥239/算法 | §4.7.4 |
| 图4-5 | `fig-runtime-risk.png`/`runtime-risk.png` | 更快 ≠ 更高风险 | **扩样本版**：A*与Dijkstra风险点重合而运行时间分离；静态场与风险无关基线用于说明风险差异。 | sweep CSV wall_ms+max_risk | n≥239/算法 | §4.7.4 |
| 图4-6 | `fig-risk-timeseries.png`/`risk-timeseries.png` | 风险降低不只是均值 | **扩样本版**：每窗口3条代表走廊的逐段风险序列，展示峰值变化与窗口差异。 | sweep case raw `step_edge_risk_score` | n=3代表走廊/窗口 | §4.8.1 |
| 图4-7 | `fig-risk-distribution.png`/`risk-distribution.png` | 多数场景下都更好 | **扩样本版**：104算例全部航段的风险聚合箱线，比较分布位置、离散度与极端风险。 | 同图4-6 | 聚合104算例全部航段 | §4.8.2 |
| 图4-8 | `fig-funnel.png` | 改进候选未超越当前 | 6个候选均在正确性、真实输入、资源或稳定性门禁中被拒绝，支撑“未被超越+正确性保守”的反向论证。 | `CORE_ALGORITHM_IMPROVEMENT_PLAN.md`与候选实证 | 不等于性能最优 | §4.9.2 |

**配套表**：

| 表号 | 文件 | 论文结论 | 建议插入 |
|---|---|---|---|
| 表4-X | `docs/CHAPTER4_FAIRNESS_TABLE.md` | 比较公平 | §4.2.1 |
| 表4-Y | `docs/CHAPTER4_ABLATION.md` | 消融研究 | §4.5.1 |

### 2.1 扩样本新增图（2026-09-01 19:11 +08:00）

原有性能图（现图4-3至图4-7）的规范算例观测仍受样本量限制；为回应“样本太少、说服力不足”，新增104算例配对图。数据源 `summary-sweep.csv`（312单元，其中246个效率对、239个质量对可比较）。图4-2属于独立的空间回放展示，不计入该统计样本。

| 图号 | 文件（中/英） | 论文结论 | 100-200 字分析 | 数据来源 | 局限性 | 建议插入 |
|---|---|---|---|---|---|---|
| 图4-9 | `fig-sweep-delta-distribution`/`sweep-delta-distribution` | 配对差异非偶发 | 三个面板（扩展减少 / 最大风险 / 平均风险）箱线 + 逐算例散点：扩展减少全部为正（中位数84.24%），平均风险多数为负，最大风险呈走廊相关。 | `summary-sweep.csv` | 共享2个天气窗口 | §4.9.5 |
| 图4-10 | `fig-sweep-outcome-counts`/`sweep-outcome-counts` | 胜负计数可检验 | 每指标“本文更优/持平/本文更差”计数及精确符号检验p值。 | 同图4-9 | 目标单元间相关 | §4.9.5 |
| 图4-11 | `fig-sweep-risk-vs-hops`/`sweep-risk-vs-hops` | 风险优势与航段长度 | 最大风险变化与网格跳数的关系，解释最大风险优势的走廊相关性。 | 同图4-9 | 跳数粒度有限 | §4.9.5 |

---

## 3. 原始数据位置索引

| 图表 | 原始数据 | 落盘路径 |
|---|---|---|
| 图4-2 route-replanning-map | 冻结bundle中的12:00风险帧、14:18 timeline、R2/R3航路及GEBCO底图 | `.runtime/experiments/c-chapter4-route-map/{route-overlay.csv,risk-frame-20260215T120000Z.csv,metadata.json}` |
| 图4-3 runtime-scale log | `summary-data.csv` synthetic 4档 | `.runtime/experiments/c-algorithm-comparison-synthetic-{small,medium,large,stress}/comparison.json` |
| 图4-4 runtime-cost | `summary-sweep.csv`（wall_ms + cost_hours四算法） | `.runtime/experiments/c-algorithm-comparison-summary/summary-sweep.csv` |
| 图4-5 runtime-risk | `summary-sweep.csv`（wall_ms + max_risk四算法） | 同上 |
| 图4-6 risk-timeseries | 每窗口3条代表走廊的raw `step_edge_risk_score`（`recommended`） | `.runtime/experiments/c-algorithm-comparison-sweep/cases/*/comparison.json` |
| 图4-7 risk-distribution | 104算例全部航段的raw `step_edge_risk_score` | 同上 |
| 图4-8 funnel | 改进候选门禁状态与实证 | `docs/CORE_ALGORITHM_IMPROVEMENT_PLAN.md`及对应实验构件 |
| 图4-9/4-10/4-11 sweep | `summary-sweep.csv`（312单元） | `.runtime/experiments/c-algorithm-comparison-summary/summary-sweep.csv` |
| 表4-X fairness | fixture + 算法名清单 | `work_package_c/scripts/benchmark_algorithm_comparison.py` 内置 |
| 表4-Y ablation | 4档从既有数据派生 | 同图4-5/4-6 |

扩样本原始构件：`.runtime/experiments/c-algorithm-comparison-sweep/`（`sweep-manifest.json` 列出 104 个算例的身份、起终点、出发偏移与状态；每个算例一个 `cases/<case-id>/comparison-summary.json`，schema `c.algorithm-comparison.v3`，含 `od_override/case_id/departure_offset_hours/static_frame_index`）。

---

## 4. 空间图证据边界与缺失实验说明

图4-2已补充空间回放证据；其余缺失实验仍只声明原因。**绝不伪造实验、不画虚假误差棒**。

### 4.1 风险场空间热力底图与路径地理空间叠加图

- **更新现状**：发布的 `formal-motion-original-dynamic-viewer-package-v1` 已同时提供EPSG:4326底图范围、31×11风险网格经纬度、R2/R3航路、连续航迹和重规划事件。因此图4-2可直接从同一冻结回放包绘制，无需反推投影或构造坐标。
- **证据用法**：风险底图固定为R3决策时刻2026-02-15 12:00 UTC；12:00的决策点、14:17:23的下一执行节点生效点、截至生效点的已航行轨迹、被替代R2和生效R3均按源字段原样导出。
- **诚实性边界**：该包属于研究航行仿真的回顾性动态回放。图4-2证明的是空间展示、延迟接纳和无瞬移语义，不证明实时因果重规划、实船效果或适航级能力，也不增加算法对比的独立样本数。
- **防漂移**：绘图脚本锁定bundle SHA256 `7d513b40...8fd4f`与底图SHA256 `924e8eea...6ada`；源摘要、坐标维度、风险帧或R3事件链变化时失败闭锁。图、CSV与元数据统一落盘至 `.runtime/experiments/c-chapter4-route-map/`。

### 4.2 `no_replanning` / `no_correction` 独立消融档

- **现状**：正式 `plan()` 默认就是**单次规划**（replanning baseline 是单独验证产物，不在 `plan()` 默认路径上）。"再剥一层"不构成新档。
- **缺失原因**：4 档可严格定义（full / risk_blind / no_heuristic / no_temporal）；"no_replanning" 在正式 control 下无独立语义。
- **替代**：消融表4-Y 严格只列 4 档，备注"重规划 baseline 与 `plan()` 默认路径正交"。详见 `docs/CHAPTER4_ABLATION.md`。

### 4.3 参数敏感性研究

- **现状**：`planner_config.time_bucket_minutes / edge_sample_count / max_risk_frame_gap_minutes` 等参数有配置开关，但**无系统化扫参实验数据**，SSOT 也未记录 sweep 矩阵。
- **缺失原因**：构建敏感性需 4-5 个参数笛卡尔积扫描，每组合保留 v2 raw 数据，汇总到 sensitivity matrix，预算与时间超出本轮 12h 截止线。
- **替代**：在主报告 §2 声明"实验设置一致"。**第四章不画敏感性图**。

### 4.4 development 窗口上 `risk_blind` 与本文部分目标路线重合

- **现状**：单算例证据（`c-algorithm-comparison-development-24h/comparison.json`）中 `risk_blind` 与 `recommended` 在同一 departure 下三条路线完全相同；扩样本（§12，104 算例）后 development 上 risk_blind 与本文路线仍有较高重合（risk 权重在该窗口不占决定作用），而 holdout 上两路线通常不同。
- **缺失原因**：development 窗口 risk 权重不起决定性作用，**真实数据特征**，非缺失。
- **替代**：图4-5/4-7对规范算例明确标注 `dev/risk_blind ≡ recommended`；扩样本聚合图（图4-9/4-10）单独呈现risk_blind多付风险的分布，不再依赖单算例观测。

> 2026-09-03更新：原§4.1“无坐标、无法出图”的限制已被新的冻结空间回放包解除；§4.2的独立消融缺失与§4.3的参数敏感性缺失仍未改变。

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

# 2b) 扩样本扫描（104 算例 = 2 窗口 × 5 起点 × 3 档航段 + 3 个额外出发时刻；含 risk_blind）
uv run python scripts/benchmark_algorithm_comparison_sweep.py \
  --od-per-bucket 2 --temporal-per-bucket 1 \
  --departure-offset-hours 36 --departure-offset-hours 72 --departure-offset-hours 108 \
  --workers 6 --repetitions 1 --warmup 1 \
  --output-dir /root/my_project/.runtime/experiments/c-algorithm-comparison-sweep

# 3) 汇总（单算例 + 扩样本聚合）+ 渲染全部图（含新增 sweep 3 类）
uv run python scripts/summarize_algorithm_comparison.py \
  --runs-root /root/my_project/.runtime/experiments \
  --sweep-root /root/my_project/.runtime/experiments/c-algorithm-comparison-sweep \
  --output-dir /root/my_project/.runtime/experiments/c-algorithm-comparison-summary
uv run --with matplotlib python scripts/plot_algorithm_comparison.py \
  --csv /root/my_project/.runtime/experiments/c-algorithm-comparison-summary/summary-data.csv \
  --sweep-csv /root/my_project/.runtime/experiments/c-algorithm-comparison-summary/summary-sweep.csv \
  --experiments-root /root/my_project/.runtime/experiments \
  --output-dir /root/my_project/.runtime/experiments/c-algorithm-comparison-summary/figures --both

# 4) 总体框架图
uv run --with matplotlib python scripts/plot_chapter4_architecture.py \
  --output-dir /root/my_project/.runtime/experiments/c-algorithm-comparison-summary/figures

# 5) 动态重规划航线图（只读固定Viewer bundle，不运行规划器）
uv run --with matplotlib python scripts/plot_chapter4_route_map.py \
  --bundle /root/my_project/work_package_d/output/formal-motion-original-dynamic-viewer-package-v1/bundle.json \
  --basemap /root/my_project/work_package_d/output/formal-motion-original-dynamic-viewer-package-v1/gebco_basemap.png \
  --output-dir /root/my_project/.runtime/experiments/c-chapter4-route-map

# 6) 冒烟测试
uv run python -m pytest tests/unit/test_benchmark_algorithm_comparison_script.py -q
uv run python -m pytest tests/unit/test_plot_chapter4_route_map.py -q
```

---

## 6. 与生产晋级门禁的关系

本章所有图均属**外部展示证据**，与生产晋级无关：

- 不修改 planner 实现、合同、`eta_refinement_policy` 默认值
- 不写 formal latest / replanning baseline / frozen artifact
- `uv.lock` SHA256 仍为 G0 冻结值 `8893cb83...`
- 漏斗图 6→0 仅用于说明正确性保守与候选筛选，**不得**作为性能最优证据

完整 SSOT 与诚实性边界见 `docs/ALGORITHM_COMPARISON_REPORT.md`。
