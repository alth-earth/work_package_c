---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - FROZEN
  - EXPERIMENTAL
Document Role: SUPPORTING
Applicability: RESEARCH_ONLY
Scope: 受约束局部三次 B 样条航线平滑的 R0/R1 问题定义、展示实现、合成船模约束 shadow 与验证门禁
Canonical For: 工作包 C 航点处航向突变和平滑航线研究的详细定义；从属于核心算法 SSOT
Canonical Current State: NO
Branch: research-validation-system
Last Verified: 2026-08-31 12:50 +08:00
Related Canonical Docs:
  - CORE_ALGORITHM_IMPROVEMENT_PLAN.md
  - ARCHITECTURE_AND_DECISIONS.md
  - CD_CONTRACT.md
  - ROUTE_SMOOTHING_DISPLAY_ONLY_LIMITATIONS.md
  - ../../arctic_route_governance/standards/AGENT_DOCUMENTATION_RULES.md
---

# 受约束局部三次 B 样条航线平滑：R0.1 问题定义、展示实现与研究计划

> 当前结论入口：第 1～15 节保留 R0 的历史问题定义、计划和当时证据；R1 的实施、真实
> 环境 shadow、资源门禁和终态以第 16 节为准。R1 不回写或重解释 R0 历史运行结果。

## 0. 文档定位与更新规则（2026-08-31 03:26 +08:00）

**首要参考声明：** 本文档是“受约束局部三次 B 样条航线平滑”研究主题的详细支持文档，
负责记录航点处航向突变的定义、参数来源、候选算法、实验方案、证据和决策。工作包 C
核心算法的总体状态、正式路线合同、生产边界和跨主题优先级仍以
[`CORE_ALGORITHM_IMPROVEMENT_PLAN.md`](CORE_ALGORITHM_IMPROVEMENT_PLAN.md) 为唯一核心算法
SSOT。本文档不能取代 C 核心 SSOT、`cd.route-plan.v2`、四层 v3 或治理仓库中的当前规范。

**更新规则：**

- 先更新本文档，再实施与本文档一致的研究代码、测试或实验；实现后必须记录 commit、
  输入身份、实验 identity、结果摘要和验证成熟度。
- R0.1 的问题定义已经完成。D Viewer 目前实现展示-only 的局部曲线绘制，并让 Viewer
  仿真船位、航向、近期轨迹和 completed-track 绘制跟随该曲线；原始 waypoint ETA 只作为
  时间锚点。C 现在同时提供默认关闭、独立 experiment identity、可回退的 geometry-only
  research sidecar、R0.2 几何基线 runner，以及显式绑定 `RiskSampler`、船舶性能模型和
  caller-supplied corridor/control-envelope proof 的 synthetic qualification API。真实
  replay、真实操纵性资格和生产资格仍分别写成 `NOT RUN` 或 `NOT_QUALIFIED`，不能把
  geometry-only sidecar 或 synthetic pass 当作真实航行证据。
- 原始网格折线继续是 C 的正式 control；D 的展示曲线可以作为 Viewer 的默认绘制策略，
  但只能存在于非权威 paint layer，不得静默进入 ingress、service、合同、formal latest、
  replanning baseline 或 frozen artifact。
- 任何风险、硬掩膜、ETA、来源身份和船舶参数均须复用现有正式语义；缺失、越界、身份不匹配
  或无法证明的曲线必须 fail-closed 回退原始路线。
- 若未来改变 `cd.route-plan.v2`、四层 v3 或 D 的权威几何语义，必须另走跨包合同提案，
  并把批准结果回填本文档和核心 SSOT；不得在本研究文档中直接改变正式接口。
- 本文档不创建 `P0.2-M35`，也不重开已在 M34 收束的 P0.2 路径。R0 当前研究范围在
  synthetic qualification 与现有显示实现处收束；只有提出实质不同、可形式化且有真实
  可观测收益的新命题，并重新满足安全、非重复性、cgroup 和独立 experiment identity
  门禁后，才允许另行立项。
- R1 已在独立 v2 sidecar 中实施多跨度 G2 三次 B 样条、最终 ETA 后逐点合成运动学复核、
  caller-owned raster-resolution containment、真实 RiskFrame shadow 和同 digest Viewer
  运动。R1 最终结论是 `DISPLAY_ONLY_RETAINED; NO_PRODUCTION_CUTOVER`：语义、视觉和
  有限 cgroup 证据形成，但相对 raw-route 重算的附加 wall-time 门禁失败。第 16 节覆盖
  本文其他位置仍写为 `NOT RUN` 的 R0 历史口径。
- R2 仅按第 16.5 节允许的新性能命题完成分段归因、prepared raster 和 exact sample cache
  复核；语义 digest 一致，但冷/暖附加 wall-time 仍约为 raw baseline 的 `138×/70×`，终态
  `R2_PROFILE_ONLY_PERFORMANCE_GATE_FAIL_NO_PRODUCTION_CUTOVER`。详见第 16.6 节。

**治理和证据依据：** 本文档遵循
[`AGENT_DOCUMENTATION_RULES.md`](../../arctic_route_governance/standards/AGENT_DOCUMENTATION_RULES.md)
的元数据、时间戳、SSOT、ADR、`RUN`/`NOT RUN`/`INHERITED` 和验证成熟度规则。它是
`Document Role: SUPPORTING`、`Applicability: RESEARCH_ONLY`，不是生产路线规范。

## 1. 执行摘要（2026-08-31 03:26 +08:00）

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
   风险，也不自动满足最小转弯半径、速度、偏航率和 ETA 约束。本轮保留 D 的展示几何
   和 Viewer 仿真绘制，并补充 C 的研究-only qualification API；后者只在 synthetic 或
   显式 research replay 中工作，不能被解释为生产可执行路线。

### R0.1 关键增量表（2026-08-31 02:20 +08:00）

| 指标 / 声明 | Before | After | Delta | Verdict / 原因 |
|---|---|---|---|---|
| 当前路线几何 | 网格航点 `LineString` | 明确定义为离散折线基线 | 问题口径被固定 | `COMPLETED`；代码未改变 |
| 航点处航向突变 | 只有 `turn_count` 等间接计数 | 定义入射/出射航向差 `Δψ` 与转弯长度需求 | 新增可审计问题指标 | `COMPLETED_AS_DEFINITION`；尚无路线统计 |
| 船舶参考参数 | C 内部未校准演示值 | 公开 Nordic Odyssey 尺度/速度 + 明确的 2,000 m 工作假设 | 来源和假设分离 | `RESEARCH_ONLY`；不是实船校准 |
| B 样条实现 | D 展示-only 已存在 | C 增加自适应最大可行半径、RiskSampler/船模/走廊证据绑定的 research sidecar；D 增加显式 sidecar 研究运动开关，默认关闭 | 仍不改变权威路线 | `IMPLEMENTED / SYNTHETIC_PASS / NOT_QUALIFIED` |
| 真实航线平滑收益 | 未证明 | 未运行 | 无新增真实证据 | `NOT RUN` |
| 正式合同与生产路线 | `cd.route-plan.v2` / v3 冻结 | 保持不变 | 无语义变化 | `UNCHANGED` |

## 2. 范围 / 非范围（2026-08-31 03:26 +08:00）

**本轮范围：**

- 完成航点处航向突变的数学和工程定义；
- 核对当前 C 的路线生成、航点、路线几何和 Viewer 约束；
- 检索公开散货船和北方航线数据；
- 将现有船模值与公开资料分层记录；
- 确定候选算法和后续研究阶段；
- 设定安全、语义、质量、资源和停止门禁。

**本轮非范围：**

- 不修改正式 Python API、schema、合同、搜索上限或生产数据流；新增模块均位于 C
  `research` 命名空间，且不从正式顶层 API 导出；
- 不把研究曲线接入 C planner、ingress、service、正式发布或生产船舶运动。C 的
  qualification API 可以在显式 research replay 中调用现有 `RiskSampler` 和船舶性能模型，
  但不得写回 C 或 Orchestrator 的权威 artifact；只有完整资格 evidence 的 sidecar 才允许
  研究运动消费者读取，D Viewer 默认仍使用既有 display-only 曲线且研究开关默认关闭；
- 不运行 real replay、不运行长时间实验、不宣称真实操纵性或生产资格；本轮 synthetic
  qualification 仅证明代码门禁和 fail-closed 语义；
- 不把地图显示加密当作权威路线平滑；
- 不修改 `demo_bulk_carrier_v1` 或 `nordic_odyssey_reference_v1` 配置；
- 不将公开船舶资料当作当前船舶的实测操纵性；
- 不启动 P0.2-M35，不重开 M31–M34 的剪枝/资源研究；
- 不修改 formal latest、replanning baseline 或 frozen artifact；D 只增加非权威绘制层。

## 3. 起始基线与问题定义（2026-08-31 02:20 +08:00）

### 3.1 当前代码事实（2026-08-31 02:20 +08:00）

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

### 3.2 航向突变的形式化定义（2026-08-31 02:20 +08:00）

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

### 3.3 R0.1 的具体问题陈述（2026-08-31 02:20 +08:00）

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

## 6. 代码 / 架构设计边界（2026-08-31 03:32 +08:00）

当前同时保留两条严格隔离的旁路。D 是 Viewer 展示层的运行时 owner；C 的研究模块只在
显式 experiment/replay 中生成独立 sidecar。两者都不写回 C 或 Orchestrator 的正式路线
数据：

```text
formal C route (authoritative waypoint polyline)
        |  (raw waypoints / ETA / route metrics remain unchanged)
        +--> D display-only constrained local cubic B-spline renderer
                    |
                    +--> smoothed pixels for route lines
                    +--> Viewer-only vessel position/heading/trail/track paint motion
                    +--> raw polyline fallback on invalid geometry

        +--> C research-only constrained sidecar qualifier
                    |
                    +--> adaptive-radius geometry candidates
                    +--> RiskSampler / vessel model / corridor evidence
                    +--> risk / hard-mask / coverage / ETA / speed gates
                    +--> qualified sidecar only (otherwise FALLBACK)
                              |
                              +--> explicit Orchestrator research motion
                              +--> explicit D research-mode consumer
```

展示-only renderer 的约束是几何约束，不是航行安全证书：

- 局部米制坐标，不在经纬度度数上拟合；
- 保留原始路线首尾点，内部只替换足够长且转角超过阈值的局部折点；
- 使用 Cox–de Boor 评估的 clamped degree-3 cubic B-spline；单跨度四控制点与 Bézier 的
  多项式等价只用于数学实现，不改变其 B 样条定义；
- 对相邻转角避免局部曲线重叠，检查最大显示偏离、有限值和最小显示曲率半径；
- 任一检查失败时，绘制原始折线/线性加密结果；
- 不生成或回写 C 的 ETA、速度、风险、hard mask、航向或船位字段；D Viewer 仅在本地
  仿真显示层从曲线和原始 ETA 时间锚点派生绘制位置/航向，异常时回退 timeline。

当前 C research qualifier 已实现为独立、默认不调用的研究 API；它只有在 caller 明确提供
同一 RiskSampler、船舶性能模型以及完整 corridor/control-envelope proof 时，才会继续做
曲线风险、hard mask、coverage、速度和弧长 ETA 重评估。它输出的 `ACCEPTED` 仍是
`RESEARCH_ONLY`，不是生产资格；真实 replay 和实船操纵性资格本轮没有运行。该 sidecar
至少记录：

- 原始 `plan_id`、原始路线 semantic digest、输入/模型/规划配置 digest；
- vessel profile、工作半径、速度场景和约束版本；
- 局部坐标参考、degree、节点向量摘要和控制点摘要；
- 起终点和硬点保持结果；
- 最大曲率、最大偏航率、最大横向加速度和最大偏离；
- 风险/硬掩膜采样覆盖、来源身份和时间范围；
- 原始路线与平滑路线的距离、ETA、风险差异；
- `accepted` 或明确的 `fallback_reason`；资格失败时必须 `FALLBACK`、不输出可消费的
  研究运动样本。

R0.2 runner 仍是 `GEOMETRY_ONLY` 静态基线：它只提取既有 Viewer bundle 的身份并生成可
审计几何构件，明确把 RiskSampler、hard mask、coverage、资源和生产资格写为
`NOT_EVALUATED`/`false`，不得与 qualifier 的 synthetic pass 混为一谈。

这不是现有公共 schema 的设计批准。任何进入生产的曲线都必须保留原始权威 waypoint，并
另行提交合同、消费者、digest、重规划和回滚方案。

## 7. 语义 / 合约变更（2026-08-31 03:32 +08:00）

R0.1 没有业务语义和合同变更。未来研究必须维持以下不变量：

当前 C qualifier 的 synthetic pass 只证明研究 API 的门禁和 fail-closed 语义；它不新增
`cd.route-plan.v2` 字段、不改变正式 planner，也不把未校准的船模变成实船操纵性证书。

| 领域 | 当前语义 | 平滑研究约束 |
|---|---|---|
| B → C | RiskFrame、窗口、provenance 和 identity 由 B 提供 | 使用同一已提交 RiskFrame，不改风险公式 |
| C 搜索 | 原始网格折线为正式 control | 后处理不能篡改搜索结果或伪造 expansion/edge 收益 |
| 路线风险 | 按实际位置和 ETA 采样 | 曲线改变位置后必须重新采样，不能复用旧折线风险指标 |
| ETA / 速度 | waypoint ETA 严格递增，速度来自 C 船模/边评估 | 按弧长重新参数化，失败则回退 |
| C → D | waypoint 和指标为当前权威几何 | D 可从原始字段计算展示-only 曲线，并在 Viewer 仿真层派生位置/航向/轨迹绘制；不得替换权威 geometry、ETA、metrics 或生产船舶运动语义 |
| 重规划 | adopted route 受现有 generation/request/revision 围栏保护 | 每次新路线重新生成 sidecar，不允许瞬移 |
| 失败语义 | 未知、缺测、不匹配必须 fail-closed | 曲线无效时返回原始路线，并保留拒绝原因 |

## 8. 实验 / 备选方案与实施计划（2026-08-30 22:46 +08:00）

### 8.1 阶段计划（2026-08-31 03:32 +08:00）

| 阶段 | 目标与工作 | 交付物 | 进入条件 | 状态 |
|---|---|---|---|---|
| R0.1 | 固定航向突变问题、公开参考和工作假设 | 本文档、`Δψ`/`s_turn` 定义、约束边界 | 代码和当前文档只读核验完成 | `COMPLETED` |
| R0.2 | 统计当前 synthetic/冻结路线的转角、可用空间和基线粗糙度 | baseline report、候选点清单 | R0.1 完成；不改生产路径 | `COMPLETED / GEOMETRY_ONLY_BASELINE` |
| R0.3-D | 在 D Viewer 实现局部米制、clamped cubic B-spline 和端点保持 | `viewer/route_smoothing.js`、`app.js` 绘制接入 | R0.1 完成；不改变权威路线 | `COMPLETED / DISPLAY_ONLY` |
| R0.4-D | 建立显示几何的转角、重叠、偏离、曲率和 fail-closed 约束 | 展示-only validator、原始折线回退 | R0.3-D 输出可重复 | `COMPLETED / DISPLAY_ONLY` |
| R0.5-D | display-only synthetic/unit/source regression 矩阵 | D focused tests、Node syntax、C/D 语义回归 | R0.4-D 拒绝条件可观测 | `UNIT_PASS` |
| R0.3-C | 生成自适应最大可行半径的独立曲线与 ETA 参数化 sidecar | C research module、runner、sidecar digest、半径敏感性摘要 | R0.1 完成；validator 缺失时仅 geometry-only | `IMPLEMENTED / RESEARCH_ONLY` |
| R0.4-C | 为 sidecar 消费建立身份校验、研究运动插值和 fail-closed fallback | Orchestrator reader、D 显式研究开关 | sidecar schema 与原始 route digest 一致 | `IMPLEMENTED / IDENTITY_GATE_PASS / NOT_QUALIFIED` |
| R0.5-C | 绑定 RiskSampler、船模和 corridor/control-envelope proof，完成 synthetic 资格矩阵 | qualified research sidecar、风险/硬掩膜/coverage/ETA/速度证据、fail-closed tests | R0.3-C 输出可构造；证据缺失必须 fallback | `IMPLEMENTED / SYNTHETIC_PASS / NOT_QUALIFIED` |
| R0.6 | 固定输入真实 6h shadow 对比原始折线 | real route quality/resource summary | R0.5 全通过且存在 real 候选点 | `CANCELLED_FOR_CURRENT_SCOPE / NOT RUN` |
| R0.7 | 仅在 R0.6 有真实收益且资源方案完整时做 24h | cgroup-complete qualification evidence | 预注册门禁全部通过 | `CANCELLED_FOR_CURRENT_SCOPE / NOT RUN` |
| R0.8 | 决定展示-only、研究保留、合同提案或收束 | final decision record | 展示目标已明确；可执行资格仍需独立证据 | `COMPLETED / DISPLAY_ONLY_SCOPE / RESEARCH_CLOSED` |

### 8.2 R0.2 基线统计（2026-08-31 03:32 +08:00）

R0.2 不先改算法，而是对现有路线做统计：

- 每个内部航点的入射/出射 bearing、`Δψ` 和转角分箱；
- `s_turn` 与前后航段长度的可行性比较；
- 路线到 hard mask、风险高区和允许区域边界的距离；
- 直线段、连续锯齿、短边、重复点和端点附近转角；
- 原始路线的离散粗糙度指标：最大单位距离航向变化、转角数量和长度代价；
- 记录哪些“转角”只是网格表达，哪些确实有足够空间进行局部平滑。

R0.2 必须使用固定输入和可复现 digest；若没有真实路线中的有效候选转角，R0 应直接
停止，不为了展示 B 样条而制造 synthetic 收益。

**R0.2 当前静态运行结果（2026-08-31 03:32 +08:00）。** 使用现有 D Viewer bundle 中的
单条 Winter `routes[0]` 作为固定 waypoint 输入，仅做 geometry-only 分析，不读取或重算
RiskFrame，不运行 replay。该输入有 `22` 个 waypoint、`21` 条边、总几何长度约
`955.616 km`；按 `1°` 的数值容差和当前候选阈值识别出 `6` 个有效转角，最大转角约
`47.763°`，均满足 `turn_radius_m=2000 m` 的几何切入长度条件。输入坐标 digest 为
`502ca8c94cdeae76dfc6d4c98ad7d6b99faa8146f7f2558201f9cea8a90bb3af`，路线 semantic digest 为
`71f85e07616c127892c1feddad94314040dd7b9c808b3979fa48a77dfb6a277d`，输入 bundle 文件
SHA-256 为 `ec8653b65ff0c5de6bb498c80ce25570f310c5ea9e650986bf6443e67b6e10fd`；当前
baseline digest 为
`c16a376fe9b62ac29c77a1fcf21012bae0b319936340557011c4d5c8894f2080`。

在同一输入上运行 R0 research runner 后，6 个候选转角均可由自适应策略选择约
`41.412 km` 的最大几何可行半径；`1000/2000/4000 m` 三个最小半径场景选择相同的
最大半径。这只说明当前开阔、长边的几何表达允许较温和的视觉/研究曲线，不能把
`41.412 km` 解释为目标散货船的操纵半径。构件保存在
`/root/my_project/.runtime/experiments/c-r0-route-smoothing-baseline-20260831-r1/`，其中
`baseline.json` 与 `route-smoothing-sidecar.json` 均标记为 geometry-only research
evidence；baseline digest 为
`c16a376fe9b62ac29c77a1fcf21012bae0b319936340557011c4d5c8894f2080`，sidecar digest 为
`6629ca197f82ede1598739723695020d530b3bf0df4f0d23862fd2a25d25d944`。该构件记录 `6/6`
个几何候选和 `6` 个曲线段、`880` 个采样点，但风险、hard mask、coverage、资源和生产
资格仍分别为 `NOT_EVALUATED`/`false`；它不是 real replay 或 safety evidence。

### 8.3 R0.3-D–R0.5-C 展示几何和研究资格实现（2026-08-31 03:32 +08:00）

本轮已实现的展示-only 实现满足：

1. 不在原始经纬度度数上计算曲率；
2. 保持路线首尾点；内部仅替换有足够转弯空间且超过阈值的折点；
3. 使用入射/出射线段方向作为 clamped degree-3 B-spline 的端点切向；C 侧以
   Cox–de Boor 基函数计算，解析提供一阶、二阶导数；与 Bézier 的等价只是一段 clamped
   单跨度的数学表示，不把实现降格为全局插值；
4. 控制点和曲线采样受相邻转角不重叠、最大显示偏离和最小显示曲率半径约束；
5. 采样间距按局部曲线长度和固定显示上限确定；
6. 所有非有限、重复、短边或约束失败输入都回退到原始折线的线性显示；
7. D 展示返回值只含显示坐标和诊断状态，不含 ETA、速度、风险或安全资格。

展示-only 默认参数固定在 D 的 `route_smoothing.js`，只代表画面尺度，不代表目标船校准。
此前 `2,000 m` 在当前约 `40 km` 的网格边上只产生近似亚像素的转角偏离，截图仍近似
折线；因此本次把展示尺度调整为可见范围。`40,000 m` 不是船舶操纵半径、航行安全限值
或生产资格参数：

| 参数 | 默认值 | 含义 |
|---|---:|---|
| `nominalRadiusM` | `40,000 m` | 局部曲线的可见显示尺度；不代表船舶操纵半径 |
| `maxDeviationM` | `20,000 m` | 曲线采样点到原始折线的最大显示偏离，仅为显示约束 |
| `cornerAngleThresholdDeg` | `8°` | 小于该变化量的折点保持线性绘制 |
| `maxTrimFraction` | `0.48` | 单个局部曲线从相邻边切出的最大比例，防止跨越整条边 |
| `sampleSpacingM` | `750 m` | 曲线采样的显示间距上限 |
| `maximumDisplayPoints` | `10,000` | 防止异常输入造成无界绘制工作 |

**R0.3-C geometry-only sidecar（2026-08-31 03:32 +08:00）。** C 新增
`arctic_route_planning.research.route_smoothing` 与
`route_smoothing_runner`，输入 authoritative waypoint route，使用局部米制坐标建立每个
候选转角的单跨度 clamped degree-3 cubic B-spline，按确定性候选集选择最大可行半径，并
以原始 waypoint ETA 作为弧长时间锚点输出 `c.research-route-smoothing-sidecar.v1`。
sidecar 保留原始 route digest、控制段摘要、采样点、ETA、拒绝原因和
`1000/2000/4000 m` 最小半径敏感性摘要；没有 validator 时明确为 `GEOMETRY_ONLY`，且
`risk_rechecked=false`、`hard_mask_rechecked=false`、`coverage_complete=false`、
`resource_evidence_complete=false`、`production_qualified=false`。当前 static R0.2 输入
生成的 sidecar digest 为
`6629ca197f82ede1598739723695020d530b3bf0df4f0d23862fd2a25d25d944`。

**R0.5-C synthetic qualification（2026-08-31 03:32 +08:00）。** C 新增
`route_smoothing_qualification`，只作为 research-only qualifier：它绑定现有
`RiskSampler`、现有 `VesselPerformanceModel` 和 caller-supplied corridor/control-envelope
proof，逐候选检查 hard mask、coverage、风险、速度和几何门禁，再对选定整条曲线按弧长
重建 ETA 并与原始路线重新比较风险。没有完整 corridor/control-envelope 证据、身份不匹配、
风险增加、ETA/速度失败或采样不完整时均 `FALLBACK`；成功 sidecar 也明确
`research_eligible=true`、`production_qualified=false`、`calibration_status=NOT_CALIBRATED`
和 `manoeuvring_qualification=NOT_MANOEUVRING_QUALIFIED`。本轮 synthetic 资格矩阵已
覆盖端点/导数、风险、hard mask、coverage、速度/ETA、身份和 fail-closed 反例；不代表
真实 replay、实船操纵性或生产资格。

R0.4-C 新增 Orchestrator 研究运动读取器和 D Viewer 的独立研究开关。只有显式传入并
通过 route digest/waypoint/ETA、资格标记、验证门和 canonical digest 校验的 `ACCEPTED`
sidecar 才会成为研究回放的时间样本；
缺失、过期、不匹配或 fallback sidecar 一律回到既有 timeline motion。默认 Viewer 不启用
该开关，当前蓝色曲线展示和原始折线隐藏行为保持不变。

2026-08-31 的 Viewer 运动呈现补充如下：

- 蓝色平滑曲线是主路线绘制层，白色原始折线由独立图层控制且默认隐藏；打开该图层只
  用于几何对照；
- Viewer 仿真船位、航向、近期轨迹和 completed-track 的绘制使用同一条已接受的曲线，
  以原始 waypoint ETA 作为时间锚点；不重新计算或写回权威 ETA；
- 如果 route identity、ETA 顺序、几何约束无效，或曲线位置与 timeline 位置超过保护
  阈值，运动层 fail-closed 回退 timeline；这不是生产船舶控制或实际航行指令；
- 该行为只存在于 D Viewer 的本地 simulation/presentation layer，C 路线 artifact、
  metrics、replan adoption 和 C→D 合同不变。

以下仍不能从本轮结果推出：

- 真实路线的 corridor/hard mask 连续包含证明；本轮只以 synthetic caller proof 验证
  qualifier 的门禁；
- real 6h/24h 的曲线风险、coverage、ETA、资源和可观测平滑收益；
- 真实船舶操纵性约束、有限 cgroup 资源证据和 production qualification；
- 任何把研究 sidecar 写入正式 RoutePlan、生产运动或服务数据流的语义。

旧版独立入口 `work_package_d/web/demo_viewer.html` 不能依赖外部脚本，因此也使用内联的
局部 cubic path 版本；它同样只改变 SVG paint geometry，并为非法、短边和相邻转角保留
回退/跳过行为。Replay Viewer 的标准入口仍使用 `viewer/route_smoothing.js` 的局部米制
实现。两者都不把曲线写回 route artifact。

### 8.4 R0.5-D–R0.7 验证与收束结果（2026-08-31 03:32 +08:00）

display-only synthetic 至少包括：单个 45°/90° 转角、连续锯齿、重复点、短边、端点保持、
相邻转角重叠、超出偏离限界和非法坐标回退。它只验证画面几何，不验证海图安全。

可执行 research sidecar 的 synthetic adversarial 集合已在 C focused tests 中覆盖狭窄/不完整
corridor proof、hard-mask、风险增加、coverage 缺失、时间/速度失败、身份不匹配、曲率和
fail-closed fallback；这些测试验证的是 qualifier 的代码门禁，不是当前真实海域的连续
安全证明。

R0.5-C synthetic qualification focused set 为 `20 passed`，另有 Orchestrator motion/export
focused set `20 passed`、D route-smoothing focused set `7 passed`。它们证明 Cox–de Boor
曲线、解析导数、候选门禁、身份/digest 校验和研究运动 fallback 的实现语义；历史 M31–M34
结果不计入本轮数字。

真实 6h 原本应使用与原始路线相同的输入、时间窗口、船模、RiskFrame、目标和搜索上限做
shadow，并记录平滑成功率、回退率、曲率、航向变化率、最大偏离、硬掩膜距离、风险和 ETA
差异、耗时、RSS、digest 和确定性。本轮按停止规则不执行：没有可用于资格化的真实操纵性
数据，且当前 R0.2 产物明确是 geometry-only；因此 `R0.6 = CANCELLED_FOR_CURRENT_SCOPE /
NOT RUN`，不把 synthetic 或 Viewer 画面收益升级为真实收益。

真实 24h 依赖通过 R0.6 的真实收益和有限 cgroup 证据。本轮 `R0.7 =
CANCELLED_FOR_CURRENT_SCOPE / NOT RUN`；没有补跑 cgroup 资源复核，也没有扩大搜索上限或
重复任何 M31–M34 已覆盖路径。

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
| 先做现有路线几何审计，不先写代码 | 第 8.2 节 | `R0.2 COMPLETED / GEOMETRY_ONLY_BASELINE` |
| 使用局部米制坐标和局部受约束 cubic B-spline | 第 5.1、5.2、8.3 节 | D 展示-only、C geometry-only sidecar 和 C synthetic qualification API 已实现；真实安全资格未形成 |
| 保留端点、端点方向和必要硬点 | 第 5.2、6、11.1 节 | 设计约束已确定 |
| 同时约束 corridor、hard mask、risk、curvature、ETA 和速度 | 第 6、7、8.3、11.1 节 | synthetic qualifier 门禁已实现并通过；真实连续安全与操纵性资格未形成 |
| 采用自适应采样并重新计算曲线风险和时间 | 第 5.2、7、8.3 节 | C qualifier synthetic 已重采样风险并按弧长重建 ETA；R0.2/真实输入未运行 |
| 所有失败 fail-closed，回退原始折线 | 第 5.2、7、11.1 节 | C qualifier、Orchestrator 和 D 研究消费者的 synthetic/focused fallback 已通过 |
| synthetic adversarial 矩阵 | 第 8.4 节 | C `20 passed`，包含资格、风险/硬掩膜/coverage/ETA/身份失败关闭；不构成 real evidence |
| 固定输入 real 6h shadow，24h 条件性启动 | 第 8.4 节、9 节 | `R0.6/R0.7 CANCELLED_FOR_CURRENT_SCOPE / NOT RUN`；不因零收益或缺少 cgroup 证据重复运行 |
| 若仅有视觉收益则不改 C 权威路线 | 第 2、7、15.2 节 | 已确定 |
| 若要生产化则另走合同和消费者提案 | 第 0、6、7、15.1 节 | 已确定，未启动 |

上一轮方案的停止规则也一并固定：没有真实候选转角、没有可验证船舶操纵性约束、没有
非零真实质量收益、风险/硬掩膜/ETA 不能保持，或新增资源成本超过预注册预算时，均不得
通过增加采样点、改变显示方式、放宽约束或重复 synthetic 来制造成功结论。

### 8.6 当前范围收束与执行结论（2026-08-31 03:32 +08:00）

本轮已完成计划中不依赖长时真实回放的实现和 synthetic 证明：D 的视觉曲线继续保留，C
提供可独立调用的受约束 B 样条 geometry/qualification sidecar，Orchestrator 与 D 只在
显式且已资格化的 research sidecar 下使用研究运动。R0.2 当前构件仍是
`GEOMETRY_ONLY`，R0.5-C 的 `20 passed` 只证明 synthetic 门禁、身份绑定和 fail-closed
语义；真实路线的平滑收益、连续安全和实船操纵性仍没有证据。

因此当前结论固定为：

`DISPLAY_ONLY_RETAINED; EXECUTABLE_SMOOTHING_NOT_QUALIFIED; NO_PRODUCTION_CUTOVER`

- R0.6/R0.7 不在当前范围内启动；不因缺少 cgroup 证据单独重跑一条尚未显示真实收益的路径，
  也不重复 M31–M34 已覆盖的 envelope/bound 研究；
- 不新增 `P0.2-M35`，不改变正式 planner、RoutePlan、合同、默认配置、candidate、ingress/
  service、formal latest、replanning baseline 或 frozen artifact；
- 只有实质不同、可形式化的新 pruning/resource 命题，或独立且可观测的真实曲线安全命题，
  才能重新立项。重启申请必须同时给出安全性证明、相对本研究及 M31–M34 的非重复性、预期
  真实收益、有限 cgroup 方案、真实操纵性数据边界和独立 experiment identity。

## 9. 权威运行 / 真实验证（2026-08-31 03:32 +08:00）

本轮完成了 C/D/Orchestrator 的短时 synthetic、静态和身份门禁验证，但不运行 replay 或
长时间实验。以下仍为 `NOT RUN`：

- 真实路线上的 RiskFrame/hard mask/coverage 约束曲线资格化；
- C 全量单元测试或完整 `make check`；
- real 6h/24h replay、真实曲线风险/ETA/资源对比；
- cgroup 资源资格复核；
- 真实船舶操纵性和生产 ingress/service。

M31–M34 的历史实验结果只能作为当前 C 研究边界的 `INHERITED` 背景，不能作为 B 样条
平滑的验证证据；本轮 display-only 测试也不能作为船舶安全或生产资格证据。

此前展示阶段的 D `25 passed`、Orchestrator `13 passed` 和显示尺度修正的 D `11 passed`
均为 `INHERITED`；D 的历史全量测试数、此前 Firefox 证据和 Orchestrator 历史测试数也
继续标记为 `INHERITED`，不得冒充本轮运行结果。本轮新增测试数量只记录在本节，不改写
历史计数。

2026-08-31 当前实现的 `RUN` 证据为：C route-smoothing focused tests `20 passed`，覆盖
geometry、Cox–de Boor 解析导数、synthetic RiskSampler/船模/走廊资格和 fail-closed 反例；
C R0.2 baseline 与 sidecar runner 对当前固定 bundle 成功生成结果；Orchestrator sidecar
motion/export focused tests `20 passed`；D route smoothing focused tests `7 passed`，
`research_route_motion.js` 与 `app.js` Node syntax 通过，离线 VM 检查覆盖 sidecar identity
和 ETA。R0.2 baseline 识别 `22` 个权威 waypoint、`6` 个有效转角；geometry-only sidecar
为 `6` 个曲线段输出 `880` 个时间样本。上述证据只证明研究几何、synthetic 资格门禁、
身份绑定和 Viewer fallback 行为，不证明真实路线风险/硬约束、真实船舶操纵性、cgroup 资源
或生产资格。
本次浏览器截图尝试被运行环境缺少 `libnspr4.so` 阻断，未形成浏览器通过证据；同时未进行
真实 replay、长时间实验或船舶可执行性验证。

## 10. 性能分解（2026-08-31 03:32 +08:00）

当前没有 Before/After 运行数据：

| 指标 | Before | After | Delta | Verdict / 原因 |
|---|---|---|---|---|
| A* 扩展/队列/label | 当前正式 control | 未改变 | `UNKNOWN` | R0 不优化搜索，不得伪造收益 |
| 平滑额外耗时 | `UNKNOWN` | `NOT RUN` | `UNKNOWN` | 必须在 shadow 中单独测量 |
| 平滑额外 RSS | `UNKNOWN` | `NOT RUN` | `UNKNOWN` | 继承 C 对资源证据的严格边界 |
| edge/risk 评估次数 | 原折线评估 | 可能增加曲线采样 | `UNKNOWN` | 不能把额外采样隐藏在旧指标中 |
| 航线长度/ETA/风险 | 原始路线指标 | 未计算 | `UNKNOWN` | 曲线改变空间后必须重新计算 |

本轮 geometry-only sidecar 的 `880` 个曲线样本只用于证明输出可复现和 ETA 锚点可构造，
不作为性能收益；它没有调用 `RiskSampler`，因此不能填充 edge/risk evaluation、RSS、
风险差异或资源资格字段。

本轮展示 track 的首要收益目标是减少航点处的几何锐角观感，不是减少 A* expansion，也不是
证明可执行性。若未来进入可执行 track，必须另行测量真实质量、风险、ETA、资源和船舶约束；
不能把本轮画面改善当作这些证据。

## 11. 正确性 / 验证门禁（2026-08-31 03:32 +08:00）

### 11.1 强制门禁（2026-08-31 03:32 +08:00）

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

### 11.2 质量和资源目标（2026-08-31 03:32 +08:00）

以下是 R0.6/R0.7 原本需要满足的预注册研究门槛；本轮没有真实 replay，因此它们不是当前
事实或资格结论：

- 至少 `80%` 的“有足够安全走廊且满足船舶约束”的 eligible corners 成功平滑；
- 对存在候选转角的路线，预注册粗糙度指标下降至少 `20%`；
- 平滑路线的最大偏离不超过路线安全走廊允许值；
- 最大风险和积分风险不出现未经批准的恶化；
- 平滑额外耗时和 RSS 不超过预注册预算；
- 只要无法获得真实船舶操纵性参数，就不得给出生产资格结论。

如果 R0.2 显示真实路线没有可平滑转角，或者 R0.6 的收益低于测量噪声，则不启动 R0.7。

### 11.3 本轮展示-only 验收门禁（2026-08-31 03:32 +08:00）

| 门禁 | 本轮要求 | 结果口径 |
|---|---|---|
| 路线 authority | 原始 waypoint、候选 geometry、ETA、metrics 和 route revision 不被改写 | 代码结构检查；通过后仍只称 `DISPLAY_ONLY` |
| 几何坐标 | 使用局部米制坐标，首尾点保持，局部曲线不跨相邻转角 | Node synthetic checks；通过 |
| 视觉约束 | 有限值、最小显示曲率半径、最大显示偏离和显示点数受限 | Node synthetic checks；通过 |
| 失败语义 | 非法坐标、重复点、短边、无候选角或曲线约束失败回退原始折线加密 | Node synthetic checks；通过 |
| 仿真呈现语义 | Viewer vessel position、heading、近期轨迹和 completed-track 绘制沿平滑曲线；原始 ETA 仍作时间锚点，异常时回退 timeline；权威 artifact 语义不变 | Viewer source/离线 VM motion checks；通过；仍为 `DISPLAY_ONLY` |
| 安全/资格 | 不得由显示曲线推出 hard mask、风险、ETA、操纵性或生产资格 | 文档与 metadata 明确；未资格化 |

### 11.4 C synthetic qualification 验收门禁（2026-08-31 03:32 +08:00）

| 门禁 | synthetic 结果 | 真实/生产口径 |
|---|---|---|
| B 样条几何、端点和解析导数 | `PASS` | 仅证明有限测试曲线；不证明真实连续路线 |
| corridor/control-envelope、hard mask、coverage | `PASS`（显式完整 fixture）；缺失或不完整即 `FALLBACK` | 当前真实路线未复核 |
| RiskSampler 风险重采样 | `PASS`，候选最大/积分风险不恶化 | 不构成真实风险资格 |
| 船模速度和弧长 ETA | `PASS`，严格递增并收敛 | 当前 vessel profile 仍 `NOT_CALIBRATED` |
| 身份、digest 和 deterministic/fail-closed | `PASS` | 不构成 production qualification |
| resource / manoeuvring / production | `NOT_EVALUATED` 或 `NOT_QUALIFIED` | 不允许研究 sidecar 进入正式生产运动 |

## 12. 确定性 / 可复现性（2026-08-31 03:32 +08:00）

当前状态：

- R0.1 文档定义：`RUN`；
- D Viewer B 样条展示实现：`IMPLEMENTED / DISPLAY_ONLY`；
- D display-only synthetic/结构验证：`UNIT_PASS`（本轮）；
- C geometry-only sidecar、R0.2 baseline runner：`IMPLEMENTED / UNIT_PASS`；
- C RiskSampler/船模/corridor qualification API：`IMPLEMENTED / SYNTHETIC_PASS / RESEARCH_ONLY`；
- C 真实 RiskFrame/hard mask/船舶安全 B 样条与 real 平滑：`NOT RUN / NOT_QUALIFIED`；
- Orchestrator sidecar reader、D 显式 research motion toggle：`IMPLEMENTED / NOT_QUALIFIED`；
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

## 13. 构件 / 溯源（2026-08-31 03:48 +08:00）

本轮没有生成新的 real replay/runtime 实验构件；新增 Viewer 源文件、C/Orchestrator research
sidecar 代码和 focused 测试属于代码构件。R0.2 静态几何产物留在 workspace 根目录
`.runtime/experiments/`，公开资料和当前配置的溯源如下：

| 构件/来源 | 类型 | 状态 | 说明 |
|---|---|---|---|
| `configs/vessel_models/demo_bulk_carrier_v1.toml` | 当前 C 配置 | `INHERITED` | `13.5 kn`、`R=2000 m`，明确未校准 |
| `configs/vessel_models/nordic_odyssey_reference_v1.toml` | 当前 C 配置 | `INHERITED` | `10 kn`、`15.7 kn`、`R=2000 m`，明确未校准 |
| Port of Hamburg Nordic Odyssey 页面 | 公开资料 | `REFERENCE_ONLY` | 尺度、DWT、标称速度 |
| POAC 2013 NSR 研究 | 公开研究 | `REFERENCE_ONLY` | NSR 航速和 Nordic Odyssey 货运背景 |
| 55,000 DWT 操纵性研究 | 公开研究 | `REFERENCE_ONLY` | 特定试验条件下的转弯数量级 |
| Capesize 转弯半径研究 | 公开研究 | `REFERENCE_ONLY` | 舵角相关半径数量级 |
| IMO MSC.137(76) | 官方标准 | `REFERENCE_ONLY` | 操纵性试验指标，不是路线半径参数 |
| `work_package_d/viewer/route_smoothing.js` | 展示-only 代码 | `IMPLEMENTED / DISPLAY_ONLY` | 把原始路线转换为 Canvas paint coordinates；Viewer 运动层复用该曲线，当前使用可见显示尺度 |
| `work_package_c/src/arctic_route_planning/research/route_smoothing.py` | C research geometry | `IMPLEMENTED / GEOMETRY_ONLY` | Cox–de Boor 局部 degree-3 曲线、自适应最大半径、ETA 锚点 sidecar；geometry-only 不含安全资格 |
| `work_package_c/src/arctic_route_planning/research/route_smoothing_baseline.py` | R0.2 baseline runner | `IMPLEMENTED / GEOMETRY_ONLY` | 输出转角、边长、候选空间和 roughness 基线 |
| `work_package_c/src/arctic_route_planning/research/route_smoothing_qualification.py` | C research qualifier | `IMPLEMENTED / SYNTHETIC_PASS / NOT_QUALIFIED` | 绑定 RiskSampler、船模和 caller corridor proof；真实 replay 未运行 |
| `work_package_c/src/arctic_route_planning/research/route_smoothing_experiment.py` | R0.2 experiment runner | `IMPLEMENTED / GEOMETRY_ONLY` | 只消费既有 bundle，显式输出风险/hard mask/coverage 为 `NOT_EVALUATED` |
| `/root/my_project/.runtime/experiments/c-r0-route-smoothing-baseline-20260831-r1/` | 静态 research 构件 | `RUN / GEOMETRY_ONLY` | `manifest.json`、`cases.jsonl`、`summary.json`、`ALL_DONE` 和 sidecar；非 real safety/replay evidence |
| `work_package_d/viewer/research_route_motion.js` | D research reader | `IMPLEMENTED / RESEARCH_ONLY` | 校验 sidecar 身份和 ETA，失败返回 timeline fallback |
| `work_package_d/web/demo_viewer.html` | 旧版独立展示入口 | `IMPLEMENTED / DISPLAY_ONLY` | 内联 cubic SVG path；不改原始 waypoints 或指标 |
| `work_package_d/tests/unit/test_route_smoothing.py` | focused 测试 | `UNIT_PASS` | Node synthetic、加载顺序和 authority 分离检查 |

此前展示实现提交：D `efd2ec6ccf38f881ebc39afae195cb9bfdaa36a6`，Orchestrator
`d22980e816557a16e062fade3e06826aae845e66`；可见尺度与旧版入口修正提交为 D
`66bd4fb4eb3b9333731d6d9e03d970df9d2c00c8`。本次曲线主图层、原始折线开关和 Viewer
仿真曲线运动提交为 D `b9375982ba1b1807ffb7329a68b3829199d144fd`，Orchestrator 说明
同步提交为 `72d81081695ecebd8b68614e081ef6c05905ed87`。这些提交均为本地提交，未 push。

本轮研究旁路实现提交为 C `5a9af953ab7d6258ce0d4c9547c77d832e91a0af`，SSOT 收束提交为 C
`972e0fe1ab094a99be168015f441a615c63b16e6`；Orchestrator
`356b9dc9b301029ad7304f98e6b3893186c918c7`、D
`0d122c359440e1522a2a3724df84b1c7548bfc5f`。这些提交均未 push。

拟议未来实验 identity 前缀为 `c.route-smoothing.bspline.v1-<digest>`，目前只是命名
建议，不代表已存在构件。

## 14. 已知限制 / 技术债（2026-08-31 03:32 +08:00）

| ID | 影响 / 严重度 | 当前限制 | 下一步 |
|---|---|---|---|
| `RS-TD-01` | 高 | 没有目标船的实测操纵性、偏航率和速度-曲率数据 | R0.1 使用透明假设；资格前必须补齐或明确不能资格化 |
| `RS-TD-02` | 高 | 当前 `turn_radius_m` 是未校准 C 假设 | 不改配置；只作为 research working value |
| `RS-TD-03` | 高 | 当前正式 RoutePlan 没有曲线/曲率字段；现有 sidecar 仅为 geometry-only | 继续 sidecar；生产化另走合同提案 |
| `RS-TD-04` | 高 | 风险采样和硬掩膜是时间/空间约束，样条会改变采样位置 | C qualifier 已在 synthetic 使用同一 RiskSampler 重新评估；真实路线未运行 |
| `RS-TD-05` | 中 | 当前 R0.2 仅对固定 bundle 做静态 geometry-only 统计，尚非真实操纵性证据 | R0.6 按停止规则关闭，不把 synthetic pass 或画面收益升级为真实资格 |
| `RS-TD-06` | 中 | 采样验证不等于连续安全证明 | 建立保守走廊；否则只标记诊断 |
| `RS-TD-07` | 中 | D 显示加密和 C 权威几何容易混淆 | 继续明确非权威显示与可执行路线的区别 |
| `RS-TD-08` | 高 | 当前没有目标散货船的实测操纵性数据，且本轮没有完整有限 cgroup 资源证据 | 保持 `NOT_CALIBRATED`/`NOT_MANOEUVRING_QUALIFIED`，不启动 R0.6/R0.7 |

## 15. 决策 / 下一阶段（2026-08-31 03:32 +08:00）

### 15.1 ADR：选择受约束局部三次 B 样条作为首选候选（2026-08-31 03:32 +08:00）

**Decision：** 允许继续设计“局部、受约束、默认关闭、可 fail-closed 回退”的三次 B 样条
后处理；不允许直接使用无约束全局插值，也不改变当前正式路线合同。

**Context：** 当前 C 的网格 waypoint 在 `Δψ ≠ 0` 的内部点形成几何折点；导师提出的三次
B 样条具有局部控制和连续导数的潜力。C 现在有研究-only 的曲线安全/时间门禁 API 和
synthetic 证据，但仍缺少真实路线的连续安全与目标船操纵性资格。

**Alternatives：** Viewer 仅显示加密、无约束全局样条、圆弧/回旋线、曲率感知规划器。

**Reason：** 局部 B 样条可以先在 C 内部验证真实问题，改动范围小于重写搜索状态；同时
必须通过硬掩膜、风险、ETA 和船舶约束检查。显示加密不解决可执行性，无约束样条安全性
不足；圆弧/回旋线和曲率感知规划器保留为更强但更大范围的后备。

**Consequences：** 研究会增加路线后处理和风险重采样成本；曲线不能自动继承原始路线指标；
任何失败必须回退；如果最终需要生产发布，将产生新的合同和 D 消费影响评审。

### 15.2 当前决策（2026-08-31 03:32 +08:00）

- R0.1：`COMPLETED`，完成问题定义、参数分层和候选路线选择；
- R0.3-D/R0.4-D：已实现为 Replay Viewer 与旧版 standalone viewer 的展示-only 局部曲线；
  Replay Viewer 仿真船位、航向、近期轨迹和 completed-track 绘制沿曲线，原始 waypoint
  ETA 作为时间锚点；原始 waypoint、ETA、metrics、active route 和 adoption 事件仍是
  唯一权威语义；
- R0.5-D：`UNIT_PASS`，D focused pytest、Node syntax、加载/authority 结构检查通过；
- R0.2：`COMPLETED / GEOMETRY_ONLY_BASELINE`，固定 Viewer bundle 有 6 个有效转角，
  geometry-only sidecar 的自适应结果约为 41.412 km；这不是船舶半径资格；
- R0.3-C：`IMPLEMENTED / RESEARCH_ONLY`，sidecar 输出 Cox–de Boor 局部曲线、自适应半径、
  曲线样本、原始 route digest、ETA 锚点和 1000/2000/4000 m 敏感性摘要；geometry-only
  runner 不调用 RiskSampler；
- R0.4-C：`IMPLEMENTED / IDENTITY_GATE_PASS / NOT_QUALIFIED`，Orchestrator 和 D 仅在显式
  sidecar 开关/参数且资格门、身份和 digest 校验通过时读取，失败时回退既有 timeline；
- R0.5-C：`IMPLEMENTED / SYNTHETIC_PASS / NOT_QUALIFIED`，C qualifier 在 synthetic 中
  绑定 RiskSampler、船模和完整 corridor/control-envelope proof，重评估风险、hard mask、
  coverage、速度和弧长 ETA；真实路线和操纵性仍未资格化；
- R0.6/R0.7 的真实 6h/24h、有限 cgroup、真实风险/操纵性资格：
  `CANCELLED_FOR_CURRENT_SCOPE / NOT RUN`；
- P0.2：继续 `COMPLETED_TO_M34`，不创建 M35；
- 正式 planner、默认配置、candidate、合同和生产数据流：保持不变；
- 当前展示目标已完成；不以展示效果或 synthetic pass 推导真实可执行性或生产资格。研究
  范围按停止规则收束；只有未来提出实质不同、可形式化且有真实可观测收益的新命题，才
  另立可执行研究计划。

R0.8 当前选择固定为：

`DISPLAY_ONLY_RETAINED; EXECUTABLE_SMOOTHING_NOT_QUALIFIED; NO_PRODUCTION_CUTOVER`

未来若重新启动，只能在满足独立安全、非重复性、真实收益、有限 cgroup、操纵性边界和
experiment identity 门禁后另立计划；不能仅因资源证据缺失而重跑零收益路径。

历史上 R0.8 的候选决策含义如下：

1. 仅显示平滑：留在非权威显示层（本轮已选择）；
2. 研究型可执行改善：另立合同和生产资格计划；
3. 没有真实价值：关闭 R0，保留本文档作为审计记录。

## 16. R1：合成船模约束的多跨度三次 B 样条（2026-08-31 12:50 +08:00）

### 16.1 目标、范围和与 R0 的关系（2026-08-31 12:50 +08:00）

R1 将上一轮提出的完整方案收敛为一个独立、默认关闭的 research-only shadow：保留现有
D `route_smoothing.js` 的 display-only 呈现和默认行为，同时新增真正绑定曲率、速度、
RiskFrame、raster、ETA 和资源证据的 v2 曲线。R1 不改变正式 waypoint 折线、planner、
candidate、搜索上限、B/C 或 C/D 合同、ingress/service、formal latest、replanning
baseline 或 frozen artifact。

R1 的预注册目标如下：

1. 用 clamped degree-3、4-span、7-control-point B-spline 替换独立局部锐角；内部节点
   `0.25/0.5/0.75` multiplicity 为 1，内部保持 C2；前三/后三控制点分别沿入射/出射方向
   等距共线，使直线拼接端点曲率为零。
2. 不把“内部 C2”误称为整条路线 G2。每个局部窗口必须同时验证端点位置、切向、零曲率
   和内部 C2；只要仍有 raw corner，`full_route_g2_claimed=false`。
3. 保留 `2 km` 为未校准基础下限，候选从 65 个确定性降序半径中选择最大可行值；不把
   `20/40 km` 写成固定显示半径。
4. 运动学使用 conservative 准入门禁，并在最终 ETA 收敛后逐点复核，而不是使用“全局
   最大速度 × 全局最大曲率”：

   \[
   R_{\min}(v)=\max\left(2000,\frac{v}{\omega_{\max}},
   \frac{v^2}{a_{y,\max}}\right)
   \]

5. caller 以每个 B-spline span 的控制点凸包做 `500 m` 主门禁，并记录 `1/2 km`
   sensitivity；陆地、unknown 或 coverage 缺失均拒绝。该证据只能称
   `RASTER_RESOLUTION_CONTAINMENT_PASS`，不能称连续海域或真实可航证明。
6. 使用同一 RiskSampler 和船速模型重算 raw/curve；最大风险与积分风险不得超过 raw 加
   容差；ETA 必须严格递增，curve 与同模型 raw 重算的终点差不得超过
   `max(10 min, 2% raw duration)`。发布 ETA 与 raw 重算的既有偏差单独诊断，不能错误
   归因于平滑。
7. 当前 `1024×1024`、`mapZoom=1` 下，accepted-corner 法向偏离中位数不少于 `3 CSS px`，
   最大值不少于 `5 CSS px`；采样后的最大屏幕弦误差不大于 `0.5 CSS px`，单弦弧长亏损
   不大于 `25 m`。
8. 真实 6h case 必须以各候选角 ETA 为中心，至少 `5/6` 通过；随后才允许约 `53.4h`
   全程 3 次重复。资源限定为 `memory.max=2 GiB`、`memory.swap.max=0`、`pids.max=256`、
   timeout `90 min`、附加 RSS 不超过 `128 MiB`、附加 wall time 不超过 raw baseline 的
   `10%`。

最大允许结论只有：

- `SYNTHETIC_VESSEL_AND_REAL_ENVIRONMENT_SHADOW_PASS`；
- `SHADOW_PASS_VISUAL_GAIN_INSUFFICIENT`；
- `DISPLAY_ONLY_RETAINED`；
- `FALLBACK_RAW_ROUTE`。

四种结论都必须附加 `NO_PRODUCTION_CUTOVER`，且
`production_qualified=false`。

### 16.2 实现结构和身份隔离（2026-08-31 12:50 +08:00）

R1 新增 `c.research-route-smoothing-sidecar.v2`，不修改 v1 schema、digest 或历史构件：

| Owner | R1 实现 | 权威边界 |
|---|---|---|
| C | `route_smoothing_multispan.py`：固定 knot 的 4-span/7-control-point 数学切片、解析一/二阶导数、曲率和 G2 evidence | 只在 `research` 命名空间；不进入正式 C API/RoutePlan |
| C | `route_smoothing_manoeuvring.py`：conservative/nominal/permissive 合成运动学 envelope | 全部标记 `SYNTHETIC_UNCALIBRATED`；不是实船校准 |
| C | `route_smoothing_v2.py`：路线级 65 半径候选、相邻窗口 fail-closed、逐点曲率装配 | v1 不变；整条路线不越界声明 G2 |
| C | `route_smoothing_qualification_v2.py`：RiskSampler、船速、ETA、风险、hard mask、coverage、raster 和最终逐点运动学门禁 | 任一失败原子回退；`production_qualified=false` |
| Orchestrator | `raster_corridor_evidence.py`、v1/v2 dispatcher、replay exporter 和 R1 runner | caller-owned raster 证明；不导入 A/B 正式实现，不改变生产 replay |
| D | `research_route_motion.js` 的严格 v2 reader、同 geometry/motion digest、course/speed 消费 | 默认 research 开关关闭；非法/过期/摘要不一致回退 timeline |

中央控制点最初仍位于 raw vertex，虽然局部 G2，但真实 viewport 法向偏离只有约 `1.10 px`。
R1 没有降低视觉门禁，而是利用不受端点 G2 约束的唯一中央控制点，将其移到两切点弦中点；
前三/后三控制点的共线关系和端点零曲率保持不变，真实偏离提升到约 `6.02 px`。这一区分
“数学连续”和“肉眼可见的真实 corner cut”，避免再次以纯绘图线宽制造平滑效果。

v2 的 `motion_samples` 同时携带 `lon/lat/eta/course_degrees/speed_knots`。C 计算
`same_geometry_motion_digest`；Orchestrator 重新计算并校验，D 也使用 Python-compatible
canonical JSON 重新计算 sidecar 和 geometry/motion digest。蓝色路线、船位、航向、轨迹
和 completed-track 因而绑定同一 v2 identity；任何不一致不得混用局部几何。

### 16.3 输入、实验 identity 和证据等级（2026-08-31 12:50 +08:00）

正式实验目录：

`/root/my_project/.runtime/experiments/c-r1-route-smoothing-synthetic-vessel-real-env-20260831-r1/`

主要输入：

| 输入 | 当前证据 |
|---|---|
| Winter Viewer authoritative route | `22` waypoints、约 `53.4056 h`、`6` 个候选角；`INHERITED` 输入 |
| RiskWindow | commit `risk-window-sha256-b5bed6bb48893e32620710e8c765dc60ec37a2fc384f0c49014b92f0a1c056b2`，`145` frames |
| A land/sea raster | GEBCO-derived `0.05°` raster；`1=sea`、`0=land_or_coast`；metadata 明确 `navigation_semantics=none`、`hard_mask_semantics=none` |
| vessel scale | `225 m × 32.31 m` Nordic Odyssey 数量级；10 kn economic speed、15.7 kn upper reference |
| manoeuvring | conservative `0.15°/s`、`0.02 m/s²` 为准入；nominal/permissive 只作敏感性 |

因此 R1 的环境证据来自现有真实 artifact，但船舶操纵性仍为合成、未校准假设。正确成熟度
是 `SYNTHETIC_VESSEL_AND_REAL_ENVIRONMENT_SHADOW`，不是 real-vessel pass、连续海域安全或
生产资格。

调试期间保留了四个不作为正式结果的 attempt 目录：首次因 ETA 基线错误把既有 raw 模型
偏差归因于曲线；第二次证明中央点在 raw vertex 时视觉不足；第三/四次用于补齐资源 baseline
和屏幕弦误差记录。它们只作实现审计，正式结论只读取无 attempt 后缀的目录。

### 16.4 真实 shadow 结果矩阵（2026-08-31 12:50 +08:00）

| 门禁 | R1 结果 | Verdict / 边界 |
|---|---:|---|
| candidate-centered 6h | `6/6` | `PASS`；不是起始 6h 替代 |
| 4-span/7-control-point、端点 G2、内部 C2 | 6 个 accepted corners 全通过 | `PASS_LOCAL_G2`；`full_route_g2_claimed=false` |
| 选定半径 | 约 `41.439 km` | 最大可行离散候选；不是固定显示半径或实船标定 |
| 实际最小曲率半径 | 约 `3.158 km` | conservative 逐点门禁通过；基础 `2 km` 未被改写 |
| conservative 最大 yaw rate | 约 `0.0898°/s` | 小于 `0.15°/s`；`SYNTHETIC_UNCALIBRATED` |
| conservative 最大横向加速度 | 约 `0.00775 m/s²` | 小于 `0.02 m/s²`；`SYNTHETIC_UNCALIBRATED` |
| 500 m raster 主门禁 | land/unknown/missing coverage 均为 0 | `RASTER_RESOLUTION_CONTAINMENT_PASS`；非连续海域证明 |
| 1/2 km raster sensitivity | 两档均 accepted | `PASS_AS_SENSITIVITY`；不是额外生产缓冲 |
| hard mask / RiskFrame coverage | violation `0` / complete | `PASS`；绑定同一 145-frame RiskWindow |
| 最大风险 delta | `0.0` | `PASS` |
| 积分风险 delta | 约 `-0.02721 risk-hours` | `PASS`；不宣称稳定风险优势 |
| curve vs raw recomputed ETA | 约 `-674.48 s` | `PASS`；小于 `3845.20 s` 门槛 |
| raw recomputed vs published ETA | 约 `+5061.17 s` | 既有模型诊断，不归因于曲线，不隐藏 |
| viewport 法向偏离 | median/max 均约 `6.023 px` | `PASS`，超过 `3/5 px` |
| 最大屏幕弦误差 | 约 `0.00146 px` | `PASS`，小于 `0.5 px` |
| 最大单弦弧长亏损 | 约 `0.0103 m` | `PASS`，小于 `25 m` |
| full-route digest | 3 次一致 | `PASS`；904 个 v2 motion samples |
| cgroup | `2 GiB / swap 0 / pids 256 / timeout 90 min` | `EVIDENCE_COMPLETE`；无 OOM、swap 或 timeout |
| 附加峰值 RSS | 约 `43.1 MiB` | `PASS`，小于 `128 MiB` |
| wall time | smoothing/proof median 约 `1.29 s`；raw recompute median 约 `0.0051 s`；overhead ratio 约 `252.15` | `FAIL`，远高于 `10%`；不以绝对时间较短掩盖相对门禁失败 |
| 生产资格 | `false` | `NO_PRODUCTION_CUTOVER` |

注意：`resource_evidence_complete=true` 只表示强制 cgroup 和比较指标齐全，不表示资源资格
通过；本次 `resource_evidence.qualified=false`。同理，`all_resource_clean` 或没有 OOM 不能
替代 wall-time 门禁。

### 16.5 终态、停止规则和后续条件（2026-08-31 12:50 +08:00）

R1 的权威终态固定为：

`DISPLAY_ONLY_RETAINED_NO_PRODUCTION_CUTOVER`

原因不是几何、视觉、安全采样、RiskFrame 语义、逐点合成运动学、确定性或 cgroup 证据
缺失；这些门禁在当前 artifact 上均已形成。阻止升级的是预注册 wall-time 相对门禁明确
失败，以及真实船舶操纵性仍未校准。当前实现和 v2 sidecar 保留为 research shadow，D
默认仍使用既有 display-only 呈现；不得自动把船舶正式运动切到 v2，也不得修改正式合同。

本阶段到此收束，不通过以下方式制造成功：

- 不将 raw baseline 改为 Dijkstra 或不相关的长耗时流程；Dijkstra 继续只作正确性 oracle；
- 不把绝对约 1.3 秒描述成稳定性能优势，忽略 `252×` 相对开销；
- 不因 resource evidence 已完整而绕过 `qualified=false`；
- 不把 GEBCO raster cell pass 写成连续海域安全；
- 不把公开相似散货船参数写成目标船实测操纵性；
- 不删除或改写 v1 历史 sidecar，不启动 P0.2-M35，不提高搜索上限；
- 不把 attempt 调试目录冒充正式复现结果。

若未来重新立项，必须提出独立 R2 identity，并至少满足以下一个实质新命题：

1. 在不减少当前 raster/RiskFrame/运动学证据的前提下，把证明和资格计算增量化或缓存化，
   使附加 wall time 达到预注册 `<=10%`；
2. 获得目标船 manoeuvring booklet、试验或经批准的可追溯校准，从
   `SYNTHETIC_UNCALIBRATED` 升级；
3. 提出比 raster-resolution bbox 枚举更强且可形式化的连续 corridor 证明。

重启前仍须说明与 R0/R1、P0.2-M31～M34 的非重复性、预期真实可观测收益、强制 cgroup
方案和独立 artifact identity。不得只因想让曲线进入生产运动而重复运行同一 R1。

### 16.6 R2 性能归因、ETA 诊断与再次停止（2026-08-31 13:35 +08:00）

R2 只实施第 16.5 节第一项性能命题，不修改 R1/v1/v2 历史 artifact、正式 RoutePlan、生产
replay 或 D 默认开关。正式构件为：

`/root/my_project/.runtime/experiments/c-r2-route-smoothing-performance-profile-20260831-r3/`

本轮新增内容均保持 research-only：C 的 v2 qualifier 可通过 out-of-band observer 记录候选
corridor、RiskSampler、ETA 和最终复核分段耗时，计时值不进入 sidecar digest；Orchestrator
将静态 raster bounds/cell classification 编译为 caller-scoped prepared index；现有
`ExperimentalRiskSampler` 只以 RiskWindow fingerprint、时间和坐标 IEEE-754 bits 作精确
bounded-LRU 复用。另新增 ETA drift 逐航段诊断和生产提案准入检查；后者即使所有外部证据
通过也只允许 `READY_FOR_PRODUCTION_PROPOSAL_NO_PRODUCTION_CUTOVER`，不授权切换。

| 证据 | R2 r3 结果 | Verdict / 边界 |
|---|---:|---|
| canonical unprepared profile | 约 `1.550 s` | 分段归因值，observer 开销不作为资格数字 |
| prepared raster profile | 约 `0.615 s` | raster 重复解析明显下降；语义不变 |
| cold prepared + exact cache | 约 `0.623 s` | raw median 约 `0.00447 s`，附加 ratio 约 `138.18` |
| warm prepared + exact cache | median 约 `0.316 s` | 附加 ratio 约 `69.75`；不能替代 cold 资格 |
| exact sample cache | cold `4513` misses / `79` hits；3 次 warm 后累计 `13855` hits | 无 eviction；production default 未改变 |
| sidecar digest | canonical/prepared/cached/cold/3 次 warm 全一致 | `PASS_SEMANTIC_DIGEST_MATCH` |
| cgroup / RSS | `2 GiB / swap 0 / pids 256`；约 `41.27 MiB` | evidence complete，无 OOM/swap；RSS 通过 |
| wall-time 资格 | `qualified=false` | 冷、暖均远高于预注册 `<=10%` |

profile 显示 R1 的首要可消除热点是每次 corridor 检查重新展开 raster 网格；prepared index
已将该部分显著降低。剩余冷路径主要由约 `4513` 个唯一风险采样、曲线 ETA/risk integration
和仍需遍历 prepared cell 的 corridor 检查构成。缓存只减少重复请求，不能消除首次完整安全
证明，因此本轮不能以 warm-cache 数字制造通过结论。

ETA 诊断确认了一个可复算的部分根因：Viewer route 对同一 22 个 waypoint 发布的距离为约
`921.3796 km`，当前 qualifier 的 `C_LOCAL_EQUIRECTANGULAR_PATH_METRIC` 重算为约
`955.6161 km`，距离基准相差约 `34.2366 km`。因此状态为
`PARTIAL_ROOT_CAUSE_DISTANCE_BASIS_MISMATCH_OBSERVED`；但 Viewer bundle 未声明发布距离方法，
发布速度模型版本和 wait/replan 调整也未完全绑定，故 `+5061.17 s` 仍标记
`UNRESOLVED_EXISTING_PUBLISHED_VS_RECOMPUTED_DRIFT`，且继续排除 smoothing attribution。

proposal-readiness 的当前 blocker 固定为：performance gate failed、目标船校准缺失、连续
corridor proof 缺失、published ETA drift 未完全解决。按预注册顺序，performance 已再次
失败，故不启动真实船模校准接入、导航级连续走廊接入或生产合同提案；代码只保留这些外部
证据的严格 fail-closed 准入形状，不生成伪造的 booklet、trial、navigation semantics 或
continuous proof。R2 权威终态为：

`R2_PROFILE_ONLY_PERFORMANCE_GATE_FAIL_NO_PRODUCTION_CUTOVER`
