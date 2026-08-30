---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
Document Role: SUPPORTING
Applicability: CURRENT
Scope: 当前 D Viewer 视觉平滑实现的事实、限制和与可执行研究曲线的边界
Canonical For: 当前 Viewer 绘制捷径的限制说明；不取代核心算法 SSOT
Canonical Current State: NO
Branch: research-validation-system
Last Verified: 2026-08-31 03:32 +08:00
Related Canonical Docs:
  - "CORE_ALGORITHM_IMPROVEMENT_PLAN.md"
  - "ROUTE_SMOOTHING_BSPLINE_PLAN.md"
  - "../../arctic_route_governance/standards/AGENT_DOCUMENTATION_RULES.md"
---

# 当前 Viewer 视觉平滑实现限制说明

## 0. 文档定位与更新规则（2026-08-31 02:20 +08:00）

**首要参考声明：** 本文档只记录当前 Viewer 的视觉平滑捷径、它已经改变的展示行为、
尚未证明的语义，以及向受约束可执行曲线研究迁移时必须保留的边界。航线平滑研究的
问题定义、算法方案、实验门禁和最终研究结论以
[`ROUTE_SMOOTHING_BSPLINE_PLAN.md`](ROUTE_SMOOTHING_BSPLINE_PLAN.md) 为准；工作包 C
核心算法、正式路线合同和生产边界以
[`CORE_ALGORITHM_IMPROVEMENT_PLAN.md`](CORE_ALGORITHM_IMPROVEMENT_PLAN.md) 为准。

**更新规则：**

- 本文档的当前事实必须与 D Viewer 代码一致；任何显示行为变化都必须同时更新本文档。
- 本文档不得把 `DISPLAY_ONLY` 变成 `RESEARCH_ONLY`、生产路线或船舶控制语义。
- 若新增可执行曲线研究，必须使用独立的研究文档、sidecar、实验 identity 和验证证据；
  不得把本文件中的显示参数直接复用为船舶参数。
- 当前 C 已提供 `c.research-route-smoothing-sidecar.v1` 的 geometry-only 研究输出，
  但本文件仍不把它视为安全或生产证据；D 必须由操作员显式启用研究运动开关，默认不启用。
- 历史测试、历史截图和历史实验必须标记为 `INHERITED`，不能冒充本轮运行结果。

## 1. 当前实现事实（2026-08-31 02:20 +08:00）

当前正式 C 路线仍然由 waypoint `LineString` 表示，航点、ETA、速度、风险和路线指标
保持不变。D Viewer 在 paint layer 中从这些权威航点生成局部曲线，并把曲线用于路线线条
绘制；当前 Viewer 仿真中的船位、航向和近期轨迹也会跟随该展示曲线，但这只改变本地
展示层，不改变 C 或 Orchestrator 的正式路线数据。

当前视觉参数为：

| 参数 | 当前值 | 真实含义 |
| --- | ---: | --- |
| `nominalRadiusM` | `40,000 m` | 为当前全航程地图提供可见弯曲的显示尺度 |
| `maxDeviationM` | `20,000 m` | 显示几何的最大偏离限制 |
| `cornerAngleThresholdDeg` | `8°` | 小角度折点保持线性显示 |
| 原始折线图层 | 默认隐藏 | 仅用于人工几何对照 |

`40,000 m` 不是目标船的操纵半径、最小安全转弯半径、航线安全边界或生产资格参数。
它是为了解决当前约 40 km 网格边在整张地图上仍显示为折线的问题而采用的展示尺度。

当前 C geometry-only sidecar 在固定 Viewer bundle 上选择约 `41.412 km` 的最大可行几何
半径，并提供 `1000/2000/4000 m` 最小半径敏感性摘要。这只是说明自适应几何能够脱离
固定 `2 km` 下界产生可见尺度，不能替代风险、硬掩膜、覆盖、船舶操纵性或 ETA 证明。

## 2. 为什么当前曲线仍不等于真实航行（2026-08-31 02:20 +08:00）

当前实现没有使用：

- 连续风险和 hard mask 的完整曲线包含证明；
- 曲线位置对应的 RiskFrame coverage 复核；
- 依据曲线弧长重建的正式 ETA 和速度；
- 目标船实测操纵手册、转圈试验或偏航率限制；
- 与重规划 adoption 状态绑定的可执行曲线证书。

因此当前曲线可以改善画面中的锐角观感，但不能证明船舶在该曲线上可以安全操纵。
尤其不能因为 Viewer 中船图标沿曲线移动，就声称生产船舶已经沿曲线航行。

## 3. `2 km` 与 `40 km` 的关系（2026-08-31 02:20 +08:00）

C 中的 `turn_radius_m=2000` 是明确标为 `demo_unvalidated` 的研究假设。它只能作为
候选可执行曲线的最小半径约束，不能作为当前 Viewer 的显示半径。

当前路线的明显转角约为 40°，前后边长约为 40–56 km。以圆角作数量级估算：

- 2 km 半径的切入长度约为 `2 km × tan(20°) = 0.73 km`；
- 其相对折点的最大内缩约为 `2 km × (sec(20°) - 1) = 0.13 km`；
- 该偏离放在当前全航程画布上通常接近亚像素，因此看起来仍像折线。

这不能推出 2 km 一定不是船舶可用半径，也不能推出 40 km 一定不真实。正确的研究
含义是：2 km 是下限，实际半径应在安全走廊、风险、ETA、相邻航段和船模限制下自适应
选择；如果开阔海域允许，20–40 km 的温和转弯可以作为候选结果，但必须由 sidecar
证据证明，而不能由 Viewer 为了截图固定生成。

## 4. 三种路线语义必须分开（2026-08-31 02:20 +08:00）

| 路线类型 | 来源 | 是否权威 | 是否改变船位语义 | 当前状态 |
| --- | --- | --- | --- | --- |
| 原始 waypoint 折线 | C planner | 是 | 是，作为正式 control | `CURRENT` |
| Viewer 视觉曲线 | D paint layer | 否 | 当前 Viewer 会跟随，但仅限展示仿真 | `DISPLAY_ONLY` |
| 受约束研究曲线 | C research sidecar | 否，直到另行批准 | 仅在研究 replay 中跟随 | `RESEARCH_ONLY` |

任何研究曲线失败，都必须回到原始 waypoint 运动。视觉曲线不能作为研究曲线的隐式
后备，因为它没有同等的安全、风险、时间和身份证据。

## 5. 当前实现的保留策略（2026-08-31 02:20 +08:00）

当前视觉实现暂时保留，原因是它满足 Viewer 的演示需求，并且删除它会破坏已有截图、
图层对照和显示回归。保留不代表认可其为真实操纵模型。

后续可执行研究必须：

1. 保留原始路线及其 digest；
2. 在 C 侧生成独立曲线和证据 sidecar；
3. 重新验证 hard mask、risk、coverage、ETA 和速度；
4. 在 Orchestrator 侧保持物理位置与 planner origin 分离；
5. 在 D 侧只消费已验证 sidecar；
6. sidecar 缺失、过期、不匹配或验证失败时回退原始后端运动。

D 的研究运动开关位于展示图层控制中，默认关闭。未加载 sidecar 时控件不可用；加载后
仍须通过 schema、route id、waypoint 坐标/ETA 和样本单调性检查。检查失败时船位/航向回到
既有 timeline，不使用 D 的视觉曲线作为研究 sidecar 的隐式后备。

## 6. 明确不允许的解释（2026-08-31 02:20 +08:00）

以下说法均越界：

- “40 km 是 Nordic Odyssey 的真实最小转弯半径”；
- “2 km 通过了 IMO 船舶操纵性标准”；
- “Viewer 曲线证明了生产船沿曲线航行”；
- “视觉曲线的风险与原路线风险相同”；
- “曲线连续就等于全局最优或生产可用”；
- “放大显示后的平滑效果就是实际航线性能”。

当前正确表述是：

> Viewer 已实现非权威的视觉曲线绘制；该曲线用于改善画面中的折线观感，但尚未获得
> 真实船舶操纵性、连续风险/硬约束、ETA 或生产资格证明。新的受约束曲线只能通过
> 独立研究 sidecar 和 fail-closed 门禁进入研究 replay。

## 7. 证据状态（2026-08-31 02:20 +08:00）

本文件记录的当前 Viewer 行为属于代码事实。此前 Viewer focused test、Node syntax、
离线 VM 和历史截图若未在本轮重新运行，均应继续标记为 `INHERITED`。本文件不提供
真实 6h/24h replay、cgroup 资源证据、实船操纵性证据或生产资格证据。
