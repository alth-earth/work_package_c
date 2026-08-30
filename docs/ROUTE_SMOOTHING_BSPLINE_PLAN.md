---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - PLANNED
  - EXPERIMENTAL
Document Role: SUPPORTING
Applicability: RESEARCH_ONLY
Scope: 受约束局部三次 B 样条航线平滑的 R0.1 问题定义、展示-only 实现、研究设计与验证门禁
Canonical For: 工作包 C 航点处航向突变和平滑航线研究的详细定义；从属于核心算法 SSOT
Canonical Current State: NO
Branch: research-validation-system
Last Verified: 2026-08-30 23:38 +08:00
Related Canonical Docs:
  - CORE_ALGORITHM_IMPROVEMENT_PLAN.md
  - ARCHITECTURE_AND_DECISIONS.md
  - CD_CONTRACT.md
  - ../../arctic_route_governance/standards/AGENT_DOCUMENTATION_RULES.md
---

# 受约束局部三次 B 样条航线平滑：R0.1 问题定义、展示实现与研究计划

## 0. 文档定位与更新规则（2026-08-30 22:46 +08:00）

**首要参考声明：** 本文档是“受约束局部三次 B 样条航线平滑”研究主题的详细支持文档，
负责记录航点处航向突变的定义、参数来源、候选算法、实验方案、证据和决策。工作包 C
核心算法的总体状态、正式路线合同、生产边界和跨主题优先级仍以
[`CORE_ALGORITHM_IMPROVEMENT_PLAN.md`](CORE_ALGORITHM_IMPROVEMENT_PLAN.md) 为唯一核心算法
SSOT。本文档不能取代 C 核心 SSOT、`cd.route-plan.v2`、四层 v3 或治理仓库中的当前规范。

**更新规则：**

- 先更新本文档，再实施与本文档一致的研究代码、测试或实验；实现后必须记录 commit、
  输入身份、实验 identity、结果摘要和验证成熟度。
- R0.1 的问题定义已经完成。本轮只实现 D Viewer 的展示-only 局部曲线绘制；C 的可执行
  后处理、风险/硬掩膜/ETA 重评估、真实 replay 和生产资格仍分别写成
  `NOT_IMPLEMENTED`、`NOT RUN` 或 `NOT_QUALIFIED`。
- 原始网格折线继续是 C 的正式 control；D 的展示曲线可以作为 Viewer 的默认绘制策略，
  但只能存在于非权威 paint layer，不得静默进入 ingress、service、合同、formal latest、
  replanning baseline 或 frozen artifact。
- 任何风险、硬掩膜、ETA、来源身份和船舶参数均须复用现有正式语义；缺失、越界、身份不匹配
  或无法证明的曲线必须 fail-closed 回退原始路线。
- 若未来改变 `cd.route-plan.v2`、四层 v3 或 D 的权威几何语义，必须另走跨包合同提案，
  并把批准结果回填本文档和核心 SSOT；不得在本研究文档中直接改变正式接口。
- 本文档不创建 `P0.2-M35`，也不重开已在 M34 收束的 P0.2 路径。R0 是独立的路径几何/可
  执行性研究候选，只有完成独立门禁后才可决定是否继续。

**治理和证据依据：** 本文档遵循
[`AGENT_DOCUMENTATION_RULES.md`](../../arctic_route_governance/standards/AGENT_DOCUMENTATION_RULES.md)
的元数据、时间戳、SSOT、ADR、`RUN`/`NOT RUN`/`INHERITED` 和验证成熟度规则。它是
`Document Role: SUPPORTING`、`Applicability: RESEARCH_ONLY`，不是生产路线规范。

## 1. 执行摘要（2026-08-30 22:46 +08:00）

R0.1 的目标是具体确认“当前航线在航点处存在航向突变”这一问题，而不是先假定 B 样条
一定能够解决它。只读核对当前代码后，结论如下：

1. 当前 C 从规则经纬度网格的离散节点生成 `RouteStep`，再生成带 ETA 和推荐速度的
   `Waypoint`；路线 GeoJSON 以这些航点形成 `LineString`。因此几何上是折线。
2. 对任意内部航点，如果入射航向与出射航向不同，折线在该点只有位置连续（通常为
   `C0`），切向方向发生不连续；若要求有限转弯半径，这个理想化折点不能代表连续船舶运动。
3. 这证明了“存在几何平滑问题”，但尚未证明当前正式路线已经造成真实船舶操纵失败。现有
   航点可能被控制器解释为航路指导点，船舶也可能提前转向；该行为目前不在 C 路线几何中
   被显式表达。
4. 三次 B 样条适合作为候选表达和局部平滑方法，但无约束插值不自动避开陆地、硬掩膜或
   风险，也不自动满足最小转弯半径、速度、偏航率和 ETA 约束。本轮因此只把它用于
   D 的展示几何；不会把视觉曲线当成可执行路线。

### R0.1 关键增量表（2026-08-30 22:50 +08:00）

| 指标 / 声明 | Before | After | Delta | Verdict / 原因 |
|---|---|---|---|---|
| 当前路线几何 | 网格航点 `LineString` | 明确定义为离散折线基线 | 问题口径被固定 | `COMPLETED`；代码未改变 |
| 航点处航向突变 | 只有 `turn_count` 等间接计数 | 定义入射/出射航向差 `Δψ` 与转弯长度需求 | 新增可审计问题指标 | `COMPLETED_AS_DEFINITION`；尚无路线统计 |
| 船舶参考参数 | C 内部未校准演示值 | 公开 Nordic Odyssey 尺度/速度 + 明确的 2,000 m 工作假设 | 来源和假设分离 | `RESEARCH_ONLY`；不是实船校准 |
| B 样条实现 | 不存在 | D 已实现局部米制、端点保持、偏离限界和 fail-closed 的展示-only 曲线 | 仅改善画面，不改变权威路线 | `IMPLEMENTED / DISPLAY_ONLY` |
| 真实航线平滑收益 | 未证明 | 未运行 | 无新增真实证据 | `NOT RUN` |
| 正式合同与生产路线 | `cd.route-plan.v2` / v3 冻结 | 保持不变 | 无语义变化 | `UNCHANGED` |

## 2. 范围 / 非范围（2026-08-30 22:46 +08:00）

**本轮范围：**

- 完成航点处航向突变的数学和工程定义；
- 核对当前 C 的路线生成、航点、路线几何和 Viewer 约束；
- 检索公开散货船和北方航线数据；
- 将现有船模值与公开资料分层记录；
- 确定候选算法和后续研究阶段；
- 设定安全、语义、质量、资源和停止门禁。

**本轮非范围：**

- 不修改 Python API、schema、合同、搜索上限或生产数据流；
- 不把展示曲线接入 C planner、ingress、service、风险采样、ETA、船舶运动或生产发布；
- 不运行 real replay、不运行长时间实验、不宣称真实操纵性或生产资格；
- 不把地图显示加密当作权威路线平滑；
- 不修改 `demo_bulk_carrier_v1` 或 `nordic_odyssey_reference_v1` 配置；
- 不将公开船舶资料当作当前船舶的实测操纵性；
- 不启动 P0.2-M35，不重开 M31–M34 的剪枝/资源研究；
- 不修改 formal latest、replanning baseline 或 frozen artifact；D 只增加非权威绘制层。

## 3. 起始基线与问题定义（2026-08-30 22:46 +08:00）

### 3.1 当前代码事实（2026-08-30 22:46 +08:00）

当前 C 的 `RegularGrid` 允许规则网格的八邻域移动，边几何由相邻节点之间的线性采样构成。
Planner 的 `RouteStep` 保存节点、经纬度、ETA、入射航向和速度；应用服务把它们转换成
`Waypoint`。`RoutePlan` 当前只要求航点、ETA、速度和路线指标，未包含样条控制点、节点向量、
曲率或转向率字段。C 的 GeoJSON 路线几何直接按 waypoint 坐标生成 `LineString`。

相关代码：

- [`RegularGrid.neighbors` 和边线性采样](../src/arctic_route_planning/grid/regular.py:75)；
- [`RouteStep`](../src/arctic_route_planning/planners/time_dependent_astar.py:104)；
- [`Waypoint` 和 `RoutePlan`](../src/arctic_route_planning/contracts/models.py:184)；
- [`RoutePlan` 转换](../src/arctic_route_planning/service.py:366)；
- [`LineString` 序列化](../src/arctic_route_planning/publishing/serialization.py:170)；
- D 侧明确说明路线加密只用于屏幕绘制，不改变权威 waypoint 几何：
  [`USER_GUIDE.zh-CN.md`](../../work_package_d/docs/USER_GUIDE.zh-CN.md:150)。

### 3.2 航向突变的形式化定义（2026-08-30 22:46 +08:00）

设连续航点为 (P_{i-1},P_i,P_{i+1})，在局部米制坐标中定义：

\[
\psi_i^- = \operatorname{bearing}(P_{i-1},P_i), \qquad
\psi_i^+ = \operatorname{bearing}(P_i,P_{i+1})
\]

\[
\Delta\psi_i = \operatorname{wrap}_{[-180^\circ,180^\circ]}(\psi_i^+ - \psi_i^-)
\]

其中 (i) 是内部航点。

定义如下：

| 条件 | R0.1 分类 | 含义 |
|---|---|---|
| `Δψ = 0` | `STRAIGHT_CONTINUATION` | 局部共线，不存在方向突变 |
| `Δψ ≠ 0` | `CORNER_PRESENT` | 折线切向发生突变，是候选平滑点 |
| `Δψ ≠ 0` 且要求有限半径 | `FINITE_RADIUS_GAP` | 原始折点不能直接代表有限曲率运动 |
| 数据缺失、重复点或坐标异常 | `INVALID_GEOMETRY` | 不进行平滑，必须回退原始路线 |

原始折线的转角报告分箱 `0–15°`、`15–45°`、`45–90°`、`>90°` 仅用于统计和选点，
不是安全阈值。是否可平滑还取决于可用走廊、硬掩膜、风险、速度和船舶约束。

如果采用半径 (R_{min}) 的圆弧作为最简单的有限转弯参考，转角为

\[
s_{turn}=R_{\min}|\Delta\psi_i|_{rad}
\]

在本轮工作假设 (R_{min}=2000\,m) 下：

| 转角 | 所需参考转弯弧长 | 10 kn 下参考时间 | 13.5 kn 下参考时间 |
|---:|---:|---:|---:|
| 45° | 1.571 km | 约 5.1 min | 约 3.8 min |
| 90° | 3.142 km | 约 10.2 min | 约 7.5 min |

这说明“在同一个航点瞬间改变航向”和“具有有限转弯半径的船舶运动”不是同一种几何
语义。它仍不等于当前正式航线已经不安全，因为当前系统没有明确表达提前转向或控制器
跟踪误差；R0.2 必须统计真实路线中实际出现的转角和可用转弯空间。

### 3.3 R0.1 的具体问题陈述（2026-08-30 22:46 +08:00）

> 在当前 C 规则网格航点路线中，内部航点处 `Δψ ≠ 0` 的折点没有显式的有限转弯长度、
> 曲率上界或船舶航向变化过程。需要研究一种默认关闭、可回退、可证明不越过安全走廊的
> 局部曲线表示，判断它是否能在真实路线中减少航向突变，同时不改变原始路线的风险、
> ETA、来源身份和生产合同语义。

R0.1 的问题定义完成条件：

- 能从每条路线计算 `Δψ`、转角分箱和 `s_turn`；
- 能区分“显示不平滑”和“可执行几何不连续”；
- 能指出哪些点是候选平滑点，哪些点必须保持原始折线；
- 能用公开资料和明确假设解释 (R_{min}) 的研究用途；
- 不将“存在折点”越界表述为“真实船舶已失败”或“当前路线已不适航”。

## 4. 船舶参考、数据来源与工作假设（2026-08-30 22:46 +08:00）

### 4.1 公开参考船（2026-08-30 22:46 +08:00）

当前 C 已有 `nordic_odyssey_reference_v1`，其名称与既有项目背景对应一艘公开的北方航线
Panamax 散货船参考。汉堡港公开船舶资料给出的 Nordic Odyssey 信息包括：散货船、
75,603 DWT、船长 225 m、船宽 32.31 m、吃水 14.08 m、标称速度 15.7 kn。
北方航线研究资料报告，近年航次平均航速大体约 10 kn，逐月统计约为 10–11 kn；这比
船舶标称最大速度更适合作为北方航线的运行参考。

来源：

- [Port of Hamburg: Nordic Odyssey](https://www.hafen-hamburg.de/en/vessels/nordic-odyssey-25019/)；
- [POAC 2013: Study on feasibility of the Northern Sea Route from recent voyages](https://poac.com/Papers/2013/pdf/POAC13_225.pdf)，其表格列出 Nordic Odyssey 为 75,603 DWT 的冰级散货船，并给出北方航线航速统计。

### 4.2 操纵性资料的用途限制（2026-08-30 22:46 +08:00）

公开资料可用于确定数量级，但不能替代目标船的操纵性试验或 manoeuvring booklet：

- 一篇 55,000 DWT 散货船研究示例使用 220 m、16 kn，并在指定 35° 舵角试验条件下报告约
  3 个船长的战术直径；这是特定试验条件，不是北极巡航的最小安全转弯半径。
- Capesize 研究中的不同舵角模拟得到约 608–1,366 m 的转弯半径范围，说明半径会随船型、
  舵角、速度和试验条件变化，不能压缩成一个普遍常数。
- IMO MSC.137(76) 给出的是船舶操纵性试验的验收指标，例如 advance 和 tactical diameter
  的上限；它不是规划曲线的最小转弯半径，也不能直接由 `5L` 反推路线半径。

来源：

- [55,000 DWT bulk carrier manoeuvrability study](https://oaji.net/articles/2016/3207-1468300478.pdf)；
- [Capesize bulk carrier turning-radius study](https://www.bibliotekanauki.pl/articles/1203872.pdf)；
- [IMO Resolution MSC.137(76)](https://wwwcdn.imo.org/localresources/en/KnowledgeCentre/IndexofIMOResolutions/MSCResolutions/MSC.137(76).pdf)。

### 4.3 当前工作参数分层（2026-08-30 22:46 +08:00）

| 参数 | 数值 | 来源等级 | R0 用途 | 是否为真实校准 |
|---|---:|---|---|---|
| Nordic Odyssey 船长 | 225 m | 公开港口资料 | 尺度参照 | 否，非目标船实测模型 |
| Nordic Odyssey 船宽 | 32.31 m | 公开港口资料 | 尺度参照 | 否 |
| Nordic Odyssey DWT | 75,603 t | 公开港口/POAC 资料 | 船型相似性参照 | 否 |
| 标称最大速度 | 15.7 kn | 公开港口资料；C reference model | 上界数量级参照 | 否 |
| 北方航线运行速度 | 10–11 kn | POAC 航次统计 | R0 运行速度场景 | 否，非本船稳定速度 |
| C demo cruise speed | 13.5 kn | C 本地配置 | 保持现有兼容场景 | 明确未校准 |
| C reference economic speed | 10.0 kn | C 本地配置 + NSR 公开统计数量级 | 保持现有兼容场景 | 明确未校准 |
| `turn_radius_m` | 2,000 m | C 本地演示假设 | R0 暂定 `R_min` 工作值 | 否 |

当前配置文件已明确写出 `demo_unvalidated` 和“不得用于真实航行”的限制：

- [`demo_bulk_carrier_v1.toml`](../configs/vessel_models/demo_bulk_carrier_v1.toml)；
- [`nordic_odyssey_reference_v1.toml`](../configs/vessel_models/nordic_odyssey_reference_v1.toml)。

因此 R0.1 的正式口径为：

> 使用公开船舶资料支持“当前散货船数量级”判断；使用 C 现有 `turn_radius_m=2000 m`
> 作为保守、透明、可复现的研究工作假设；不得将其写成 Nordic Odyssey 或任何真实目标船
> 的实测最小转弯半径。

## 5. 算法确认与路线选择（2026-08-30 22:46 +08:00）

### 5.1 三次 B 样条的适用性（2026-08-30 22:46 +08:00）

三次 B 样条使用三次基函数表示二维参数曲线：

\[
C(u)=\sum_i N_{i,3}(u)P_i
\]

普通内部节点不重复时，三次曲线通常可使位置、一阶导数和二阶导数连续；重复节点会降低
连续性。B 样条的局部支撑允许只调整某个转角附近的控制点。它们是有利条件，不是安全
证明：

- 插值样条可以穿过所有输入航点，但仍可能在航点之间过冲；
- 逼近/平滑样条可能不穿过中间航点，必须明确哪些航点是硬点；
- `C2` 连续不自动给出曲率上界；
- 曲线在经纬度度数空间拟合会扭曲距离和曲率，必须使用局部米制坐标；
- 控制点安全不等于整条曲线在非凸陆地/硬掩膜附近安全；
- 曲线必须重新按弧长和时间进行风险、速度、ETA 评估。

官方说明：[SciPy `make_interp_spline`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.interpolate.make_interp_spline.html)
和 [SciPy B-spline basis/knots 说明](https://docs.scipy.org/doc/scipy/tutorial/interpolate/splines_and_polynomials.html)。

### 5.2 选定候选（2026-08-30 22:46 +08:00）

选定的研究候选是：

> 受约束局部三次 B 样条后处理（Constrained Local Cubic B-spline Post-processor）

候选流程：

1. 输入已完成搜索的原始 waypoint 折线、端点航向、同一 RiskFrame、船舶参数和时间窗口；
2. 将局部经纬度变换为米制坐标；
3. 识别 `CORNER_PRESENT`，排除狭窄通道、硬掩膜边界、数据覆盖不足和没有足够转弯长度的点；
4. 对可行转角建立局部三次 B 样条，保留端点和必要硬点，使用实际入射/出射方向作为端点切向约束；
5. 在控制点偏移、走廊、曲率、风险和路径长度之间进行受限优化；
6. 使用自适应弧长采样评估硬掩膜、风险、曲率、速度、偏航率和 ETA；
7. 生成带来源 digest、控制点/节点摘要、约束结果和采样摘要的 research sidecar；
8. 任一检查失败则返回原始折线，不发布不完整曲线。

### 5.3 方案取舍（2026-08-30 22:46 +08:00）

| 方案 | 能解决的问题 | 主要风险 | R0 决策 |
|---|---|---|---|
| Viewer 显示加密 | 视觉折线感 | 不改变权威路线和实际操纵性 | 仅在问题被证明为显示问题时考虑 |
| 无约束全局三次插值 | 视觉连续、实现简单 | 过冲、穿陆、端点方向错误、曲率失控 | 拒绝作为正式候选 |
| 局部受约束三次 B 样条 | 局部转角、曲率和偏离可控 | 需要安全走廊、风险重采样和时间参数化 | `R0` 首选研究方案 |
| 圆弧/回旋线/曲率感知规划器 | 更直接表达操纵约束 | 需要更强规划模型和更大改动 | 作为 B 样条失败后的备选，不自动启动 |

## 6. 代码 / 架构设计边界（2026-08-30 22:46 +08:00）

本轮实现采用展示-only 路径。D 是 Viewer 的唯一运行时 owner，曲线只由浏览器绘制层从
原始 waypoint/候选 geometry 计算，不写回 C 或 Orchestrator 的路线数据：

```text
formal C route (authoritative waypoint polyline)
        |  (raw waypoints / ETA / route metrics remain unchanged)
        +--> D display-only constrained local cubic B-spline renderer
                    |
                    +--> smoothed pixels for route lines
                    +--> raw polyline fallback on invalid geometry

future, separately approved:
formal C route --> executable post-processor --> risk/ETA/motion qualification
```

展示-only renderer 的约束是几何约束，不是航行安全证书：

- 局部米制坐标，不在经纬度度数上拟合；
- 保留原始路线首尾点，内部只替换足够长且转角超过阈值的局部折点；
- 使用端点切向控制的 clamped cubic B-spline（等价的四控制点 Bézier 段）；
- 对相邻转角避免局部曲线重叠，检查最大显示偏离、有限值和最小显示曲率半径；
- 任一检查失败时，绘制原始折线/线性加密结果；
- 不生成 ETA、速度、风险、hard mask、航向或船位字段。

若未来实现可执行 research sidecar，仍至少需要记录：

- 原始 `plan_id`、原始路线 semantic digest、输入/模型/规划配置 digest；
- vessel profile、工作半径、速度场景和约束版本；
- 局部坐标参考、degree、节点向量摘要和控制点摘要；
- 起终点和硬点保持结果；
- 最大曲率、最大偏航率、最大横向加速度和最大偏离；
- 风险/硬掩膜采样覆盖、来源身份和时间范围；
- 原始路线与平滑路线的距离、ETA、风险差异；
- `accepted` 或明确的 `fallback_reason`。

这不是现有公共 schema 的设计批准。任何进入生产的曲线都必须保留原始权威 waypoint，并
另行提交合同、消费者、digest、重规划和回滚方案。

## 7. 语义 / 合约变更（2026-08-30 22:46 +08:00）

R0.1 没有业务语义和合同变更。未来研究必须维持以下不变量：

| 领域 | 当前语义 | 平滑研究约束 |
|---|---|---|
| B → C | RiskFrame、窗口、provenance 和 identity 由 B 提供 | 使用同一已提交 RiskFrame，不改风险公式 |
| C 搜索 | 原始网格折线为正式 control | 后处理不能篡改搜索结果或伪造 expansion/edge 收益 |
| 路线风险 | 按实际位置和 ETA 采样 | 曲线改变位置后必须重新采样，不能复用旧折线风险指标 |
| ETA / 速度 | waypoint ETA 严格递增，速度来自 C 船模/边评估 | 按弧长重新参数化，失败则回退 |
| C → D | waypoint 和指标为当前权威几何 | D 可从原始字段计算展示-only 曲线，但不得替换权威 geometry、ETA、metrics 或船位语义 |
| 重规划 | adopted route 受现有 generation/request/revision 围栏保护 | 每次新路线重新生成 sidecar，不允许瞬移 |
| 失败语义 | 未知、缺测、不匹配必须 fail-closed | 曲线无效时返回原始路线，并保留拒绝原因 |

## 8. 实验 / 备选方案与实施计划（2026-08-30 22:46 +08:00）

### 8.1 阶段计划（2026-08-30 22:46 +08:00）

| 阶段 | 目标与工作 | 交付物 | 进入条件 | 状态 |
|---|---|---|---|---|
| R0.1 | 固定航向突变问题、公开参考和工作假设 | 本文档、`Δψ`/`s_turn` 定义、约束边界 | 代码和当前文档只读核验完成 | `COMPLETED` |
| R0.2 | 统计当前 synthetic/冻结路线的转角、可用空间和基线粗糙度 | baseline report、候选点清单 | R0.1 完成；不改生产路径 | `PLANNED` |
| R0.3-D | 在 D Viewer 实现局部米制、clamped cubic B-spline 和端点保持 | `viewer/route_smoothing.js`、`app.js` 绘制接入 | R0.1 完成；不改变权威路线 | `COMPLETED / DISPLAY_ONLY` |
| R0.4-D | 建立显示几何的转角、重叠、偏离、曲率和 fail-closed 约束 | 展示-only validator、原始折线回退 | R0.3-D 输出可重复 | `COMPLETED / DISPLAY_ONLY` |
| R0.5-D | display-only synthetic/unit/source regression 矩阵 | D focused tests、Node syntax、C/D 语义回归 | R0.4-D 拒绝条件可观测 | `UNIT_PASS` |
| R0.6 | 固定输入真实 6h shadow 对比原始折线 | real route quality/resource summary | R0.5 全通过且存在 real 候选点 | `PLANNED` |
| R0.7 | 仅在 R0.6 有真实收益且资源方案完整时做 24h | cgroup-complete qualification evidence | 预注册门禁全部通过 | `PLANNED` |
| R0.8 | 决定展示-only、研究保留、合同提案或收束 | final decision record | 展示目标已明确；可执行资格仍需独立证据 | `COMPLETED / DISPLAY_ONLY_SCOPE` |

### 8.2 R0.2 基线统计（2026-08-30 22:46 +08:00）

R0.2 不先改算法，而是对现有路线做统计：

- 每个内部航点的入射/出射 bearing、`Δψ` 和转角分箱；
- `s_turn` 与前后航段长度的可行性比较；
- 路线到 hard mask、风险高区和允许区域边界的距离；
- 直线段、连续锯齿、短边、重复点和端点附近转角；
- 原始路线的离散粗糙度指标：最大单位距离航向变化、转角数量和长度代价；
- 记录哪些“转角”只是网格表达，哪些确实有足够空间进行局部平滑。

R0.2 必须使用固定输入和可复现 digest；若没有真实路线中的有效候选转角，R0 应直接
停止，不为了展示 B 样条而制造 synthetic 收益。

### 8.3 R0.3-D–R0.4-D 展示几何和约束实现（2026-08-30 23:38 +08:00）

本轮已实现的展示-only 实现满足：

1. 不在原始经纬度度数上计算曲率；
2. 保持路线首尾点；内部仅替换有足够转弯空间且超过阈值的折点；
3. 使用入射/出射线段方向作为 cubic B-spline 的端点切向；
4. 控制点和曲线采样受相邻转角不重叠、最大显示偏离和最小显示曲率半径约束；
5. 采样间距按局部曲线长度和固定显示上限确定；
6. 所有非有限、重复、短边或约束失败输入都回退到原始折线的线性显示；
7. 返回值只含显示坐标和诊断状态，不含 ETA、速度、风险或安全资格。

展示-only 默认参数固定在 D 的 `route_smoothing.js`，只代表画面尺度，不代表目标船校准：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `nominalRadiusM` | `2,000 m` | 局部曲线的显示尺度；沿用 C 中透明的未校准工作数量级 |
| `maxDeviationM` | `5,000 m` | 曲线采样点到原始折线的最大显示偏离 |
| `cornerAngleThresholdDeg` | `8°` | 小于该变化量的折点保持线性绘制 |
| `sampleSpacingM` | `750 m` | 曲线采样的显示间距上限 |
| `maximumDisplayPoints` | `10,000` | 防止异常输入造成无界绘制工作 |

以下仍未实现，不能从本轮 Viewer 代码推出：

- corridor/hard mask 连续包含关系；
- 使用 `RiskSampler` 的曲线风险和 coverage 重评估；
- 按曲线弧长重建 ETA、速度、偏航率或横向加速度；
- 真实船舶操纵性约束、cgroup 资源证据和 production qualification。

### 8.4 R0.5-D–R0.7 验证方案（2026-08-30 23:38 +08:00）

display-only synthetic 至少包括：单个 45°/90° 转角、连续锯齿、重复点、短边、端点保持、
相邻转角重叠、超出偏离限界和非法坐标回退。它只验证画面几何，不验证海图安全。

可执行研究的原始 adversarial 集合仍包括狭窄通道、陆地边界、风险 coverage 缺失、时间身份
不匹配和重规划身份变化；本轮不运行这些 C 侧验证。

真实 6h 使用与原始路线相同的输入、时间窗口、船模、RiskFrame、目标和搜索上限，只做
shadow，不改正式发布。每条路线记录平滑成功率、回退率、曲率、航向变化率、最大偏离、
硬掩膜距离、风险和 ETA 差异、耗时、RSS、digest 和确定性。

真实 24h 不是默认步骤；只有 6h 发现真实且可解释的非零平滑收益，并且能启用完整 cgroup
限制和证据时才允许进入。

### 8.5 上一轮方案归纳与目标矩阵（2026-08-30 22:50 +08:00）

本节将上一轮已经提出的完整 R0 方案集中归纳，作为后续执行时的单一任务清单。上一轮方案
的核心不是“把折线画成曲线”，而是先确认航点处航向突变是否构成实际可执行性问题，再在
不改变当前生产合同的前提下验证受约束曲线是否有真实价值。

| 目标 | 具体目标 | 必须证明的内容 | 未满足时的动作 |
|---|---|---|---|
| `G1` 问题确认 | 证明当前网格航点在 `Δψ ≠ 0` 时存在几何切向不连续 | 原始路线的转角、长度、可用转弯空间和风险边界可复算 | 若只有视觉问题，转为非权威显示研究；不改 C 路线 |
| `G2` 曲线质量 | 生成局部、连续、受船舶约束的三次 B 样条 | 端点/硬点、切向、曲率、最大偏离和采样策略明确 | 曲线不满足约束时局部回退，不使用全局无约束插值 |
| `G3` 安全与语义 | 不改变原始风险、ETA、来源身份和失败关闭语义 | hard mask、风险 coverage、时间参数化、确定性和 replan 语义通过 | 拒绝曲线并返回原始路线 |
| `G4` 真实证据 | 在固定 synthetic、real 6h 和条件性 real 24h 中观察真实收益 | 平滑成功率、粗糙度、曲率、风险/ETA 差异、耗时、RSS 和 cgroup 证据 | 不把 synthetic 或显示收益升级为生产资格 |
| `G5` 范围决策 | 判断继续研究、显示-only、合同提案或收束 | 有真实、可解释、超过测量噪声的收益 | 关闭 R0，不创建新的重复路径 |

上一轮方案与当前阶段的对应关系固定如下：

| 上一轮计划内容 | 本文档承接位置 | 当前状态 |
|---|---|---|
| 先区分显示平滑、几何平滑和可执行平滑 | 第 1、2、3.3、7 节 | 已归纳；R0.1 已完成定义 |
| 先做现有路线几何审计，不先写代码 | 第 8.2 节 | `R0.2 PLANNED` |
| 使用局部米制坐标和局部受约束 cubic B-spline | 第 5.1、5.2、8.3 节 | D 展示-only 已实现；C 可执行 sidecar 未实现 |
| 保留端点、端点方向和必要硬点 | 第 5.2、6、11.1 节 | 设计约束已确定 |
| 同时约束 corridor、hard mask、risk、curvature、ETA 和速度 | 第 6、7、8.3、11.1 节 | 仅显示几何约束已实现；安全/时间约束未实现 |
| 采用自适应采样并重新计算曲线风险和时间 | 第 5.2、7、8.3 节 | 显示采样已实现；风险/时间重算未实现 |
| 所有失败 fail-closed，回退原始折线 | 第 5.2、7、11.1 节 | display-only 几何失败回退已实现；安全失败仍未实现 |
| synthetic adversarial 矩阵 | 第 8.4 节 | display-only 单元矩阵 `UNIT_PASS`；完整安全矩阵未运行 |
| 固定输入 real 6h shadow，24h 条件性启动 | 第 8.4 节、9 节 | `R0.6/R0.7 PLANNED`，当前 `NOT RUN` |
| 若仅有视觉收益则不改 C 权威路线 | 第 2、7、15.2 节 | 已确定 |
| 若要生产化则另走合同和消费者提案 | 第 0、6、7、15.1 节 | 已确定，未启动 |

上一轮方案的停止规则也一并固定：没有真实候选转角、没有可验证船舶操纵性约束、没有
非零真实质量收益、风险/硬掩膜/ETA 不能保持，或新增资源成本超过预注册预算时，均不得
通过增加采样点、改变显示方式、放宽约束或重复 synthetic 来制造成功结论。

## 9. 权威运行 / 真实验证（2026-08-30 23:38 +08:00）

本轮只验证 D 展示-only 几何和接口边界，不运行 replay 或长时间实验。以下仍为
`NOT RUN`：

- C 侧 synthetic B 样条安全生成；
- R0.2 路线统计；
- C 单元测试或完整 `make check`；
- real 6h/24h replay；
- cgroup 资源资格复核；
- C 侧风险、hard mask、ETA 和船舶运动重评估；
- 生产 ingress/service。

M31–M34 的历史实验结果只能作为当前 C 研究边界的 `INHERITED` 背景，不能作为 B 样条
平滑的验证证据；本轮 display-only 测试也不能作为船舶安全或生产资格证据。

本轮新增 `RUN` 证据为：D display-only focused pytest `25 passed`，Orchestrator 展示策略
focused pytest `13 passed`，D 新增测试 Ruff 通过，`route_smoothing.js` 和 `app.js` 的
Node syntax 通过。D 的历史全量测试数、此前 Firefox 证据和 Orchestrator 历史测试数继续
标记为 `INHERITED`，没有在本轮冒充重跑结果。

## 10. 性能分解（2026-08-30 22:46 +08:00）

当前没有 Before/After 运行数据：

| 指标 | Before | After | Delta | Verdict / 原因 |
|---|---|---|---|---|
| A* 扩展/队列/label | 当前正式 control | 未改变 | `UNKNOWN` | R0 不优化搜索，不得伪造收益 |
| 平滑额外耗时 | `UNKNOWN` | `NOT RUN` | `UNKNOWN` | 必须在 shadow 中单独测量 |
| 平滑额外 RSS | `UNKNOWN` | `NOT RUN` | `UNKNOWN` | 继承 C 对资源证据的严格边界 |
| edge/risk 评估次数 | 原折线评估 | 可能增加曲线采样 | `UNKNOWN` | 不能把额外采样隐藏在旧指标中 |
| 航线长度/ETA/风险 | 原始路线指标 | 未计算 | `UNKNOWN` | 曲线改变空间后必须重新计算 |

本轮展示 track 的首要收益目标是减少航点处的几何锐角观感，不是减少 A* expansion，也不是
证明可执行性。若未来进入可执行 track，必须另行测量真实质量、风险、ETA、资源和船舶约束；
不能把本轮画面改善当作这些证据。

## 11. 正确性 / 验证门禁（2026-08-30 22:46 +08:00）

### 11.1 强制门禁（2026-08-30 22:46 +08:00）

| 门禁 | 目标 | 证据要求 |
|---|---|---|
| 端点 | 起点、终点和必要硬点保持 | 几何误差在预注册数值容差内 |
| 方向 | 端点切向符合输入航向；内部曲线无未检查折点 | 端点导数、曲率和采样报告 |
| 硬安全 | hard mask 穿越数为 `0` | 保守走廊或足够的自适应采样；不把未覆盖当安全 |
| 风险 | 使用同一 RiskSampler 和完整来源身份 | 风险 coverage、source IDs、时间窗完整 |
| 曲率 | `κ <= 1 / R_min` | 每段最大值、P95 和证书/诊断状态 |
| 船舶运动 | 偏航率、横向加速度、速度变化不超已知限制 | 若无真实限制，只能 `NOT QUALIFIED` |
| 时间 | ETA 严格递增且与曲线弧长/速度一致 | 重新计算后的 waypoint/sidecar |
| 失败 | 任何失败都回退原始路线 | synthetic fail-closed 矩阵 |
| 确定性 | 同输入产生相同曲线和 digest | 至少重复运行比较 |
| 重规划 | 不出现物理位置瞬移 | 复用现有 adoption 语义并记录身份 |

### 11.2 质量和资源目标（2026-08-30 22:46 +08:00）

建议在 R0.2 开始前冻结以下初始研究门槛；它们不是当前事实：

- 至少 `80%` 的“有足够安全走廊且满足船舶约束”的 eligible corners 成功平滑；
- 对存在候选转角的路线，预注册粗糙度指标下降至少 `20%`；
- 平滑路线的最大偏离不超过路线安全走廊允许值；
- 最大风险和积分风险不出现未经批准的恶化；
- 平滑额外耗时和 RSS 不超过预注册预算；
- 只要无法获得真实船舶操纵性参数，就不得给出生产资格结论。

如果 R0.2 显示真实路线没有可平滑转角，或者 R0.6 的收益低于测量噪声，则不启动 R0.7。

### 11.3 本轮展示-only 验收门禁（2026-08-30 23:38 +08:00）

| 门禁 | 本轮要求 | 结果口径 |
|---|---|---|
| 路线 authority | 原始 waypoint、候选 geometry、ETA、metrics 和 route revision 不被改写 | 代码结构检查；通过后仍只称 `DISPLAY_ONLY` |
| 几何坐标 | 使用局部米制坐标，首尾点保持，局部曲线不跨相邻转角 | Node synthetic checks；通过 |
| 视觉约束 | 有限值、最小显示曲率半径、最大显示偏离和显示点数受限 | Node synthetic checks；通过 |
| 失败语义 | 非法坐标、重复点、短边、无候选角或曲线约束失败回退原始折线加密 | Node synthetic checks；通过 |
| 物理语义 | vessel position、heading、completed track 不读平滑后的点 | Viewer source checks；通过 |
| 安全/资格 | 不得由显示曲线推出 hard mask、风险、ETA、操纵性或生产资格 | 文档与 metadata 明确；未资格化 |

## 12. 确定性 / 可复现性（2026-08-30 22:46 +08:00）

当前状态：

- R0.1 文档定义：`RUN`；
- D Viewer B 样条展示实现：`IMPLEMENTED / DISPLAY_ONLY`；
- D display-only synthetic/结构验证：`UNIT_PASS`（本轮）；
- C 侧安全 B 样条与 real 平滑：`NOT_IMPLEMENTED / NOT RUN`；
- M31–M34 结果：`INHERITED`，仅用于研究边界背景。

未来实验必须绑定：

- C commit；
- 原始 `plan_id` 和路线 semantic digest；
- RunContext、RiskFrame commit/content digest；
- scenario、corridor、vessel profile 和 planner config digest；
- B 样条 degree、节点、控制点、坐标投影和约束版本；
- 采样策略和随机种子（若有）。

`compute_ms`、进程耗时等 wall-clock 字段允许变化，但曲线几何 digest、端点、约束结果、
风险来源身份和失败语义必须稳定。

## 13. 构件 / 溯源（2026-08-30 22:46 +08:00）

本轮没有生成新的 real replay/runtime 实验构件；新增 Viewer 源文件和 focused 测试属于代码
构件，公开资料和当前配置的溯源如下：

| 构件/来源 | 类型 | 状态 | 说明 |
|---|---|---|---|
| `configs/vessel_models/demo_bulk_carrier_v1.toml` | 当前 C 配置 | `INHERITED` | `13.5 kn`、`R=2000 m`，明确未校准 |
| `configs/vessel_models/nordic_odyssey_reference_v1.toml` | 当前 C 配置 | `INHERITED` | `10 kn`、`15.7 kn`、`R=2000 m`，明确未校准 |
| Port of Hamburg Nordic Odyssey 页面 | 公开资料 | `REFERENCE_ONLY` | 尺度、DWT、标称速度 |
| POAC 2013 NSR 研究 | 公开研究 | `REFERENCE_ONLY` | NSR 航速和 Nordic Odyssey 货运背景 |
| 55,000 DWT 操纵性研究 | 公开研究 | `REFERENCE_ONLY` | 特定试验条件下的转弯数量级 |
| Capesize 转弯半径研究 | 公开研究 | `REFERENCE_ONLY` | 舵角相关半径数量级 |
| IMO MSC.137(76) | 官方标准 | `REFERENCE_ONLY` | 操纵性试验指标，不是路线半径参数 |
| `work_package_d/viewer/route_smoothing.js` | 展示-only 代码 | `IMPLEMENTED / DISPLAY_ONLY` | 仅把原始路线转换为 Canvas paint coordinates |
| `work_package_d/tests/unit/test_route_smoothing.py` | focused 测试 | `UNIT_PASS` | Node synthetic、加载顺序和 authority 分离检查 |

本轮实现提交：D `efd2ec6ccf38f881ebc39afae195cb9bfdaa36a6`，Orchestrator
`d22980e816557a16e062fade3e06826aae845e66`。两者均为本地提交，未 push。

拟议未来实验 identity 前缀为 `c.route-smoothing.bspline.v1-<digest>`，目前只是命名
建议，不代表已存在构件。

## 14. 已知限制 / 技术债（2026-08-30 22:46 +08:00）

| ID | 影响 / 严重度 | 当前限制 | 下一步 |
|---|---|---|---|
| `RS-TD-01` | 高 | 没有目标船的实测操纵性、偏航率和速度-曲率数据 | R0.1 使用透明假设；资格前必须补齐或明确不能资格化 |
| `RS-TD-02` | 高 | 当前 `turn_radius_m` 是未校准 C 假设 | 不改配置；只作为 research working value |
| `RS-TD-03` | 高 | 当前正式 RoutePlan 没有曲线/曲率字段 | 研究 sidecar；生产化另走合同提案 |
| `RS-TD-04` | 高 | 风险采样和硬掩膜是时间/空间约束，样条会改变采样位置 | R0.4 使用同一 RiskSampler 重新评估 |
| `RS-TD-05` | 中 | 网格路线是否真的需要平滑尚无真实路线统计 | R0.2 先统计，不先实现 |
| `RS-TD-06` | 中 | 采样验证不等于连续安全证明 | 建立保守走廊；否则只标记诊断 |
| `RS-TD-07` | 中 | D 显示加密和 C 权威几何容易混淆 | 继续明确非权威显示与可执行路线的区别 |

## 15. 决策 / 下一阶段（2026-08-30 22:46 +08:00）

### 15.1 ADR：选择受约束局部三次 B 样条作为首选候选（2026-08-30 22:46 +08:00）

**Decision：** 允许继续设计“局部、受约束、默认关闭、可 fail-closed 回退”的三次 B 样条
后处理；不允许直接使用无约束全局插值，也不改变当前正式路线合同。

**Context：** 当前 C 的网格 waypoint 在 `Δψ ≠ 0` 的内部点形成几何折点；导师提出的三次
B 样条具有局部控制和连续导数的潜力，但当前 C 缺少曲线安全、时间参数化和船舶操纵约束。

**Alternatives：** Viewer 仅显示加密、无约束全局样条、圆弧/回旋线、曲率感知规划器。

**Reason：** 局部 B 样条可以先在 C 内部验证真实问题，改动范围小于重写搜索状态；同时
必须通过硬掩膜、风险、ETA 和船舶约束检查。显示加密不解决可执行性，无约束样条安全性
不足；圆弧/回旋线和曲率感知规划器保留为更强但更大范围的后备。

**Consequences：** 研究会增加路线后处理和风险重采样成本；曲线不能自动继承原始路线指标；
任何失败必须回退；如果最终需要生产发布，将产生新的合同和 D 消费影响评审。

### 15.2 当前决策（2026-08-30 23:38 +08:00）

- R0.1：`COMPLETED`，完成问题定义、参数分层和候选路线选择；
- R0.3-D/R0.4-D：已实现为 D Viewer 展示-only 局部曲线；原始 waypoint、ETA、metrics、
  active route、船位和船头方向仍是唯一权威语义；
- R0.5-D：`UNIT_PASS`，D focused pytest、Node syntax、加载/authority 结构检查通过；
- R0.2：`PLANNED`，尚未运行真实路线统计；
- R0.3/R0.4 的 C 可执行后处理、风险/硬掩膜/ETA/船舶运动约束：`NOT_IMPLEMENTED`；
- P0.2：继续 `COMPLETED_TO_M34`，不创建 M35；
- 正式 planner、默认配置、candidate、合同和生产数据流：保持不变；
- 当前展示目标已完成；不以展示效果推导真实可执行性或生产资格。只有未来提出实质不同、
  可形式化且有真实可观测收益的新命题，才另立可执行研究计划。

最终只能在 R0.8 选择以下结果之一：

1. 仅显示平滑：留在非权威显示层（本轮已选择）；
2. 研究型可执行改善：另立合同和生产资格计划；
3. 没有真实价值：关闭 R0，保留本文档作为审计记录。
