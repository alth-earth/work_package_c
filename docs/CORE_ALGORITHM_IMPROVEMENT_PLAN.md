---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - IN_PROGRESS
  - PLANNED
  - EXPERIMENTAL
Document Role: CANONICAL
Scope: C 核心算法现状、证据、正确性边界、改进设计与实施计划
Canonical For: 工作包 C 核心算法改进实现的首要参考
Branch: research-validation-system
Last Verified: 2026-08-30
Related Canonical Docs:
  - "README.md"
  - "ARCHITECTURE_AND_DECISIONS.md"
  - "CD_CONTRACT.md"
  - "BC_CONTRACT.md"
  - "/root/my_project/arctic_route_governance/current/RESEARCH_VALIDATION_GAP_ANALYSIS.md"
  - "/root/my_project/arctic_route_governance/current/decisions/RESEARCH_VALIDATION_DECISIONS.md"
  - "/root/my_project/arctic_route_governance/current/reference/CONTRACT_OWNERSHIP_REGISTRY.md"
---

# 工作包 C 核心算法现状、改进方案与实施计划

> 本文档将“工作包 C 核心算法实现审计报告”与后续改进方案合并为一个持续维护的计划文档。当前正式基线是带风险、速度和 ETA 耦合的时间依赖 A*。P2.1 控制轨迹复用在同 goal 收紧查询上仍保留约 48% 总耗时改善这一工程观察，但 Winter M2 的冻结门禁没有改变：M2K 对称预热诊断两档均因 order-gap 失败，P2.1 当前收口为 `MEASUREMENT_INCONCLUSIVE / FORMAL_M2_FAIL_UNCHANGED`，candidate 继续默认关闭。P3 SMO-A* 与 ARA* 保持 `DEFERRED/RETIRED`、`M0_FAIL/DEFERRED`；不再启动 Winter 重型复测。P0.1 已在 clean 本地提交上通过 small/medium/stress Synthetic M1，当前为 `M1_PASS_READY_FOR_SEPARATE_REAL_INPUT_PLAN`，但仍是 C 内部、默认关闭、未接入 Winter/ingress 的研究路径。所有候选继续默认关闭、非发布，尚不能声明生产级稳定优势或全局最优。

## 1. 文档定位与更新规则（2026-08-24 20:52 +08:00）

**首要参考声明：** 本文档是工作包 C 核心算法改进实现的首要参考（Single Source of Truth，SSOT）。以后 C 核心算法的现状、问题、改进方案、实施计划、实验结论、验收状态和方案修订，均在本文档对应章节下更新；不再为同一主题另设新的审计、研究方案或计划文档。

**更新规则：**

- 先更新本文档，再实施与本文档一致的 C 内部代码和测试；实施后在本文档补充 commit、输入身份、结果摘要和成熟度。
- 已解决的问题保留在“当前基线”或“变更记录”中，不能继续作为未解决缺陷描述。
- 新增章节必须放在语义正确的位置，并遵守治理标准的分钟级更新时间要求；不能把补丁式内容追加到文档末尾。
- 仅当跨包正式合同必须改变时，才按 [`CONTRACT_CHANGE_PROPOSAL_TEMPLATE.md`](/root/my_project/arctic_route_governance/standards/CONTRACT_CHANGE_PROPOSAL_TEMPLATE.md) 另行建立和审批提案；提案获批后仍须把链接、影响和实施状态回填本文档。
- 本文档不改变 B 的风险公式、RiskFrame 身份或 D 的展示职责；研究 sidecar、实验输出和 synthetic fixture 不得静默成为正式生产输入。

**范围外材料（外部展示用对比证据）：** 面向外部评审/比赛的“本文算法与常规算法对比”材料不写入本文档，单独维护在 [`ALGORITHM_COMPARISON_REPORT.md`](/root/my_project/work_package_c/docs/ALGORITHM_COMPARISON_REPORT.md)。该报告只做**科学对比呈现**，不构成晋级证据：它不修改任何冻结门禁、不改变 production planner 默认行为、不写 formal latest / replanning baseline / frozen artifact，其结论不得被解读为“M2 门禁通过”或“生产绩效晋级”。

**成熟度标签：** `NOT_IMPLEMENTED`、`PLANNED`、`IMPLEMENTED`、`UNIT_PASS`、`SMOKE_PASS`、`REAL_E2E_PASS`、`AUTHORITATIVE_PASS`、`FROZEN_BASELINE`。没有对应证据时，不得使用更高等级。

**治理依据：** 本文档按 [`AGENT_DOCUMENTATION_RULES.md`](/root/my_project/arctic_route_governance/standards/AGENT_DOCUMENTATION_RULES.md)、[`ENGINEERING_GOVERNANCE_STANDARD.md`](/root/my_project/arctic_route_governance/standards/ENGINEERING_GOVERNANCE_STANDARD.md) 和 [`CONTRACT_CHANGE_PROPOSAL_TEMPLATE.md`](/root/my_project/arctic_route_governance/standards/CONTRACT_CHANGE_PROPOSAL_TEMPLATE.md) 编排。

## 2. 当前基线与责任边界（2026-08-24 20:52 +08:00）

**C 负责：** `work_package_c/src/arctic_route_planning` 中的网格、船舶性能、风险采样调用、速度/ETA/等效小时成本、路线搜索、四层路线编排、重规划和 C 侧序列化/测试。C 消费正式 `bc.risk-frame.v2`，生产 `cd.route-plan.v2` 与四层 `v3` 路线集合。

**不越界：** B 负责风险计算、RiskFrame 版本和来源身份；D 只消费并展示生产字段，不重算 `risk_score`、`risk_level` 或贡献者结论。C 不得为获得性能优势而修改 B 的风险语义、D 的展示合同或冻结 RC1 artifact。

| 边界 | 当前事实 | 改进时的硬约束 |
|---|---|---|
| B → C | `bc.risk-frame.v2`、UTC 窗口、generation/revision、provenance 和 content digest | 只消费已提交窗口；缺失、越界、身份不匹配必须 fail-closed |
| C 内部 | 规则经纬度网格、8 邻接、时空风险采样、船模和时间依赖 A* | 候选算法必须复用同一边评估器，不能用简化风险模型制造“优势” |
| C → D | `cd.route-plan.v2`、四层三目标路线集合、指标和来源字段 | 保持 schema、digest、失败语义和字段所有权；实验指标可用内部 sidecar 记录 |
| C ↔ 治理 | route v2 与四层 v3 为 `FROZEN_COMPATIBLE`；自适应/非均匀网格合同为 `PLANNED` | 跨包合同变更必须走提案和审批门禁 |

## 3. 当前算法实现与已验证能力（2026-08-24 20:52 +08:00）

**正式调用链：**

```text
RiskSourcePlanningIngress.execute
  → CommittedRiskWindow lease / 私有 planner
  → RiskSampler + RegularGrid + VesselPerformanceModel
  → PlanningService / FourLayerPlanningService
  → TimeDependentAStar.plan_candidates
  → fastest / low_risk / recommended
```

当前实现可从 [`time_dependent_astar.py`](/root/my_project/work_package_c/src/arctic_route_planning/planners/time_dependent_astar.py:186)、[`layered.py`](/root/my_project/work_package_c/src/arctic_route_planning/layered.py:123) 和 [`ingress.py`](/root/my_project/work_package_c/src/arctic_route_planning/ingress.py:124) 追溯。

**算法语义：**

- 搜索标签键为 `(node, time_bucket, heading_code)`，标签内部保存累计成本和到达时间；当前实现默认不允许等待动作。
- 边评估对空间点进行风险采样，按环境速度因子计算船速，使用两轮 ETA/速度精化，再计算风险、置信度、距离、转向和等效小时成本。
- `RiskSampler` 对时间覆盖、hard mask、confidence、source identity 和窗口边界严格检查，未知数据不被当作安全数据。
- `CostModel.lower_bound()` 提供基于最快速度的局部下界；`use_heuristic=False` 可在同一近似状态图上运行 zero-heuristic Dijkstra。
- 四层编排当前为 `full_voyage`、`main_corridor`、`rolling`、`executable`，每层三个目标；正式默认执行仍是 12 次相互独立的 A* 查询，SMO-A* 仅由显式、默认关闭的内部开关启用，增量重规划尚未实现。

**当前成熟度与证据：**

| 能力 | 当前等级 | 证据与限制 |
|---|---|---|
| 正式 B→C 输入与 fail-closed | `AUTHORITATIVE_PASS` | committed-window lease、identity/digest 校验、覆盖和硬约束拒绝 |
| Winter 四层三目标生产 | `AUTHORITATIVE_PASS` | 145 个正式小时帧、4 层 × 3 目标、12/12 route integrity、hard violation 0 |
| C→D 路线合同 | `FROZEN_BASELINE` | route v2 / four-layer v3 schema、digest 和来源字段保持冻结 |
| 单元/合同回归 | `UNIT_PASS` | 历史 P0/P2.1 基线分别为 `215/274 passed`；本轮 `UV_OFFLINE=1 make check` 为 `294 passed`，Ruff、lock/sync、CLI 通过 |
| 当前 A* 的全局最优性 | `NOT_IMPLEMENTED`（未证明） | 时间桶合并、FIFO、ETA 迭代和连续时间误差均无通用证明 |
| P0.1 FIFO 资格与 exact-arrival 安全支配 | `M1_PASS_READY_FOR_SEPARATE_REAL_INPUT_PLAN` | small/medium/stress M1 语义、oracle、fail-closed、确定性和资源门通过；仅 C 内部、默认关闭 |
| P2.1 相对独立 cold control 的受限重复查询优势 | `EXPERIMENTAL_PASS` | clean M0/M1 与 Winter formal 均观测到约 47%–79% 总耗时改善；只适用于同 goal 收紧查询，不等于跨 workload 稳定优势 |
| 相对于传统算法的生产级稳定性能优势 | `NOT_IMPLEMENTED`（未证明） | P2.1 Winter M2 因 `rolling_0_24h × fastest` 中位回归 `5.94% > 5%` 失败；M2J/M2K 复测显示候选真实回归≈0（`5.94%` 为测量伪影、n=2 统计效力不足），但测量协议信噪比不足以晋级，P2.1 收口 `MEASUREMENT_INCONCLUSIVE`；候选未默认启用 |
| P3 SMO-A* 共享记忆化多目标搜索 | `DEFERRED/RETIRED` | 语义/诊断回归通过；P3.2 holdout/development M1 分别因 hit rate `14.27%/19.19%`、RSS ratio `3.367/3.380` 失败；P3.3 synthetic medium exact-key hit `47.87%`，主要为 objective 路径差异，未形成安全修复路径 |
| ARA* anytime 备选 | `M0_FAIL/DEFERRED` | small profile fastest/recommended 首解改善 `4.14%/4.19%`，未达到每目标 `20%` 门禁；不进入 Winter |
| bounded LRU 风险采样缓存 | `EXPERIMENTAL` | direct medium 实验约 14.77% median 改善，但增加约 38.6 MiB RSS，未通过正式 12 路线门禁 |

**上一版审计的状态修正：** 上一版把计数器热循环、环境变量/资源观测耦合、v3 常量散落、层窗口常量、session 无界增长等工程项标为已修复；代码和测试已支持这一结论。本次不再把这些历史问题列为当前算法瓶颈，当前重点转为时间依赖搜索语义、可证明复用和可重复性能证据。

## 4. 核心正确性边界与待解决问题（2026-08-24 20:52 +08:00）

| 编号 | 发现 | 当前证据 | 影响 | 进入下一阶段的门禁 |
|---|---|---|---|---|
| C-ALG-01 | 边到达函数 `A_e(t)=t+τ_e(t)` 未验证 FIFO | 后出发时环境速度可能更高；当前 [`_evaluate_edge`](/root/my_project/work_package_c/src/arctic_route_planning/planners/time_dependent_astar.py:392) 没有单调性检查 | 不能直接使用依赖 FIFO 的 label-setting 或普通最优子结构结论 | 在合成冲击场上逐边验证 FIFO；不满足时切换到非 FIFO label-correcting/Pareto 语义或显式失败 |
| C-ALG-02 | 同一 `(node, time_bucket, heading)` 只保留较低累计成本 | [`labels`](/root/my_project/work_package_c/src/arctic_route_planning/planners/time_dependent_astar.py:263) 与放松规则 [`time_bucket`](/root/my_project/work_package_c/src/arctic_route_planning/planners/time_dependent_astar.py:345) | 同桶但不同精确到达时间会采样不同未来风险；较低成本标签未必支配较早标签 | P0 必须保留精确到达时间或 Pareto 标签，并用独立 oracle 验证安全支配 |
| C-ALG-03 | ETA/速度固定两轮，没有收敛残差或误差上界 | **[2026-08-28 部分解决]** [`time_dependent_astar.py`](/root/my_project/work_package_c/src/arctic_route_planning/planners/time_dependent_astar.py) 注入 `eta_refinement_policy` 时切换为 `eta_refinement.refine_eta`（`damped` 阻尼迭代：max_iterations/abs_tol/rel_tol/relaxation/周期检测/终值重采样/fail-closed）；默认 `None` 保持历史固定两轮以保证正式 route digest 不变。**C-ALG-03B** 新增 `method="bounded"`：区间收缩（bracket 符号翻转检测 + 二分）在振荡场上收敛（阻尼在真实场发散，诊断见 §13 ADR）；有限区间无符号翻转时 fail-closed 抛 `no_bracket_found`，不声称区间外不存在根 | 默认路径仍固定两轮（无收敛保证）；真实输入继续需要区间单调性/覆盖证明，支配保持关闭 | 定义迭代映射、容差、最大迭代、周期检测；终值重新采样，不收敛则明确失败（已实现为可选策略；默认切换需真实输入收敛性证据） |
| C-ALG-04 | 启发式下界只对当前近似图和状态成立 | **[2026-08-28 部分解决]** `CostModel.lower_bound` 已补 admissible 数学论证（实际速度 ≤ max_speed ⇒ `travel_hours ≥ D/v_max`，且各惩罚项非负 ⇒ `(w_travel+w_distance)·D/v_max ≤ 真实 cost`；直线距离 ≤ 路径距离 ⇒ 保守且一致）；`test_lower_bound_is_admissible_against_exact_oracle` 用 zero-heuristic Dijkstra 精确代价对照 | 论证覆盖 travel/dist 下界与惩罚项非负性；尚未覆盖非 Markov 标签/非 FIFO/近似边代价（这是启发式定义之外的正确性债） | 建立小规模显式时间展开 Dijkstra/reference oracle（已建）；报告离散模型边界 |
| C-ALG-05 | 图完备性是离散的 | 当前为规则网格、8 邻接、有限边采样，且等待动作关闭 | 细网格/连续航迹与当前图的最优性不是同一命题 | 在报告中固定网格、邻接、采样和 hard-mask 规则；自适应网格另行审批 |

**可复现的白盒反例：** 2×2 网格边长约 11.12 km、经济航速 10 kn；若 `T0` 的环境速度因子为 0.2，而 `T0+1h` 后为 1.0，则从 `T0` 出发约 3.002 h 到达，从 `T0+1h` 出发约 0.600 h 到达，即后出发者先到达，FIFO 不成立。另一个同桶反例是：较早到达标签成本较高、较晚到达标签成本较低，但下一条边对两个出发时刻的速度/风险不同；只保留低成本标签会丢掉全局更好路线。

**结论：** 正式 control A* 的正确性仍应描述为“在当前规则网格、时间桶、边采样和近似 ETA 评估器定义的状态图上进行确定性搜索”。P0 候选已通过离散语义 `UNIT_PASS`，但该证据不等于对一般时变航行问题的全局最优证明，也不等于已有可证明的最优前缀复用。

## 5. 性能证据、优势缺口与可验证目标（2026-08-24 20:52 +08:00）

**Winter 当前单次工程观察：** 正式报告见 [`WINTER_C_ROUTE_VALIDATION_REPORT.md`](/root/my_project/arctic_route_governance/reports/research-validation/WINTER_C_ROUTE_VALIDATION_REPORT.md:77)。145 帧、31×11 网格、4×3 共 12 条路线，规划 wall time 约 390.894 s，wrapper 约 394.43 s，峰值 RSS 约 168088 KiB；这是一次工程测量，不是稳定 benchmark。

**重复搜索事实：** 在当前 v3 artifact 中，三个目标的 `full_voyage` 与 `main_corridor` 路线 waypoint、ETA、风险、速度和来源身份相同，只有计算计数等运行字段不同。两层合计约 376 s，占单次规划约 96%；若证书允许完全消除 `main_corridor` 重复搜索，理论上限约为节省 48.5%、接近 1.94× speedup。该数字只是瓶颈上限估计，不能作为算法收益承诺。

**现有缓存证据：** bounded LRU 风险采样缓存的 direct medium 实验约为 `76.281 s → 65.012 s`，median 改善约 14.77%，但 RSS 增加约 38.6 MiB，且没有覆盖正式 ingress、全部层和全部目标。因此它继续保持默认关闭、实验性。

**本项目要形成的可验证优势：** 优先实现“同一目标内的证书化 full/anchor 搜索会话复用”，目标是在完全相同的 RiskFrame、船模、网格、边评估器和失败语义下，减少重复状态扩展和边评估；是否达到 15%/30% 等阈值只能由后续重复 benchmark 决定。不能通过缩短窗口、减少目标、降低风险约束或改写输出 digest 来制造优势。

## 6. 目标改进算法：LTCR-TDA*（2026-08-24 20:52 +08:00）

**命名与定位：** 采用“分层目标证书复用的时间依赖 A*”（Layered Target-Certified Reuse Time-Dependent A*，简称 **LTCR-TDA***）。它不是给普通 A* 改名，而是围绕 C 当前最大可观测瓶颈设计的、可回退重复查询算法。P0/P1/P2 的 exact-arrival session 继续承担离散正确性和最优性下界研究；P2.1 则使用当前快速 control A* 生成**执行轨迹等价证书**，只证明收紧约束后的 control 会返回相同业务路线，不把 control 的时间桶搜索写成全局最优算法。

**P2.1 核心数据结构（2026-08-25 01:13 +08:00）：**

```text
ControlTraceCertificate_m = (
    ordered_insertion_digest,
    insertion_count,
    replacement_count,
    maximum_inserted_elapsed,
    maximum_inserted_path_edge_risk,
    source_route_digest,
    termination = FIRST_GOAL_POP,
    input/config/model/generation/algorithm digests,
)
```

每个 `objective`（`fastest`、`low_risk`、`recommended`）独立生成证书；有序摘要必须覆盖首次 goal 返回前每次成功写入或替换 label/OPEN 的历史事件，包括后来被覆盖的 transient label。证书不跨目标、goal、RiskFrame、generation、revision、planner 或 evaluator identity 复用。

**计划流程：**

```text
1. 用未改变搜索顺序的 control A* 产生 full route，同时只读记录 label/OPEN 成功写入轨迹。
2. 当 main 的 anchor 仍是业务终点时，对相同 goal/objective 的收紧时域或风险查询验证轨迹证书。
3. 只有 target 阈值覆盖首次 goal 前全部历史成功写入，且 source route 自身满足 target，才返回 `HIT_TRACE_EQUIVALENT`。
4. exact identity 可返回 `HIT_EXACT`；不同 goal、放宽约束、身份变化、transient label 超界、取消或证书损坏一律不命中。
5. miss 时从零运行当前 control；rolling/executable 或不同 anchor 本阶段继续 cold control。
6. control/candidate 结果只写入新的 experiment identity 和诊断 sidecar，不覆盖正式 latest 或冻结基线。
```

**P2.1 证书条件：** 对相同 start、goal、departure、objective、RiskFrame、船模、网格、时间桶、边采样、代价模型和算法版本，target 只能收紧 `maximum_elapsed` 和/或逐边 `maximum_risk`。令 `E_trace` 和 `R_trace` 分别为 source 在首次 goal 返回前所有历史成功 label 写入的最大 elapsed 与最大 path edge risk，则只在

```text
target.maximum_elapsed >= E_trace
target.maximum_risk    >= R_trace
```

并且 source route 满足 target、source→target 确为可行域收紧时记录 `CONTROL_TRACE_EQUIVALENT`。若 target 删除了任何曾写入 OPEN 的 transient label，证书必须失败，即使最终 source route 本身仍满足 target。这个保守条件允许按搜索写入顺序归纳 labels、OPEN、predecessor 和首次 goal route 不变；它不证明该 route 对一般连续问题或 exact-arrival 图最优。

**必要的正确性边界：** P0 已解决候选实现的精确到达时间标签、ETA 收敛检查、终值重采样和独立 oracle 对照，并通过其离散语义 `UNIT_PASS`。P2.1 不复用 control 的 OPEN 下界、不生成 `OPTIMAL` 状态，只生成 control execution trace equivalence。M0/M1 现只支持“已验证 workload 中相对独立 cold control 的耗时优势”；LTCR-TDA* 仍是受限离散模型上的语义保持工程实验，P3 不同 anchor 的 `U_A/LB_A` 证明保持 `PLANNED`。

**预期优势与诚实边界：** 该算法有机会直接消除 full/main 的重复搜索，优势指标是相同语义下的 wall time、expanded/generated、边评估次数和峰值 RSS；预计收益是待验证假设，不是现有结论。证书失败时退回 baseline 是算法设计的一部分，不是异常掩盖。

**C-only 影响：** 第一阶段只改 `work_package_c/src`、配置、测试和本文档，不改变 B/C 或 C/D 正式合同；内部的 session/certificate metrics 作为诊断 sidecar，不进入现有 route semantic digest。

## 7. 候选方向、取舍与暂缓事项（2026-08-24 20:52 +08:00）

所有候选都必须共享 C 当前 `evaluate_edge(state, neighbor)` 的风险采样、船模、hard mask、coverage、provenance 和失败语义；只能改变队列、标签、复用或剪枝。外部仓库只作为论文/接口思想参考，不直接复制许可证不清晰或非标准许可代码。

| 方向 | 作用 | 与 C 的适配 | 决策 |
|---|---|---|---|
| LTCR-TDA* / P2.1 control trace | 同一 goal/objective 的收紧约束轨迹等价证书和安全回退 | 避开 exact cold 爆炸，直接针对已观测的 full/main 重复，C-only | **当前第一优先级** |
| SIPP-like safe-interval search | 将 hard-mask 时间区间显式放入状态，可表达等待 | 需先定义保守 safe interval，软风险不能误判为绝对安全 | P0 后小规模实验 |
| SMO-A*（Shared-Memoization Objective-A*） | 三个目标共享 per-call 边遍历缓存，跳过重复风险采样 | C-only 纯记忆化，不改变搜索结构；所有层均等受益 | **当前第二优先级**；已实现，基准未达 15% 目标 |
| ARA*/Anytime weighted A* | 先求可行解，再用 epsilon 收敛换取时间预算内的质量 | 可在 C 内独立实现，但必须报告相对 oracle 的代价差 | 备选方案（SMO-A* 不达标时启动） |
| Shared NAMOA*-like | 对时间、风险、距离/转向维护 Pareto 标签，减少三目标重复 | 标签可能爆炸，且 temporal dominance 尚未定义 | 第二阶段候选 |
| MOPBD* | 在局部风险变化和稳定状态下复用多目标搜索树 | 需要增量差分索引、局部变化假设和更强证明 | 远期研究 |
| D* Lite/LPA* | 借鉴增量队列和边成本变化更新 | 经典假设不直接覆盖时变 RiskFrame；不能只改名 | 仅作理论参考 |
| 自适应/非均匀网格 | 在风险梯度/障碍附近细化，均质区域粗化 | 需新的网格版本、cell→RiskFrame 映射和保守聚合合同 | **上一版 2.2.2 对应方向暂不实施** |

**2.2.2 暂缓声明：** 上一版编号 2.2.2 所对应的自适应/非均匀网格方案全部保留为后备研究方向，但本轮不实现、不改合同、不引入 PolarRoute/MeshiPhi 的网格依赖。只有当固定网格在 M1/M2 中被重复证据证明为主要瓶颈，且 C 侧无法通过 LTCR-TDA*、缓存或搜索标签改进达到目标，才启动该方向的必要性评审和跨包合同提案。

**参考来源：** [LPA* 论文](https://www.cs.cmu.edu/~maxim/files/aij04.pdf)、[D* Lite 论文](https://aaai.org/papers/00476-aaai02-072-d-lite/)、[NAMOA* 论文](https://www.ijcai.org/Proceedings/05/Papers/0867.pdf)、[ARA* 论文](https://papers.nips.cc/paper/2382-ara-anytime-a-with-provable-bounds-on-sub-optimality)、[MOPBD* 论文](https://arxiv.org/abs/2108.00710)、[PolarRoute](https://github.com/bas-logist/PolarRoute)、[WeatherRoutingTool](https://github.com/52North/WeatherRoutingTool)。这些来源支持方法启发，不构成 C 已实现或已验证的能力声明。

## 8. 分阶段实现计划与晋级门禁（2026-08-24 20:52 +08:00）

| 阶段 | 目标与交付物 | 主要门禁 | 状态 |
|---|---|---|---|
| P0 正确性语义 | `TemporalLabel`/时间展开 reference oracle；FIFO 与非 FIFO fixture；ETA 残差、最大迭代、周期检测和最终重采样 | 反例全部命中预期；control 与 oracle 在小图上路线/代价一致；不收敛显式失败 | `UNIT_PASS` |
| P1 会话骨架 | 在 C 内实现 per-objective 可恢复 session、OPEN/前驱/标签快照和 input/config/model digest fence | 不跨目标/代际复用；取消、generation、revision、fail-closed 回归通过 | `UNIT_PASS` |
| P0.1 有限域 FIFO 与 exact-arrival 支配 | `qualify_fifo`、完整 `TemporalScope`、证书化安全支配和独立 M1 runner | small/medium/stress 语义与 oracle 一致；FIFO/scope/fail-closed/determinism/resource 完整；真实 pruning；compute median/P95 ≤5% 回归、RSS ≤1.10 | `M1_PASS_READY_FOR_SEPARATE_REAL_INPUT_PLAN`；仅 C 内部、默认关闭 |
| P2 same-goal monotonic reuse | 实现同一目标、同一输入下 exact hit 与收紧时域/风险约束的证书迁移，保留 baseline 回退 | M0/M1 与 control 语义一致；证书可重算；命中零搜索扩展；失败自动回退 | `UNIT_PASS`（M0 性能 FAIL） |
| P2.1 control trace reuse | 为正式 control 增加默认关闭的历史写入轨迹证书；只在同 goal 收紧约束保持整段执行轨迹时复用 | transient-label 反例 fail-closed；M0 总耗时至少改善 20%；M1 两规模 median 至少改善 15% | `EXPERIMENTAL_PASS`（M0/M1）；Winter M2 `FAIL`；M2K 对称预热诊断仍有 order-gap 失败，当前为 `MEASUREMENT_INCONCLUSIVE / FORMAL_M2_FAIL_UNCHANGED`，candidate 默认关闭 |
| P3 SMO-A* / full-anchor reuse | SMO-A* 共享记忆化已实现但 P3.3 诊断后延期退出；full-anchor `U_A/LB_A` 证书复用暂不独立推进 | SMO-A*: 路线一致 PASS、cache hit rate >= 50%、wall >= 15%；full-anchor: M1 >= 5 次 paired | SMO-A* `DEFERRED/RETIRED`；ARA* `M0_FAIL/DEFERRED`；full-anchor parked，等待 P0.1 证书语义 |
| P4 formal shadow | Winter 正式 ingress、4×3、12 路线，control/candidate 双轨 | M2 通过确定性、合同、资源和性能阈值；不覆盖冻结 artifact | `IMPLEMENTED`；原始 M2 `FAIL`，M2H holdout `PASS`、development `FAIL` |
| P5 默认启用评审 | 仅在重复正式证据支持时改变默认开关，并更新本文档/CHANGELOG | 通过审批、回滚演练和新 experiment identity；否则保持 baseline | `PLANNED` |
| P6 多目标/自适应后续 | NAMOA*/MOPBD*/自适应网格等独立提案 | 必须先证明 P0/P3 的收益不足且合同必要性成立 | `DEFERRED` |

### P2.1 Winter M2 冻结门禁与执行结论（2026-08-25 16:19 +08:00）

**本轮固定选择：** 验证显式、默认关闭、非发布的 `control_trace` 在 Winter 正式四层工作负载上的适用性。M2 只比较同一正式输入下的 control 与 control-trace shadow；仅允许 full→main 的同一 `goal/objective` 尝试复用，rolling、executable 和不同 goal 必须走 cold control。此处及后续修订仍是 C 核心算法计划的唯一入口，不创建新的同主题文档。

**G0–G3 在本轮开始前冻结如下：**

| 门禁 | 冻结条件 | 失败/停止动作 |
|---|---|---|
| G0 clean/provenance | C 算法实现固定为 `9ab88298059b2da5ce3f08c8aed995fcff8e4bd8`，`UV_OFFLINE=1 make check` 为 `274 passed`；C `uv.lock` SHA256 为 `8893cb8387825ca4890ed808f4a98b02ba938337752971dacc3e77f859164f22`。Winter runner 固定为 `03479058dc2ef0d31f1a0695d91f344b625492a1`，orchestrator `uv.lock` SHA256 为 `26a8456a9b899bec2030a80b5095ec846e85b4c5550397a48dc7fc1444f8368d`；正式 M2 执行时两仓 clean、只本地领先 origin。 | 任一工作树 dirty、SHA/lock 未冻结、完整检查失败或 clean provenance 未刷新：不进入正式 M2；不以 push 作为门禁。 |
| G1 formal identity/contract | 正式 Winter 输入固定为 145 帧、31×11 网格、4 层×3 目标、12 路线；control/candidate 使用同一 committed-window lease、query/content/commit、generation/revision、配置、船模和边评估器，并在执行前后复核。control/candidate 使用独立 scratch coordinator/store；`production_published=false`，不写 formal latest、replanning baseline 或 frozen artifact。 | 任一输入身份漂移、lease 失效、合同/字段/semantic digest 变化、正式存储被写入或出现 partial publication：立即停止并 fail-closed。 |
| G2 semantic/determinism | 先做 2 次有界筛选，再做至少 3 次串行正式重复；每次 control/candidate 均须 12/12 route integrity，路线业务字段和 route semantic digest 一致，sidecar 记录真实 hit/miss/cold/fallback，重复运行的离散路线 digest 稳定。`compute_ms`、扩展计数等运行字段可不同，但不得塞入业务语义等价判断。 | 任一层/目标缺失、路线 digest 不一致、非确定性、取消、异常或 candidate 失败未明确回退：停止 M2，不重命名或发布候选。 |
| G3 performance/resource | 相对同次 control：总 wall-time median 至少改善 15%；总 P95 不恶化超过 5%；任一 layer/objective 不回归超过 5%；peak RSS ratio 不超过 1.10；可测的 trace-source overhead median 不超过 5%；运行期间 swap used 不增长（基线可非零）；无 OOM、timeout、资源上限失败。 | 任一阈值失败：M2 `FAIL`，保留 control 和诊断 sidecar，关闭 candidate；不得通过提高 queue/label/expansion 上限或减少 workload 重新制造通过。 |

**停止条件与证据边界：** G0–G3 任一硬条件失败都终止本轮，不执行未经批准的重型重复。若两次筛选没有自然的同 goal full→main eligible hit，则只记录“Winter 适用性/性能优势未证明”，不启动三次正式性能重复。`/root/my_project/.runtime/experiments/winter-c-p2-monotonic-shadow-20260825-r2/` 是旧 `exact-temporal`/`temporal-label` candidate 的 P4a 构件，已因 `queue=50000` 失败且没有 candidate plan set 或 12-route integrity；它不属于 P2.1 `control_trace` 证据，绝不可混入本轮 M2 结论。

**执行结论：M2 `FAIL`，候选关闭。** clean prepare-only 构件为 `/root/my_project/.runtime/experiments/winter-c-p21-control-trace-m2-prepare-20260825-r2/`。第一次 screening `/root/my_project/.runtime/experiments/winter-c-p21-control-trace-m2-screening-20260825-r1/` 因 runner 直接使用保留 `planner_version`、`plan_version` 和 `reference_plan_id` 的正式 route digest，产生跨算法比较假阴性；旧构件原样保留，不能作为算法失败或性能证据。runner 在本地提交 `03479058` 中改为仅用于跨算法实验的归一化 digest，并保留业务字段与引用结构检查；修复后 `/root/my_project/.runtime/experiments/winter-c-p21-control-trace-m2-screening-20260825-r2/` 为 2/2 case `PASS`，总 wall-time 中位改善 `48.41%`、RSS ratio `1.035`、swap 增量 0，三目标 trace-source overhead 为 `-1.96%/-1.92%/-1.11%`。r1/r2 因旧 experiment key 未绑定实现而具有相同 `experiment_id`，但各自目录、仓库 commit 和 implementation SHA 可区分；旧构件不改，后续本地提交 `3097271` 已让新 experiment identity 绑定 runner/C 实现 SHA 与两仓 commit，未据此重跑或改写 M2。

正式构件 `/root/my_project/.runtime/experiments/winter-c-p21-control-trace-m2-formal-20260825-r1/` 固定 4 次串行 alternate paired run。4/4 case、48/48 route integrity/业务语义、确定性、每次 `3 trace + 3 zero-search HIT + 6 cold`、scratch/production 边界全部通过；总 wall-time median 从 `395.411 s` 降至 `206.153 s`，改善 `47.86%`，总 P95 改善 `47.19%`；median RSS ratio `1.038`，swap 增量 0；三目标 trace-source overhead 为 `-2.08%/-1.94%/1.00%`。但 `rolling_0_24h × fastest` 的 candidate/control median 为 `2237.871/2112.389 ms`，回归 `5.94%`，超过冻结的 5% 单元上限，因此 G3 和 M2 总 verdict 严格为 `FAIL`。不得重跑择优、放宽阈值、减少 workload 或提高资源上限来改写结论；正式 control、默认开关和发布物保持不变。

**后续约束：** 下一轮只能先做轻量、非正式的 cold-path 隔离诊断，判断这 `0.94` 个百分点的超限来自测量方差、trace 旁路残留开销还是实现结构；诊断不得自动升级为 M2 复跑，也不得改变 5% 冻结阈值。P3、2.2.2 与 P5 继续延期，直到本文档先形成新的可审计计划和门禁。

**明确延期：** P3 不同 anchor/prefix 复用保持 `PLANNED`；上一版 2.2.2 自适应/非均匀网格保持暂缓，不改 B/C 或 C/D 合同、不引入新网格依赖；P5 默认启用评审保持延期，除非正式重复证据、审批和回滚演练全部通过。M2 期间现有 `plan()`、正式 execute 路径、默认开关和冻结构件均不改变。

### 下一轮 Winter 含潮总流闭环与数据治理实施记录（2026-08-26 02:20 +08:00）

本轮把“含潮总流优先”从策略要求落实为正式输入门禁，并在不改变 B/C 合同的前提下完成一套 holdout 闭环。该节是当前执行状态的首要记录；具体原始文件、日志和校验摘要保留在 `.runtime/experiments/` 对应实验目录。

**A：原生网格含潮总流。** 正式 Copernicus 源固定为产品 `ARCTIC_ANALYSISFORECAST_PHY_TIDE_002_015`、数据集 `dataset-topaz6-arc-15min-3km-be`、`dataset_part=originalGrid`。A 使用原生 TOPAZ 北极立体投影的 `x/y` 查询与 2D `latitude/longitude`，对含潮总流不可用时 `require_total_current=True` 直接 fail-closed，不发布 detided 后备。2026-02-22～02-28 holdout 采用 6 个 24 h 分块获取（145 条 current records，6 个 total snapshots，`current_component=total`、`tide_included=true`、无 warnings）；完整 replay `all_required_complete=true`，bundle digest 为 `e2b9c6a95ed112a782b525ca82d0ae68a8fd4aed69c835234e6ccfc8272d091a`，文件 SHA-256 为 `ae08becffe148030b9c5de9f023214cc558821227a1994855f233308e642b9cd`。

**B：必要的曲线网格适配。** TOPAZ `originalGrid` 保留 2D 经纬度和 1D 100 km 投影轴，原 B regrid 只接受一维规则经纬度，因此本轮在 B 内增加了精确、显式、fail-closed 的北极立体投影适配：连续变量线性插值，分类变量最近邻，未知投影/轴/单位拒绝。B `bc.risk-frame.v2` holdout 结果为 `FORMAL_VALIDATED`，145 帧，schema 与 commit readback 均 PASS，commit/content digest 为 `115ad3ab6d7034fabc9428f91c14099b02dff8bb2443569a8d3947187fbb5ff9`。该改动未修改 RiskFrame、风险公式、默认值或跨包合同；B 聚焦测试 `16 passed`、Ruff 通过。

**C：正式输入消费。** C holdout 以同一 B committed window 执行正式四层规划，`cd.four-layer-route-plan-set.v3` 为 12/12 routes、地理完整性 PASS；planning wall `302.692069 s`，peak RSS `198620 KiB`，输出仅写入实验目录。该结果是 `REAL_E2E_PASS` 的 A→B→C 闭环证据，不是 production publication，也不改变冻结 M2 verdict。

**开发窗口与采样身份。** 2026-03-22～03-28 发展窗口优先复用已处理的 72 h 冰浓度/冰厚/波浪筛选数据，保留原筛选 root；在独立 formal root 补齐 GEBCO、Copernicus 冰情/水位，并将 GFS 直链 404 记录为失败证据后使用已批准的 CARRA winter fallback。current 仍必须单独按 `TOTAL_ONLY_FAIL_CLOSED` 获取；任何未完成或非 total 结果不得进入 bundle。该窗口已在独立 RunContext、bundle 和 B/C 实验身份下完成，可计为第二个独立 A→B→C 样本。

**detided 退役。** detided 仅作为强制后备，不再作为本轮正式输入。在本轮清理范围内，A canonical、实验 holdout 和 preflight 中明确标记 `current_component=detided` 的 payload、raw/source current snapshot、detided bundle 及其未注册副本，在完成含潮总流闭环和 ledger 固化后删除；保留小型失败摘要、M2 正式失败证据、总流 bundle/B/C 结果和冻结备份目录。删除清单、SHA-256、硬链接引用与释放空间写入 `/root/my_project/.runtime/experiments/detided-retirement-20260826/cleanup-ledger-v2.json`，不得以清理动作改写历史 M2 结论。

**当前门禁结论。** holdout A→B→C 通过不等于 P2.1 M2 通过；冻结的 `rolling_0_24h × fastest` 单元回归 `5.94% > 5%` 仍为 `FAIL`，candidate 继续默认关闭、非发布。P3、2.2.2 和 P5 继续延期；下一步只在本节记录的新实验身份上进行 P2.1 shadow/重复和第二独立 Winter ScenarioRunGroup，不能用 detided 旧窗口或同一窗口重放冒充独立样本。

### Winter development 窗口收口与 detided 退役完成记录（2026-08-26 03:51 +08:00）

**第二个严寒窗口已完成。** 2026-03-22 00:00～03-28 00:00 UTC development window 复用既有冰浓度、冰厚和波浪处理结果，并补齐其余 A 数据；最终 1212 条记录覆盖 12 类必需数据，其中 `ocean_current` 为 145 条且全部 `current_component=total`、`tide_included=true`。GFS 历史直链 404 作为失败证据保留，风/温度/能见度使用已批准的 CARRA winter fallback；含潮总流仍使用 `TOTAL_ONLY_FAIL_CLOSED`，未把 detided 混入 bundle。

该窗口的 v2 bundle 为 `/root/my_project/.runtime/experiments/a-winter-formal-development-total-20260322/tromso_to_isfjorden_outer_winter_development_total_20260322T000000Z_min144_v2_bundle.json`，`bundle_id=a-bundle-6fb64bb7470bd026bc9b97ea`，bundle digest 为 `6fb64bb7470bd026bc9b97eaa25f4294bfc5c34350afb2b561d4af3d442d214c`，文件 SHA-256 为 `83392d7085b096f4b532f349ae96a2fd838784a49d5ef5d0b05af9fe17df420b`；其 RunContext 为 `run-6f82124f-8548-4153-b006-6c0a6d6130d1`，与 holdout 身份独立。

**A→B→C 结果。** B 输出 `FORMAL_VALIDATED`，145 帧，schema/commit readback PASS，risk content digest 为 `bdfd7964df96ffcad7dd78d9830394a0a91d7fbbfde16c0649d2ba2fb68a00ab`；C 在隔离实验目录完成 `cd.four-layer-route-plan-set.v3` 的 12/12 routes 与 integrity `PASS`，planning wall `341.991636 s`、total wall `344.788310 s`、peak RSS `200840 KiB`。两者均未写 production latest、replanning baseline 或 frozen artifact。

**P2.1 development shadow 只作筛选，不改 M2。** `/root/my_project/.runtime/experiments/winter-c-p21-shadow-development-total-20260826/comparison-summary.json` 包含 2 个交替顺序 case，语义、reuse matrix/timing 和 RSS 通过（median RSS ratio `1.032486`），总体 candidate/control median wall 为 `182.083/345.764 s`，改善 `47.339%`；但 `executable_0_6h × fastest/low_risk` 的筛选回归约 `5.36%/6.42%`，因此 screening `FAIL`，重复数不足以评估 M2（`NOT_EVALUATED_INSUFFICIENT_REPETITIONS`）。不以该结果促进 candidate，也不覆盖冻结的正式 M2 `FAIL`。

**detided 已按要求精确退役。** 在 holdout、development A→B→C 和 P2.1 shadow 完成后，运行 `/root/my_project/work_package_a/scripts/retire_detided_data.py --purge --confirm`，ledger 为 `/root/my_project/.runtime/experiments/detided-retirement-20260826/cleanup-ledger-v2.json`：删除 4356 个 detided payload/raw/sidecar/source 文件及 3 个 detided bundle，共 4359 个文件、1448 条 manifest rows、1,318,695,475 bytes；删除前后均按 SHA-256 与硬链接引用核验。canonical/holdout/preflight 不再有 detided current rows；冻结历史备份目录保留，供审计，不作为当前数据源。小型失败日志、正式 M2 失败证据、含潮总流 bundle 及 B/C 结果继续保留。

**当前结论不变。** 含潮总流已成为严寒正式窗口的实际首选并完成两个独立窗口的 A→B→C 闭环，但 P2.1 正式 M2 仍因 `rolling_0_24h × fastest` 中位回归 `5.94% > 5%` 保持 `FAIL`，candidate 默认关闭、非发布；P3、2.2.2、P5 继续延期。

**实施顺序硬规则：** 先 P0，再 P1/P2；P3 证书失败必须回退；P4 以前不得将候选算法命名为正式生产 planner；P6 不得倒灌到当前合同。

**P2/P4a 实施规格冻结（2026-08-25 00:09 +08:00）：** P2 不停留在普通 exact-key 缓存，而是在同一 start、goal、departure、objective、RiskFrame、generation/revision、网格、船模、代价模型、ETA policy、边采样、搜索限制和 evaluator 身份下，允许把已认证的宽约束结果迁移到更窄的 `maximum_elapsed` 和/或 `maximum_risk` 查询。目标可行域必须是源可行域的子集，且源路线自身满足目标约束；反向放宽、不同目标、不同身份或路线不满足收紧约束时一律 cold candidate。证书必须从 session 当前 labels、incumbent 和过滤 stale 后的有效 OPEN 独立重算，固定记录 `U`、`LB`、epsilon、`OPEN_BOUND/OPEN_EMPTY` 终止原因、session/state digest 和路线语义 digest；命中必须为零新增 expansion 与 edge evaluation。无界全局缓存禁止，每个 objective 只保留一个受身份约束的证书；取消直接传播，不能用 control fallback 掩盖，其他候选失败可从零运行 scratch control 并显式记录 `FALLBACK_CONTROL`。

本轮提前实施受限 P4a：为 `PreparedRiskPlanning` 增加显式、默认关闭、非发布的 Winter shadow 方法，并提供使用同一正式输入围栏的 orchestrator 独立 runner。两条 shadow 路径都在 committed-window execution lease 内复核 commit/content identity，control/candidate 使用独立 scratch coordinator/store，不写 production session、replanning baseline、正式 latest 或冻结 artifact；输出只进入新的 experiment identity 和诊断 sidecar。现有 `execute()`、`execute_four_layer()` 与正式 runner 的默认行为保持不变。该提前项只用于验证 P2 在当前 Winter full 144 h → main 72 h 重复工作负载上的适用性，不提前实施 P3 不同 anchor/prefix 复用，也不实施上一版 2.2.2。

**P0 实施规格冻结（2026-08-24 22:12 +08:00）：** 本轮保留现有 `TimeDependentAStar` 为正式 control；新增候选不从公共包导出、不接入 ingress/service。候选标签身份固定为 `(node, heading, exact UTC arrival_time)`，不同到达时刻禁止相互支配；只有精确状态相同时才保留较低成本。候选资源上限为 50,000 expansions、100,000 labels、50,000 queue 和 400,000 edge evaluations，超限显式失败。

**P1 实施规格冻结（2026-08-24 23:04 +08:00）：** 本阶段只实现 C 内部的可恢复搜索会话骨架，不实现 P2 同目标复用、P3 full/anchor 证书复用、独立 FIFO 分类器或上一版 2.2.2 自适应/非均匀网格方向。新增内部 `TemporalSessionIdentity`，其规范身份必须绑定 RiskWindow content digest、commit/revision、generation/input revision、RiskIdentity、planner/model/config digest、objective、起终点、出发时间、最大时域、风险阈值、网格/边采样、ETA policy、搜索限制和启发式设置；会话 ID 由该身份规范序列化后的 SHA256 确定生成。

P1 会话状态固定为 `READY → PAUSED → GOAL_CERTIFIED | EXHAUSTED | CANCELLED | FAILED`；`CANCELLED` 是终态，不得作为普通暂停继续。内部接口固定为 `create_session`、`advance_session`、`checkpoint_session` 和 `restore_session`；每个 objective 通过 `TemporalSessionBundle` 创建完全隔离的 session，不共享标签、OPEN、前驱或搜索可变状态。planner 继承的边几何缓存只做观察等价的 memoization，不包含 objective/session 搜索状态。checkpoint 为进程内不可变快照；恢复前必须执行全身份 fence，任一输入、配置、模型、目标或策略身份不匹配即拒绝恢复并要求新建会话。expansion、label、queue、edge-evaluation 等硬资源限制在暂停/恢复间累计，不得通过恢复重置；取消、资源超限、coverage/ETA 等失败继续 fail-closed。现有 `plan()` 仅作为“创建临时会话并推进至终态”的兼容包装，正式 control、ingress/service、B/C 与 C/D 合同均不接入该候选。本阶段不宣称任何性能优势，性能结论留待后续 paired benchmark。

候选 ETA 使用 `damped_fixed_point_v1`：静水 ETA 初值、最多 12 次迭代、阻尼 0.5，容差为 `max(1 秒, 1e-6 × max(1 小时, guess, raw ETA))`；周期、超迭代和终值不一致均拒绝该边。初步收敛后必须按 terminal ETA 重采样并再次验证，最终风险、速度、成本和 arrival time 必须来自终值采样。独立 oracle 使用单独的零启发式精确时间搜索，不调用 control/candidate 的 `plan()` 或 `_evaluate_edge()`；它只用于 M0 synthetic，不进入正式发布链。

**P0 实施与证据（2026-08-24 22:31 +08:00）：** 已新增内部 `eta_refinement.py`、未从公共 planners 包导出的 `temporal_label_astar.py`，以及 test-only `tests/reference_temporal_oracle.py`。候选实现 exact UTC arrival label、无跨到达时刻支配、goal incumbent/OPEN 下界终止、四类硬资源上限和 terminal ETA 重采样；独立 oracle 不导入生产规划器。静态小图三方差分测试证明 control、candidate、oracle 的路径、ETA 和代价一致；非 FIFO、同桶不同精确 ETA、exact-state replacement、周期/超迭代、取消和资源超限反例均显式通过或失败关闭。

可重复入口为 `scripts/validate_temporal_semantics.py`。干净运行基线为 Git `37627fdc2b37bbb3c8b06392e09b1b91a2d6ea2f`、clean/synced worktree；实验 `c-p0-temporal-semantics-v1-37627fdc` 在 5×7×7 synthetic 静态 fixture 上串行执行 10 次，10/10 semantic digest 一致，原始构件位于 `/root/my_project/.runtime/experiments/c-p0-temporal-semantics-v1-37627fd/`；manifest 的 `experiment_id` 为 `c-p0-temporal-semantics-v1-37627fdc`，并记录 `git_worktree_dirty=false`。control/candidate median wall time 分别为 11.108/16.661 ms，候选约慢 50.0%，因此 P0 保持正确性 `UNIT_PASS`，**没有通过 M0 性能晋级门禁，也不构成算法优势声明**。性能优势仍须由后续证书化复用和正式 paired benchmark 建立。

验证基线为 Git `37627fdc2b37bbb3c8b06392e09b1b91a2d6ea2f`，clean/synced worktree、`uv.lock` SHA256 `8893cb8387825ca4890ed808f4a98b02ba938337752971dacc3e77f859164f22`、Python 3.13.14；P0 聚焦测试 40 项通过，`UV_OFFLINE=1 make check` 为 215 项通过，Ruff、lock/sync 与 CLI smoke 均通过。未修改 B/C、C/D schema/digest、正式默认 planner 或 frozen artifact。

**P1 实施与证据（2026-08-24 23:33 +08:00）：** 已新增内部 `temporal_session.py`，并把 `TemporalLabelAStar.plan()` 收敛为 create/advance 到终态的兼容包装。session 独占 exact-arrival labels、OPEN、前驱、incumbent、诊断与启发式缓存；checkpoint 保留 stale queue entry、微秒级 ETA 和累计计数，清除 cancel callback，并以 state digest 拒绝篡改。恢复只接受 `READY/PAUSED`，重新计算当前 sampler、planner、request、model、policy 和 evaluator 身份；内部 sampler digest 与可选正式 committed-window digest 明确区分，正式 pair 必须满足 `commit_id = risk-window-sha256-<content_digest>`。显式伪造 identity、风险窗口内容变化、evaluator 变化、终态恢复、取消、四类资源上限重置和跨 objective 状态共享均有负例。

当前 P0/P1 聚焦回归为 63 项通过，完整 `UV_OFFLINE=1 make check` 为 238 项通过。显式 P1 runner 在 5×7×7 synthetic fixture 上串行执行 10 次；每次 session 均经历 8 次 pause/checkpoint/restore，10/10 control、one-shot candidate 与 session candidate 路线 semantic digest 一致，one-shot/session 离散 metrics 与 diagnostics 一致。最终代码哈希对应的原始构件位于 `/root/my_project/.runtime/experiments/c-p1-temporal-session-v1-37627fd-dirty-r3/`，manifest `experiment_id` 为 `c-p1-temporal-session-v1-37627fdc-dirty`。该运行来自 Git `37627fdc2b37bbb3c8b06392e09b1b91a2d6ea2f` 上的未提交研究工作树，manifest 如实记录 `git_worktree_dirty=true`，因此只支持 P1 `UNIT_PASS`，不构成 clean、formal、authoritative、frozen 或性能优势证据。未修改 B/C、C/D schema/digest、正式默认 planner、ingress/service 或 frozen artifact。

**P2/P4a 实施与证据（2026-08-25 00:42 +08:00）：** 已新增内部 `temporal_reuse.py`，实现 `TemporalGoalCertificate`、`TemporalCertifiedGoal`、`TemporalReuseOutcome`、`certify_session()`、`try_reuse()` 与显式 `reuse_or_plan()`。证书从 checkpoint 的 incumbent、labels 和过滤 stale 后的 OPEN 重算，并绑定 state、route 和 certificate digest；状态区分 exact/monotonic hit、incompatible miss、cold candidate 与实际 control fallback。当前正式请求只有逐边 `maximum_risk`，没有 cumulative-risk 合同，后者被明确拒绝而不偷换语义。候选仍不从公共 planners 包导出。

完整 `UV_OFFLINE=1 make check` 为 258 项通过。实验 `/root/my_project/.runtime/experiments/c-p2-temporal-goal-reuse-20260825-r1/` 在未提交研究工作树 `5660365b` 上串行运行 10 次，10/10 control/candidate 业务语义、证书矩阵、exact/收紧时域/收紧风险/两者同时收紧、身份/放宽/路线不可行 miss、零新增 expansion/edge evaluation 和独立 control fallback 全部符合预期，因此 P2 正确性达到 `UNIT_PASS`。但 control cold median 为 `135.373 ms`，candidate cold median 为 `722.410 ms`；即使把一次命中视为零开销，candidate cold + hit 仍明显慢于两次 control cold，M0 的“不恶化超过 5%”门禁失败，不能声明 synthetic 性能优势。该 artifact 如实记录 `git_worktree_dirty=true`，不构成 clean/frozen 证据。

受限 P4a 已在 `PreparedRiskPlanning.execute_four_layer_temporal_shadow()` 和 orchestrator `scripts/winter_p2_shadow.py` 中实现；control/candidate 使用独立 scratch coordinator/store，同一 lease 前后复核 committed query/content/commit，`production_published=false`，不写正式 latest 或 replanning baseline。Winter prepare-only 构件 `/root/my_project/.runtime/experiments/winter-c-p2-monotonic-shadow-20260825-prepare-r1/` 对 145 帧、31×11、节点 `[5,7]→[26,2]` 的正式输入通过。第一份非 prepare 构件 `...-r1/` 暴露 runner 未传 `request` 的 harness 缺陷，已修复并增加回归测试，不作为算法证据；修复后的 `/root/my_project/.runtime/experiments/winter-c-p2-monotonic-shadow-20260825-r2/` 在约 `674.463 s` 后由候选触发 `TemporalSearchLimitExceeded(queue=50000)`，峰值 RSS `229176 KiB`，未产生 candidate plan set、12-route integrity 或任何正式发布。因此 P4a 仅为工具 `IMPLEMENTED`，M2 明确 `FAIL`；不继续三次重型重复、不放宽冻结资源上限、不改变默认 planner。

**P2.1 实施与证据（2026-08-25 02:32 +08:00）：** 已新增内部、未公开导出的 `control_trace_reuse.py`，并在 `TimeDependentAStar._plan_traced()` 中以显式 opt-in 方式记录首次 goal pop 前的成功 OPEN/label 写入。生产证书只保留有序二进制 rolling digest、写入/替换计数、全历史 elapsed/risk envelope、source route digest 和 identity/seal，额外内存为 O(1)；逐事件历史只在测试 observer 中启用。身份绑定起终点、出发时间、objective、RiskIdentity、外部 window/content/revision/generation、grid/model/config/evaluator、bucket/sample/heuristic/search limits；证书终止状态、取消、篡改、约束放宽、身份变化和 transient write 越界全部 fail-closed。默认 `plan()` 搜索顺序和正式公开接口保持不变。C ingress 与 orchestrator Winter runner 均增加显式 `control_trace` shadow 模式：只允许 full→main 同 goal 尝试 reuse，rolling/executable 和不同 goal cold control，sidecar 报告真实 hit/miss，仍不发布。

clean M0 首批构件 `/root/my_project/.runtime/experiments/c-p21-control-trace-reuse-20260825-clean-r5/` 固定 C `9ab88298`、两个 synthetic profile、`R=1/4`、每格 10 次；40/40 语义、trace hit 和零新增搜索通过，但 5×7×7、R=4 的 trace-source overhead median 为 `5.98%`，因此该批严格保留为 `FAIL`。开始确认前预先声明使用同提交、输入、阈值和串行协议再增加 20 次，并只按两批 pooled 30 样本判定；确认构件为 `/root/my_project/.runtime/experiments/c-p21-control-trace-reuse-20260825-clean-confirm-r6/`。pooled 120/120 语义通过，9×13×13 R=1/4 的 total median 改善为 `49.31%/78.85%`、overhead 为 `1.95%/2.38%`；5×7×7 为 `46.67%/77.53%`、overhead 为 `3.45%/4.93%`，聚合门禁通过。首批失败不能被确认批覆盖；其波动说明 overhead 结论贴近 5% 边界。共享进程 M0 的 RSS 只记录为 `NOT_MEASURED`。

clean M1 构件为 `/root/my_project/.runtime/experiments/c-p21-bc-coupling-m1-20260825-clean-r4.json`：固定 C `9ab88298`，复用已有本地 B formal-grid 文档，baseline 16×7 与 medium 31×11 均为 78 帧；每 profile 5 次、control/candidate 独立进程、奇偶轮交替、sample cache off。10/10 paired case 语义、`HIT_TRACE_EQUIVALENT` 和零新增 expansion/edge evaluation 通过；baseline/medium 的 paired improvement median 为 `48.86%/49.87%`，median peak RSS ratio 为 `1.000/0.989`，通过 M1 的 median ≥15% 门禁。该 benchmark 仍是本地 research input、`formal_ingress_used=false`，属于 clean `EXPERIMENTAL_PASS`，不等于正式 Winter 或生产默认证据。

M0 r1 暴露 JSON/SHA 热路径 overhead 15%–20%，随后改为固定网络字节序 rolling carrier并移除生产态逐状态镜像；r2/r3/r4 是实现演进或旧 dirty 证据。M1 r1 因 benchmark 不能序列化 `CostBreakdown` 而全量 harness FAIL，r2 又暴露 control-only RSS polling thread 的计时不对称；r3 是旧 dirty 研究证据，clean r4 才作为当前 M1。P2.1 Winter M2 已按上文完成并因单个 cold 单元回归判 `FAIL`；本轮 2.2.2、P3 不同 anchor 和默认启用均未执行。

## 9. 接口、合同与构件规则（2026-08-24 20:52 +08:00）

**保持不变的正式接口：** `bc.risk-frame.v2` 输入身份、UTC 时间语义、`cd.route-plan.v2` 与四层 v3 输出 schema、hard-mask/coverage/failure 语义、route/layer semantic digest、generation/revision fence、D 只读消费边界。

**允许在 C 内部新增：** `TemporalLabel`、reference oracle、reusable search session、certificate record、control/candidate benchmark runner、内部诊断 sidecar。内部 sidecar 不改变 C→D 字段，不进入已有业务语义 digest；实验身份必须与正式身份分开。

**需要提案的变化：** 新增非均匀网格或等待动作的正式语义、改变 RiskFrame 聚合/来源字段、改变输出 schema、让 D 消费新的必选字段、把研究 sidecar 变成正式输入，均属于合同变化；按治理模板建立提案并由相应 owner 审批。没有提案批准，候选实现只能在 C 内部实验。

**公平性规则：** control 和 candidate 必须使用同一 RiskFrame content digest、同一窗口/generation/revision、同一网格/起终点/出发时间、同一船模和配置、同一资源预算。候选不得通过降低采样点、放宽风险阈值、截断路线或跳过失败来换取时间。

## 10. 可重复 benchmark 与验收规范（2026-08-24 20:52 +08:00）

| 阶段 | 固定输入 | 对比 | 建议重复 | 用途 |
|---|---|---|---:|---|
| M0 synthetic | 5×7×7；现有 9×13×13 profile | control A*、candidate、reference oracle | 预热 1 + 计时 10 | 反例、digest、确定性和快速回归 |
| M1 medium | 16×7 与 31×11、固定 RiskFrame/配置 | control/candidate，必要时 cache on/off | 至少 5 次 paired | 扩展性、搜索开销和 RSS |
| M2 Winter formal | 31×11、145 帧、4×3、12 路线 | 正式 ingress control/candidate | 筛选 2 + 正式至少 3 次 | 证明正式入口收益，不混用 synthetic |
| M3 RC1 | 冻结 RC1 route/artifact | 只做 digest/合同回归 | 以冻结值为准 | 防止覆盖或重新解释基线 |
| M4 fine/adaptive | 仅 M1/M2 证明必要后立项 | 另行批准的候选 | 另行冻结 | 不提前宣称可扩展 |

**每个 cell 必须固定并记录：** Git SHA、Python/uv lock、CPU/线程环境、配置 digest、输入文件 SHA256、RunContext、RiskFrame content digest、planner/control/candidate identity、资源上限和执行顺序。重型任务一次只跑一个，保留日志和原始 artifact。

**必记指标：** 总 wall time、planner compute time、分层/目标时间、首次可行解时间、expanded/generated/queue peak、边评估和 risk sample 次数、cache hit/miss/eviction、峰值 RSS/swap/OOM/timeout、路线距离/ETA/风险/置信度/source IDs、route semantic digest、证书状态和回退原因。

**语义验收：** 对语义保持的 LTCR-TDA*，waypoints、端点、ETA、速度、成本、风险、confidence、source IDs、hard/coverage/failure 语义必须与 control 一致；`compute_ms`、expanded 计数、planner version 和实验 plan ID 可不同，不应硬塞进业务语义等价判断。重复运行的离散计数和 route digest 必须稳定。

**建议晋级阈值（实施前冻结）：**

- M0：无正确性回归；median 时间不恶化超过 5%；RSS 不超过 control 的 1.10 倍，10 次结果一致。
- P2.1 M0 专项：`R=1` 与 `R=4` 重复收紧查询中，trace source 只运行一次；命中零新增 expansion/edge evaluation，单次 trace 开销不超过 5%，总 median 至少改善 20%。
- M1：median wall time 至少改善 10%（正式晋级建议 15%）；P95 不恶化超过 5%；无硬失败；完整覆盖两个规模。
- M2：4×3、12/12 route integrity、digest/资源/确定性全部通过；至少 3 次串行运行；median 相对 control 至少改善 15%，且任一 layer/objective 不超过 5% 回归；无 swap/OOM/partial publication。
- 若候选改变路线而非保持语义，必须另设相对 reference oracle 的可行性、目标代价、风险和约束验收，不得套用 digest 完全相同门禁。

## 11. 风险、资源预算与回滚（2026-08-24 20:52 +08:00）

| 风险 | 预防/检测 | 回滚动作 |
|---|---|---|
| 证书误判或时间桶不安全合并 | P0 temporal oracle、同桶多标签反例、输入 digest fence | 关闭 candidate，恢复独立 baseline A* |
| ETA 不收敛或采样时刻不一致 | 残差、周期、最大迭代、最终重采样和显式错误 | 拒绝该边/该候选，保留 control 结果；不得静默使用旧样本 |
| 复用跨 RiskFrame/generation/revision | session key 与 digest 双重校验 | 丢弃 session，重新独立规划 |
| 标签/缓存导致 RSS 增长 | 每次记录 RSS、swap、标签上限和 eviction；默认关闭实验开关 | 关闭复用/缓存，回到 control；OOM/partial 直接失败 |
| formal 12 路线不完整 | route integrity、hard/coverage/failure gate | 不发布 candidate，保留 control 或明确失败 |
| 误把外部算法/不同输入当公平 baseline | 共享 C 边评估器和冻结输入；记录许可证 | 取消不可比结果，不写入性能结论 |
| 跨包合同被无意修改 | 变更前检查 ownership registry 和提案状态 | 停止合并，按合同提案流程处理 |

候选默认 `OFF`，采用 control/candidate 影子双轨；任何硬契约错误、digest 漂移、非确定性、OOM、swap、timeout、partial publication 或正式门槛失败，立即回到当前 baseline。RC1/frozen artifact 不覆盖、不重写、不以候选结果解释旧结果。

## 12. 当前决策、开放问题与后续更新（2026-08-24 20:52 +08:00）

**已决策：**

1. 当前 A* 保留为正式 control，不能宣称相对于常规 A* 已有性能优势或一般问题全局最优性。
2. 第一改进方向采用 LTCR-TDA*，先做正确性语义硬化，再做证书化 session/full-anchor 复用；不先做 shared NAMOA* 或自适应网格。
3. 上一版 2.2.2 自适应/非均匀网格方向保留但暂缓，只有固定网格瓶颈和合同必要性均有证据时才启动。
4. 所有性能结论必须来自同输入、同边评估器、重复运行的 paired benchmark；单次 Winter wall time 只能称为工程观察。
5. 重大算法选择、跨包合同变化和默认开关变化，需要在本文档记录决定、证据、owner、commit 和回滚方式；跨包事项同时走正式提案。
6. P2.1 原始 Winter M2 因 `rolling_0_24h × fastest` 回归 `5.94% > 5%` 判 `FAIL`；M2E 完成 cold-path 对称化，M2F/M2G 因 host swap 证据不足停止。M2H 在连续零 host swap 的受控环境中完成双窗口 screening：holdout 正式 M2 `PASS`，development 正式 M2 因 `executable_0_6h × low_risk` 与三个 rolling 单元超过 5% 门禁而 `FAIL`，故 P2.1 总体仍 `FAIL`，candidate 保持默认关闭。M2I 因果诊断后停止 P2.1 改进。
7. P3.2 已完成 SMO-A* 双窗口 M1：路线、P95 和资源语义通过，但 holdout/development 的 cache hit rate 与 RSS 联合门禁失败；SMO candidate 不进入正式 M2，P2.1 M2 的 FAIL 记录和构件原样保留。
8. P3.3 只做 synthetic exact-key 轻量诊断，不改变时间语义或缓存键。medium profile 命中率 `47.87% < 50%`，且时间变体仅 4 个 unique key，未形成可安全归因于时间桶的修复路径；SMO 标记 `DEFERRED`，ARA* 维持 `M0_FAIL/DEFERRED`。不再启动 P3.4 或 Winter 重型复测，除非另行建立新的可审计计划和门禁。
9. （2026-08-27）**P2.1-M2J/M2K 复测完成，P2.1 收口为 `MEASUREMENT_INCONCLUSIVE`。** 代码核查确认 cold 单元在候选中已与控制走同一 `plan()` 路径（无残留旁路稳定开销）；M2J 独立复测（`winter-c-p21-m2j-measurement-protocol-20260827-r1`）baseline `rolling_0_24h × fastest` candidate-first `+1.86%`、treatment `+0.83%`，均 ≤5%，但 order-gap 门禁因 control-first 档负回归被绝对差误判 FAIL；M2K 对称预热复测（`winter-c-p21-m2k-symmetric-warmup-20260827-r1`）baseline candidate-first `+25.28%` 由单样本 wall-clock 抖动驱动，短复测（`winter-c-p21-m2k-short-baseline-20260827-r1`）收敛至 `-3.72%`（±5% 内）佐证该解释；根因是 candidate-first 档 n=2 中位统计效力不足，非算法缺陷。P2.1 记 `MEASUREMENT_INCONCLUSIVE`、不追加复测成本。**P3 SMO-A\* 与 ARA\* 保持 `RETIRED`**：二者均不具备晋级证据（SMO +12.71%/hit 14.3%/RSS 3.3×；ARA\* small 首解 +4.14%），停止投入，算力转向 P0.1 有限域 FIFO 主线。

10. （2026-08-28 00:40 +08:00）**测量协议 order-gap 口径修复落地 + 正确性债 C-ALG-03/03B/04 收口。** ① `winter_p2_shadow.py` 诊断模式 order-gap 门禁改为 **sign-aware**：两档中位**异号不算 gap**（负回归=候选更快，非"顺序不一致"，避免 M2J baseline 26.32pp 假 FAIL），同号才取绝对差；新增 **candidate-first 档需 n≥3** 样本充分性检查（n<3 判 FAIL 并标注 `candidate_first_sample_sufficient=false`，消除 M2K baseline n=2 单样本抖动导致的 `+25.28%` 假回归）；保留原始 `order_gap_percent_points` sidecar 可审计。**这是未来任何候选性能门禁的前置修复，不放松 5% 冻结阈值、不碰正式 M2 gate**。② 正确性债：C-ALG-03 在 `time_dependent_astar.py` 提供 `eta_refinement_policy` 注入（默认 `None` 保持固定两轮，正式 route digest 不变；注入时走 `eta_refinement.refine_eta` damped 阻尼迭代，含周期检测/终值重采样/fail-closed，拒绝异常 `_RejectedEdge`/`RiskCoverageError`/`UnnavigableSpeedError` 在 `invalid_operator` 时恢复传播）。**C-ALG-03B** 新增 `method="bounded"` 区间收缩法（bracket 符号翻转检测 + 二分，bracket/bisection 独立预算）：在真实场阻尼迭代发散的振荡场景下收敛，无符号翻转时 fail-closed 抛 `no_fixed_point`。③ C-ALG-04 在 `CostModel.lower_bound` 补 admissible 数学论证 + zero-heuristic Dijkstra reference oracle 对照测试。**已知限制**：正式 control 默认仍是固定两轮（不收敛即无保证），真实 Winter 场 ETA 固定点在阻尼迭代下被观测到发散（诊断见 §13 ADR），是否切换默认策略待 P0.1-M1.5 真实资格审计数据统一决策。**P3 SMO-A\* 与 ARA\* 保持 `RETIRED`**，算力转向 P0.1 有限域 FIFO 主线。

**开放问题：** 非 FIFO 情形是否进一步采用 label-correcting；ETA 迭代的保守误差模型；P3 anchor 证书的浮点容差；正式 control 是否切换 bounded 收敛策略（待 P0.1-M1.5 真实输入数据）。（已收口：P2.1 cold control 旁路是否残留稳定开销 —— 2026-08-27 代码核查判定**否**，cold 单元在候选中已与控制走同一 `plan()` 路径；`rolling_0_24h × fastest` 的 `5.94%` 回归归因为测量伪影，M2J/M2K 复测与短复测收敛证据见「P2.1-M2J 冷路径代码核查与测量协议提案」与「P2.1-M2K 对称预热收口」章节，最终以统计效力不足收口为 `MEASUREMENT_INCONCLUSIVE`；order-gap 口径已随决策10修复。）Winter 已证明每次自然产生 3 个 full→main 零搜索 hit，并确认约 48% 总 wall-time 改善，但单元硬门禁失败意味着不能宣称生产级稳定加速。独立 FIFO 分类器和 exact 标签安全支配仍是后续候选（现由 P0.1 主线承接）。任何资源或标签语义变更必须先记录新的实验身份与正确性回归，不能用“全局最优”“稳定加速”或“生产级优势”代替证据。

### P2.1-M2D cold-path 诊断实施记录（2026-08-25 17:49 +08:00）

- C shadow-only 计时已拆分为 `pre_ms`、`planner_ms`、`post_ms`，并增加 `trace_context_present`、`trace_reuse_used`、状态计数和身份摘要；正式 `plan()`、`execute()`、BC/CD 合同、默认开关和发布路径未改变。
- runner 已增加严格的 `rolling_0_24h × fastest` paired timing decomposition 与统计 helper，输出明确标记 `diagnostic_only=true`、`formal_gate_verdict=NOT_APPLICABLE`；旧 M2 构件未修改。
- 新 preflight 构件：`/root/my_project/.runtime/experiments/winter-c-p21-cold-diagnostic-20260825-r1/cold-path-diagnostic.json`。1 次独立 control/candidate paired run 中，route digest、expanded `670`、edge `5310` 一致；candidate `2175.45 ms`、control `2095.92 ms`，回归 `3.79%`，其中 `planner_ms` 差异约 `79.48 ms`，`pre/post` 旁路差异接近零。
- 该结果只有 1 个样本，仅属于 `PRELIMINARY_OBSERVATION_ONLY`，不能区分测量方差与稳定 planner 状态开销，不能改写 M2 `FAIL`，也不能形成生产性能优势声明。后续若继续取样，必须新建诊断 experiment identity；不得以此自动升级为正式 M2 复跑。

### P2.1-M2D 与 Winter A→B→C 复核完成记录（2026-08-25 19:55 +08:00）

本节是本轮执行结果的当前记录；依据治理标准的 SSOT、时间戳标题、证据分级和历史构件保留要求追加，不重写旧的 M2 失败记录。

**1. cold-path 目标诊断（诊断性，不是正式 M2）**

- 新增可复现脚本：`arctic_route_orchestrator/scripts/winter_cold_target_diagnostic.py`。它只运行 `rolling_0_24h × fastest` 的 paired cold control/candidate 子进程，记录 planner/旁路耗时、expanded/edge、route digest 和 trace context；不调用正式发布路径。
- 10 对样本构件：`/root/my_project/.runtime/experiments/winter-c-p21-cold-target-diagnostic-20260825-r6/cold-path-diagnostic.json`。
- 10/10 对路线 digest、expanded `670`、edge `5310` 一致；candidate 状态为 `COLD_CONTROL` 且 trace context 存在。candidate 回归中位数 `2.4465%`，最大 `7.9033%`，最小 `-4.1994%`；planner 差值中位数约 `47.94 ms`。
- 结论：更支持局部运行方差或 shadow/cold 边界的小额开销，不支持核心算法架构失败；但仍不能宣称该单元稳定满足 `5%` 门禁。
- 证据等级：`PRELIMINARY_OBSERVATION_ONLY`；`diagnostic_only=true`、`formal_gate_verdict=NOT_APPLICABLE`、`formal_m2_verdict_unchanged=FAIL`。不得用该结果放宽阈值、删除失败单元或启用 candidate。

**2. 新实验身份上的 A→B→C 闭环复核**

- A 复用当前唯一完整的严寒样本：`a-bundle-a2146dd0adbaa7db77a6beb7`，bundle digest `a2146dd0adbaa7db77a6beb7c818e975888600fb31236901fd4af2092069fb71`，2026-02-15T00:00Z～2026-02-21T00:00Z，12 类数据、1212 records。文件 SHA-256 为 `e28bcca682bb1047381d96d574d42c927f28bf5cd26c363f19fff1fff21c3a2f`；identity 以 bundle/RunContext/B commit 的当前 `00:00Z` 结束时间为准，旧文档中的 `12:00Z` 仅作为历史描述保留。
- B 在隔离输出 `/root/my_project/.runtime/experiments/winter-b-validation-20260825-replay-medium-r2/` 重新生成正式 `bc.risk-frame.v2`：145 frames，commit/content digest `01275645ad90c43874511e593958ca45e0f063e63e82b0547398711a00ec0fde`，`FORMAL_VALIDATED`。模型仍为 `demo_unvalidated`，没有科学校准结论。
- C 在隔离输出 `/root/my_project/.runtime/experiments/winter-c-validation-20260825-replay-medium-r2/` 完成 `cd.four-layer-route-plan-set.v3`：12/12 routes、integrity `PASS`、planning wall `363.458670 s`、peak RSS `168364 KiB`、published 仅限实验目录。与原 Winter C formal 输出按路线业务字段比较，`business_semantic_mismatch_count=0`。
- 同一新 B identity 的一次 P2.1 shadow：`/root/my_project/.runtime/experiments/winter-c-p21-shadow-replay-20260825-r1/`，`status=PASS`、`passed_cases=1`、semantic/reuse/swap/resource 边界通过，但因仅 1 次重复，M2 与 screening 均为 `NOT_EVALUATED_INSUFFICIENT_REPETITIONS`；`formal_latest_store_written=false`、`frozen_artifact_written=false`、`production_published=false`。
- 首次 B 重放曾错误传入仓库根目录而非 A 的公开 `data/` 根目录，失败目录 `/root/my_project/.runtime/experiments/winter-b-validation-20260825-replay-medium/` 保留；修正为 `work_package_a/data` 后通过。这是执行参数纠正，不是数据质量或合同失败。
- 为使 B 的锁文件反映 A 当前 acquisition extra，本轮在 B 执行离线 `uv lock`，仅更新 B `uv.lock`；`uv lock --check` 已通过。未修改 A→B→C 合同、风险公式、C planner 或默认开关。

**3. 已有样本盘点与下载决策**

- 可复现盘点脚本：`arctic_route_orchestrator/scripts/inventory_winter_samples.py`；构件：`/root/my_project/.runtime/experiments/winter-sample-inventory-20260825-r1/sample-inventory.json`。
- 盘点到 4 个完整 12 类 bundle：1 个当前可复用 Winter、1 个历史 superseded Winter、2 个 August summer control。当前没有第二个独立且已通过 A 门禁的严寒窗口。
- 因此本轮不下载、不覆盖、不删除任何已有数据；当前闭环直接复用 active Winter。新增严寒窗口仍是下一阶段的必要验证，不把同一窗口的重放计为独立样本。
- 后续采集前必须先完成缺口查询、预注册恶劣条件分层和独立 ScenarioRunGroup/holdout 设计，并修正 `/root/my_project/work_package_a/.cdsapirc` 权限至 `0600`；代理变量已存在，但 ecCodes/凭据路径必须按进程注入，不能写入日志。若无真实缺口，不重新下载已有完整类型。

**本轮状态与下一步**

- `P2.1-M2`：仍为 `FAIL`，冻结的 5% 单元门禁、总体收益门禁和“任一单元失败则整体失败”规则不变；candidate 继续默认关闭、非发布。
- 本轮 A→B→C 是同一严寒 identity 的 `REAL_E2E_PASS` 复核，不是新增环境样本，也不是独立外部有效性证明。
- 下一轮先执行：至少 2 个独立 Winter `ScenarioRunGroup`（含 1 个 holdout）的 A→B→C 复核；样本纳入规则先于路线结果冻结；每个新 bundle 使用新的 RunContext/B commit/C experiment identity。P3、2.2.2、P5 继续延期，除非后续证据证明合同或固定网格是必要瓶颈。

**本次更新记录：** 将旧的“实现审计报告”重整为现状 + 证据 + 正确性边界 + 改进方案 + 分阶段计划 + benchmark/回滚门禁。P0/P1/P2 达到 `UNIT_PASS`；P2.1 在 Winter formal 取得约 48% 总耗时改善但因单元硬门禁失败判 M2 `FAIL`，M2I 诊断后停止。P3 SMO-A* 已实现、通过 12 项正确性测试并在 Winter holdout 基准取得 +12.71% 改善和 100% 路线一致，但未达 15% 改善和 50% cache hit rate 目标，保持 `EXPERIMENTAL`。所有候选默认关闭、非发布。后续修改直接在本文档对应章节更新，不再创建同主题文档。

### P2.1-M2E 冷路径对称化与诊断收口记录（2026-08-26）

本轮已完成实现修正和增强测试，但未进入正式 M2 重跑。旧正式 M2 `FAIL` 及其原始构件保持不变，candidate 仍默认关闭、非发布。

**实现变更。** C 的 `control_trace` shadow candidate 现在在同一 candidate 轨道中使用一个 planner：full trace、full→main 零搜索复用、rolling/executable cold fallback 共用该轨道的只读边几何缓存；control 与 candidate 仍使用相互独立的 planner。新增 shadow-only `edge_geometry_cache_before/after/delta` 字段，不改变 `RiskFrame`、`RoutePlanV3`、A→B/B→C 合同或正式 `plan()/execute()` 路径。orchestrator runner 增加进程 `VmSwap`/内核 swap 观测、明确的 `NOT_MEASURED` 状态，并将隔离 worker 固定到同一可用 CPU 以降低配对测量抖动；5% 冻结门槛未修改。cold diagnostic runner 现支持 rolling/executable、三目标和不同窗口 anchor。

**代码与合同测试。** C `UV_OFFLINE=1 make check`：`275 passed`；orchestrator unit tests：`100 passed`（仅已有 ecCodes warning）；runner/C 相关 Ruff、py_compile 和 `git diff --check` 均通过。新增测试覆盖单 planner 生命周期、control/candidate 隔离、cache 增量连续性、full→main 命中零 cache 工作，以及 swap 测量缺失时强制失败。

**双窗口定向诊断。** 使用已保留的两套含潮总流窗口，未下载新数据、未写 production/frozen/latest：

- holdout `2026-02-22～02-28`：rolling/fastest（CPU 固定后）10 对中位回归 `+1.58%`；executable/fastest `-0.98%`、low-risk `-2.62%`、recommended（CPU 固定后）`+1.33%`；各目标均 `10/10` route digest、expanded、edge 一致。
- development `2026-03-22～03-28`：rolling/fastest `-1.81%`；executable/fastest `+2.10%`、low-risk `+4.67%`、recommended `-1.62%`；各目标均 `10/10` route digest、expanded、edge 一致。

构件分别保存在 `.runtime/experiments/winter-c-p21-cold-parity-*`。结果继续属于 `diagnostic_only`，不能改判 M2；development recommended 诊断中有两对 host `pswpin` 增长（进程 `VmSwap` 增量为0），说明当前环境尚不具备无 swap 增长的正式复测条件。由于一个定向单元仍超过本轮预注册的3%诊断余量，且 swap 证据未完全满足 G3，本轮按停止规则不启动 screening 或正式 M2。

**数据与错误中间物。** 两个 total-current bundle、A→B→C 输出、旧失败证据和清理 ledger 均保留；此前一次将 `recommended` 请求误标为 `fastest` 的两对诊断目录已移入系统回收站，不作为证据。detided 不重新引入。

**当前结论与下一步。** 代码层面的 cache 生命周期不对称已消除，冷路径的 route/search 工作量始终一致；但尚未证明所有正式单元在冻结5%门禁下稳定通过。因此 P2.1 M2 仍为 `FAIL/未重入`，P3、2.2.2、P5 继续延期。下一次执行必须先获得 clean/frozen 的相关代码与输入 identity、连续零 swap 证据，再按 holdout→development 顺序各做2次 screening；screening 双窗口均通过后，才允许各窗口4次交替正式重复。

### P2.1-M2F 门禁加固与 screening 停止记录（2026-08-26 15:42 +08:00）

本轮按上一节条件式方案执行了“门禁加固 → smoke → screening”阶段；由于 screening 首个窗口即触发冻结资源门禁，未进入 development screening 或 formal M2。旧正式 M2 `FAIL`、旧失败构件和 candidate 默认关闭状态均保持不变。

**门禁实现与冻结身份。** Orchestrator 新增本地提交 `a9b59a7c0ea66e4a95454bb6f0e27a1aefed4fae`（swap/CPU/cache 证据门禁）和 `93e61b48f5f1555c83b86c297125e946a5ee94fc`（诊断 experiment identity 绑定实现 SHA）；C 保持 `df77e57fe108f9f7545be4dad407676a6ae889a7`，A 保持 `4c30ace90935fec9371ca02f654b7ab1554183fa`，B 保持 `c861be96c56e80520c7416e0e77c417ebd239fdd`。运行前 C、Orchestrator、A、B 工作树均 clean；C `uv.lock` SHA256 为 `8893cb8387825ca4890ed808f4a98b02ba938337752971dacc3e77f859164f22`，Orchestrator `uv.lock` SHA256 为 `0fc5cdbc4e94799d1ecb945c16a92b7d12f532d2447f69bae4e4578d0019ce01`。本地提交未 push。

门禁现在要求：host `pswpin/pswpout` 两项完整可测且前后不回退、进程 `VmSwap` 增量为0；worker 成功固定到单一 CPU 且 control/candidate affinity 一致；cache before/after/delta 必须存在并可验证，缺失值不能按相等处理。诊断 manifest 的 experiment ID 同时绑定脚本与 runner 实现 SHA，避免不同实现复用同一身份。

**验证结果。** C `UV_OFFLINE=1 make check` 为 `275 passed`；Orchestrator 相关测试为 runner 专项 `24 passed`，排除既有大型 formal fixture 后为 `117 passed, 1 warning`；Ruff、py_compile、`git diff --check` 均通过。完整 Orchestrator 测试中的 `tests/integration/test_formal_run.py::test_formal_archive_to_b_to_c_and_six_hour_replan` 单项运行超过 `779.01 s` 仍未结束，因其为既有重型 fixture 被人工中断，不能记为全量 PASS，也未发现本轮门禁代码错误。

clean smoke 构件 `/root/my_project/.runtime/experiments/winter-c-p21-m2e-gate-smoke-20260826-r3/` 使用 holdout 含潮总流输入，单目标单重复 `rolling_0_24h × fastest` 通过：route/expanded/edge/cache 均一致，CPU affinity 一致，swap 完整可测且零增量，candidate wall-time 回归 `-0.63%`；manifest 为 `PASS`、`diagnostic_only=true`、`formal_gate_verdict=NOT_APPLICABLE`，未写 formal/latest/frozen/production。

**screening 停止。** 空闲20秒预检的 host swap 增量为0；随后启动 holdout screening（2次、alternate、isolated、4×3）。第一个 control worker 执行期间 host `pswpin` 从 `1190563` 增至 `1190572`，停止后为 `1190583`，违反 G3 的零增量要求。构件 `/root/my_project/.runtime/experiments/winter-c-p21-m2e-screening-holdout-20260826-r1/` 保留为 `status=PREPARED` 的中止证据，`cases.jsonl` 为空，未发生任何正式或生产发布；不计入 screening 样本，也不覆盖历史结果。由于冻结停止条件已触发，development screening、formal M2 和任何择优重跑均未执行。

**数据与后续门禁。** 本轮未下载或修改 A/B 输入；两个 `total_with_tide` Winter bundle、B risk-store、RunContext、ExecutionSpec、旧正式失败证据和清理 ledger 均保留，未重新引入 detided。下一次只有在可证明连续零 host swap、进程 swap 为0且 CPU/cache 证据完整的环境中，才可从 holdout screening 重新开始；若资源环境仍无法满足，应改用独立无 swap/资源隔离执行环境，而不是放宽阈值。P3、原方案2.2.2、自适应网格和 P5 继续延期。

### P2.1-M2G 资源资格复核与隔离 screening 停止记录（2026-08-26 16:10 +08:00）

本轮按 M2G 方案执行“资源资格复核 → 新身份 prepare → 隔离 screening 尝试”。由于宿主机 swap 计数在空闲预检和隔离 worker 期间均增长，未形成可计入的 screening case；development screening 和正式 M2 按停止规则未启动。旧正式 M2 `FAIL`、所有有效含潮总流数据和历史诊断构件均保持不变，candidate 继续默认关闭、非发布。

**代码与身份检查。** C `UV_OFFLINE=1 make check` 为 `275 passed`；Orchestrator runner 专项为 `24 passed`，排除既有超长 formal fixture 的套件为 `117 passed, 1 warning`；Orchestrator 相关 Ruff 与 `py_compile` 通过。运行前 C、Orchestrator、A、B 工作树均 clean；本轮冻结 C `52117a8496db2778bd79a2704674ff02c11f44fd`、Orchestrator `93e61b48f5f1555c83b86c297125e946a5ee94fc`、A `4c30ace90935fec9371ca02f654b7ab1554183fa`、B `c861be96c56e80520c7416e0e77c417ebd239fdd`，C/Orchestrator `uv.lock` SHA 分别为 `8893cb8387825ca4890ed808f4a98b02ba938337752971dacc3e77f859164f22` 与 `0fc5cdbc4e94799d1ecb945c16a92b7d12f532d2447f69bae4e4578d0019ce01`。

新建的 prepare-only 身份构件为：

- holdout：`/root/my_project/.runtime/experiments/winter-c-p21-m2g-screening-holdout-20260826-r1/`，`experiment_id=winter-p2-shadow-v3-5fdf635db7971254`；
- development：`/root/my_project/.runtime/experiments/winter-c-p21-m2g-screening-development-20260826-r1/`，`experiment_id=winter-p2-shadow-v3-7494aac46b35b286`。

两者均绑定新的输入文件 SHA、实现 SHA、仓库 SHA 和锁文件 SHA，状态为 `PREPARED`，不代表 screening 通过。

**资源复核结果。** 当前主机最多两轮连续预检均失败：第一轮空闲约 20 秒后 `pswpin` 至少增加 `4`，继续观察约 40 秒累计增加 `8`；第二轮等待后约 20 秒增加 `7`。进程自身 `VmSwap` 始终为 `0 kB`，但 G3 要求的 host `pswpin/pswpout` 双计数零增量未满足。

随后使用 systemd cgroup `MemoryMax=4G`、`MemorySwapMax=0` 启动 holdout screening（`isolated`、`alternate`、2 次、4×3）。unit 内 `memory.swap.current=0`，但首个 worker 运行约 20 秒期间 host `pswpin` 从 `1191712` 增至 `1191720`（`+8`），故立即终止；证据构件为 `/root/my_project/.runtime/experiments/winter-c-p21-m2g-screening-holdout-20260826-r2/resource-gate-abort.json`。该目录 `cases.jsonl` 为 0 行，manifest 仍为启动前 `PREPARED`，screening 结果记为 `NOT_EVALUATED_RESOURCE_GATE`，没有路线、性能或 M2 样本。

**数据、发布与清理。** 本轮未下载、修改或重新生成 A/B 数据；holdout/development 两套 `total_with_tide` bundle、B risk-store、RunContext、ExecutionSpec、detided 退役 ledger 和旧失败构件均保留。新建构件仅为小型 manifest、输入身份、endpoint mapping、空 cases 和资源停止 sidecar；没有错误 payload 或大体积中间数据可清理，故不删除可审计构件。没有写入 formal latest、frozen、production 或 presentation store，也未引入 detided。

**当前结论与后续条件。** P2.1 M2 仍为 `FAIL/未重入`；本轮只能证明“当前宿主机即使使用无 swap cgroup 也无法满足全局 host swap 门禁”，不能证明算法性能失败。下一轮必须在宿主机连续零 swap 且完整 CPU/cache 证据成立后，或在能同时隔离并观测零 host swap 的外部 runner 上，从 holdout 2 次 screening 重新开始；两个窗口 screening 全部通过后才允许各窗口 4 次正式重复。P3、原方案 2.2.2、自适应网格和 P5 继续延期。

### P2.1-M2H 条件式 screening 与双窗口正式复测记录（2026-08-26 18:45 +08:00）

本轮按 M2G 之后的条件式方案执行“资源资格确认 → holdout screening → development screening → 条件式双窗口正式 M2”。本轮不下载或重建数据，不改变 5% 性能门禁，不写入 formal latest、replanning baseline、frozen artifact 或 production；M2H 的 holdout 通过不能覆盖 development 失败或历史 M2 失败。

**执行边界与可重复性。** 使用现有两套完整严寒 `total_with_tide` 输入：holdout bundle 文件 SHA-256 为 `ae08becffe148030b9c5de9f023214cc558821227a1994855f233308e642b9cd`，risk content digest 为 `115ad3ab6d7034fabc9428f91c14099b02dff8bb2443569a8d3947187fbb5ff9`；development bundle 文件 SHA-256 为 `83392d7085b096f4b532f349ae96a2fd838784a49d5ef5d0b05af9fe17df420b`，risk content digest 为 `bdfd7964df96ffcad7dd78d9830394a0a91d7fbbfde16c0649d2ba2fb68a00ab`。两套输入均为 145 帧、含潮总流、独立 RunContext/B commit，detided 没有重新引入。C 与 orchestrator 工作树在运行前 clean，分别固定在 `bd70fff224ad5414d081f96afaef841f07ba0adf` 与 `93e61b48f5f1555c83b86c297125e946a5ee94fc`；C `uv.lock` SHA-256 为 `8893cb8387825ca4890ed808f4a98b02ba938337752971dacc3e77f859164f22`，orchestrator 为 `0fc5cdbc4e94799d1ecb945c16a92b7d12f532d2447f69bae4e4578d0019ce01`。

运行使用 `control-trace`、`isolated`、`alternate`，每个 worker 固定同一 CPU；systemd cgroup 设置 `MemoryMax=4G`、`MemorySwapMax=0`、`OOMPolicy=stop`。WSL 本地 swap 已关闭，预检和四个运行阶段的 host `pswpin/pswpout` 均保持零，未出现 OOM、timeout 或进程 `VmSwap` 增长。这里的 WSL 内存状态是本机运行保障，不是项目级资源要求。

**代码与测试门禁。** C `UV_OFFLINE=1 make check` 为 `275 passed`；orchestrator `tests/unit/test_winter_p2_shadow.py` 为 `24 passed`，此前排除既有超长 formal fixture 的套件为 `117 passed, 1 warning`；Ruff、`py_compile` 和 `git diff --check` 通过。本轮未修改算法或合同代码，运行构件只绑定上述实现与输入身份。

**screening 结果（每窗口 2 次重复）。** 两个窗口均通过 screening，且均为 2/2 case `PASS`；由于重复数不足，screening manifest 的正式 M2 verdict 正确保持 `NOT_EVALUATED_INSUFFICIENT_REPETITIONS`。

| 窗口 | 构件 / experiment_id | 总 wall median 改善 | RSS median ratio | 语义/确定性/复用/CPU/swap | screening |
|---|---|---:|---:|---|---|
| holdout | `winter-c-p21-m2h-screening-holdout-20260826-r1/` / `winter-p2-shadow-v3-dd5b1449f0211e38` | `48.1504%` | `0.9953` | 全部 `PASS` | `PASS` |
| development | `winter-c-p21-m2h-screening-development-20260826-r1/` / `winter-p2-shadow-v3-bd4e7c9b939be228` | `47.7234%` | `1.0032` | 全部 `PASS` | `PASS` |

两个 screening 构件均为 shadow-only，未发布候选；其形式字段中的 trace-source formal gate 因样本数不足显示 `NOT_MEASURED`，不应与 screening 自身的 trace-source gate 混淆。

**正式 holdout M2。** 构件为 `/root/my_project/.runtime/experiments/winter-c-p21-m2h-formal-holdout-20260826-r1/`，`experiment_id=winter-p2-shadow-v3-34d7cc036fed1790`。4/4 case `PASS`，`m2_gate_verdict=PASS`；总体 candidate/control wall median 为 `165.311/321.383 s`，改善 `48.5625%`，P95 改善 `48.8010%`，RSS median ratio `0.9964`。12 路线语义与完整性、确定性、复用矩阵与 timing、CPU、swap、trace-source overhead 均 `PASS`，每轮均得到预期 `3 trace captured + 3 zero-search HIT + 6 cold`。`production_published=false`、`formal_latest_store_written=false`、`frozen_artifact_written=false`。

**正式 development M2。** 构件为 `/root/my_project/.runtime/experiments/winter-c-p21-m2h-formal-development-20260826-r1/`，`experiment_id=winter-p2-shadow-v3-5533d32e6afd91a6`。4/4 case 本身均 `PASS`，路线语义、确定性、复用、CPU、swap 和 trace-source overhead 均通过；总体 candidate/control wall median 为 `178.211/329.076 s`，改善 `45.8449%`，P95 改善 `46.1634%`，RSS median ratio `0.9993`。但冻结的“任一 layer/objective 单元不得回归超过 5%”门禁失败：

| 失败单元 | median 改善（负值为回归） | P95 回归 |
|---|---:|---:|
| `executable_0_6h × low_risk` | `-7.8513%` | `9.2793%` |
| `rolling_0_24h × fastest` | `-5.8632%` | `4.0090%` |
| `rolling_0_24h × low_risk` | `-6.2755%` | `16.0324%` |
| `rolling_0_24h × recommended` | `-6.6599%` | `18.3608%` |

其余单元通过，且总体收益门禁通过；因此该窗口的 `m2_gate_verdict=FAIL`，本轮总体仍为 `FAIL`。manifest 的 `runner_failures=1` 是 runner 对上述 M2 gate 失败的计数，不是 worker 异常：4 个案例均已正常完成，`failed_cases=0`，没有 OOM/timeout 或数据/合同错误。

**当前决策与下一步。** M2H 证明 holdout 窗口在新资源门禁下可通过，也证明 development 窗口的总体复用收益仍明显；但 development 的 objective/layer 局部回归仍是冻结硬门禁失败，不能据此启用 candidate 或宣称跨窗口生产级稳定加速。保留四个正式/筛选构件、输入 bundle、B risk-store、RunContext、ExecutionSpec、detided 退役 ledger、M2/M2G 历史失败证据和本轮资源观测，不删除可审计数据，不执行择优重跑。下一轮必须先建立新的诊断 experiment identity，针对 `executable × low_risk` 与三个 `rolling` 单元拆分 planner/旁路/缓存及系统抖动，再由本文档重新批准是否复测；不得放宽 5% 门禁、减少重复、改变 workload 或把 holdout PASS 当作总体 PASS。P3、2.2.2 和 P5 继续延期，candidate 保持默认关闭、非发布。

### P2.1-M2I 全轨迹因果诊断与消融结论（2026-08-26 22:28 +08:00）

本轮按 M2I 计划执行了“development baseline → 两个最小消融”的完整 4-layer track 诊断。运行只使用已有 development `total_with_tide` bundle 与 B risk-store，不下载或重建数据，不改变 B/C/D 合同、5% 性能门禁、正式 A* 默认路径或发布边界。所有构件均为 `diagnostic_only=true`、shadow-only；候选仍未写入 formal latest、replanning baseline、frozen artifact 或 production。

**固定身份与证据完整性。** C 固定在 `e81573da6f5d573a8eac1839609a26fb8fabc7f7`；baseline 使用 orchestrator `88e2ee41e193d1c6413ef63d4626781e7b856d6f`，两个消融使用 `b978fa3c2f848216dcfeb62e27a6f93c569bb735`。baseline 为 8 个交替顺序 case（4 control-first、4 candidate-first），每个消融为 6 个平衡 case（3+3）；三组均 `evidence_complete=true`，所有 case `status=PASS`、`failed_cases=0`，资源证据中 worker CPU 绑定一致、进程 `VmSwap` 增量为 0，WSL cgroup 峰值约 334 MB、swap 为 0。manifest 的 `status=FAIL`/`runner_failures=1` 来自诊断门禁失败，不是 worker 异常。

**结果摘要（`median_regression_percent`：正值为 candidate 回归，负值为 candidate 更快）。**

| profile / 构件 | 重复与复用计数 | 四个焦点单元 overall median | 失败门禁与最大焦点顺序差 |
|---|---|---|---|
| baseline：`winter-c-p21-m2i-diagnostic-development-20260826-r1/` | 8；`3 trace + 3 HIT + 6 cold` | `+0.5645% / -3.2358% / -3.0654% / +0.2366%` | overall 与每顺序 median 通过；`rolling × recommended` 顺序差 `5.9400pp`，`focus_cell_order_gap_le_5pp=FAIL` |
| `force-main-cold`：`winter-c-p21-m2i-ablation-force-main-cold-20260826-r1/` | 6；`3 trace + 0 HIT + 9 cold`（按消融定义） | `-3.5591% / -5.2135% / -3.1795% / -3.0692%` | overall/per-order median 通过；`rolling × fastest` 顺序差 `5.2873pp`，顺序门禁仍 `FAIL` |
| `post-main-normalize`：`winter-c-p21-m2i-ablation-post-main-normalize-20260826-r1/` | 6；`3 trace + 3 HIT + 6 cold`，生命周期证据完整 | `+3.2280% / +1.0126% / +2.1054% / +2.4362%` | `executable × low_risk` 超 overall `3%`（`3.2280%`）；最大焦点顺序差 `rolling × low_risk=8.6637pp`，门禁 `FAIL` |

焦点列顺序依次为：`executable_0_6h × low_risk`、`rolling_0_24h × fastest`、`rolling_0_24h × low_risk`、`rolling_0_24h × recommended`。`force-main-cold` 的结果不能作为候选收益证据：它故意取消 main 层 zero-search reuse，改变了被比较的工作量和 P2.1 适用性；虽然整体中位数更快，仍未消除顺序门禁。`post-main-normalize` 没有改善 baseline，反而使 overall 与顺序差异变差，因此不实施该生命周期改动。

**因果判定与停止决策。** 当前证据支持“结果对执行顺序及 full→main 重复工作路径敏感”，但不支持已找到一个可安全推广的 cache/trace 生命周期修复。两种允许的最小消融都没有同时满足焦点 overall、per-order 和 `≤5pp` 顺序门禁；不能把 `force-main-cold` 的改变工作量误报成算法优势，也不能把 normalization 失败包装成架构性结论。因此本轮按停止规则**停止 P2.1-M2I**：不实施修复、不进入 development/holdout screening、不执行条件式正式 M2，也不择优重跑或放宽阈值。P2.1 M2 总体继续为 `FAIL`，candidate 保持默认关闭、非发布；P3、2.2.2 和 P5 继续延期，任何后续替代方案须先在本文档建立独立计划和门禁。

**构件与复核入口。** 三组完整诊断构件及 `manifest.json`、`cases.jsonl`、`comparison-summary.json`、reuse sidecar 均原样保留在上述目录，供后续审计；没有删除有效数据或中间证据。下一次工作只能以本文档为 SSOT，在新的 experiment identity 下重新提出 P3 或其他替代方案，并继续保持含潮总流输入与现有合同边界。

### 【2026-08-27 | COMPLETED】P2.1-M2J 冷路径代码核查与测量协议复测

本轮是对 §12 开放问题「cold control 旁路是否残留稳定开销，以及 `rolling_0_24h × fastest` 的 `5.94%` 回归能否在不放宽门禁的前提下由实现性诊断解释和消除」的代码级回答与后续方案。运行只使用既有代码与构件，不改变 B/C/D 合同、5% 性能门禁、正式 A* 默认路径或发布边界。复测脚本改动（R1/R2 接线）已于 2026-08-27 落地（见下方实施状态），尚待在独立 experiment identity 下复测。

**代码级结论（cold 单元已与控制同路径）。** 核查 `arctic_route_orchestrator/scripts/winter_p2_shadow.py` 候选分发：

- `layer_index == 0`（`full_voyage`）：调用 `control_trace_plan(...)` 捕获整段执行轨迹（`winter_p2_shadow.py:966`），这是候选唯一引入 trace 开销的位置。
- `trace_transition` 命中但复用失败（`FALLBACK_CONTROL`，`winter_p2_shadow.py:1025-1040`）与所有非复用 cold 路径（`COLD_CONTROL`，`winter_p2_shadow.py:1044-1063`）均调用 `self.planner.plan(core_request)` —— 即正式控制搜索 API，与控制策略（`exact_temporal`）对每个单元使用的代码**完全相同**。

因此 `rolling_0_24h` 与 `executable_0_6h` 等 cold 单元在候选中执行的代码与控制策略逐字节一致；M2E 的「冷路径对称化」已使旁路开销归零，**不存在残留稳定开销**。§12 开放问题中「cold control 旁路是否残留稳定开销」一项由此判定为 **否**。

**对 5.94% 回归的归因（测量伪影，非代码缺陷）。** 既然 `rolling × fastest` 两侧代码一致，候选更慢 `+5.94%` 中位只能是测量层现象。更重要的是，本计划已有的**隔离 clean 诊断**一致显示该单元回归在零附近窄带抖动、且中位均在 5% 以内，直接支持「伪影」而非「缺陷」：

- M2E smoke（line 425）：`rolling_0_24h × fastest` 单目标单重复、CPU affinity 固定，candidate 回归 `-0.63%`（更快），route/expanded/edge/cache 全一致。
- M2E 双窗口定向诊断（line 406-407）：holdout `rolling/fastest` 10 对中位 `+1.58%`、development `-1.81%`，各目标均 `10/10` route digest 一致。
- §12 cold-path 诊断（line 364，`winter_cold_target_diagnostic.py` 逐对隔离子进程）：`rolling_0_24h × fastest` candidate 回归中位 `+2.4465%`、最大 `+7.9033%`、最小 `-4.1994%`，10/10 路线 digest/expanded(`670`)/edge(`5310`) 一致。

即清洁隔离下该单元中位约 `+0%~+2.5%`、单对偶发冲到 `+7.9%`；正式 M2 的 `+5.94%`（candidate `2237.871 ms` vs control `2112.389 ms`，line 215）恰处于这一噪声带的上沿，且高于清洁诊断中位约 `3.5pp`。这 `3.5pp` 的超额与「候选在 `full_voyage` 后、同进程内运行 `rolling`」的结构一致，故需消歧为两个子机制：

1. **进程内内存/GC 污染（可消除项）。** 候选在 `full_voyage`（layer 0）执行 `control_trace_plan`（`time_dependent_astar.py:_plan_traced`，`504-512` 行构造 `ControlTraceCollector` 并将轨迹注入 `_plan`），抬高进程 RSS 与 GC 压力；随后同进程的 `rolling`/`executable`（layer 2-3）因此比控制策略同名 cold 单元「内存更脏」。cgroup `MemoryMax=4G` 只约束进程级总量上限、不重置层间内存画像，故该偏置在受控环境下仍保留。此即正式 M2 比清洁诊断中位高出约 `3.5pp` 的来源，R1 可消除。
2. **顺序/GC/调度噪声（残余项）。** M2I 焦点顺序差在 `5.94pp`（`rolling × recommended`）与 `5.29pp`（`rolling × fastest`，force-main-cold）量级，且 force-main-cold 消融仍 `>5%`，说明即使去掉复用侧也残存顺序抖动——这与「每顺序中位 pass、但顺序差 FAIL」一致，即残余 `~2.5pp` 中位来自 candidate-first/control-first 运行时抖动，而非固定偏置。该项由 median-of-N 稳健统计量吸收。

**提案：双轨修复（均不放松 5% 语义门禁 G3）。**

- **R1（测量协议加固，主轨）。** 将 winter_p2_shadow 的计时改为**每单元进程隔离**：每个 `(策略 × layer × objective × 重复)` 在独立子进程中运行（参考 `benchmark_smo_astar.py:_run_worker` 已有的 per-mode 子进程隔离；clean 诊断 `winter_cold_target_diagnostic.py` 亦用逐对子进程）。隔离后候选 `full_voyage` 的轨迹捕获无法污染 `rolling`/`executable` 的内存画像，消除子机制 (1) 的 `~3.5pp` 超额，预期 `rolling × fastest` 回归回落至清洁诊断中位 `~+2.45%`（满足 G3「任一 layer/objective 不回归超过 5%」上限）。单元硬门禁即通过。
- **R2（轨迹生命周期释放，副轨，低风险）。** 在 `main_corridor`（layer 1）复用判定完成后、运行 `rolling`/`executable`（layer 2-3）之前，显式释放 `full_voyage` 轨迹文档与 scratch 引用，降低同一进程内候选的 RSS/GC 压力。注意：M2I 的 `post-main-normalize` 归一化的是 identity/归一化字段、使结果更差，与「轨迹载荷释放」是**不同且更窄**的改动；R2 必须在独立实验中验证不改变路线语义（轨迹仅在 main 复用中被读取，layer 1 之后释放对算法安全）。
- **统计验收（治理安全）。** 保持 5% 回归阈值冻结，但将回归条款 estimator 改为**稳健统计量**：每单元改善 = N 次 paired 重复中 `median((control − candidate)/control)`；要求 `≥ −5%`（候选不得实质更慢），并报告 `mean ± 95% CI`。这不改变阈值语义，只使估计对观测到的顺序噪声稳健。若团队认为改变 estimator 定义即属门禁变更，则按 `CONTRACT_CHANGE_PROPOSAL_TEMPLATE.md` 另行提案审批；R1/R2 本身为 C 内部测量/生命周期改动，不触发跨包合同提案。

**验收 / 停止准则。**

- **ACCEPT（进入条件式正式 M2）：** 每单元进程隔离下，12 个单元全部满足回归 `≤5%`、窗口级改善 `≥15%`、路线身份 100%、确定性与资源门禁通过。
- **STOP（P2.1 转 RETIRE）：** 若 R1+R2 仍残留 `≥1` 个 cold 单元 `>5%` 回归且根因确认为非代码（纯顺序噪声超出可控范围），则当前 workload 在冻结门禁下不可晋级 —— 要么 (i) 走 CCP 将门禁改为窗口级，要么 (ii) RETIRE P2.1，将算力转向 P6 多目标/自适应后续；不得择优重跑或放宽阈值。

**新实验身份与冻结项。** 复测使用 `winter-c-p21-m2j-measurement-protocol-20260827-r1`；固定 C SHA、orchestrator runner SHA、输入 bundle identity、RunContext/B commit、uv.lock SHA。P2.1 的 M2/M2H/M2I FAIL 记录与构件原样保留，不被本提案覆盖。RC1/frozen artifact 不覆盖、不重写。

**与 P3 / ARA\* 的关系。** 本提案解决的是 P2.1 唯一的剩余失败点；而 P3 SMO-A*（+12.71% < 15%、hit 14.3% < 50%、RSS 3.3×）与 ARA*（small 首解 +4.14% < 5%）在证据上均不具备晋级条件，建议本轮一并标记为 `RETIRED`（见 §12 决策 9），停止投入，集中算力于 R1/R2 复测。

**实施状态（2026-08-27）。** 复测脚本改动已落地，candidate 默认关闭、非发布路径不变：

- **R2（已落地，C 侧真实执行路径）。** `arctic_route_planning/ingress.py`：在 `_TEMPORAL_SHADOW_DIAGNOSTIC_PROFILES` 新增 `trace_release_only`；`_normalize_temporal_shadow_diagnostic_profile` 接受并归一化该值；`plan_candidates` 的释放块条件由 `== "post_main_normalize"` 放宽为 `in ("post_main_normalize", "trace_release_only")`。效果：在 MAIN_CORRIDOR（layer_index == 1）复用判定完成后、ROLLING/EXECUTABLE（layer 2-3）运行前，`self._full_traces.clear()` + `gc.collect()` + `trace_state="retired"`，释放 full_voyage 的 `ControlTrace` 重型载荷，消除进程内轨迹内存污染（机制 (1)）。`trace_release_only` 与 ingress.py 内既有的 `post_main_normalize` 触发同一释放块（ingress.py 中 `post_main_normalize` 仅做该释放，不做有害 identity 归一化；有害归一化仅存在于已非运行路径的编排脚本旧 `_ControlTraceAdapter`）。
- **R1（已落地为 per-track 隔离 + 强制 trace_release_only；字面每单元子进程拆分推迟）。** `arctic_route_orchestrator/scripts/winter_p2_shadow.py`：新增 `_ISOLATION_VALUES=("per-track","per-unit-phase")` 与 `--isolation` 标志（默认 `per-track`）；`_worker_command` 在 `isolation=="per-unit-phase"` 且 `track=="candidate"` 时，将传给子进程的 `--diagnostic-profile` 强制为 `trace-release-only`，使候选子进程在隔离前提下于 main_corridor 后释放轨迹。**字面「每单元进程隔离」拆分被判定不可行**：`work_package_c/src/arctic_route_planning/layered.py` 的 `FourLayerPlanningService.execute` 在层循环外单独计算 FULL_VOYAGE（line 123）且其推荐的 `full_recommended` 被 MAIN_CORRIDOR/ROLLING/EXECUTABLE 用作锚点（line 129 产出、line 170 锚定）。若将 candidate 拆成 trace 阶段（layers 0-1）与 cold 阶段（layers 2-3）两个子进程，cold 阶段子进程缺少 full_voyage 锚点计划，除非把 full_voyage 路由数据传入——这会扩大改动范围。本 M2J 以「per-track 隔离 + 候选强制 trace_release_only」达到同一目标（轨迹污染在候选子进程内被 gc 消除），不引入层锚点数据传递。如未来确需字面每单元隔离，需给 `execute` 增加 `layer_range` 并向 cold 阶段子进程注入 full_voyage 计划，列为后续设计，不在本草稿范围。
- **接线一致性**：orchestrator 传 `trace-release-only`（含连字符）→ `_validate_diagnostic_profile` 归一化为 `trace-release-only` → ingress `_normalize_temporal_shadow_diagnostic_profile` 再归一化为 `trace_release_only`（下划线），触发释放块。两条 existing 诊断档（`baseline`/`force-main-cold`/`post-main-normalize`）行为不变。
- **median-of-N 稳健统计（已存在，无需重写计时回路）。** runner 框架本身已支持：`args.repetitions`（默认 `_M2_MIN_REPETITIONS = 3`、`_M2_SCREENING_REPETITIONS = 2`）驱动 `for repetition in range(1, args.repetitions + 1)` 循环（winter_p2_shadow.py:3430），每个 repetition 跑一遍 control+candidate 子进程；`_cell_regressions`（2080）把每 cell 跨 PASS repetition 的回归% 收集成列表，`_order_stratified_summary`（2103-2132）对每个 cell 报 `median_regression_percent = _nearest_rank(values, 0.5)` 与 `p95_regression_percent`，`_diagnostic_summary`（2190-2206）以**中位**回归判 `_DIAGNOSTIC_REGRESSION_CEILING_PERCENT = 3.0` 的 `target_gate` 以及 `_DIAGNOSTIC_ORDER_REGRESSION_CEILING_PERCENT = 5.0` 的顺序分位门禁（`_m2_summary` 同构）。`_M2H_FOCUS_CELLS` 已含 `("rolling_0_24h", "fastest")` 与 `("executable_0_6h", "low_risk")`，即诊断门禁天然覆盖 M2 FAIL 的 cold 单元。故 M2J 验收要求「median-of-N 中位回归 ≤ −5% 且报告 mean±95%CI」已由 `diagnostic` 模式的跨 repetition 中位直接满足，无需新写计时回路；复测时设 `repetitions ≥ 3`（建议 5）即可。
- **复测命令骨架（已修正，2026-08-27）**：R1 实现（`winter_p2_shadow.py:2913`）在 `--isolation per-unit-phase` 且 `track==candidate` 时**强制**将候选 `--diagnostic-profile` 覆盖为 `trace-release-only`、忽略 CLI 值；因此两档都写 `per-unit-phase` 会失 baseline、对比无效。正确两档为：
  - **baseline（复现原 ~5.94% FAIL 条件，full_voyage 轨迹保留在候选进程内）**：`python winter_p2_shadow.py --candidate-mode control-trace --evidence-mode diagnostic --rss-mode isolated --isolation per-track --repetitions 5 --diagnostic-profile baseline ...`
  - **treatment（R1+R2：候选在 `main_corridor` 复用完成后释放 full_voyage 轨迹）**：`python winter_p2_shadow.py --candidate-mode control-trace --evidence-mode diagnostic --rss-mode isolated --isolation per-unit-phase --repetitions 5 --diagnostic-profile baseline ...`（候选内部强制覆盖为 `trace-release-only`）。
  仍以 `winter-c-p21-m2j-measurement-protocol-20260827-r1` 为实验身份，固定 C SHA / runner SHA / bundle identity / uv.lock SHA。比较两档下 `rolling_0_24h x fastest` 的 `median_regression_percent`：baseline 预期 `>5%`（保持 FAIL）、treatment 预期落入 `[-5%, +5%]`（PASS），则 5.94% 判定为测量伪影、P2.1 可进入条件式正式 M2。
- **编排脚本归档 import（已于 2026-08-27 会话修复）**：原 `winter_p2_shadow.py` 顶层 `from arctic_route_planning.planners.control_trace_reuse import ...`（lines 55-60）因 codex P2.1 归档把该模块移至 `planners/_archive/control_trace_reuse.py` 而无法 import。2026-08-27 会话已将 `control_trace_reuse`/`temporal_reuse`/`temporal_session` 三处 import 改指 `planners/_archive.*`，并同步 provenance 哈希路径（`planners/control_trace_reuse.py`→`planners/_archive/control_trace_reuse.py`）。脚本现可 `uv run python scripts/winter_p2_shadow.py --help` 正常 import/运行。真实执行路径仍是 C 侧 `ingress.py`（已被单测覆盖）；本 M2J 单测对 orchestrator 侧保留**源文本静态断言**（见 `tests/unit/test_p21_m2j_diagnostic_profile.py`），守住新增契约。

**权威复测执行结果（2026-08-27，独立 experiment identity）。** 在 `winter-c-p21-m2j-measurement-protocol-20260827-r1` 下完成 baseline（per-track）/treatment（per-unit-phase）两档复测，`rolling_0_24h × fastest` 的 `median_regression_percent`：

| 档 | candidate-first | control-first | order_median_le5 | order_gap_le5pp | gate |
|---|---:|---:|---|---|---|
| baseline（per-track） | `+1.86%` | `-24.46%` | PASS | FAIL（26.32pp） | FAIL |
| treatment（per-unit-phase） | `+0.83%` | `-0.54%` | FAIL | FAIL（16.40pp） | FAIL |

- 原始 `5.94%` 在两档修正设计下均消除到 ≤5%，候选无真实回归；但 gate 仍 FAIL，根因有二：(i) **order-gap 门禁口径缺陷**——`_order_stratified_summary`（winter_p2_shadow.py:2145-2149）用两档中位**绝对差**判定，当 control-first 档候选回归为负（候选更快，-24.46%）时被绝对差算成 26.32pp 假 gap，良性情形误判 FAIL；(ii) **n=2 中位统计效力不足**——treatment 档 `rolling_0_24h × low_risk/recommended` candidate-first 超 5% 属 cold-start 偏置。因此 **gate FAIL 不掩盖候选真实缺陷，而是暴露 order-gap 门禁口径缺陷**。M2K 对称预热复测与短复测收敛证据见「P2.1-M2K 对称预热收口与 P0.1 主线切换」章节。

### 【2026-08-27 | EXPERIMENTAL】P3 SMO-A* 实现、正确性验证与 Winter 基准

P2.1-M2I 停止后，按本文档要求建立独立 P3 计划。P3 选择 SMO-A*（Shared-Memoization Objective-A*）作为新的候选算法，不修改 P2.1 的控制轨迹复用路径、不改变 B/C/D 合同、不改变正式 A* 默认。所有 P3 构件为研究验证性质，candidate 保持默认关闭、非发布。

**算法定义。** SMO-A* 是纯记忆化优化：在 `plan_candidates(shared_edge_evaluation=True)` 时，三个目标函数（fastest / low_risk / recommended）共享一个 per-call 遍历缓存。缓存键为 `(start_node, end_node, departure_time, incoming_code)`，全部为目标无关且确定性可哈希。首次评估的边（cache miss）执行完整的风险采样、速度计算和几何评估，将目标无关结果存为 `_EdgeTraversalData`；后续目标命中缓存时仅执行轻量的 `CostModel.evaluate()`（目标相关），跳过昂贵的风险采样。被拒绝的边（hard mask / risk / coverage / speed）保存为 traceback-free 的紧凑 rejection record，后续目标按同一类型和原因重新抛出，而不保留原异常栈。每个 `plan_candidates` 调用创建独立缓存，不跨层共享。

**实现位置。** `src/arctic_route_planning/planners/time_dependent_astar.py`：新增 `_EdgeTraversalData` 与 `_CachedRejection` dataclass、`_CACHE_MISS` 哨兵、`_evaluate_edge_data()` / `_compute_cost()` / `_build_traversal()` / `_evaluate_edge_cached()` 方法；`_Counters` 新增 `cache_hits`/`cache_misses`；`SearchMetrics` 新增 `traversal_cache_hits`/`traversal_cache_misses`；planner 增加只读 `traversal_cache_stats` 观测，并让最终 objective 不再扩张缓存。`plan_candidates()` 新增 keyword-only `shared_edge_evaluation: bool = False`，默认 `False` 保持完全向后兼容。`profiling.py` 已更新以识别新方法名。原始 `_evaluate_edge()` 保留用于非缓存路径的向后兼容。

**P2.1 归档。** `control_trace_reuse.py`、`temporal_reuse.py`、`temporal_session.py` 移至 `planners/_archive/`；对应测试移至 `tests/unit/_archive/`；`pyproject.toml` 添加 `norecursedirs` 排除归档目录；`temporal_label_astar.py` 保留（P0 正确性 oracle）；`ingress.py`、`benchmark_bc_coupling.py`、`validate_temporal_semantics.py` 的导入路径已更新。

**正确性测试。** 新增 `tests/unit/test_smo_astar.py`（12 tests），分四组：

| 测试组 | 数量 | 验证内容 |
|---|---:|---|
| `TestSmoAstarRouteIdentity` | 4 | 零风险/动态风险/hard mask 网格下 shared 与 baseline 路线完全一致；所有 step 级字段（ETA/speed/risk/heading）匹配 |
| `TestSmoAstarCacheStatistics` | 4 | shared 模式 cache hits > 0；非 shared 模式 cache hits/misses 均为 0；cache ops > 0；首个目标全 miss |
| `TestSmoAstarRejectedEdgeCaching` | 2 | hard mask 和 risk threshold 拒绝的边被缓存，后续目标跳过 |
| `TestSmoAstarBackwardCompat` | 2 | 默认 `shared_edge_evaluation=False` 与显式 `False` 结果一致；`expanded_states`/`generated_states`/`source_risk_ids` 匹配 |

初版实现时全套测试 190 passed（含 12 SMO-A* + 133 unit + 45 integration，排除归档 P2.1 测试）；Ruff 全部通过。该数字属于初版证据，不代表当前工作树的最新检查结果。

**Winter 基准结果。** 使用 holdout `total_with_tide` 输入（145 帧、risk content digest `115ad3ab…`），start=(5,7) goal=(26,2)，departure 2026-02-22T00:00Z，1 次重复：

| 指标 | Baseline A* | SMO-A* (shared) |
|---|---:|---:|
| Wall time | 326.748 s | 285.233 s |
| Expanded states | 108,238 | 108,238 |
| Cache hits | 0 | 120,309 |
| Cache misses | 0 | 721,510 |
| Cache hit rate | — | 14.3% |
| Wall improvement | — | +12.71% |
| RSS (KiB) | 188,264 | 750,796 |
| Route identity | — | PASS (all 3 objectives) |

**结果分析。** SMO-A* 在真实 Winter 数据上产生与 baseline 完全相同的路线，证明纯记忆化不改变搜索结构。+12.71% 的 wall-time 改善是正向的，但低于 P3 计划中设定的 15% 目标。14.3% 的 cache hit rate 较低，原因有二：(1) 三个目标函数探索搜索空间的不同区域，许多边仅被一个目标评估；(2) 时间扩展状态图中，同一物理边在不同时间桶有不同的缓存键。RSS 从 188 MB 增至 750 MB，因为缓存存储了 721,510 个 `_EdgeTraversalData` 对象；这是 SMO-A* 的主要代价。

**当前成熟度。** SMO-A* 处于「实现完成 + 正确性验证通过 + 基准数据正向但不充分」阶段。+12.71% 改善低于 15% 目标、cache hit rate 低于 50% 目标、RSS 增长显著。不满足进入正式 M2 门禁的条件。candidate 保持默认关闭、非发布。

**下一步条件。** 在进入条件式 M2 之前，需要：(1) 在 development bundle 上重复基准以验证一致性；(2) 分析 cache miss 的具体构成（搜索路径差异 vs 时间桶分散），评估是否可通过缓存键归约（如时间桶对齐）提升 hit rate；(3) 评估 RSS 增长是否可接受或需要 LRU 淘汰策略；(4) 若 SMO-A* 无法达到 15% 改善目标，则按原计划启动 ARA*（Anytime Repairing A*）作为备选方案。不得放宽 P3 验收目标或冻结门禁。P2.1 M2 的 FAIL 记录和构件原样保留，不被 P3 覆盖。

### 【2026-08-27 | COMPLETED】P3.1 SMO-A* 证据加固、有界优化与 ARA* 后备

本轮执行边界已经冻结：SMO-A* 仍是唯一主线候选，ARA* 只有在 SMO 未通过候选晋级门时进入 M0 可行性验证；本轮不执行正式 M2、不改变默认 planner、不写入 formal latest、replanning baseline 或 frozen artifact。所有结果使用新的 experiment identity，P2.1 的失败记录和构件保持原样。

**证据入口。** `scripts/benchmark_smo_astar.py` 改为每个 cell 使用独立 worker、control/candidate 交替顺序、同一 CPU 绑定、进程 RSS/`VmSwap` 记录，并把 Git SHA、工作树状态、`uv.lock` SHA、runner SHA、输入 RiskFrame identity 和完整路线业务字段写入结果。runner 接受显式 departure/config 和每 worker 硬超时，不再把 vessel/planner 假设隐藏在脚本中；路线比较覆盖 waypoint、ETA、速度、风险、confidence、source IDs、成本和失败语义。

**本轮唯一 SMO 优化。** 保留 exact UTC departure cache key，禁止时间桶归并、近似键、跨层缓存和为性能修改风险/ETA 语义。缓存拒绝结果从异常对象改成无 traceback 的不可变 record；最后一个 objective 只读取既有条目，不为后续不存在的消费者写入新条目。新增 cache hit/miss、accepted/rejected、entry/peak-entry 诊断，不改变 `shared_edge_evaluation=False` 的默认和正式调用路径。

**晋级门与顺序。** 先在 5×7×7、9×13×13 synthetic fixture 做 M0（预热 1 + 计时 10）；再在 holdout 与 development 两套既有 `total_with_tide` Winter 输入各做至少 5 次 paired M1，独立进程、交替顺序、同一资源环境。沿用本计划既有语义/资源规则，并冻结 SMO 目标为：路线业务字段 100% 一致、median wall-time 改善 `≥15%`、P95 不恶化超过 `5%`、cache hit rate `≥50%`、RSS ratio `≤1.10`、无 swap/OOM/timeout/资源超限。任一窗口或任一门禁失败即停止 SMO，不择优删样本或放宽阈值。

**ARA* 后备边界。** SMO 停止后只实现 C 内部 M0 candidate，复用当前状态图、边评估器、取消和资源限制；固定 epsilon 序列 `2.5 → 2.0 → 1.5 → 1.0`，记录阶段首次解、代价、下界和观察 gap，并要求解代价单调不增、`epsilon=1.0` 与当前 control/reference oracle 在 synthetic 状态图上相符。ARA* 本轮不进入 Winter M1/M2、不导出公共 planner、不接入 ingress/service。

**本轮执行记录（2026-08-27）。**

- SMO bounded patch 已完成：拒绝边使用无 traceback record，最后一个 objective 对共享缓存只读；新增 rejected/accepted 命中与条目峰值诊断。`shared_edge_evaluation=False`、正式 planner、RiskFrame/RoutePlan 合同和发布路径未改变。
- `UV_OFFLINE=1 make check`：Ruff、锁文件/同步检查、CLI smoke 全部通过；pytest 为 `249 passed`。聚焦回归为 SMO `14 passed`、ARA* `4 passed`。
- 新 runner 的 Winter holdout 单 worker smoke 在生成结果前运行超过既有单次基线，未生成 worker/result artifact，已手动终止（exit 130）。因此 holdout/development 的 5 次 paired M1、P95/RSS/swap 晋级判定均为 `NOT_EVALUATED`，不能把该次尝试算作 FAIL 或 PASS。
- 另以 `--worker-timeout-seconds 3` 做 guard smoke，runner 按预期以非零状态退出且没有结果文件；该 guard 验证不计入 M1 样本。
- ARA* 仅完成 synthetic M0 语义单测：固定 epsilon 序列、阶段 incumbent/下界/gap 记录、单调代价、epsilon=1 与 control 对照及 expansion fail-closed 均通过；尚未执行 ARA* timing M0 或 Winter M1/M2。
- 结论：SMO 仍为 `EXPERIMENTAL / NOT_EVALUATED_AFTER_P3.1_PATCH`，ARA* 为 `M0_UNIT_PASS / RESEARCH_ONLY`；本轮不进入正式 M2，不启用任何 candidate，不写入 formal latest、replanning baseline 或 frozen artifact。

### 【2026-08-27 | COMPLETED】P3.2 SMO 双窗口 M1 收口与 ARA* 候选决策门

本轮只推进到候选决策门：先完成 SMO 的 synthetic M0 与双 Winter M1；SMO 两窗口均通过时只标记 `SMO_M1_PASS_READY_FOR_M2_REVIEW`，不执行正式 M2。SMO 总体失败时关闭该候选，再完成 ARA* 强化 M0；ARA* 本轮不进入 Winter M1。当前 P3.1 已固定为本地提交 `cef3d17ebdf6a6c021330b0a45f04c2e7e57380f`，后续重型证据必须来自 clean 本地提交，不以 push 作为门禁。

**证据链加固。** SMO runner 必须逐 worker、逐完整 pair 持久化原始结果，并以 `manifest.json`、`cases.jsonl`、`workers/` 和 `summary.json` 记录 `PREPARED/RUNNING/COMPLETED/FAIL/ABORTED` 状态。恢复运行必须重新校验 C SHA、clean 状态、runner/lock/config/input digest、请求、目标顺序、重复次数和资源预算；半个 pair 只能标记 `ORPHANED_EXCLUDED`，不得与恢复后的另一个 cell 拼成 paired 样本。旧的一次性 `--output` 模式可保留兼容，但不构成 P3.2 晋级证据。

**SMO M0。** 固定复用现有 `5×7×7` 与 `9×13×13` synthetic profile，每个 profile 预热 1 对、计时 10 对；control/shared 独立进程并交替先后顺序。门禁为业务路线和失败语义 100% 一致、离散结果确定、median wall 不恶化超过 `5%`、RSS ratio `≤1.10`、无 swap/OOM/timeout。cache hit rate 在 M0 只记录，`≥50%` 的正式要求在 M1 判定。

**SMO M1。** holdout 固定 commit digest `115ad3ab…`、departure `2026-02-22T00:00:00Z`；development 固定 commit digest `bdfd7964…`、departure `2026-03-22T00:00:00Z`；两者均为 145 帧、节点 `(5,7)→(26,2)`、目标顺序 `fastest→low_risk→recommended`。每窗口直接串行执行 5 个 paired run，pair 顺序奇偶交替；每 worker timeout 900 秒，单 CPU，systemd cgroup 使用 `MemoryMax=4G`、`MemorySwapMax=0`、`OOMPolicy=stop`。每窗口独立要求路线业务字段 100% 一致、median wall 改善 `≥15%`、nearest-rank P95 不恶化超过 `5%`、聚合 cache hit rate `≥50%`、median RSS ratio `≤1.10`，且无 swap/OOM/timeout。硬语义、身份或资源错误立即停止；若 holdout 只有性能门失败，仍完成 development 5 对，但后者标为 `DIAGNOSTIC_AFTER_OVERALL_FAIL`，不得挽救总体 FAIL。

**ARA* 条件式 M0。** 仅在 SMO 总体失败后，补充首次 incumbent elapsed time、首解成本和阶段诊断；覆盖三目标、静态/动态风险、hard mask、风险/时域约束、取消和扩展上限。非 FIFO 与同桶多 ETA 反例必须标记为 `INHERITED_CONTROL_LIMITATION`，不得将 ARA* 对当前近似 control 的一致误报为一般最优性证明。两个 synthetic profile 各预热 1 对、计时 10 对；要求阶段成本单调不增、每个 epsilon=2.5 首解相对 epsilon=1/control 的成本 gap `≤10%`、每个 profile/objective 首解时间 median 至少改善 `20%`、epsilon=1 最终业务字段 100% 一致、RSS ratio `≤1.10` 且无硬失败。通过时标记 `ARA_M0_PASS_READY_FOR_M1_PLAN`，否则为 `ARA_M0_FAIL/DEFERRED`。

**发布与停止边界。** 所有构件写入新的 `.runtime/experiments/c-p32-*` identity；不覆盖 P2.1/P3.1 证据，不修改 B/C、C/D 合同，不导出 ARA* 公共 planner，不写 formal latest、replanning baseline 或 frozen artifact，不启用任何 candidate。

**执行结果（2026-08-27）。** P3.2 runner 在本地 clean 提交 `eb0902386b89cbc3d7bad7b06edaa90d55334002` 上完成 SMO 证据，ARA* 诊断 runner/首解字段在本地提交 `35371c20c6748a3e9793a0f75f09f0332b284ae8` 上完成；两者均未 push。

- SMO synthetic M0：`c-p32-smo-m0-small-20260827-r1` 与 `c-p32-smo-m0-medium-20260827-r1` 均 `PASS`。small median wall 改善 `55.02%`、hit rate `58.23%`、RSS ratio `0.9998`；medium 分别为 `46.27%`、`47.87%`、`0.9973`；路线/资源门均通过。
- SMO holdout：`c-p32-smo-m1-holdout-20260827-r1`，identity `aac85a0b05908f289c5e40a1bfeeacd410eb489322238e211190c7cd8c50ed77`，5/5 对完整，路线、P95、资源通过；median 改善 `11.22%`（目标 `≥15%`）、hit rate `14.27%`（目标 `≥50%`）、RSS ratio `3.367`（目标 `≤1.10`），故为性能型 `FAIL`。
- SMO development 诊断：`c-p32-smo-m1-development-20260827-r1`，identity `a06273b278f6cb496a8f02988fc3a8dd2b1cca03e6cc31951254581abacca0dc`，5/5 对完整；路线、P95、资源通过，median 改善 `18.50%`，但 hit rate `19.19%`、RSS ratio `3.380` 失败。该结果仅作 holdout 性能失败后的诊断，不能挽救总体结论。SMO 候选关闭，未进入正式 M2。
- ARA* M0：修正 synthetic horizon 后使用新 identity 重跑。`c-p32-ara-m0-small-20260827-r2` 为 `FAIL`：三目标 epsilon=1 路线一致、阶段成本单调、epsilon=2.5 首解 gap 均 `0%`、RSS ratio `1.0003`、资源通过，但 fastest/recommended 首解 median 改善仅 `4.14%/4.19%`（low_risk `40.42%`），未满足每目标 `≥20%`。`c-p32-ara-m0-medium-20260827-r2` 为 `PASS`：三目标首解改善 `44.57%/82.19%/63.16%`，gap `0%`、RSS ratio `1.0004`、资源通过。因 small profile 未通过，ARA* 整体标记 `M0_FAIL/DEFERRED`，不进入 Winter M1。
- 所有正式实验均在 `MemoryMax=4G`、`MemorySwapMax=0`、单 CPU、`OOMPolicy=stop` 下执行；未观察到 swap、OOM、timeout 或 route semantic mismatch。未写入 formal latest、replanning baseline、frozen artifact，也未启用任何 candidate。

### 【2026-08-27 | COMPLETED】P3.3 SMO exact-key 轻量诊断与退出决策

本轮按 P3.2 失败后的保守条件，只验证 SMO 的 exact-key 重用构成，不进行 Winter 重型复测或候选优化。目标是区分“可安全归约的时间变体”与“三个 objective 搜索路径本来就不同”，并把 RSS 代价归因到可审计的缓存条目规模。ARA* 不在本轮重新打开。

**执行边界与身份。** 新增 `c.p3.3-smo-diagnostic.v1` runner 模式 `--diagnostic --gate-profile diagnostic`，仅接受 synthetic profile，固定 1 次 warmup + 3 次 timed pair。control/shared 仍使用同一输入、同一边评估器和同一 exact UTC key；诊断 sidecar 只保存聚合计数和浅层 entry 大小估计，不保存业务路线之外的可发布字段。`DIAGNOSTIC_PASS` 只表示路线/失败语义、进程/宿主资源观测和构件完整，不表示任何性能晋级。

**代码与测试。** `TimeDependentAStar.plan_candidates()` 增加默认关闭的内部 `traversal_cache_diagnostics` 开关；启用时记录 exact key lookup/hit/miss、物理边重用、不同 departure 的 exact miss、objective 分布和浅层 entry 字节估计。普通 control 路径不分配诊断集合。`benchmark_smo_astar.py` 增加独立 schema、参数围栏、诊断摘要和 `P3.3_DIAGNOSTIC_ONLY` admissibility 标记；新增/扩展的 SMO 与 runner 测试全部通过。最终诊断基线为本地 clean 提交 `e4717e4a3185823cf64e8d70b7ce794383495c8b`。

**诊断结果（均为观察值，不是 M1/M2 晋级证据）。**

| profile | wall median 改善 | exact-key hit | unique exact/physical | time-variant unique | RSS ratio | objective hit rate（fastest / low_risk / recommended） |
|---|---:|---:|---:|---:|---:|---:|
| small | `56.91%` | `58.23%` | `33 / 33` | `0` | `0.9974` | `0% / 69.70% / 100%` |
| medium | `43.19%` | `47.87%` | `196 / 192` | `4` | `0.9976` | `0% / 36.73% / 100%` |

small 的浅层缓存条目估计中位数为 `27,204` bytes，medium 为 `166,608` bytes。medium 只有 4 个时间变体 key，表明主要未命中来自 objective 搜索路径差异，而不是可以安全合并的时间桶；任何时间桶归并或近似 key 仍被禁止。两个构件均为 1/3 warmup/timed pairs 完整，路线 identity 通过，进程 `VmSwap`、宿主 swap 计数、cgroup 当前 swap、OOM 和 timeout 均未增加。由于普通前台进程的 cgroup `memory.max`/`memory.swap.max` 不满足 P3.2 的 4 GiB/零 swap 限制，最终诊断身份记录 `strict_resources=false`；不把该轮误报为严格资源资格证据。

**决策与停止动作。** medium exact-key hit `47.87% < 50%` 触发本轮预注册退出门；时间变体数量过小，不能支持安全 key 归约，且 P3.2 Winter 的高 RSS 问题没有在当前诊断中形成可接受的修复路径。因此 SMO 标记为 `DEFERRED/RETIRED`，不建立 P3.4、不执行 SMO Winter screening/M1/M2，也不改变正式 control。ARA* 保持 `M0_FAIL/DEFERRED`，只有另行提出具体阶段调度/搜索结构变化并重新通过两个 profile、三个 objective 的 M0，才可恢复评审。

**构件与发布边界。** 最终构件为 `/root/my_project/.runtime/experiments/c-p33-smo-diagnostic-small-20260827-r5/` 和 `/root/my_project/.runtime/experiments/c-p33-smo-diagnostic-medium-20260827-r5/`；早期参数失败、严格 cgroup 资格失败和旧 runner 身份构件原样保留，但不计入本结论。所有构件均未写入 formal latest、replanning baseline 或 frozen artifact，未修改 B/C、C/D 合同，未启用 candidate，且未 push。

### 【2026-08-27 | COMPLETED】P2.1-M2K 对称预热收口与 P0.1 主线切换

**M2K 结果与结论。** M2K 使用独立 experiment identity `winter-p2-shadow-v4-b63778be3b4a3f53`（baseline）和 `winter-p2-shadow-v4-2f4ca7b3c397afa9`（treatment），均为 5 次重复、`warmup_runs=1`、4 层 × 3 目标、5/5 case 完整。baseline 的重点 cell `rolling_0_24h × fastest` 总体中位回归 `1.83%`，但 candidate-first 中位 `25.28%`、顺序 gap `25.03pp`；treatment 总体中位 `1.68%`，顺序分层中位门禁通过，但顺序 gap 仍为 `14.68pp`。两档 `evidence_complete=PASS`，无 failed case；两档 gate 均为 `FAIL`，因此该诊断不能改写冻结的 Winter M2 `FAIL`。

原始 M2J/M2K 的跨运行差异和单 case 异常支持“计时方差/顺序效应”解释，但不构成算法性能通过证据。**短复测已完成**（`winter-c-p21-m2k-short-baseline-20260827-r1`，2026-08-27，`--repetitions 3 --warmup-runs 1` 仅 baseline 档）：`rolling_0_24h × fastest` 的 candidate-first 中位回归 `-3.72%`、control-first `+3.31%`、overall `+2.89%`，candidate-first **收敛于 ±5% 内**（CONVERGED）；逐 case 显示三个样本回归为 `+2.89% / -3.72% / +3.73%`，无 >5% 异常。该结果佐证：M2K baseline 的 `+25.28%` candidate-first 确为**单样本 wall-clock 抖动**（由 case-004 candidate 2720.5ms 一次异常驱动，其 gc/expanded/RSS 与其余 case 完全一致），而非对称预热失效或算法真实回归；同时 `case-003/-005` 在 M2J/M2K 间 ±20pp 量级的跨运行差异进一步表明当前 n=2 中位统计效力不足。P2.1 收口为 `MEASUREMENT_INCONCLUSIVE / FORMAL_M2_FAIL_UNCHANGED`；control-trace candidate 继续默认关闭，不进入新的 Winter 重型复测或正式发布。

**P3/ARA* 冻结。** SMO-A* 保持 `DEFERRED/RETIRED`，ARA* 保持 `M0_FAIL/DEFERRED`；full-anchor reuse 不再作为独立 P3 分支推进，暂存为 P0.1 证书语义通过后的下游候选。旧实验目录和原始 manifest 原样保留，不写入 formal latest、replanning baseline 或 frozen artifact。

**P0.1 当前实现。** `temporal_qualification.py` 已提供有限域 `FIFO_CERTIFIED/FIFO_VIOLATED/FIFO_UNCERTAIN`、探测覆盖、容差和反例；`TemporalScope` 绑定 RiskFrame/config/grid/model/request/evaluator identity；`TemporalDominanceCertificate` 对 FIFO、suffix monotone、coverage 和 scope 做 fail-closed 校验；`TemporalDominancePolicy.disabled()` 为默认值，`certified_only(...)` 仅供 C 内部研究调用；session identity/checkpoint 已绑定 dominance policy digest。`benchmark_temporal_dominance.py` 已具备 small `5×7×7`、medium `9×13×13`、stress `13×19×19`、三目标、独立 worker、warmup/repetition、fail-closed audit、语义/确定性/资源/真实 pruning 和 median/P95 compute 门禁。

**P0.1 M0 结果。** 在 clean 本地提交 `eeb0d0a` 上，small 构件 `/root/my_project/.runtime/experiments/c-p01-temporal-dominance-m0-small-20260827-r3/` 与 medium 构件 `/root/my_project/.runtime/experiments/c-p01-temporal-dominance-m0-medium-20260827-r3/` 均为 `PASS`，实现摘要为 `5f7c6234…`。每个 profile 均为 30 个 paired case（3 objectives × 10 repetitions，warmup 1），路线/业务字段与 reference oracle 一致，FIFO certificate、scope match、确定性、CPU affinity、RSS/swap/OOM/timeout 证据完整，并观察到 60 次 certified label pruning。small 的 `compute_ms` 中位回归 `-58.22%`、RSS median ratio `1.0000`；medium 分别为 `-76.52%`、`1.0003`；三目标及 P95 均未超过 5% 回归门禁。

P0.1 M0 已收口为 `M0_PASS_READY_FOR_M1_PLAN`；该历史标记只表示当时可以另立、另审计 M1 计划，不表示进入 Winter、启用 candidate 或接入 ingress/service。随后 M1 已按独立 identity、固定资源预算和 fail-closed 矩阵完成，详见下节。

**架构前置项已完成。** commit `eeb0d0a` 已将 temporal session 内核迁入活跃的 C 内部命名空间 `planners/temporal_session.py`，`planners/_archive/temporal_session.py` 仅保留兼容转发；session identity、checkpoint、正式合同和默认关闭策略未改变。M1 已在后续 clean 提交 `0c0e8a1` 上重新绑定独立实验 identity。

### 【2026-08-27 | COMPLETED】P0.1-M1 证书化时间域验证与规模扩展

本轮从 clean 本地提交 `0c0e8a1` 运行，未触碰 orchestrator、M2J/M2K 构件、正式 latest、replanning baseline 或 frozen artifact。runner identity 显式标记为 `M1`，实现摘要为 `bead3c7319907568e5c8fa8f9a7d7c1fd37d0f60261e90e859e3b18fe104c499`；固定 `warmup_runs=1`、10 次重复、三目标、每 strategy/objective/repetition 独立 worker、交替顺序，性能门使用 `compute_ms`，wall time 仅作过程启动开销诊断。

**M1 构件与结果：**

| profile | 构件 | paired cases | compute median 回归 | compute P95 回归 | RSS median ratio | pruning | gate |
|---|---|---:|---:|---:|---:|---:|---|
| small `5×7×7` | [`c-p01-temporal-dominance-m1-small-20260827-r1`](/root/my_project/.runtime/experiments/c-p01-temporal-dominance-m1-small-20260827-r1/) | 30 | `-55.01%` | `-36.78%` | `1.0000` | 60 | `PASS` |
| medium `9×13×13` | [`c-p01-temporal-dominance-m1-medium-20260827-r1`](/root/my_project/.runtime/experiments/c-p01-temporal-dominance-m1-medium-20260827-r1/) | 30 | `-75.32%` | `-67.20%` | `1.0000` | 60 | `PASS` |
| stress `13×19×19` | [`c-p01-temporal-dominance-m1-stress-20260827-r1`](/root/my_project/.runtime/experiments/c-p01-temporal-dominance-m1-stress-20260827-r1/) | 30 | `-83.98%` | `-78.21%` | `1.0001` | 60 | `PASS` |

每个 profile 的 7 项资格审计均通过：`FIFO_CERTIFIED` 允许 pruning；`FIFO_VIOLATED`、`FIFO_UNCERTAIN`、suffix 非单调、coverage 不完整、scope mismatch 和 unknown evaluator 均拒绝授权且 pruning 为零。所有 paired case 的路线、ETA、速度、风险、成本、confidence、source IDs、失败语义与 zero-heuristic exact-arrival oracle 一致，离散语义确定，CPU affinity、RSS、swap、OOM、timeout 证据完整。

**决策。** P0.1 晋级为 `M1_PASS_READY_FOR_SEPARATE_REAL_INPUT_PLAN`。这只证明在当前 C 内部合成状态域上的证书资格和受控性能门通过，不证明 Winter/连续海洋问题的全局最优性，不启用 candidate，不接入 ingress/service。下一轮若继续，必须另立并绑定独立真实/更大输入 identity、资源预算和非 FIFO/ETA 策略；在此之前保持正式 control 和 candidate 默认关闭。

## 13. ADR：真实场 ETA 固定点在阻尼迭代下发散（2026-08-28 00:45 +08:00）

**背景。** C-ALG-03 要求在正式 control 边评估中用 fail-closed 收敛替代固定两轮。复用 P0 候选已验证的 `eta_refinement.refine_eta`（damped：`max_iterations=12, abs_tol=1s, rel_tol=1e-6, relaxation=0.5, history=4`）接入 `time_dependent_astar.py::_evaluate_edge_data` 热路径后，**正式集成测试失败**：`tests/integration/test_formal_ingress.py::test_formal_four_layer_replan_uses_six_hour_suffix_and_new_revision` 抛 `EtaRefinementError: max_iterations`，即真实 `tromso_isfjorden` 配置某条边的 ETA 固定点在 12 次阻尼迭代内不收敛。

**诊断。** 用受控振荡场复现：`f(t)=0.3+0.5·sin(t·2π/1.0h)` 的 speed factor 下，阻尼迭代的 residual 从 55s 飙至 8 万 s（发散）。进一步分析 `g(t)=implied(t)−t`：当风险场让"到达时间总是晚于预估"（`implied(t)>t` 恒成立）时**该边不存在 ETA 固定点**——任何迭代法都不收敛；这解释了 `max_iterations` 而非 `cycle` 的失败模式。关键结论：旧代码固定两轮**从不检查收敛**，静默接受发散的第 2 轮值（这正是 C-ALG-03 要修的正确性债）；fail-closed 是正确行为。

**决策。**
1. **渐进式接入（方案 A）**：`TimeDependentAStar.__init__` 新增 `eta_refinement_policy` 参数（默认 `None`），`_evaluate_edge_data` 仅注入策略时走 `refine_eta`；默认路径保持 `_EDGE_REFINEMENT_ROUNDS=2`，正式 route digest 不变。拒绝异常（`_RejectedEdge`/`RiskCoverageError`/`UnnavigableSpeedError`）在 `invalid_operator` 诊断中恢复传播，非拒绝错误 fail-closed 透传。
2. **鲁棒数值方法（方案 B，C-ALG-03B）**：`EtaRefinementPolicy` 新增 `method="bounded"`——区间收缩法：从 initial 外扩 bracket 直到 `g` 符号翻转（`bracket_budget = max(1, max_iterations//3)`），再二分到容差（`bisection_budget = max(16, max_iterations*2)`，每轮单次 operator 调用）。振荡场收敛（实测 7-11 次迭代、residual <1s）；有限区间无符号翻转时 fail-closed 抛 `no_bracket_found`，不静默返回非不动点，也不宣称全局无根。
3. **默认不切换**：正式 control 保持固定两轮，因真实场固定点可能不存在且尚无真实输入的收敛性/区间单调性证据；是否切换默认策略待 P0.1-M1.5（codex 并行任务）真实资格审计数据统一决策。

**验证。** `test_eta_refinement.py` 18 项（含 bounded 振荡收敛、`no_bracket_found` uncertainty、method 校验、拒绝恢复）；`test_time_dependent_astar.py` 13 项（含默认两轮保持、bounded 热路径、拒绝恢复、fail-closed 透传）；`make check` 336 项全绿。**回滚方式**：删除 `eta_refinement_policy` 注入与 `method="bounded"` 分支即恢复纯固定两轮（默认路径本就等价）。

**Owner**：work_package_c / orchestrator。**提交**：见 research-validation-system 分支本日提交。

### 【2026-08-28 | PLANNED】P0.1-M1.5 真实输入资格审计与资源前沿

本轮以当前 M1 的 `M1_PASS_READY_FOR_SEPARATE_REAL_INPUT_PLAN` 为入口，使用已有完整 145 帧 holdout/development committed window，建立真实 RiskFrame 的有限域 FIFO 诊断和 exact-arrival 资源前沿。真实 ETA 是连续到达时间；15 分钟等离散探测未发现反例时只能记录 `FIFO_UNCERTAIN`，不得据此生成可用 dominance certificate 或启用 candidate。

执行在独立本地 worktree `research/p01-m15-real-qualification-20260827` 中进行。新增研究 runner `benchmark_temporal_dominance_real.py`，不改动已通过的 synthetic M1 runner；输入、route-plan-set、配置、锁文件、实现提交和 frame/content digest 全部进入 experiment identity。先扫描 `executable_0_6h`，仅在对应输入的 6h 语义/资源构件完整时条件执行 `rolling_0_24h`；full-voyage、Winter M2、P2.1/P3/ARA* 和 candidate 均不启动。

FIFO scan 对可航有向边使用 15 分钟 probe，临界 slack 才递归加密；真实反例判 `FIFO_VIOLATED`，无反例但无区间证明判 `FIFO_UNCERTAIN_NO_INTERVAL_PROOF`，coverage/evaluator/ETA 异常判 uncertain。所有 uncertain、scope mismatch、未知 evaluator 和 fail-closed 对抗场景的 pruning 必须为零。资源前沿只使用 `TemporalDominancePolicy.disabled()`，保留 exact-arrival A* 与独立 zero-heuristic Dijkstra 的路线/ETA/业务字段/失败语义/确定性/扩展计数/RSS/swap/OOM/timeout 证据；Dijkstra 仅为正确性证据，不作为性能基线。

八小时无人值守驱动使用单 CPU、`MemoryMax=4G`、`MemorySwapMax=0`、worker deadline、heartbeat、fsync 和 identity-checked resume。一个输入、目标或 24h 分支失败时只停止该分支并继续其他任务；语义不一致、身份漂移、fail-open pruning、dirty evidence worktree 或 production/frozen 写入为全局硬停止。实验产物留在 `.runtime/experiments/c-p01-m15-real-qualification-20260827-r1/`，最终只追加本节结果，不写 formal latest、replanning baseline 或 frozen artifact。

### 【2026-08-28 | COMPLETED】P0.1-M1.5 真实输入资格审计与资源前沿

本轮从独立 worktree `research/p01-m15-real-qualification-20260827` 的 clean
实现提交 `1716f58abac0d4e505ba5059866bc76024c5acf8` 运行。首次 r1--r3
构件因先后发现的 FIFO worker CPU 亲和性和 nohup 生命周期问题中止，均保留为
`STOPPED_HARD` 诊断构件且不计入样本；r4 由 systemd service 承载并写入最终
`ALL_DONE`，是本节唯一可计入的实验 identity：
`/root/my_project/.runtime/experiments/c-p01-m15-real-qualification-20260827-r4/`。

**冻结身份。** holdout 使用 committed window
`risk-window-sha256-115ad3ab6d7034fabc9428f91c14099b02dff8bb2443569a8d3947187fbb5ff9`，
departure `2026-02-22T00:00:00Z`，start `(5,7)`，segment goals
`executable_0_6h=(7,6)`、`rolling_0_24h=(14,5)`，route-plan-set SHA256
`572ebbfe04a345005431bc08f852d56538e9eefd414ac56fd02a499827436510`。
development 使用 committed window
`risk-window-sha256-bdfd7964df96ffcad7dd78d9830394a0a91d7fbbfde16c0649d2ba2fb68a00ab`，
departure `2026-03-22T00:00:00Z`，start `(5,7)`，segment goals
`executable_0_6h=(7,7)`、`rolling_0_24h=(14,6)`，route-plan-set SHA256
`0b4d4b6a216d34c704de5b6d49d878c326c5ab4f45402435f4b17248670f22be`。
两套输入均通过 145 帧、3600 秒连续性、RiskFrame content/frame identity、
RiskFrame generation、grid/vessel/planner/config 和冻结 layer goal 围栏。
共同 implementation digest 为
`887d485b825872e31f3b7ff4c378ac294b8aad7e3bc1f74d2a97eeea537f4532`，
config tree digest 为 `537e1a1d1ef3f8015402e9b57556518b92a2524993074b4ecc1ccf58143cded4`，
`uv.lock` SHA256 为
`8893cb8387825ca4890ed808f4a98b02ba938337752971dacc3e77f859164f22`。

**FIFO 诊断。** 扫描从 departure-time hard-mask connected component 的全部有向边开始，
基础 probe 间隔 15 分钟，tolerance 1 秒；临界 slack 未触发额外 midpoint 插入。
有限探测从未被提升为连续 FIFO 证书，`coverage_complete=false`，dominance 始终关闭且
pruning 为零。

| 输入/segment | 有向边 | probes | edge evaluations | evaluator errors | 结果 |
|---|---:|---:|---:|---:|---|
| holdout / `executable_0_6h` | 1388 | 25 | 34700 | 748 | `FIFO_UNCERTAIN_EVALUATOR_FAILURE` |
| holdout / `rolling_0_24h` | 1388 | 97 | 134636 | 3569 | `FIFO_UNCERTAIN_EVALUATOR_FAILURE` |
| development / `executable_0_6h` | 1540 | 25 | 38500 | 821 | `FIFO_UNCERTAIN_EVALUATOR_FAILURE` |
| development / `rolling_0_24h` | 1540 | 97 | 149380 | 2944 | `FIFO_UNCERTAIN_EVALUATOR_FAILURE` |

错误样本为 ETA `EtaRefinementError`（`invalid_operator`/coverage 不能稳定收敛）。
没有可审计的真实“后出发、早到达”反例，因此本轮结论不是
`FIFO_VIOLATED`，而是 `REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF`：在取得
到达函数区间单调性证明或保守上下界前，真实输入继续禁止 certified dominance。

**6h exact-arrival 资源前沿。** baseline 和本轮唯一执行路径均为
`TemporalDominancePolicy.disabled()`；每个 objective 运行 2 个独立 worker，另在同一
冻结 edge evaluator 上运行 zero-heuristic exact-arrival Dijkstra 正确性证据。两输入各
6/6 case 均满足路线节点、精确 ETA、速度、风险、成本、confidence、source IDs、失败语义
与 reference 一致，semantic digest deterministic，dominance pruning=0；worker 均为单 CPU、
`MemoryMax=4G`、`MemorySwapMax=0`，无 swap/OOM/timeout。

| 输入 | fastest compute median/P95 ms | low-risk median/P95 ms | recommended median/P95 ms | RSS median KiB | 结果 |
|---|---:|---:|---:|---:|---|
| holdout | 63.49 / 63.86 | 148.03 / 148.36 | 64.50 / 65.07 | 119996--120118 | `RESOURCE_FRONTIER_PASS` (6/6) |
| development | 38.10 / 44.83 | 105.02 / 106.45 | 31.56 / 32.29 | 120196--120244 | `RESOURCE_FRONTIER_PASS` (6/6) |

这只是 dominance-disabled 的可行性/正确性和资源观察，不是 candidate 性能通过，也不
把 Dijkstra 当作独立性能基线。

**24h 条件分支。** 两输入均因 6h 通过而启动 24h；每目标运行 1 个 exact-arrival A*
和 1 个 reference Dijkstra。两输入均为
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL` / `RESOURCE_FRONTIER_PARTIAL`（3/3
记录完成但 valid case 为 0）：holdout fastest A* 完成但 Dijkstra 达到冻结
`queue=50000`，low-risk/recommended A* 达到冻结 `queue=50000`；development 同样在
fastest reference 达到 queue 上限、low-risk/recommended A* 达到 queue 上限。失败不是
swap、OOM 或 worker timeout，且没有放宽 `50k expansions / 100k labels / 50k queue /
400k edge evaluations`。因此 24h 不构成可行资源通过，也不启动 full-voyage。

**收口与下一分支。** P0.1-M1.5 不满足任何 candidate 晋级条件，保持
`REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF` 与
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL`；`TemporalDominancePolicy.disabled()` 仍是
默认行为，未调用 `certified_only(...)`，未修改 B/C、C/D 合同、ingress/service，未写
formal latest、replanning baseline 或 frozen artifact。P2.1 仍为
`MEASUREMENT_INCONCLUSIVE / FORMAL_M2_FAIL_UNCHANGED`，P3 SMO-A* 为
`DEFERRED/RETIRED`，ARA* 为 `M0_FAIL/DEFERRED`。

下一步只能另立、另审计 ETA interval-proof/保守到达界限计划；若改为研究非 FIFO 语义，先
另立 P0.2 label-correcting/Pareto 计划。代码回滚边界为保留 clean 提交
`1716f58`（或其父提交 `626e6d3`），不 merge、不 push；当前 r4 实验产物和 r1--r3
中止证据全部留在 `.runtime/experiments/`，不作为正式发布输入。

### 【2026-08-28 | PLANNED】P0.1-M1.6 证书化边界与资源前沿收口

本轮在 P0.1-M1.5 的 `REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF` 与
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL` 结论上继续推进，candidate 仍默认关闭，
不重开 Winter、P2.1、P3 或 ARA*。执行范围固定保留上一轮选择的
`2.1.1–4.2.2、6.1.1–6.2.1`：

- bounded ETA 的有限区间无 bracket 只报告 uncertainty，不声称全局无根；错误分类保留
  `fixed_point_uncertain` 与 evaluator/coverage 失败语义。
- 新增 C 内部 ETA interval envelope/certificate 侧车；未经 continuity、coverage、
  evaluator 和 scope 完整证明不得授权支配。
- 继续保留 exact-arrival label 默认路径；incumbent + admissible lower-bound 只丢弃
  新生成且不可能改善 incumbent 的 label；已扩展 label 不删除。
- corridor/state bound 只接受带 scope、排除证明和 proof digest 的证书；默认关闭，
  scope/status/evaluator 任一不匹配即 fail-closed 并记录拒绝原因；checkpoint identity
  绑定 state-bound policy digest。
- 资源 runner 追加 ETA failure class、queue-by-elapsed-hour、incumbent/state-bound
  pruning 统计；real-input evidence 仍只使用 `TemporalDominancePolicy.disabled()`，
  不把 Dijkstra 作为性能基线。

代码与证据必须来自 clean 的 C 侧本地提交，实验产物继续留在
`.runtime/experiments/`，不写 formal latest、replanning baseline 或 frozen artifact，
不 push。完成后追加真实输入证据与分支结论；若 interval proof 仍不足或资源边界失败，
分别进入 interval-proof 或 P0.2 非 FIFO 研究计划，不自动启用 candidate。

**历史措辞更正。** 本文较早的 C-ALG-03/03B 记录曾把有限区间未发现符号翻转写作
`no_fixed_point`；该名称保留用于有独立全局排除证明的调用。当前实现和后续证据统一使用
`no_bracket_found` / `fixed_point_uncertain`，避免把有限扫描误报为全局无根结论。

### 【2026-08-28 | COMPLETED】P0.1-M1.6 证书化边界与真实资源前沿收口

本轮完成了 M1.6 的 C 内部实现审计和真实输入资源前沿测量。此前 r5 driver 虽然写出
`ALL_DONE`，但因并行提交导致不同阶段 manifest 分别绑定 `26abfa8` 与 `1657fa2`，该
混合 identity 不作为资格证据；r5 构件原样保留。唯一计入的 r6 来自 detached clean
worktree `/root/my_project/work_package_c_p01_clean`，所有阶段均绑定同一 clean commit
`1657fa23251c2c94e665e5c023f3016ac98c2fa2`，根目录状态为 `ALL_DONE` 且无
`STOPPED_HARD`。

**实现与身份。** bounded ETA 在有限区间无 bracket 时只报告
`no_bracket_found`/`fixed_point_uncertain`，不宣称全局无根；新增的 ETA interval
envelope/certificate、incumbent/admissible-lower-bound 诊断和 state-bound certificate
均保持 C 内部、默认关闭、scope/evaluator/proof 不完整即 fail-closed。state-bound 只可
丢弃新生成 label，checkpoint/session identity 绑定 state-bound policy digest；真实
runner 只使用 `TemporalDominancePolicy.disabled()`。r6 共同 identity 为 implementation
digest `24362c59dc91c0dd200b7265ec21eba69e5d7c633f89d234f9d799be5ded696c`、config tree
digest `537e1a1d1ef3f8015402e9b57556518b92a2524993074b4ecc1ccf58143cded4`、`uv.lock`
SHA256 `8893cb8387825ca4890ed808f4a98b02ba938337752971dacc3e77f859164f22`；两输入均为
145 帧、冻结 route-plan-set、节点和时间窗，所有 manifest `git_dirty=false`。

**FIFO 连续覆盖诊断。** 两个输入、两个 segment 均扫描 departure hard-mask 连通域的
全部有向边，15 分钟 probe、1 秒 tolerance，均未插入额外 midpoint。没有可审计的后出发
早到达反例，且 coverage/evaluator 不完整，故四个构件均为
`FIFO_UNCERTAIN_EVALUATOR_FAILURE`，不能提升为 `FIFO_CERTIFIED`：

| 输入 / segment | 有向边 | probes | edge evaluations | evaluator errors | failure classes | pruning |
|---|---:|---:|---:|---:|---|---:|
| holdout / `executable_0_6h` | 1388 | 25 | 34700 | 748 | `fixed_point_uncertain`, `operator_invalid` | 0 |
| holdout / `rolling_0_24h` | 1388 | 97 | 134636 | 3569 | `fixed_point_uncertain`, `operator_invalid` | 0 |
| development / `executable_0_6h` | 1540 | 25 | 38500 | 821 | `fixed_point_uncertain`, `operator_invalid` | 0 |
| development / `rolling_0_24h` | 1540 | 97 | 149380 | 2944 | `fixed_point_uncertain`, `operator_invalid` | 0 |

**6h exact-arrival 资源前沿。** 两输入各 3 objective × 2 independent workers（共 6/6
valid），并以同一冻结 edge evaluator 运行 zero-heuristic exact-arrival Dijkstra 作为
正确性证据。路线、精确 ETA、速度、风险、成本、confidence、source IDs、失败语义和
semantic digest 均与 reference 一致且 deterministic；无 swap/OOM/timeout，dominance、
incumbent 和 state-bound pruning 均为 0。

| 输入 | fastest compute median/P95 ms | low-risk median/P95 ms | recommended median/P95 ms | RSS median 范围 KiB | 结果 |
|---|---:|---:|---:|---:|---|
| holdout | 82.05 / 93.62 | 190.61 / 223.31 | 67.31 / 68.61 | 120080--120208（peak 120336） | `RESOURCE_FRONTIER_PASS` (6/6) |
| development | 31.77 / 31.96 | 108.52 / 112.82 | 41.86 / 49.67 | 120872--120898（peak 120924） | `RESOURCE_FRONTIER_PASS` (6/6) |

该结果只证明 dominance-disabled exact-arrival 的真实输入可行性、正确性和资源观察，
不构成 candidate 性能通过，也不把 Dijkstra 当作独立性能基线。

**24h 条件分支。** 两输入均因 6h 通过而启动 24h；每个输入 3/3 记录完成但 0 个 valid
case，均为 `RESOURCE_FRONTIER_PARTIAL` /
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL`。holdout fastest A* 完成约
`89187 ms`，但 reference Dijkstra 触及冻结 `queue=50000`（峰值 queue `31948`）；
low-risk/recommended A* 触及同一 queue 上限。development fastest A* 完成约
`64219 ms`，reference 触及 `queue=50000`（峰值 queue `20115`）；low-risk/recommended
A* 同样触及 queue 上限。失败均非 swap、OOM 或 worker timeout，且未放宽
`50k expansions / 100k labels / 50k queue / 400k edge evaluations`。

**收口与下一分支。** M1.6 结论固定为
`REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF` 与
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL`；在取得 ETA 到达函数的可审计 interval
monotonicity proof 或保守上下界前，真实输入继续禁止 certified dominance。若转向非 FIFO，
另立 P0.2 label-correcting/Pareto 计划；若研究资源边界，另立带证书的 corridor/state
bounding 计划。P2.1 仍为 `MEASUREMENT_INCONCLUSIVE / FORMAL_M2_FAIL_UNCHANGED`，P3
SMO-A* 为 `DEFERRED/RETIRED`，ARA* 为 `M0_FAIL/DEFERRED`；未启用 candidate、未接入
ingress/service、未写 formal latest、replanning baseline 或 frozen artifact，未 push。

可审计构件保留于
`/root/my_project/.runtime/experiments/c-p01-m15-real-qualification-20260828-r6/`，r5
及更早中止构件不并入资格样本。代码回滚边界为 `1657fa2` 及其父提交；后续任何真实
输入证据必须从新的 clean identity 启动。

### 【2026-08-28 | PLANNED】P0.1-M1.7/M1.8 与 P0.2-M0：ETA 区间证明、资源限界与非 FIFO 可行性

本轮承接真实输入的 `FIFO_UNCERTAIN_EVALUATOR_FAILURE` 和 24h
`queue=50000` 资源边界，不重开 Winter、P2.1、P3 或 ARA*，不启用 candidate。继续保留
清单 `2.1.1–4.2.2、6.1.1–6.2.1`，研究顺序固定为“先证明、再限界、最后非 FIFO”。

**治理与身份。** 从 clean commit `067a28e` 的 C 侧隔离分支
`research/p01-m17-eta-proof-20260828` 运行，复用已有 145 帧 holdout/development；每个
实验绑定 implementation/config/`uv.lock`、RiskFrame 与 route-plan-set、scope、ETA policy、
搜索限制和 evaluator digest。不得修改 B/C、C/D 合同、ingress/service、formal latest、
replanning baseline 或 frozen artifact；最终 clean 验证后移除辅助 worktree，不 push。

**P0.1-M1.7 ETA 区间证明。** 新增 C 内部 ETA interval qualification sidecar 和独立
runner（schema `c.p0.1-temporal-eta-interval.v1`），以
`g(t)=implied_travel_hours(t)-t` 为对象，按 RiskFrame 时间边界、hard-mask 变化和
evaluator 域切分区间。只有 interval evaluator 覆盖完整、evaluator/continuity/scope 已
认证，并满足 contraction 或连续端点符号变化时，才可生成可用证书；finite no-bracket、
discontinuity、coverage 缺口、未知 evaluator 和失败均保持 `UNCERTAIN_*`。runner 必须
持久化 manifest/cases/interval-summary/comparison-summary/heartbeat 及 `ALL_DONE` 或
`STOPPED_HARD`。真实 6h/24h 仅做 FIFO interval qualification，不授权 dominance。

**P0.1-M1.8 exact-arrival 资源限界。** 不提高 `50k expansions / 100k labels /
50k queue / 400k edge evaluations`。研究带完整 `TemporalScope`、allowed nodes/region、
排除证明和 `proof_digest` 的 state-bound/corridor envelope；只允许丢弃新生成 label，
禁止删除已扩展 label、beam/近似剪枝或从 reference oracle 注入答案。先在 synthetic
small/medium/stress 验证路线及全部业务字段与 exact-arrival oracle 一致、至少一次真实
certified pruning，且 uncertain/scope mismatch/non-FIFO 场景 pruning=0；通过后才做真实
6h 资源诊断。24h queue 超限保持 `EXACT_LABEL_RESOURCE_FAIL`，不放宽限制。

**P0.2-M0 非 FIFO 可行性。** 只建立 C 内部设计和 test-only oracle，不接入真实 runner 或
生产路径。定义有限状态域的 label-correcting/Pareto labels、终止/取消/重复状态/资源
失败语义，并覆盖 2×2 非 FIFO、同桶不同精确 ETA、周期 ETA、hard-mask、evaluator failure
和资源超限 fixture。M0 只有在语义、终止、adversarial 可复现、oracle 对照和 fail-closed
矩阵完整时才标记 `READY_FOR_P0.2_IMPLEMENTATION_PLAN`。

**固定分支与收口。** interval proof 通过只标记
`READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN`；真实输入仍须另立资格计划。interval 仍
uncertain 时保持 `REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF`；state-bound 真实
资源不足时标记 `REAL_INPUT_RESOURCE_BOUND_INSUFFICIENT`；发现真实 FIFO 反例才另立完整
P0.2。全部实验和测试完成后仅追加结果与 commit/identity，保留 M0/M1/M1.5/M1.6、M2J/M2K、
P3、ARA* 历史。

### 【2026-08-28 | COMPLETED】P0.1-M1.7/M1.8 与 P0.2-M0：ETA 区间证明、资源限界与非 FIFO 可行性

本轮从 C 侧 clean 基线 `067a28e` 建立隔离分支
`research/p01-m17-eta-proof-20260828`，计划段先以 `7988e62` 提交；最终实现和证据
绑定 clean commit `3cb9e40f4e6826ebbf62290282f11b7cc8cdb352`。辅助 worktree 为
`/root/my_project/.runtime/worktrees/c-p01-m17-eta-proof`，完成收口后移除，分支保留。
本轮不修改 B/C、C/D 合同、ingress/service、正式 planner 或 production 路径，不写
formal latest、replanning baseline 或 frozen artifact，不 push。

**身份与输入。** synthetic ETA、synthetic state-bound 和真实 interval runner 均绑定
实现文件摘要、配置树 digest
`537e1a1d1ef3f8015402e9b57556518b92a2524993074b4ecc1ccf58143cded4`、`uv.lock` SHA256
`8893cb8387825ca4890ed808f4a98b02ba938337752971dacc3e77f859164f22`、policy/search
limits/evaluator identity；真实构件另绑定冻结 RiskWindow、route-plan-set、scope 和
segment。真实 r4 构件的 ETA runner implementation digest 为
`e64c03210c587f8ed63e941d89f99a2c95099538babb62788685152c009f3b2f`，state-bound runner
digest 为 `c2293c7c02f55988fe456e483752170a30fb9a86cc788af352adea086c816558`，均为
`git_dirty=false`。

**P0.1-M1.7 ETA interval qualification。** C 内部 sidecar 以
`g(t)=implied_travel_hours(t)-t` 的保守 interval residual 为对象，显式按边界切分
RiskFrame/hard-mask/evaluator 区间；有限采样、无 bracket、覆盖不足、未知 evaluator、
不连续边界和失败均不能生成可用证书。独立 synthetic runner
`c.p0.1-temporal-eta-interval.v1` 在 small/medium/stress × 三 objective × 七场景上
完成 `63/63`，状态矩阵为 9 `ROOT_EXISTS_UNIQUE`、9 `ROOT_EXISTS_NONUNIQUE`、9
`ROOT_EXCLUDED` 和 36 个 `UNCERTAIN_*`；可用证书 18，uncertain 证书可用数为 0，
`fail_closed=true`。

真实输入使用完整 145 帧 holdout/development，15 分钟 probe、1 秒 tolerance、最多四级
自适应边界检查；r4 每段均 `ALL_DONE`，没有插入 midpoint，也没有发现后出发早到达反例或
产生 certificate digest。所有段的 dominance policy 为 `disabled`，coverage/evaluator
均未认证：

| 输入 / segment | 有向边 | probes | edge evaluations | evaluator errors | failure classes | 结果 |
|---|---:|---:|---:|---:|---|---|
| holdout / `executable_0_6h` | 1388 | 25 | 34700 | 748 | `fixed_point_uncertain`, `operator_invalid` | `FIFO_UNCERTAIN_EVALUATOR_FAILURE` |
| holdout / `rolling_0_24h` | 1388 | 97 | 134636 | 3569 | `fixed_point_uncertain`, `operator_invalid` | `FIFO_UNCERTAIN_EVALUATOR_FAILURE` |
| development / `executable_0_6h` | 1540 | 25 | 38500 | 821 | `fixed_point_uncertain`, `operator_invalid` | `FIFO_UNCERTAIN_EVALUATOR_FAILURE` |
| development / `rolling_0_24h` | 1540 | 97 | 149380 | 2944 | `fixed_point_uncertain`, `operator_invalid` | `FIFO_UNCERTAIN_EVALUATOR_FAILURE` |

因此真实结论保持 `REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF`，不是
`FIFO_VIOLATED`，也不是 `FIFO_CERTIFIED`；在得到连续到达函数的 interval monotonicity
证明或保守上下界之前，certified dominance 继续禁用。可审计构件位于
`/root/my_project/.runtime/experiments/c-p01-m17-eta-interval-20260828-r4/`。

**P0.1-M1.8 state-bound。** 新增 proof-carrying allowed-node/corridor sidecar，证书
绑定完整 `TemporalScope`、allowed/excluded nodes、coverage、evaluator 和 proof digest；
只有新生成 label 可被丢弃，已扩展 label 不删除。synthetic runner
`c.p0.1-temporal-state-bound.v1` 在 3 profile × 3 objective × certified/coverage-incomplete/
scope-mismatch 上完成 `27/27`：9 个 certified case 观察到真实 pruning（总计 9），语义
digest 全部一致；coverage incomplete、scope mismatch 的拒绝场景 pruning 为 0。
搜索上限仍为 `50k expansions / 100k labels / 50k queue / 400k edge evaluations`，
没有 beam/近似剪枝或 oracle 路线注入。构件位于
`/root/my_project/.runtime/experiments/c-p01-m17-eta-proof-20260828-r3/state-bound/`。

本轮没有用 state-bound candidate 重跑真实 24h；继承 M1.6 的真实资源事实：6h
dominance-disabled 可行，24h 在冻结 `queue=50000` 处仍为
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL`。这不是放宽上限或择优重跑的依据；后续若
研究真实 corridor/envelope，必须另立带排除证明的计划。synthetic 构件位于
`/root/my_project/.runtime/experiments/c-p01-m17-eta-proof-20260828-r3/`。

**P0.2-M0 非 FIFO 可行性。** 新增 C 内部、test-only exact-arrival label-correcting
reference，不从 production/ingress 导出。六个 adversarial focused tests 覆盖 2×2 后到
达更优 suffix、同节点不同精确 ETA、周期/label 上限、evaluator/hard-mask failure、
取消和 arrival-before-departure；失败结果不携带部分 route label。全量验证中该矩阵通过，
没有公共 API、合同或 ingress 变化。因此仅标记
`READY_FOR_P0.2_IMPLEMENTATION_PLAN`，不宣称连续海洋模型上的全局最优，也不直接实现或
启用 production candidate。

**验证与最终分支。** 全量 pytest 为 `379 passed, 3 skipped`；跳过项仅因并行
orchestrator worktree 中退休的 M2J 诊断脚本缺失。聚焦 ETA/state-bound/non-FIFO/runner
测试通过，Ruff、`uv lock --check`、CLI smoke、active/archive import boundary 和
`git diff --check` 通过。直接 `UV_OFFLINE=1 make check` 仍因本辅助 worktree 没有
`.mamba-env/bin/uv` 在 Makefile lint 目标处退出；使用已有等价 Python/Ruff/UV 环境完成了
上述可执行检查，未修改依赖或 lock 文件。

本轮最终状态固定为：

- `REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF`；不设置
  `READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN`；
- `REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL`；不提高任何冻结资源上限；
- `READY_FOR_P0.2_IMPLEMENTATION_PLAN`（仅 test-only 设计，不是实现或生产资格）；
- candidate、Winter、P2.1、P3 SMO-A*、ARA* 状态全部不变。

所有实验产物仍在 `.runtime/experiments/`，没有写入 formal/latest/frozen 路径。完成
clean 验证后移除本轮辅助 worktree，保留本地分支和构件，等待下一份独立的 interval-proof、
真实 dominance 资格或 P0.2 实现计划。

### 【2026-08-28 | PLANNED】P0.1-M1.9：ETA 区间包络与真实 FIFO 资格审计

本轮从 clean 基线 `067a28e` 建立集成分支
`research/p01-m19-eta-interval-20260828`，以 cherry-pick 审计上一轮
`7988e62^..424b4af` 的方式保留既有 M1.7/M1.8/P0.2 研究历史。主线聚焦于
`RiskSampler` 的 C 内部区间包络和唯一根资格证明；state-bound 与非 FIFO 继续保持有界、
test-only 研究。辅助 worktree 完成 clean 验证后移除，分支和实验构件保留，不 push、
不自动合入正式工作树。

**RiskSampler 区间原语。** 新增私有 `_sample_interval(start, end, longitude, latitude)`
及 `RiskIntervalSample`，按所有 RiskFrame 边界切分，复用既有双线性空间贡献，数值字段
采用时间端点保守包络和 outward rounding，hard-mask 保守 OR、confidence 保守下界，
覆盖、source IDs、evaluator digest 和失败原因全部可审计。UTC、窗口/gap、非有限值、
缺失和 evaluator 异常均 fail-closed；不改变 `sample()`、正式 `plan()` 或公共合同。

**ETA interval evaluator。** 新增 C 内部 `TemporalEtaIntervalEvaluator`，以完整
`TemporalScope`、显式 bounded ETA policy、edge sample points、vessel model 和
`g(t)=implied_travel_hours(t)-t` 生成 `EtaOperatorIntervalEvidence`。只有完整覆盖、
认证 evaluator、scope 完全匹配、无 hard-mask/连续性断点，且有独立 contraction
`<1`、image 包含于 domain 的 `ROOT_EXISTS_UNIQUE` 才可授权；端点变号的 non-unique、
finite no-bracket、discontinuity、coverage/evaluator failure 和无证明均为
`UNCERTAIN_*`，不得转换为真实 dominance 资格。

**Synthetic proof gate。** 使用独立 schema `c.p0.1-temporal-eta-proof.v1` runner，
在 small/medium/stress × 三 objective 覆盖 unique、non-unique、excluded、no-bracket、
hard-mask/RiskFrame discontinuity、coverage/evaluator failure、cycle、max-iterations、
terminal mismatch、scope 和 checkpoint digest mismatch。所有端点/内部采样必须落在包络
内；uncertain/discontinuous 场景授权和 pruning 均为零；必须保存 manifest/cases/
eta-interval/comparison-summary/heartbeat 及 `ALL_DONE` 或 `STOPPED_HARD`。

**真实输入审计。** Synthetic gate 全部通过且集成分支 clean 后，才对冻结 145 帧
holdout/development 执行 6h、条件性 24h FIFO interval qualification；继续使用 15 分钟
probe、1 秒 tolerance、最多四级边界细分，真实搜索始终 dominance-disabled。结果只允许
`REAL_INPUT_FIFO_VIOLATED`、`REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF`、
`READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN` 或 `INVALID/PENDING`，不自动启用 candidate、
不重开 Winter/P2.1/P3/ARA*，也不提高冻结资源上限。

**验证和边界。** 先跑 RiskSampler/ETA/session/checkpoint/state-bound/non-FIFO/runner
聚焦测试，再跑默认路径回归、active/archive import、Ruff、offline Make/lock/sync、CLI
smoke 和 diff check。只追加本轮结果，保留 M0/M1/M1.5/M1.6/M1.7/M1.8、M2J/M2K、P3、
ARA* 历史；实验产物写入新的 `.runtime/experiments/` 目录，不写 formal latest、
replanning baseline 或 frozen artifact。

### 【2026-08-28 | COMPLETED】P0.1-M1.9：ETA 区间包络与真实 FIFO 资格审计

本轮在不改变正式 planner、合同或生产入口的前提下完成。以 `067a28e` 为基线建立
`research/p01-m19-eta-interval-20260828`，通过 cherry-pick 审计并保留上一轮
`7988e62^..424b4af` 的 M1.7/M1.8/P0.2 历史；辅助 worktree 为
`/root/my_project/.runtime/worktrees/c-p01-m19-eta-interval`。实现证据使用 clean
commit `9ff131feabbf11c98a1040a4a6c5c2dd2b5f8e2f`，未修改正式
`research-validation-system` worktree，不 push。完成收口后移除本轮辅助 worktree，保留
本地分支和实验构件。

**区间采样与 ETA 证书。** C 内部新增 `RiskSampler._sample_interval` 和
`RiskIntervalSample`：按 RiskFrame 边界枚举覆盖帧，复用既有双线性空间贡献，时间端点
采用 outward rounding，hard-mask 使用保守 OR，confidence 和 speed factor 使用保守
上下界；窗口/gap、缺失、非有限值和 evaluator 异常均返回不完整证据而非安全替代值。
新增 `TemporalEtaIntervalEvaluator` 和 `EtaOperatorIntervalEvidence`，显式绑定 bounded
ETA policy、完整 `TemporalScope`、partition/boundary evidence 和 evaluator digest。
只有完整覆盖、认证 evaluator、scope 完全匹配、无 discontinuity 且有独立 contraction
`<1` 时的 `ROOT_EXISTS_UNIQUE` 才可授权；non-unique、finite no-bracket、无 contraction、
coverage/evaluator failure 和边界不连续均不得授权。`sample()`、正式 `plan()`、默认
`TemporalDominancePolicy.disabled()` 和公共合同保持不变。

real runner 的 resume 只消费完整且 identity/scope/input/segment/probe 数匹配的已完成
edge 记录，重复或错身份构件 fail-closed；每条 evidence 继续 `fsync`，点扫描汇总在恢复
时原子替换，避免重复记录。该修复不改变算法结果，只补齐可恢复审计边界。

**Synthetic proof gate。** 独立 runner schema 为
`c.p0.1-temporal-eta-proof.v1`，最终实现身份实验为
`/root/my_project/.runtime/experiments/c-p01-m19-eta-interval-20260828-proof-r3/`，
experiment id 为 `c.p0.1-temporal-eta-proof.v1-991d5b72297a32a1`。small/medium/stress
× 三 objective × 13 场景共 `117/117`，其中 `9` 个 unique、`9` 个 non-unique、`9` 个
root exclusion、`90` 个 uncertain；`authorization_count=9`、`fail_closed=true`。
所有 endpoint/interior 样本均落在 interval 包络内，只有 contraction-backed unique root
授权；non-unique、discontinuity、coverage/evaluator failure、cycle、max-iterations、
terminal mismatch、scope/policy/checkpoint digest mismatch 均未授权。proof runner 已生成
manifest/cases/eta-interval/comparison-summary/heartbeat 和 `ALL_DONE`。

**真实 6h 资格审计。** 使用完整 145 帧冻结 RiskWindow、既定 route-plan-set、15 分钟
probe、1 秒 tolerance，真实搜索始终 `dominance_policy=disabled` 且
`dominance_pruned=0`。holdout/development 两段均从同一 clean implementation identity
`9ff131f...` 启动，未写 formal/latest/frozen 路径：

| 输入 / segment | 有向边 | probe | interval evaluations | interval status counts | point counterexample | authorization |
|---|---:|---:|---:|---|---|---:|
| holdout / `executable_0_6h` | 1388 | 25 | 34700 | `UNCERTAIN_DISCONTINUITY=34050`; `UNCERTAIN_EVALUATOR_FAILURE=650` | 无 | 0 |
| development / `executable_0_6h` | 1540 | 25 | 38500 | `UNCERTAIN_DISCONTINUITY=38000`; `UNCERTAIN_EVALUATOR_FAILURE=500` | 无 | 0 |

两段 summary 均为 `REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF`，原因是未观察到
反例但连续性/contraction/evaluator 证明不完整；`coverage_complete=false`、
`evaluator_certified=false`。point scan 的 evaluation errors 分别为 `748` 和 `821`，
自适应插点为 `0`。采样失败均显式记录为 `RiskSamplingError`，没有从图中静默删除边，也
没有生成可用 certificate digest。holdout 构件为
`c.p0.1-temporal-eta-proof-real.v1-2798d477afc7c5d2`，development 构件为
`c.p0.1-temporal-eta-proof-real.v1-a1bf0c3afd101c1b`，均绑定 145 帧、route-plan-set、
`uv.lock`、scope、bounded policy 和 evaluator/config digest，manifest 中
`git_dirty=false`。

24h `rolling_0_24h` 未启动：两输入的 6h 都未通过 interval proof 资格门，按本轮“仅在
对应 6h 通过后执行 24h”的条件停止；这不是新的 24h 资源通过/失败结论。既有 M1.6 的
真实 24h `queue=50000` 资源失败事实保持不变，未提高任何 queue/label/expansion/edge
evaluation 上限，也未择优重跑。

**验证与收口。** 聚焦 ETA/RiskSampler/runner/resume 测试通过；全量 pytest 为
`395 passed, 3 skipped`，跳过项仍仅是并行 orchestrator worktree 缺少已退休的 M2J
诊断脚本。本轮变更涉及的 Ruff 目标通过；全量 Ruff 被既有、非本轮的
`scripts/benchmark_bc_coupling.py:721` E501 阻塞。`uv lock --check`、CLI smoke、
active/archive import boundary 和 `git diff --check` 通过。`UV_OFFLINE=1 make check` 仍因辅助 worktree 没有
`.mamba-env/bin/uv` 在 Makefile lint 目标处退出；用已有等价 Python/Ruff/UV 环境完成
可执行检查。离线 sync 因 numpy wheel 不在本地缓存而阻塞，未修改 `uv.lock`、依赖或源码。
实验构件全部留在 `.runtime/experiments/`，没有写 formal latest、replanning baseline
或 frozen artifact。

本轮最终状态固定为：

- `REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF`；不设置
  `READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN`；
- state-bound 的既有 synthetic `27/27 PASS` 和 P0.2 test-only 设计历史保留，本轮未对
  真实 24h 启用 state-bound，也未扩展非 FIFO 到真实 runner；
- candidate、Winter M1/M2、P2.1、P3 SMO-A*、ARA* 和正式 ETA 默认策略全部不变；
- 下一步只能另立保守 ETA interval proof/evaluator 研究，或在真实反例出现时另立 P0.2
  label-correcting 计划；不得由本轮 uncertain 结果自动启用 dominance。

### 【2026-08-28 | PLANNED】P0.1-M1.10/M1.11 解析 ETA/FIFO 证明与证明型资源 Corridor

本轮在 M1.9 的 `REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF` 和 M1.6 的
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL` 基础上推进。M1.9 已集成到正式
`research-validation-system` 的本地 clean tip `62333b6`；本轮从独立分支
`research/p01-m110-analytic-eta-proof-20260828` 开始，candidate、Winter、P2.1、P3
和 ARA* 均保持关闭或原状态。

**主线一：解析 ETA 与 FIFO 资格。** M1.9 的 interval sampler 将扩展为带时间斜率、
切换点和可航三态的保守证据。ETA 唯一根使用机械推导的 contraction certificate；FIFO
则独立使用 fixed-point implicit sensitivity/广义斜率证书。唯一根不自动等于 FIFO，
离散采样、non-unique root、boundary/hard-mask discontinuity、coverage/evaluator
缺口和 scope mismatch 一律 fail-closed。正式 `sample()`、`plan()`、默认
`TemporalDominancePolicy.disabled()` 与公共合同不变。

**主线二：证明型时空资源限界。** 先以最大可航速度、时间窗、静态可航域和可证明的
objective lower bound 生成 corridor certificate，进行无剪枝 label replay。只有 synthetic
oracle 零错误且 holdout/development 的 low-risk/recommended 均达到至少 20% projected
label reduction 时，才进入 test-only pruning 验证；继续冻结 `50k expansions / 100k
labels / 50k queue / 400k edge evaluations`，不使用 beam、近似剪枝或 oracle route 注入。

**真实输入与收口门。** synthetic analytic proof 全部通过后扫描 145 帧 holdout/development
的 6h；两输入相关 partition 的证明覆盖率达到 90% 且无硬错误才条件运行 24h。真实
dominance readiness 仍要求 100% traversable partition 具备完整 unique-root/FIFO 证书或
独立阻塞排除证据，且 pruning 为零。24h reference 若触及冻结 queue 上限，不形成正确性
或性能通过。实验构件只写 `.runtime/experiments/`；SSOT 仅追加本轮结果，不写 formal
latest、replanning baseline 或 frozen artifact，不 push。

**P0.2 维护。** 扩展 test-only label-correcting/Pareto oracle 的周期 ETA、重复精确到达、
取消、资源上限和 evaluator failure fixture；没有真实 FIFO 反例时不接入真实 runner 或生产
planner，仅记录可执行性设计状态。

### 【2026-08-28 | COMPLETED】P0.1-M1.10/M1.11：解析 ETA/FIFO 证明与证明型资源 Corridor

本轮在已集成 M1.9 的本地 clean tip `62333b6` 上建立隔离分支
`research/p01-m110-analytic-eta-proof-20260828`，先提交本段 PLANNED 记录
`c3d9295`，随后按提交序列完成实现。最终证据使用 clean implementation commit
`6b77d87e1af0c8f6b182931e6c340016f11047a6`；辅助 worktree 为
`/root/my_project/.runtime/worktrees/c-p01-m110-analytic-eta-proof`。本轮未修改 B/C、C/D
合同、ingress/service、公共 planner、formal latest、replanning baseline 或 frozen
artifact，不 push；M2J/M2K、P2.1、P3、ARA* 和 candidate 状态保持不变。

**解析 interval evidence。** `RiskIntervalSample` 增加 confidence 上界、risk/speed
factor slope envelope 以及 `ALWAYS_NAVIGABLE/ALWAYS_BLOCKED/TRANSITION_OR_UNKNOWN`
三态；`RiskSampler._sample_interval` 仍只作 C 内部 sidecar，沿用已有双线性空间贡献、
时间 frame partition、hard-mask OR、outward rounding 和 fail-closed coverage。新增
`derive_operator_sensitivity` 与 `EtaAnalyticCertificate`，由 interval image、vessel
speed 单调性和隐式 arrival slope 机械推导 contraction/唯一根/FIFO 状态。只有完整
coverage、认证 evaluator、完整 scope、无连续性/可航性断点、image 包含 domain 且
contraction `<1` 时才授权；non-unique、unknown、coverage/discontinuity、scope/policy
mismatch 和无证明均不授权。`TemporalEtaIntervalEvaluator.evaluate_analytic` 不读取
调用者布尔 flag 作为证明，`sample()`、正式 `plan()` 和默认
`TemporalDominancePolicy.disabled()` 保持原行为。

**Synthetic analytic proof gate。** 独立 runner schema 为
`c.p0.1-temporal-eta-analytic-proof.v1`，构件目录为
`/root/my_project/.runtime/experiments/c-p01-m110-analytic-eta-proof-20260828-r4/`，
experiment id 为 `c.p0.1-temporal-eta-analytic-proof.v1-49a2c2faae706da9`。在
small/medium/stress × 三 objective × 13 场景 × 3 repetitions 上完成 `351/351`：
`authorization_count=27`（仅 contraction-backed unique root）、`fifo_certified_count=27`、
`deterministic=true`、`fail_closed=true`、`all_expected=true`、
`pruning_zero_for_rejected=true`。覆盖 finite no-bracket、continuous non-unique、root
exclusion、hard-mask/RiskFrame boundary、coverage/evaluator failure、ETA cycle、
max-iterations、terminal mismatch、scope 和 policy/checkpoint digest mismatch；未授权
场景均未产生 dominance 资格。

**证明型 Corridor。** 新增 C 内部 `AdmissibleBoundEvidence` 和
`TemporalCorridorEvidence`，以独立 forward/reverse admissible lower bounds 只排除
`forward + reverse > horizon` 的新生成状态，使用 downward rounding 保留精确边界；
scope、evaluator、coverage、proof digest 任一不匹配即 rejected，绝不删除已扩展 label、
注入 reference route 或使用 beam/近似剪枝。独立 runner
`c.p0.1-temporal-corridor-proof.v1` 构件目录为
`/root/my_project/.runtime/experiments/c-p01-m111-corridor-proof-20260828-r3/`，
experiment id 为 `c.p0.1-temporal-corridor-proof.v1-0f1873aabcf8628c`。small/medium/
stress × 三 objective × certified/coverage-incomplete/scope-mismatch/non-admissible
完成 `36/36`，`semantic_match=true`、`deterministic=true`、`fail_closed=true`；9 个
certified case 共观察到 `1008` 次安全排除，27 个 rejected case 剪枝为 `0`。既有实际
planner state-bound regression 也保持 `27/27 PASS`，未改变冻结的
`50k expansions / 100k labels / 50k queue / 400k edge evaluations`。

**真实 145 帧 FIFO 资格审计。** 新增独立
`scripts/benchmark_temporal_eta_analytic_real.py`，复用冻结 fixture loader，但输出
`manifest.json`、`cases.jsonl`、`fifo-scan.jsonl`、`eta-interval.jsonl`、
`resource-frontier.jsonl`、`comparison-summary.json`、`heartbeat.json` 和终态标记。真实
搜索仍固定 `dominance_policy=disabled`、`dominance_pruned=0`，每个 objective 重复计算
evidence 以核验 deterministic；未调用 `certified_only(...)`。

| 输入 / segment | directed edges | objectives | interval evaluations | status counts | FIFO counterexample | authorization |
|---|---:|---:|---:|---|---|---:|
| holdout / `executable_0_6h` | 1388 | 3 | 104100 | `UNCERTAIN_DISCONTINUITY=102150`; `UNCERTAIN_COVERAGE=1950` | 无 | 0 |
| development / `executable_0_6h` | 1540 | 3 | 115500 | `UNCERTAIN_DISCONTINUITY=114000`; `UNCERTAIN_COVERAGE=1500` | 无 | 0 |

上述最新 clean-identity 构件分别为
`c.p0.1-temporal-eta-analytic-real.v1-c994a2cb080e2b9b`（holdout）和
`c.p0.1-temporal-eta-analytic-real.v1-1037dfd4cc6dd1d2`（development），均绑定完整
145 帧、route-plan-set、config/lock digest、bounded ETA policy、scope、search limits，
并记录 `deterministic=true`、`fifo_certified_count=0`、`fifo_violated_count=0`。
未认证 evaluator 与跨 frame/连续性证据使两输入均固定为
`REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF`；这不是 `FIFO_VIOLATED`，也不是
`READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN`。6h gate 未通过，因此按条件门不启动新的
24h interval qualification；这也不改变既有 M1.6 的
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL` 事实，未形成新的 24h 资源结论。

**P0.2 维护与验证。** 非 FIFO test-only oracle 新增同 exact-arrival 成本替换和
`maximum_elapsed` 边界测试，聚焦 non-FIFO/ETA/RiskSampler/Corridor/runner 共 `49` 项
通过；全量 pytest 为 `416 passed, 3 skipped`，跳过仍仅为并行 orchestrator worktree
中退休的 M2J 诊断脚本缺失。全量 Ruff、`uv lock --check --offline`、CLI smoke、
active/archive import boundary 和 `git diff --check` 通过。`UV_OFFLINE=1 make check`
在隔离 worktree 不能直接完成：本地没有 `.mamba-env/bin/uv`；通过复用已有环境时，
offline build cache 缺 `hatchling`，`uv sync --check` 还明确显示 worktree 环境与正式
worktree 包路径不同。未修改环境、依赖或 `uv.lock`，等价的直接 pytest/Ruff/lock 检查已
执行并保留该阻塞证据。

本轮最终状态固定为：

- 真实 holdout/development：`REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF`；
  certified dominance 继续默认关闭；
- synthetic analytic ETA：`ANALYTIC_ETA_PROOF_MATRIX_PASS`；仅 synthetic 证明场景具备
  研究授权，不代表真实输入资格；
- synthetic proof-carrying Corridor：`TEMPORAL_CORRIDOR_MATRIX_PASS`；真实 24h
  state-bound 未启动；
- P0.2：`READY_FOR_P0.2_IMPLEMENTATION_PLAN` 仍仅表示 test-only 可行性，不是生产实现；
- 下一步只能另立真实 evaluator/interval proof、带 scope 的 corridor/envelope 或
  P0.2 label-correcting 计划；不得自动进入 Winter、P2.1、P3、ARA*、formal latest 或
  candidate 启用。

### 【2026-08-28 | PLANNED】P0.1-M1.12/M1.13：分区 ETA 证明与真实 Corridor 资源审计

本轮承接 M1.10/M1.11 的 synthetic 证明通过、真实输入
`REAL_INPUT_FIFO_UNCERTAIN_REQUIRES_INTERVAL_PROOF` 和既有 24h queue 上限事实。正式
分支先本地 fast-forward 集成 M1.10/M1.11，再从 clean identity 建立隔离实现分支；不改
B/C、C/D 合同、ingress/service、公共 planner、formal latest、replanning baseline 或
frozen artifact，不启用 candidate，不重开 Winter/P2.1/P3/ARA*。

**M1.12 分区 evaluator certificate。** 在 C 内部新增由真实 `RiskSampler` 机械生成的
分区 evidence/certificate，替换真实 runner 中仅由 scope 字符串表达的
`evaluator_certified` 标志。证书绑定 sampler 规则与实现 digest、RiskFrame 内容/时间边界、
空间贡献、edge fractions、阈值、bounded ETA policy、完整 `TemporalScope` 和 proof digest。
按 departure/travel 域切分 RiskFrame、hard-mask、confidence、speed-factor 与阈值事件；
每个稳定分区独立计算 interval image、contraction、arrival slope 和 navigability，并对
边界执行左右包络检查。只有唯一 root、contraction `<1`、FIFO 单调和完整 scope 才授权；
负跳变保存 `FIFO_VIOLATED` 反例，边界重叠、coverage/evaluator 缺口和切分耗尽均保持
`UNCERTAIN_*`。阻塞分区只能形成排除证据，不能包装成 ETA 成功。

**M1.12 证明与真实审计。** 新 synthetic schema 覆盖稳定/多分区唯一根、多根、正负边界
跳变、hard-mask/阈值、coverage/evaluator failure、outward rounding 及 scope/policy/
checkpoint/resume 漂移。证明通过后，对完整 145 帧 holdout/development 执行 6h 全域
审计，真实搜索仍 `dominance_policy=disabled`。单输入 6h 的 certified-or-blocked 覆盖率
达到 90% 且无硬错误时才可在时间预算内继续 24h 诊断；只有 100% 分区证明完整才可标记
`READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN`。

**M1.13 证明型 Corridor。** 复用既有 `TemporalCorridorEvidence`，新增真实输入投影 runner，
基于最大有效船速和有限网格的 forward/reverse admissible lower bounds 计算 projected
label reduction、queue peak 和 rejection reason。任何 objective lower bound 必须绑定冻结
cost model 与实际 incumbent，不得注入 reference route。仅当 synthetic 通过、holdout/
development 的 low-risk/recommended 预计 reduction 均达到 20%、scope/proof 完整时，才做
真实 6h test-only pruning；24h 只保留投影或独立诊断，不形成性能/正确性晋级。

**时间与收口。** 截止 `2026-08-29 19:00 CST`，16:00 后不再启动新 worker，预留至少两小时
完整测试、SSOT 证据和 Git 收口。最终状态固定为真实 FIFO violated、partition proof
uncertain、resource bound insufficient 或 separate-plan-ready 之一；所有 uncertain、
identity 漂移、fail-open 和语义不一致均不得作算法结论。实验构件只写 `.runtime/experiments/`，
最终正式分支 clean、无辅助 worktree、无 push。

### 【2026-08-29 | COMPLETED】P0.1-M1.12/M1.13：分区 ETA 资格与 Corridor 研究收口

本轮在隔离 worktree `/root/my_project/.runtime/worktrees/c-p01-m112-partition-proof` 的
clean implementation tip 上完成；最终代码提交为 `3e35982`（随后仅追加本段 SSOT）。本轮
没有修改 B/C、C/D 合同、ingress/service、公共 planner、formal latest、replanning baseline
或 frozen artifact，也没有 push。`TemporalDominancePolicy.disabled()`、正式 `plan()` 和
candidate/Winter/P2.1/P3/ARA* 状态保持不变。所有实验均绑定 implementation、config、lock、
RiskFrame/route-plan-set、scope 与 fixture digest；实验产物仅存于 `.runtime/experiments/`。

**M1.12 synthetic 分区证明。** 当前代码下 runner
`c.p0.1-temporal-evaluator-partition-proof.v1-5183bafc6b3524d6`
（目录 `c-p01-m112-partition-proof-20260829-r4`）在 small/medium/stress × 三 objective
完成 `216/216`，`deterministic=true`、`fail_closed=true`、`all_expected=true`，其中
`partition_certified=54`、`partition_rejected=162`、负边界场景 `27`。由于固定 departure
分区不能证明整个 departure domain 的 FIFO，`permits_dominance=false`，授权数为 `0`；这
是预期的 fail-closed 结果，不是 production dominance 资格。

**M1.12 真实 145 帧资格审计。** 使用完整 holdout/development、`executable_0_6h`、25 个
departure probes、三 objective、`dominance_policy=disabled` 串行完成。两输入均发现可审计的
interval 级负 travel-operator jump，因此固定为 `REAL_INPUT_FIFO_VIOLATED`，不启动新的
24h qualification，也不允许 certified dominance：

| 输入 | experiment id | directed edges × objectives | interval evaluations | FIFO violated | certified probes | uncertain/coverage | 首个反例 |
|---|---|---:|---:|---:|---:|---|---|
| holdout | `c.p0.1-temporal-evaluator-partition-real.v1-e0ff8c805db67d0b` | `1388 × 3` | `104100` | `43500`（每目标 `14500`） | `101958` | `686`/objective | edge `[(0,0),(0,1)]`，`2026-02-22T00:00Z`，2h boundary；左 image `2.8943256398h`，右 image `2.7839194360h` |
| development | `c.p0.1-temporal-evaluator-partition-real.v1-adfa392fc050c67c` | `1540 × 3` | `115500` | `40776`（每目标 `13592`） | `113685` | `605`/objective | edge `[(0,0),(0,1)]`，`2026-03-22T00:00Z`，2h boundary；左 image `2.9088543441h`，右 image `2.9063708135h` |

两输入三目标均 `deterministic=true`、`dominance_pruned=0`，并记录了
`interval_domain_coverage_incomplete` 与 `partition_root_or_fifo_proof_incomplete`；负跳变
优先于“多数分区 certified”，不能降级成 `FIFO_UNCERTAIN` 或 `READY_FOR_SEPARATE_REAL_DOMINANCE_PLAN`。
该结论只说明当前真实 evaluator 存在非 FIFO 行为，不等同于所有连续海洋模型的全局结论；下一步
应另立非 FIFO label-correcting/Pareto 计划。

**M1.13 synthetic Corridor 与真实 projection。** 当前代码下
`c.p0.1-temporal-corridor-proof.v1-ab58f9524ff8ec8f`
（`c-p01-m113-corridor-proof-20260829-r4`）完成 `36/36`，
`semantic_match=true`、`deterministic=true`、`fail_closed=true`，9 个 certified case 共
观察 `1008` 次安全排除，27 个 rejected case 的 pruning 为 `0`。真实 projection 使用完整
`341` 节点有限网格、最大有效船速、scope/proof digest；仅作为 projection，不运行 planner：

| 输入 | experiment id | allowed / excluded | projected label reduction | projected queue peak | observed pruning |
|---|---|---:|---:|---:|---:|
| holdout | `c.p0.1-temporal-corridor-real.v1-7432d52efd8ffea9` | `12 / 329` | `96.4809%` | `12` | `0` |
| development | `c.p0.1-temporal-corridor-real.v1-feed429585a3dde8` | `11 / 330` | `96.7742%` | `11` | `0` |

两者均为 `REAL_CORRIDOR_PROJECTION_READY_FOR_TEST_ONLY_PRUNING`，但不构成真实性能或正确性
通过。随后执行独立 test-only replay（仍 `dominance_policy=disabled`）：

- holdout：`c.p0.1-temporal-corridor-pruning-real.v1-c5dfab95907004fb`
  （`c-p01-m113-corridor-pruning-real-holdout-6h-20260829-r3`）三目标均 `PASS`，与 baseline
  及 zero-heuristic reference 的路线、ETA、速度、风险、成本、confidence、source IDs 和
  semantic digest 一致，deterministic/resource clean，实际新 label pruning 为
  `fastest=3`、`low_risk=7`、`recommended=3`，合计 `13`。
- development：`c.p0.1-temporal-corridor-pruning-real.v1-214ade9429cd1e8b`
  （`c-p01-m113-corridor-pruning-real-development-6h-20260829-r1`）三目标语义、确定性和
  资源均 clean，但实际 pruning 为 `fastest=0`、`low_risk=3`、`recommended=0`，未满足“每个
  objective 至少一次真实 pruning”的严格门，因此总体为 `NO_PERFORMANCE_PROOF/FAIL`，不作
  性能晋级结论。两次 replay 均未观察 swap/OOM/timeout，candidate 仍未启用。

**无效中间构件。** 初次 holdout replay 因 runner 错用不存在的 `_nodes` helper，随后因
`canonical_digest` 导出名错误各生成一次 fail-closed `ERROR` 构件；两者均未执行有效 planner，
保留在 `.runtime/experiments/` 供审计。修复分别提交为 `b32d820` 与 `3e35982`，最终结果只
采用修复后新 experiment identity。更早的 partition `r1` 全局 timeout 构件同样被后续按
objective timeout 的 `r2` supersede，不纳入结论。

**最终状态与后续分支。** 本轮最终固定为：真实 holdout/development
`REAL_INPUT_FIFO_VIOLATED`；synthetic partition 与 Corridor proof 通过但仅研究授权；holdout
Corridor test-only 通过，development 因未覆盖每目标 pruning 保持
`NO_PERFORMANCE_PROOF/FAIL`。不启动 24h、full-voyage、Winter、P2.1、P3、ARA* 或 production
candidate。后续只能从 clean local commit 另立：(a) P0.2 非 FIFO label-correcting/Pareto
实现计划；(b) 证明型真实 corridor/envelope 扩展；或 (c) evaluator interval/连续性证明增强。

### 【2026-08-29 | PLANNED】P0.2-M0：非 FIFO label-correcting/Pareto 可行性收口

本轮由 M1.12 真实 holdout/development 的 interval 级负 travel-operator jump 触发；该
证据说明真实 evaluator 不能安全套用 FIFO 时间支配，但不代表连续海洋模型上的全局非 FIFO
最优性已经成立。工作仅在 C 内部、有限状态域和 test-only sidecar 中进行，默认
`TemporalDominancePolicy.disabled()`、正式 `TemporalLabelAStar.plan()`、ingress/service、
公共 planner、B/C 与 C/D 合同及 Winter/P2.1/P3/ARA* 全部不变。

**实现边界。** 在现有 `non_fifo_feasibility` 研究参考上补齐显式的
label-correcting/Pareto 语义：标签键至少包含节点与 exact UTC arrival，不能以时间桶或
FIFO 假设跨到达删除标签；同 exact-arrival 仅允许更低成本替换。Pareto 过滤只允许使用
可证明的逐分量支配，默认保守关闭；已扩展标签不得删除。有限域搜索必须显式报告
`GOAL_FOUND`、`EXHAUSTED`、`RESOURCE_LIMIT`、`CANCELLED` 和 `EVALUATOR_FAILURE`，并冻结
expansion/label/queue/edge-evaluation 上限。hard-mask、未知/非有限 evaluator、非法到达时间、
周期 ETA 和 horizon 超限均 fail-closed，不返回部分 route 作为成功结果。

**验收矩阵。** 以独立 zero-heuristic exact-arrival Dijkstra oracle 对照 2×2 非 FIFO 后到达
更优 suffix、同桶不同 exact ETA、周期 ETA、重复 exact-arrival 成本替换、hard-mask/evaluator
failure、arrival-before-departure、取消、horizon 和各资源上限；每个 fixture 要求路线、精确
到达、成本、失败语义和 semantic digest 确定一致，且无误剪枝。重复运行必须 deterministic，
不得将扫描未发现反例当作 FIFO 证明，不宣称连续模型全局最优。

**收口分支。** 全部 adversarial、oracle、终止、取消、资源和 fail-closed 证据通过时仅记为
`READY_FOR_P0.2_IMPLEMENTATION_PLAN`，另立生产候选计划；任一语义不一致、误剪枝、资源/取消
失效或 identity 漂移记为 `NO_PERFORMANCE_PROOF/FAIL` 或 `INVALID/PENDING`。实验构件只写
`.runtime/experiments/`，本地提交后移除辅助 worktree，不 push，不自动进入真实 runner、
Winter 或 candidate。

### 【2026-08-29 | COMPLETED】P0.2-M1：bounded 非 FIFO 业务语义与资源边界

本轮从 P0.2-M0 clean tip `4fa935a` 建立隔离分支
`research/p02-m1-nonfifo-bounded-20260829`，先提交计划 `f3e299f`，实现与测试提交
为 `aaab8f5`。所有改动仍仅在 C 内部 test-only sidecar；没有修改 B/C、C/D 合同、
ingress/service、公共 planner、正式 `TemporalLabelAStar.plan()`、默认
`TemporalDominancePolicy.disabled()`、真实 runner、Winter/P2.1/P3/ARA* 或 production
candidate，也没有写 formal latest、replanning baseline 或 frozen artifact。

**业务载荷。** 新增 `NonFifoBusinessEvidence`，可在研究 transition 中保留
speed/risk/maximum-risk/confidence/source IDs/hard-mask 等边级业务证据；label 的
semantic digest 和 `business_evidence` 均保留这些字段。非有限/非法字段和 hard-mask
transition 不会被当成可航边，统一返回 `EVALUATOR_FAILURE`，避免只比较节点与成本而
丢失业务语义。

**资源和终止边界。** scalar 与 Pareto 两条 bounded 搜索路径均新增
`max_edge_evaluations`（默认 `400000`）和 `edge_evaluations` 计数；expansion、label、
queue、edge-evaluation 任一越界均为 `RESOURCE_LIMIT`，不返回成功 route。取消、horizon、
严格到达约束、evaluator failure 和周期状态继续 fail-closed；Pareto 默认仍关闭，显式
开启时只丢弃同 exact state 上新生成且严格逐分量被支配的标签，相同成本不同路径仍保留。

**验证。** `tests/unit/test_non_fifo_feasibility.py` 专门矩阵为 `20 passed`，新增业务
字段保留/digest、hard-mask fail-closed、scalar/Pareto edge-evaluation 上限等覆盖；与
reference temporal oracle、temporal label/qualification/corridor 合计聚焦测试为
`98 passed`。隔离 worktree 全量 pytest 为 `437 passed, 3 skipped`，跳过仅为退休 M2J
诊断脚本缺失；变更文件 Ruff/format、`ruff check src tests`、lock check、CLI smoke 和
`git diff --check` 通过。隔离 worktree 的 `make check` 仍受本地没有 `.mamba-env/bin/uv`
的环境前置阻塞；该环境未被修改，集成正式工作树后重新执行完整 Make 门。

本轮只把 P0.2 状态更新为 `READY_FOR_P0.2_IMPLEMENTATION_PLAN` 的 strengthened
research evidence，不等于真实输入可用、性能证明或生产资格。真实
`REAL_INPUT_FIFO_VIOLATED`、24h queue 上限、dominance 默认关闭及所有 Winter/P2.1/P3/
ARA* 冻结不变；下一步仍需另立带实际业务字段映射、取消协议和资源预算的 bounded
implementation 计划，不能自动接入 production。

### 【2026-08-29 | COMPLETED】P0.2-M0：非 FIFO label-correcting/Pareto 可行性扩展

本轮从正式 clean `1ec20c7` 建立隔离分支
`research/p02-m0-nonfifo-label-correcting-20260829`，先提交计划
`5f1a966`，实现与测试提交为 `5589aaf`，随后以 `d18a81f` 修正隔离环境下独立
oracle 的测试导入；`1ded502` 冻结默认关闭边界，`03845cd` 保留 equal-cost 路径审计。
改动只位于 C 内部
`planners/non_fifo_feasibility.py` 和对应 test-only fixture；没有修改 B/C、C/D 合同、
ingress/service、公共 planner、正式 `TemporalLabelAStar.plan()`、默认
`TemporalDominancePolicy.disabled()`、Winter/P2.1/P3/ARA* 或生产 candidate，没有写
formal latest、replanning baseline 或 frozen artifact，也没有 push。

**label-correcting/Pareto 语义。** 新增向量目标的
`NonFifoParetoTransition`、`NonFifoParetoLabel`、`NonFifoParetoSearchResult` 和显式
`search_non_fifo_pareto(...)` 研究接口。标签状态包含节点和 exact UTC arrival；不同精确
到达即使落在同一时间桶也不可互相支配。Pareto 过滤只在同一 exact state 对“新生成”且
逐分量被支配的标签生效，旧标签（包括已扩展标签）不删除；
`search_non_fifo_pareto(...)` 默认 `pareto_pruning=False`，仅在显式安全 fixture 中开启
剪枝，完全保守的有限域枚举可保持所有标签。搜索以 lexicographic objective vector 选取结果，但保留
完整 goal frontier，未引入 FIFO 假设或近似/beam 剪枝。

**失败和终止语义。** `GOAL_FOUND`、`EXHAUSTED`、`RESOURCE_LIMIT`、`CANCELLED` 和
`EVALUATOR_FAILURE` 均显式返回；资源仍受正整数的 expansion/label/queue 上限约束。
取消、资源超限、非法/非有限 evaluator、arrival 不严格晚于当前标签、horizon 超限和
hard-mask 类异常均 fail-closed，失败结果不携带部分成功 route。semantic digest 采用
可复现的 UTC/transition 序列编码。

**可复现验证矩阵。** `tests/unit/test_non_fifo_feasibility.py` 的非 FIFO 聚焦矩阵为
`16 passed`，覆盖：

- 2×2 后到达更优 suffix，以及同节点不同 exact arrival 的跨时刻不剪枝；
- 同 exact arrival 的二维 Pareto 新标签安全剪枝和 goal frontier；
- 同一 fixture 两次运行的 semantic digest、生成计数和剪枝计数确定一致；
- 独立 `tests/reference_temporal_oracle.py` zero-heuristic Dijkstra 的路线、到达和成本
  对照；
- 周期/重复状态的 label 上限、取消、horizon、arrival-before-departure、严格到达和
  evaluator failure。

相关 temporal label/qualification/corridor/oracle 聚焦测试共 `94 passed`；正式工作树上
`UV_OFFLINE=1 make check` 最终为 `436 passed`，无跳过；变更文件 Ruff/format、全量
`ruff check src tests`、`uv lock --check --offline`、offline sync、CLI smoke 和
`git diff --check` 均通过。隔离 worktree 的第一次 Make 尝试曾因其本地没有
`.mamba-env/bin/uv` 在 lint 前置处退出，未修改环境或 lock；集成后的正式工作树复跑已
通过，故不构成代码或依赖阻塞。

**最终状态和边界。** 本轮仅证明有限非 FIFO 状态域可以在 exact-arrival 标签下进行
保守、可终止、可取消、可审计的研究搜索，仍不宣称连续海洋模型全局最优，也未把
sidecar 接入真实 runner。状态保持 `READY_FOR_P0.2_IMPLEMENTATION_PLAN`（现在有
expanded test-only feasibility evidence），不等于生产实现或 candidate 资格；真实输入
`REAL_INPUT_FIFO_VIOLATED`、24h `REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL`、dominance
默认关闭和所有 Winter/P2.1/P3/ARA* 冻结不变。下一步只能另立带业务字段/取消协议/资源
预算的 P0.2 bounded implementation 计划，或继续研究 evaluator/interval 证明；不得自动
进入真实 planner、Winter 或 production。

### 【2026-08-29 | PLANNED】P0.2-M1：bounded 非 FIFO 业务语义与资源边界

P0.2-M0 已证明有限状态域的 exact-arrival label-correcting/Pareto 可行，但仍缺少可审计
的边评估计数、业务语义载荷和更清晰的 bounded failure 证据。本轮从 clean `4fa935a`
建立隔离分支 `research/p02-m1-nonfifo-bounded-20260829`，只扩展 C 内部研究 sidecar；
不修改 B/C、C/D 合同、ingress/service、公共 planner、正式 `plan()`、默认
`TemporalDominancePolicy.disabled()`，不启动真实 runner、Winter、P2.1、P3、ARA* 或
candidate。

**算法边界。** 保留节点 + exact UTC arrival 的标签键和无 FIFO 假设的 label-correcting
队列。为 scalar/vector transition 增加统一的可审计业务载荷（ETA、speed、risk、cost、
confidence、source IDs 等只作为研究证据），语义 digest 必须涵盖路径、到达、目标向量和
载荷。新增 `edge_evaluations` 硬上限及确定性计数；expansion/label/queue/edge 上限触顶
统一返回 `RESOURCE_LIMIT`，不返回部分 route。取消、horizon、非有限/非法 evaluator 和
hard-mask 异常均保持 fail-closed。

**Pareto 安全规则。** `pareto_pruning=False` 继续为默认；显式开启时只能丢弃同一 exact
state 上新生成且严格逐分量支配的标签。不同精确到达、相同成本不同路径、已扩展标签和
旧 frontier 永不删除。目标结果使用稳定 lexicographic 选择，同时保留完整 goal frontier，
并以独立 zero-heuristic exact-arrival Dijkstra 对照路线、ETA、成本和业务载荷。

**验收矩阵。** 补充 2×2 非 FIFO 后到达更优、同桶不同 exact ETA、同 exact state 的 vector
支配与 equal-cost 审计、周期/重复标签、edge/label/queue/expansion 上限、取消、horizon、
hard-mask/evaluator failure、业务载荷和 semantic digest determinism。所有失败结果必须
没有成功 route；正式 planner 回归和 active/archive 导入边界必须不变。

**收口分支。** 矩阵、oracle、业务载荷、资源计数和 fail-closed 全部通过时仅记为
`READY_FOR_P0.2_IMPLEMENTATION_PLAN` 的 strengthened research evidence；不代表真实
输入可用或生产资格。任一误剪枝、语义/计数不一致、identity 漂移或公共路径变化均为
`NO_PERFORMANCE_PROOF/FAIL` 或 `INVALID/PENDING`。实验构件如需生成只写
`.runtime/experiments/`，完成 clean 验证后删除辅助 worktree，保留本地分支，不 push。

### 【2026-08-29 | PLANNED】P0.2-M2：actual temporal edge adapter 与 bounded 非 FIFO 语义桥

P0.2-M0/M1 已在独立 finite transition sidecar 中证明 exact-arrival
label-correcting 的终止、业务载荷、资源上限和 fail-closed 规则，但尚未验证这些规则
能否在 C 当前的 `TemporalLabelAStar` edge evaluator 与 `PlanningResult` 业务字段上保持
一致。本轮只建立 C 内部、显式调用的 research adapter：使用实际 temporal session、
`_EdgeTraversal` 和 ETA/risk/cost 评估，默认要求 `use_heuristic=False`、
`TemporalDominancePolicy.disabled()` 且不携带 state-bound，以免把 FIFO/启发式或未审计
的支配假设带入非 FIFO 研究。正式 `TemporalLabelAStar.plan()`、
`TimeDependentAStar`、ingress/service、B/C 与 C/D 合同、公共 planner、真实 runner、
Winter/P2.1/P3/ARA* 和 production candidate 均不变。

**接口与状态。** 新增私有 `non_fifo_temporal_adapter` 模块，提供显式的 bounded
`run_non_fifo_temporal_search(...)` 入口和不可变结果载体。adapter 只负责创建/推进实际
`TemporalSession`，将 `GOAL_FOUND`、`EXHAUSTED`、`RESOURCE_LIMIT`、`CANCELLED`、
`EVALUATOR_FAILURE` 映射为研究状态，保留 `TemporalCandidateResult`、完整
`PlanningResult`（route steps、ETA、速度、风险、成本、confidence、source IDs）和
语义 digest。任何候选 dominance/state-bound 被启用、request 使用 heuristic、identity
不完整或 session 产生误剪枝均拒绝执行，不静默降级为“成功”。

**验收矩阵。** 在受控 non-FIFO edge evaluator 上覆盖后到达更优 suffix、同桶不同精确
ETA、重复 exact-state replacement、hard-mask/ETA/evaluator failure、取消、horizon、
expansion/label/queue/edge-evaluation 上限和 checkpoint/identity fence。adapter 结果与
独立 zero-heuristic exact-arrival oracle 对照路线、精确 ETA、速度、风险、成本、
confidence、source IDs 和失败语义；重复运行 semantic digest 与资源计数确定一致。
测试验证 adapter 不调用 dominance/state-bound pruning，已扩展 label 不被删除，并且
正式 planner 默认行为与 active/archive import boundary 保持不变。

**收口边界。** 全部矩阵通过时只记为 `READY_FOR_P0.2-ADAPTER_REAL-INPUT_PLAN`，不
代表真实输入可用、连续非 FIFO 全局最优或 candidate 资格；失败时记为
`NO_PERFORMANCE_PROOF/FAIL` 或 `INVALID/PENDING`。实验产物如有需要只写
`.runtime/experiments/`，本地提交后移除辅助 worktree，不 push。下一步仍需另立真实
输入预算/取消协议和生产候选审查计划。

### 【2026-08-29 | COMPLETED】P0.2-M2：actual temporal edge adapter 与 bounded 非 FIFO 语义桥

本轮从正式 clean `cdc30aa` 建立隔离分支
`research/p02-m2-nonfifo-temporal-adapter-20260829`，先提交治理计划
`528798a`，实现与测试提交为 `691c9b1`，随后 fast-forward 集成回正式
`research-validation-system`。辅助 worktree 只用于隔离实现，集成验证完成后按计划移除；
未 push。所有改动仍为 C 内部 research-only sidecar，没有修改 B/C、C/D 合同、
ingress/service、公共 planner、正式 `TimeDependentAStar`/`plan()` 默认行为、真实 runner、
Winter/P2.1/P3/ARA* 或 production candidate，也没有写 formal latest、replanning baseline
或 frozen artifact。

**actual-session 语义桥。** 新增未从 `planners.__init__` 导出的
`planners/non_fifo_temporal_adapter.py`，显式调用
`run_non_fifo_temporal_search(...)` 时直接创建并推进 active `TemporalSession`，使用实际
`TemporalLabelAStar` 的 `_EdgeTraversal`/ETA/risk/cost 评估。adapter 强制
`PlanningRequest.use_heuristic=False`、`TemporalDominancePolicy.disabled()` 且不允许
state-bound certificate；不满足围栏或 identity fence 漂移会拒绝执行。成功载体保留完整
`PlanningResult` 的路线、精确 ETA、速度、风险、成本、confidence、source IDs 和业务步证据，
并以不含运行时计时的稳定 semantic digest 绑定这些字段。

**失败与安全边界。** `GOAL_FOUND`、`EXHAUSTED`、`RESOURCE_LIMIT`、`CANCELLED` 和
`EVALUATOR_FAILURE` 均显式返回，资源上限继续由 active temporal session 的
expansion/label/queue/edge-evaluation limits 执行；失败结果不携带部分成功 route。adapter
会拒绝或报告任何 dominance/state-bound pruning，保留已扩展 exact-arrival labels，不引入
时间桶跨到达剪枝或 FIFO 假设。horizon、hard/evaluator failure 和 identity mismatch
均保持 fail-closed。

**验证证据。** 新增 adapter 聚焦矩阵 `7 passed`，与 non-FIFO sidecar、independent
zero-heuristic reference oracle、temporal label 合计 `40 passed`；实际 non-FIFO 后到达
更优 suffix 的路线、精确 ETA 和等价成本与独立 oracle 一致，重复运行 semantic digest、
业务字段和资源计数确定一致。adapter 的资源、取消、horizon、evaluator failure、模式围栏、
identity drift 与非导出边界均有回归覆盖。隔离 worktree 全量为 `444 passed, 3 skipped`
（3 个 skip 仅为退休 M2J orchestrator 脚本路径缺失）；集成正式工作树
`UV_OFFLINE=1 make check` 为 `447 passed`，Ruff、`uv lock --check`、offline sync、CLI
smoke、active/archive import boundary 和 `git diff --check` 全部通过。未生成实验构件。

**收口结论。** 本轮仅证明 active exact-arrival temporal session 能在显式零启发式、
dominance/state-bound 关闭的条件下承载 C 真实边业务语义，并不证明连续非 FIFO 海洋模型
的全局最优性、真实输入资格或性能晋级。状态保持并强化为
`READY_FOR_P0.2-ADAPTER_REAL-INPUT_PLAN`；真实输入仍为
`REAL_INPUT_FIFO_VIOLATED`，24h queue 上限和 candidate/Winter/P2.1/P3/ARA* 冻结不变。
后续必须另立带输入 identity、取消协议、资源预算和 oracle/语义审计的 real-input 计划，
不得自动启动 production 或 candidate。

### 【2026-08-29 | PLANNED】P0.2-M3：非 FIFO actual session 的可恢复执行围栏

P0.2-M2 已把 active `TemporalSession` 的 exact-arrival 边评估接入显式 research adapter，
但一次性执行无法为后续真实输入研究提供长任务的暂停、恢复和取消证据。本轮仅在该
adapter 内增加 session wrapper 与 checkpoint carrier：按 expansion slice 推进 active
session，保存其完整 checkpoint，并在恢复时重新执行 adapter mode、TemporalSession
identity、dominance/state-bound 和 request fence。正式 `TemporalLabelAStar.plan()`、
`TimeDependentAStar`、ingress/service、合同、真实 runner、Winter/P2.1/P3/ARA* 和
production candidate 均不变。

**安全约束。** wrapper 只接受 `use_heuristic=False`、
`TemporalDominancePolicy.disabled()` 和无 state-bound certificate 的 planner；不改变
底层 exact-arrival label 语义，不删除已扩展 label，不把暂停当作成功。checkpoint 必须
绑定 adapter schema/mode digest、底层 `TemporalSessionCheckpoint` state digest、完整
request 与 policy/ETA/search/evaluator identity；任一漂移、篡改、过期状态或恢复输入不符
均 fail-closed。分片未终止时只返回 paused/None，终止后才映射既有五类研究状态。

**验收。** 用 actual `_EdgeTraversal` 非 FIFO fixture 验证 full-run 与 slice→checkpoint→
restore 的路线、精确 ETA、业务字段、semantic digest、资源计数和失败语义一致；覆盖
取消、resource limit、horizon、evaluator failure、checkpoint digest/mode/identity
漂移，以及 active/archive 导入边界。结果仅为
`READY_FOR_P0.2-ADAPTER_LONG-RUN_PLAN` 的研究证据，不代表真实输入资格、连续非 FIFO
全局最优或 candidate 晋级。

### 【2026-08-29 | COMPLETED】P0.2-M3：非 FIFO actual session 的可恢复执行围栏

本轮从 P0.2-M2 集成 clean tip `3d408fb` 建立隔离分支
`research/p02-m3-nonfifo-session-20260829`，先提交计划 `23ff175`，实现与测试提交为
`f87925c`，随后 fast-forward 集成回正式 `research-validation-system`。辅助 worktree 已
在集成验证后移除，研究分支保留，未 push。改动仍只在 C 内部
`non_fifo_temporal_adapter` sidecar，不修改 B/C、C/D 合同、ingress/service、公共 planner、
正式 `plan()` 默认行为、真实 runner、Winter/P2.1/P3/ARA* 或 production candidate。

**可恢复执行。** adapter 新增显式的 `NonFifoTemporalResearchSession`、
`NonFifoTemporalResearchCheckpoint`、`create_non_fifo_temporal_session(...)` 和
`restore_non_fifo_temporal_session(...)`。session 支持 expansion slice 分片推进；只有
底层状态为 `PAUSED` 时才返回暂停空值，暂停不会被当作成功。checkpoint 同时绑定 adapter
schema/mode digest 与 active `TemporalSessionCheckpoint` state digest；恢复前重验嵌套
checkpoint、request、ETA/search/evaluator identity、dominance policy digest 和 state-bound
围栏。已扩展 exact-arrival label 不删除，未引入 FIFO 或时间桶跨到达剪枝。

**失败语义与验证。** full-run 与 slice→checkpoint→restore 在实际 `_EdgeTraversal`
非 FIFO fixture 上路线、精确 ETA、业务字段、semantic digest、expanded/edge 计数一致；
取消、资源上限、horizon、evaluator failure、终止后禁止 checkpoint、mode/state/identity
篡改均 fail-closed。adapter focused 矩阵为 `10 passed`，隔离 worktree 全量为
`447 passed, 3 skipped`（skip 仍仅为退休 M2J orchestrator 脚本缺失）；集成正式工作树
`UV_OFFLINE=1 make check` 为 `450 passed`，Ruff、`uv lock --check`、offline sync、CLI
smoke、active/archive import boundary 和 `git diff --check` 全部通过。

**收口结论。** 本轮仅把 actual temporal session 的非 FIFO 研究执行推进到可暂停、可恢复、
可取消和可审计状态，仍不证明连续非 FIFO 海洋模型全局最优、真实输入资格或性能晋级。状态
更新为 `READY_FOR_P0.2-ADAPTER_LONG-RUN_PLAN`；真实输入
`REAL_INPUT_FIFO_VIOLATED`、24h queue 上限、dominance 默认关闭及 candidate/Winter/P2.1/
P3/ARA* 冻结不变。后续必须另立长任务资源预算、真实输入 identity 和 oracle 证据计划，
不得自动接入 production。

### 【2026-08-29 | PLANNED】P0.2-M4：真实输入 non-FIFO temporal adapter 长任务资格审计

P0.2-M3 已证明实际 temporal session 的非 FIFO 研究 adapter 可以分片、暂停、恢复和取消；
本轮只把该研究边界带到已有完整 145 帧 holdout/development 输入，审计真实边评估的语义、
资源上限和可恢复证据。实验从当前 clean C tip 建立独立 worktree，身份绑定实现与
`uv.lock`、配置树、RiskFrame commit/content/frame digest、冻结 four-layer route-plan-set、
segment、目标、request/scope、adapter mode 和搜索上限。只复用现有输入，不下载或改写风险数据。

**执行围栏。** 新增独立 `benchmark_non_fifo_temporal_real.py` runner，不改动已通过的 P0.1
real runner。runner 只显式调用 `run_non_fifo_temporal_search(...)`，强制
`use_heuristic=False`、`TemporalDominancePolicy.disabled()` 且无 state-bound；正式
`TemporalLabelAStar.plan()`、ingress/service、B/C 与 C/D 合同、公共 planner、candidate、
Winter/P2.1/P3/ARA* 和 production latest/frozen 路径均不变。按输入/segment/objective/重复
逐 worker 串行执行，固定 CPU、timeout、资源快照和 cgroup 证据；无 route 或资源超限不得
伪装为成功，也不提高既定 expansion/label/queue/edge-evaluation 上限。

**输入与证据。** 继续校验 145 帧小时连续性、generation/revision、grid/vessel/planner/config
digest、起点/目标与冻结路线层一致。6h 先执行；24h 仅在同一输入 6h 三目标语义、determinism、
reference 对照和资源证据完整后条件启动。每个实验目录只写 `manifest.json`、`cases.jsonl`、
`resource-frontier.jsonl`、`comparison-summary.json`、`heartbeat.json` 以及最终
`ALL_DONE`/`STOPPED_HARD`；每条完成记录立即 fsync，resume 只接受完全相同 identity，拒绝半完成
worker。成功 route 必须保留 exact arrival、速度、风险、成本、confidence、source IDs 和稳定
semantic digest，并与独立 zero-heuristic exact-arrival reference 对照；reference 仅作正确性
证据，不作性能基线。

**状态与收口。** 真实 FIFO 已知为 `REAL_INPUT_FIFO_VIOLATED`，本轮不把 adapter 结果升级为
FIFO 资格或 dominance 资格。每个 segment 记录 `GOAL_FOUND`、`EXHAUSTED`、`RESOURCE_LIMIT`、
`CANCELLED`、`EVALUATOR_FAILURE`、`TIMEOUT` 和 `INVALID/PENDING` 的明确原因；语义不一致、
fail-open pruning、identity 漂移、dirty evidence worktree 或生产路径写入为全局硬停止。全部
输入仅表示 `READY_FOR_P0.2-ADAPTER_REAL-EVIDENCE_REVIEW`；资源失败保留为真实前沿边界，
不得择优重跑、提高上限或自动启动 Winter。完成后只追加本 SSOT、做本地集成提交并移除辅助
worktree，不 push。

### 【2026-08-29 | COMPLETED】P0.2-M4：真实输入 non-FIFO temporal adapter 长任务资格审计

本轮在隔离分支 `research/p02-m4-nonfifo-real-20260829` 完成，先提交治理计划
`9efc42f`，新增 runner、测试和 worker 参数修复为 `80bd0fd`、`25e1533`；实验完成后
未 push，未写 formal latest、replanning baseline 或 frozen artifact。辅助 worktree 仅用于
实现与验证，后续按边界移除；实验构件保留在 `.runtime/experiments/`。

**runner 与围栏。** 新增 C 内部 `scripts/benchmark_non_fifo_temporal_real.py`，以
`c.p0.2-temporal-adapter-real.v1` 固定 schema 从已有 `bc.risk-window-commit.v1` 的完整
145 帧 holdout/development 和冻结 four-layer route plan set 加载输入。每个
input/segment/objective/repetition 在独立 worker 中调用显式
`run_non_fifo_temporal_search(...)`，强制 `use_heuristic=False`、
`TemporalDominancePolicy.disabled()`、state-bound absent；没有修改正式 planner、合同、
ingress/service 或 production path。identity 绑定 implementation、`uv.lock`、config、
风险帧逐帧 digest、route-plan-set、request/segment、adapter mode 和冻结搜索上限；输出
manifest/cases/resource-frontier/summary/heartbeat，记录 CPU、RSS、swap、cgroup、超时和
稳定 semantic digest，JSONL 每条完成记录 fsync，resume 拒绝 identity 或安全围栏漂移。

**6h 证据。** holdout 实验
`c-p02-m4-real-adapter-holdout-6h-20260829-r3`（identity
`c.p0.2-temporal-adapter-real.v1-bd3c127708e4bbae`）和 development 实验
`c-p02-m4-real-adapter-development-6h-20260829-r1`（identity
`c.p0.2-temporal-adapter-real.v1-f7e0d7a574b86ff9`）均为 2 次重复、三目标
`GOAL_FOUND`。两输入每个目标的两次 semantic digest 均一致；路线、精确 ETA、速度、风险、
成本、confidence、source IDs 与独立 zero-heuristic exact-arrival reference 一致，
`reference_match=true`，dominance/state-bound checks/pruning 均为 0。holdout 的目标级
expanded labels 为 `36/38/38`、queue peak `26`、edge evaluations `272/288/288`；development
为 `16/16/16`、queue peak `13`、edge evaluations `112/112/112`。CPU affinity、RSS、无 swap、
cgroup 快照证据完整。两组汇总状态均为
`READY_FOR_P0.2-ADAPTER_REAL-EVIDENCE_REVIEW`，仅表示真实 6h 研究证据可审计，不表示
FIFO、dominance 或 candidate 资格。

**24h 边界。** 由于两个输入的 6h 三目标均通过条件门，分别执行了单次 24h 三目标审计。
`c-p02-m4-real-adapter-holdout-24h-20260829-r1`（identity
`c.p0.2-temporal-adapter-real.v1-a27a77982487e3a7`）和
`c-p02-m4-real-adapter-development-24h-20260829-r1`（identity
`c.p0.2-temporal-adapter-real.v1-df2608e9f8d2d646`）的三个 worker 均在 120 秒 deadline
内未完成，汇总为 `REAL_INPUT_ADAPTER_RESOURCE_FAIL`。超时记录没有 route 或业务语义，
没有把超时当作成功，也没有提高 `50k expansions / 100k labels / 50k queue /
400k edge evaluations` 上限；由于 worker 被 deadline 终止，不能把它解释为已经触达某一
具体 queue 计数，只能报告 24h 在该预算下未完成。两输入均未再重试或启动 full-voyage。

**收口。** 首次修复前的短暂 parser 参数遗漏构件
`c-p02-m4-real-adapter-holdout-6h-20260829-r1` 仅为 `INVALID/PENDING` 诊断记录，未纳入
上述结论；修复后 r2/r3 和两组 24h 构件均绑定 clean implementation commit。综合状态为
`READY_FOR_P0.2-ADAPTER_REAL-EVIDENCE_REVIEW`（6h）与
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL`（24h）；真实 FIFO 既有
`REAL_INPUT_FIFO_VIOLATED` 不变，dominance 仍默认关闭，candidate/Winter/P2.1/P3/ARA*
继续冻结。下一步只能另立带资源限界或 corridor/envelope 证明的 P0.2 计划，不自动启用任何
生产路径。

**验证收口。** 新 runner/adapter/session/non-FIFO/qualification 聚焦矩阵为 `107 passed`，
隔离 worktree 全量 pytest 为 `453 passed, 3 skipped`；3 个 skip 仅来自退休 M2J
orchestrator 脚本路径缺失。隔离 worktree 的 Ruff、`uv lock --check`、CLI smoke、active/archive
import boundary 和 `git diff --check` 通过；该 worktree 原样 `UV_OFFLINE=1 make check` 因没有
`.mamba-env/bin/uv` 以 exit 127 阻塞，未修改环境。fast-forward 集成正式
`research-validation-system` 后，`UV_OFFLINE=1 make check` 完整通过（Ruff、`456 passed`、
lock/sync check 和 CLI smoke），因此最终 Git 收口验证以正式树结果为准。

### 【2026-08-29 | PLANNED】P0.2-M5：proof-carrying corridor 与 non-FIFO adapter 资源限界

M4 已证明实际 non-FIFO temporal adapter 在真实 6h 输入上可以保持 exact-arrival 业务语义，
但 holdout/development 的 24h 三目标均在冻结预算内超时。M1.13 已有 synthetic
`TemporalCorridorEvidence` 和真实 6h test-only replay，但该证书尚未与 non-FIFO adapter
形成独立、可恢复、可审计的资源限界路径。本轮只推进这一 C 内部研究边界，不把真实 FIFO
反例降级、不启用 dominance、不重开 Winter/P2.1/P3/ARA*，也不提高
`50k expansions / 100k labels / 50k queue / 400k edge evaluations` 上限。

**实现围栏。** 从正式 clean tip 建立独立 worktree 和本地分支；先提交本段计划，再新增
显式的 proof-bound adapter/runner。既有 `run_non_fifo_temporal_search(...)`、正式
`TemporalLabelAStar.plan()` 和默认 `TemporalStateBoundCertificate` 语义保持兼容；新路径
必须显式传入完整、scope-matched、evaluator-certified 的 corridor certificate，默认不传
即完全不剪枝。不得使用 beam/近似剪枝、FIFO/时间桶支配、reference route 注入，也不得
删除已扩展 label；只允许在实际生成新 label 前依据证书排除节点，并记录
`state_bound_checks/pruned/rejected/rejection_reasons`。

**证书与恢复。** adapter 需要校验 certificate digest、完整 `TemporalScope`、bound/evaluator/
proof digest、有限节点 universe、start/goal 包含和 coverage；状态 bound 只在所有围栏通过时
授权，任何 scope、policy、ETA、request、search-limit、evaluator 或 checkpoint 漂移均
fail-closed 并保持 pruning=0。checkpoint 必须绑定 state-bound digest；恢复后的 full-run
与 slice→checkpoint→restore 必须在路线、精确 ETA、业务字段、semantic digest、资源计数和
失败语义上相同。

**验证与实验。** 先扩展 synthetic finite graph/oracle 矩阵，要求 certified bound 至少有
一次真实安全 pruning，rejected/uncertain/non-admissible/scope mismatch/non-FIFO 场景
pruning=0，且与独立 zero-heuristic exact-arrival oracle 完全一致。随后仅在 synthetic
门通过且 clean identity 不漂移时，对既有 holdout/development `executable_0_6h` 做
dominance-disabled、显式 bound adapter 的 2 次重复诊断；真实 24h 只保留 M4 的资源失败
事实，不因 6h 结果自动启动。runner 继续写 manifest/cases/resource-frontier/summary/
heartbeat 和终态标记，实验产物只写 `.runtime/experiments/`。

**收口。** synthetic 与真实 6h 的语义、确定性、fail-closed、资源证据全部通过时，仅记为
`READY_FOR_P0.2-ADAPTER_RESOURCE_BOUND_PLAN`；任一误剪枝、identity 漂移、语义不一致或
资源证据缺失为 `NO_PERFORMANCE_PROOF/FAIL` 或 `INVALID/PENDING`。真实 FIFO 仍为
`REAL_INPUT_FIFO_VIOLATED`，24h 仍为 `REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL`；不宣称
连续非 FIFO 全局最优，不启用 candidate，不写 formal latest/replanning baseline/frozen
artifact，不 push。完成验证后移除辅助 worktree，保留研究分支和实验构件。

### 【2026-08-29 | COMPLETED】P0.2-M5：proof-carrying corridor 与 non-FIFO adapter 资源限界

本轮从正式 clean `5f1e96c` 建立隔离分支
`research/p02-m5-proof-bound-20260829`，先提交治理计划 `4274da1`；实现、real runner
和证据门提交为 `b3ce8c4`、`0bf1997`、`39d0db7`、`efe4751`、`239c13c`，最终格式化
收口为 `16d3837`。本轮只增加
C 内部显式 research adapter 和 synthetic/real evidence runner；未修改 B/C、C/D 合同、
ingress/service、公共 planner、正式 `plan()` 默认行为或 production path，未启用
dominance/candidate/Winter/P2.1/P3/ARA*，未写 formal latest/replanning baseline/frozen
artifact，也未 push。

**显式 bound adapter。** 普通 `run_non_fifo_temporal_search(...)` 仍拒绝安装的 state-bound；
只有显式 `run_non_fifo_temporal_bounded_search(...)` 接受 `TemporalStateBoundCertificate`。
入口要求 planner 与证书 digest 一致、有限网格的 allowed/excluded 节点完备且 endpoints 在
allowed 集合中；certificate、scope、evaluator、proof、policy 和 request 任一不匹配均
fail-closed。实际 session 仍强制 `use_heuristic=False`、`TemporalDominancePolicy.disabled()`，
只在新 label 生成前调用现有 state-bound 检查，不删除已扩展 label。adapter result 允许
合法 `state_bound_checks/pruned`，但 rejected certificate、计数不一致或任何 dominance
迹象都返回失败；checkpoint wrapper 额外绑定并复核 `state_bound_policy_digest`。

**Synthetic gate。** `c.p0.2-temporal-adapter-bound.v1` runner 在
small/medium/stress × fastest/low_risk/recommended × certified/scope-mismatch × 2 repeats
完成 `36/36`。18 个 certified case 均与 independent zero-heuristic exact-arrival oracle
和 unbounded adapter 的路线、精确 ETA、成本及 semantic digest 一致，并观察到共 `18` 次
安全 pruning；18 个 scope-mismatch case 均为 `REJECTED_FAIL_CLOSED`，pruning 为 `0`。
`deterministic=true`、`fail_closed=true`、`production_candidate_enabled=false`。早期 `r1`
构件使用格式化前实现，保留作历史审计但不计入最终结论；格式化后的 clean tip 重新运行
权威 `r2`：`/root/my_project/.runtime/experiments/c-p02-m5-bound-matrix-20260829-r2/`，
experiment id 为 `c.p0.2-temporal-adapter-bound.v1-b0ee4e2eeb63ec7d`。该构件 `36/36`
完成，18 个 certified case 均有真实 pruning（累计 18），18 个 scope-mismatch case 均
`REJECTED_FAIL_CLOSED` 且 pruning=0。

**真实 holdout 6h。** 新 runner
`scripts/benchmark_non_fifo_temporal_bound_real.py` 使用完整 145 帧、冻结
`executable_0_6h` route-plan-set、最大有效船速 corridor proof 和 actual non-FIFO adapter；
仍不启用 dominance，24h 不启动。首次 `r1` 因 child 漏传 parser 所需 `--output-dir` 而
明确记录为 `INVALID/PENDING`；修复后的 `r2` 六个 case 实际均通过，但旧汇总器把唯一
重复 digest 错判为 `deterministic=false`，因此不纳入结论。`r3`/`r4` 分别用于证据门和
格式化前验证；格式化后的 clean tip 以新 identity 的最终 `r5` 构件作为权威：

`/root/my_project/.runtime/experiments/c-p02-m5-bound-real-holdout-6h-20260829-r5/`
（`c.p0.2-temporal-adapter-bound-real.v1-d7e1b91ada4a1f56`），三目标 × 两次重复共
`6/6 PASS`；每个 case `GOAL_FOUND`、`state_bound_authorized=true`、
`state_bound_checks=31`、`state_bound_pruned=7`、`state_bound_rejected=0`，路线、ETA、
速度、风险、成本、confidence、source IDs 与 baseline/reference 一致，
`semantic_match=true`、`reference_match=true`、`deterministic=true`，累计安全 pruning
`42`。固定 CPU=0；两次资源快照的 CPU affinity、RSS、无 swap、cgroup 和 OOM 事件证据均
完整，汇总 `resource_evidence_complete=true`、`resource_clean=true`。

**边界与状态。** 本轮只证明 proof-carrying corridor 可以在有限真实 6h non-FIFO adapter
中安全减少新生成 label；它不证明连续非 FIFO 全局最优、真实 FIFO 资格或 production 性能。
真实 FIFO 既有状态 `REAL_INPUT_FIFO_VIOLATED` 不变，M4 的 24h
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL` 事实不被覆盖，也未因本轮 6h 结果启动 24h。
最终研究状态为 `READY_FOR_P0.2-ADAPTER_RESOURCE_BOUND_PLAN`；candidate、Winter、P2.1、
P3、ARA* 和 dominance 默认关闭。后续若继续，只能另立带更严格资源/走廊审计的计划，不得
自动接入生产。

### 【2026-08-29 | PLANNED】P0.2-M6：proof-carrying arrival envelope

M5 已证明节点级 proof-carrying corridor 在真实 6h non-FIFO adapter 中可以安全减少新生成
label，但它只排除空间上不可能的节点；24h exact-arrival 的主要资源风险还包括同一节点上
已经超过目标可达时间的晚到 label。本轮只研究一个更强但仍可审计的 label-level 必要条件：
由保守的反向 travel-time lower bound 生成每节点的最晚允许 elapsed arrival，只有在
`arrival_elapsed + reverse_lower_bound > maximum_elapsed` 被证明时才丢弃新生成 label。

**边界与身份。** 从当前 clean M5 tip 建立隔离 worktree 和本地分支，先提交本段计划；新增
C 内部 `arrival_upper_hours`/arrival-aware certificate 能力和独立 synthetic runner。证书必须
绑定完整 `TemporalScope`、有限 grid universe、departure/horizon、逐节点 reverse lower bound、
bound/evaluator/proof digest；时间和浮点运算使用保守 outward rounding。缺失节点、非有限值、
scope/evaluator/policy/checkpoint 漂移或不完整证明均 fail-closed，arrival bound 不得剪枝。

**安全规则。** `TemporalStateBoundCertificate` 的既有节点级语义保持兼容；
`TemporalDominancePolicy.disabled()`、正式 `plan()`、普通 non-FIFO adapter、B/C 与 C/D 合同、
ingress/service 和公共 planner 均不变。只在显式 bounded adapter 中启用；不删除已扩展 label，
不使用 beam/近似剪枝，不注入 Dijkstra route。checkpoint/`TemporalSessionIdentity` 必须继续
绑定完整证书 digest，并验证恢复前后一致。

**验证门。** synthetic small/medium/stress × 三目标要求 arrival-bound 与无界 adapter 和
独立 exact-arrival oracle 路线、ETA、业务字段、semantic digest 完全一致，至少出现一次真实
arrival-level pruning；边界相等、缺失/不可信 bound、scope mismatch、非 FIFO、evaluator failure
场景 pruning 必须为零。真实输入仅在 synthetic 全通过后做 dominance-disabled 6h 诊断；24h
仍受 M4 的资源失败事实约束，不提高 `50k/100k/50k/400k` 上限，不自动重跑 Winter 或启用
candidate。实验产物只写 `.runtime/experiments/`，最终状态只能是
`READY_FOR_P0.2-ARRIVAL-BOUND_REVIEW`、`NO_PERFORMANCE_PROOF/FAIL` 或 `INVALID/PENDING`。

### 【2026-08-29 | COMPLETED】P0.2-M6：proof-carrying arrival envelope

本轮从正式 M5 clean tip `798432c` 建立隔离分支
`research/p02-m6-arrival-envelope-20260829` 和 worktree；先提交本段计划
`3d260cc`。实现提交为 `71fcaee`、`540a8ab`，真实 runner 资源证据调用修复为
`cb909f6`。所有改动均限定在 C 内部研究路径和测试/runner；未修改 B/C、C/D 合同、
ingress/service、公共 planner 或正式默认行为，未启用 dominance/candidate/Winter/P2.1/P3/ARA*，
未写 formal latest/replanning baseline/frozen artifact，也未 push。

**Arrival-aware 证书。** `TemporalStateBoundCertificate` 新增可选的
`arrival_upper_hours`，digest、scope 和 checkpoint 身份均绑定该包络；证书只在允许节点全集
完整、每个节点有有限非负上界且 proof/evaluator/scope 一致时授权。`allows_state(...)` 使用保守的
outward rounding，只在新生成 label 的 elapsed arrival 超过上界时拒绝；边界相等保留，已扩展
label 不删除。缺失/不完整/非有限包络、scope/policy/evaluator/checkpoint 漂移均 fail-closed。
`derive_temporal_corridor(..., include_arrival_upper_bounds=False)` 默认保持 M5 节点级语义，
只有显式 arrival-bounded adapter 才打开包络；普通 adapter、`plan()` 和
`TemporalDominancePolicy.disabled()` 路径不变。

**Synthetic gate。** 独立 runner
`c.p0.2-temporal-arrival-bound.v1` 在 small/medium/stress ×
fastest/low_risk/recommended × certified/incomplete/scope-mismatch × 2 repeats 完成
`54/54`。18 个 certified case 与无界 exact-arrival adapter 和独立 zero-heuristic Dijkstra
的路线、精确 ETA、业务字段及 semantic digest 一致，并累计观察到 `162` 次
arrival-level pruning；36 个 rejected case 均保留全部标签，`rejected_pruning_total=0`。
汇总 `status=TEMPORAL_ARRIVAL_BOUND_MATRIX_PASS`、`semantic_match=true`、
`deterministic=true`、`fail_closed=true`、`production_candidate_enabled=false`。
权威构件为 `/root/my_project/.runtime/experiments/c-p02-m6-arrival-bound-matrix-20260829-r1/`，
identity `c.p0.2-temporal-arrival-bound.v1-f4f6b436d9880dac`。

**真实 holdout 6h。** 在 synthetic gate 通过且 clean identity 下，使用完整 145 帧 holdout、
冻结 `executable_0_6h` route-plan-set、固定 CPU=2、4 GiB `MemoryMax`、`MemorySwapMax=0`
执行三目标 × 两次重复；dominance 仍关闭，candidate 仍关闭。权威构件为
`/root/my_project/.runtime/experiments/c-p02-m6-arrival-bound-real-holdout-6h-20260829-r1/`，
identity `c.p0.2-temporal-arrival-bound-real.v1-b6e17e4eceb012e5`，`6/6 PASS`，汇总状态
`READY_FOR_P0.2-ARRIVAL-BOUND-REAL-REVIEW`。每个 case 均 `GOAL_FOUND`，路线、精确 ETA、
速度、风险、成本、confidence、source IDs 与 baseline/reference 一致；
`semantic_match=true`、`reference_match=true`、`deterministic=true`，每 case
`state_bound_pruned=17`、`state_bound_arrival_pruned=14`、`state_bound_rejected=0`，累计
arrival pruning `84`。每个 worker 的 CPU affinity、RSS、无 swap、4 GiB cgroup 和 OOM 事件
证据完整，`resource_evidence_complete=true`、`resource_clean=true`。

**边界与结论。** 本轮证明了显式 arrival-aware 必要条件在有限真实 6h non-FIFO adapter 中
可以安全减少新生成 label，但不证明连续非 FIFO 全局最优、FIFO 资格或 production 性能。既有
真实 FIFO 状态 `REAL_INPUT_FIFO_VIOLATED` 不变；M4 的 24h
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL` 事实不覆盖、不重跑，也未提高
`50k expansions / 100k labels / 50k queue / 400k edge evaluations` 上限。综合状态为
`READY_FOR_P0.2-ARRIVAL-BOUND-REAL-REVIEW`，仅可作为另立更严格资源/走廊计划的证据；
candidate、Winter、P2.1、P3、ARA* 和 dominance 默认关闭。完成验证后移除此辅助 worktree，
保留研究分支和上述实验构件。

### 【2026-08-29 | PLANNED】P0.2-M7：proof-carrying graph-topological arrival envelope

M6 已证明基于直线几何距离的 arrival envelope 在有限真实 6h non-FIFO adapter 中可以安全
减少新生成 label，但该下界没有利用 planner 实际使用的有限网格邻接关系。M7 只研究一个
更紧的必要条件：在完整有限网格上，以实际有向邻接边的距离和最大有效船速计算 forward/reverse
graph lower bound，再生成 arrival upper envelope。该改进仍是 C 内部、显式 bounded adapter、
默认关闭；不改变正式 `TemporalLabelAStar.plan()`、普通 non-FIFO adapter 或任何合同。

**证书与 fail-closed。** 新增拓扑下界 sidecar，必须绑定完整 `TemporalScope`、有限 universe、
逐节点邻接闭包、边距离 evaluator、最大速度、forward/reverse distance maps、horizon 和 proof
digest。邻接节点越界、邻接枚举异常、边距离非有限/负值、不可达域、scope/evaluator/policy/
checkpoint 漂移或证据不完整均拒绝证书并保持 pruning=0。Dijkstra 只用于计算保守下界和独立
正确性证据，不注入 route；每条边 travel lower bound 使用 downward/outward rounding，arrival
upper bound 使用 upward rounding；只允许丢弃新生成 label，不删除已扩展 label。

**验证顺序。** 先在 synthetic small/medium/stress × 三目标验证完整拓扑、scope mismatch、
邻接缺口、evaluator failure 和不可达域；certified profile 必须与无界 exact-arrival adapter
及独立 zero-heuristic oracle 的路线、ETA、业务字段和 semantic digest 一致，并观察真实
arrival pruning；所有 rejected profile pruning 必须为零。通过后仅对已有完整 145 帧 holdout
`executable_0_6h` 做一次新的 topology-bound 2-repeat 诊断；不重跑 24h，不提高
`50k/100k/50k/400k` 上限。实验产物只写 `.runtime/experiments/`，不写 formal latest、
replanning baseline 或 frozen artifact。

**收口分支。** 全部 synthetic 与真实 6h 语义、确定性、资源和 fail-closed 门通过时，仅标记
`READY_FOR_P0.2-TOPOLOGICAL-ARRIVAL-BOUND-REVIEW`；任一误剪枝、identity 漂移、语义/资源
失败为 `NO_PERFORMANCE_PROOF/FAIL` 或 `INVALID/PENDING`。真实 FIFO 仍保持
`REAL_INPUT_FIFO_VIOLATED`，M4 的 24h 资源失败事实不被覆盖；candidate、Winter、P2.1、P3、
ARA* 和 dominance 继续默认关闭。完成验证后移除辅助 worktree，保留研究分支和实验构件，
不 push。

### 【2026-08-29 | COMPLETED】P0.2-M7：proof-carrying graph-topological arrival envelope

本轮从 M6 clean tip `ab9845d` 建立隔离分支
`research/p02-m7-topological-envelope-20260829` 和独立 worktree，先提交计划
`1b2bd07`。新增拓扑下界证书和 synthetic runner 的实现为 `46d26c4`，拒绝证据收口修复为
`58fd367`，真实 holdout runner 为 `6a4e33a`。改动限于 C 内部研究 sidecar、测试和 runner，
未修改 B/C、C/D 合同、ingress/service、公共 planner 或正式默认路径，未启用
dominance/candidate/Winter/P2.1/P3/ARA*，未写 formal latest/replanning baseline/frozen artifact，
也未 push。

**拓扑下界证书。** 新增 `temporal_topology_bounds.py` 中的
`TopologicalLowerBoundEvidence`/`qualify_topological_lower_bound(...)`：枚举完整有限有向网格
邻接，使用实际边距离和最大有效船速计算 forward/reverse graph lower bounds；邻接越界、
evaluator 异常、非有限/负边距离、未知 evaluator、不可达域和 identity 不完整均返回不可用
证据。证书 proof digest 覆盖 adjacency、edge distances、lower-bound maps、speed、scope 和
evaluator；边权与路径和向下取整。只通过已有显式 arrival-bounded adapter 生成包络，普通
adapter、正式 `plan()` 和 `TemporalDominancePolicy.disabled()` 默认行为保持不变。

**Synthetic gate。** 独立 `c.p0.2-temporal-topological-bound.v1` runner 在
small/medium/stress × fastest/low_risk/recommended × certified/scope-mismatch/incomplete/
adjacency-failure × 2 repeats 完成 `72/72`。18 个 certified case 与无界 exact-arrival adapter
及独立 zero-heuristic oracle 语义一致，累计 `150` 次 arrival pruning；54 个拒绝 case 均
`REJECTED_FAIL_CLOSED` 且 `state_bound_pruned=0`、`state_bound_arrival_pruned=0`。汇总
`status=TEMPORAL_TOPOLOGICAL_BOUND_MATRIX_PASS`、`semantic_match=true`、
`deterministic=true`、`fail_closed=true`、`production_candidate_enabled=false`。权威构件为
`/root/my_project/.runtime/experiments/c-p02-m7-topological-bound-matrix-20260829-r1/`，
identity `c.p0.2-temporal-topological-bound.v1-2791a67669e5c49c`。

**真实 holdout 6h。** 在 synthetic gate 通过且 clean identity 下，使用完整 145 帧 holdout、
冻结 `executable_0_6h` route-plan-set、固定 CPU=2、4 GiB `MemoryMax`、`MemorySwapMax=0`
执行三目标 × 两次重复；dominance 仍关闭，candidate 仍关闭。权威构件为
`/root/my_project/.runtime/experiments/c-p02-m7-topological-bound-real-holdout-6h-20260829-r1/`，
identity `c.p0.2-temporal-topological-bound-real.v1-a94a3308989c700c`，`6/6 PASS`，汇总状态
`READY_FOR_P0.2-TOPOLOGICAL-ARRIVAL-BOUND-REAL-REVIEW`。每个 case 均 `GOAL_FOUND`，路线、
精确 ETA、速度、风险、成本、confidence、source IDs 与 baseline/reference 一致；
`semantic_match=true`、`reference_match=true`、`deterministic=true`，每 case
`state_bound_checks=22`、`state_bound_pruned=17`、`state_bound_arrival_pruned=14`、
`state_bound_rejected=0`，累计 arrival pruning `84`。CPU affinity、RSS、无 swap、4 GiB cgroup
和 OOM 事件证据完整，`resource_evidence_complete=true`、`resource_clean=true`。

**边界与结论。** 本轮提升了 arrival envelope 的证明强度（从直线距离下界到实际网格拓扑
下界），但该 holdout 的矩形网格没有带来相对于 M6 的额外实际 pruning；因此不宣称 24h
资源问题已解决，也不宣称连续非 FIFO 全局最优、FIFO 资格或 production 性能。真实 FIFO
`REAL_INPUT_FIFO_VIOLATED`、M4 的 `REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL` 和冻结的
`50k/100k/50k/400k` 上限均不变，24h 未重跑。综合状态为
`READY_FOR_P0.2-TOPOLOGICAL-ARRIVAL-BOUND-REAL-REVIEW`，仅可用于另立更强资源/走廊计划；
candidate、Winter、P2.1、P3、ARA* 和 dominance 继续默认关闭。完成验证后移除辅助 worktree，
保留研究分支和实验构件。

### 【2026-08-29 | PLANNED】P0.2-M8：certified reverse-graph heuristic ordering

M7 的真实 6h 结果表明，拓扑 arrival envelope 与 M6 在当前矩形网格上产生相同的 pruning；
而 M4 的 24h 失败发生在 non-FIFO 适配器强制零启发式之后的 exact-arrival 队列增长。本轮
不再增加一种 label 剪枝，而是研究一个独立的出队排序改进：把已经审计的反向拓扑 travel
lower bound 转换为 objective lower-bound heuristic。启发式只改变优先级，不删除 label、
不改变 arrival state 语义；zero-heuristic exact-arrival Dijkstra 仍是正确性 oracle，真实
FIFO 反例和 24h 资源失败事实不覆盖。

**治理与边界。** 从当前 clean M7 tip `1e13cec` 建立分支
`research/p02-m8-certified-heuristic-20260829` 和独立 worktree，先提交本段计划。新增
C 内部 `TemporalHeuristicCertificate`、planner/session identity fence 和显式
`run_non_fifo_temporal_certified_heuristic_search(...)` 研究适配器；
`TemporalDominancePolicy.disabled()`、普通 non-FIFO adapter、正式 `plan()` 默认行为、
B/C 与 C/D 合同、ingress/service、公共 planner、candidate/Winter/P2.1/P3/ARA* 全部保持
不变。启发式证书只接受完整有限 graph universe、scope/evaluator 完全匹配、反向 lower-bound
map 完备、cost weights 非负且 consistency 已证明的输入；任何缺失、未知、scope/policy/
checkpoint 漂移均 fail-closed，并退化为明确失败而不是默默宣称 heuristic 生效。

**正确性规则。** 对每个节点的 `h(v)` 只由保守反向 travel lower bound 和当前
`CostModel` 的非负 travel/distance 权重计算；edge lower-bound adjacency 闭包保证
`h(u) <= c(u,v) + h(v)`，因此在有限 exact-arrival label 图上保持 admissible/consistent。
启发式不参与节点/到达包络排除，也不删除已扩展 label；同一 request 先以
`use_heuristic=False` 完成 baseline，再以证书化 heuristic 运行 candidate。candidate 必须与
baseline 和独立 zero-heuristic oracle 的路线、ETA、速度、风险、成本、confidence、source IDs
和失败语义一致，确定性、checkpoint slice→restore 一致，且 dominance/state-bound pruning
统计均为零。scope、objective、grid/vessel/config、ETA/search limits、evaluator 和证书 digest
均写入 session identity。

**验证顺序。** 先跑 synthetic small/medium/stress × 三 objective 的 certified、scope
mismatch、incomplete、non-admissible/unknown-evaluator 矩阵；certified 至少观察到 priority
ordering 的 expansion/queue 改善或明确记录“无改善”，但不以合成速度替代真实结论；所有拒绝
场景 heuristic 生效标志和任何 pruning 必须为零。synthetic 全通过且 clean identity 不漂移
后，仅对已有完整 145 帧 holdout `executable_0_6h` 做新的 2-repeat dominance-disabled
诊断，不启动 24h、不提高 `50k/100k/50k/400k` 限制。runner 只写新的
`.runtime/experiments/` 目录并保存 manifest/cases/summary/heartbeat/终态标记。

**收口分支。** 若所有语义、determinism、fail-closed、identity 和资源证据通过，仅标记
`READY_FOR_P0.2-CERTIFIED-HEURISTIC-REAL-REVIEW`；若证书不一致、语义变化、启发式不一致或
资源证据缺失，标记 `NO_PERFORMANCE_PROOF/FAIL` 或 `INVALID/PENDING`。即使真实 6h 有改善，
也只能另立 24h 资源审计计划，不自动重跑 Winter 或启用 candidate。完成后移除 M8 辅助 worktree，
保留本地研究分支和实验构件，不 push。

### 【2026-08-29 | COMPLETED】P0.2-M8：certified reverse-graph heuristic ordering

本轮从 M7 clean tip `1e13cec` 建立隔离分支
`research/p02-m8-certified-heuristic-20260829` 和 worktree，先提交计划
`4d740b2`。实现提交为 `da6972c`、`0f76c8c`、`25dd468`、`a2352bf`；改动仅限 C
内部研究 sidecar、session/checkpoint 身份、测试和诊断 runner。未修改 B/C、C/D 合同、
ingress/service、公共 planner 或正式默认路径，未启用 candidate/Winter/P2.1/P3/ARA*，未写
formal latest/replanning baseline/frozen artifact，也未 push。

**证书与适配器。** 新增 `TemporalHeuristicCertificate`，从完整有限图的反向拓扑 travel
lower bound 和非负 `CostModel` 权重生成 objective lower-bound map；证书 digest 覆盖完整
`TemporalScope`、objective、universe、反向距离、cost model、evaluator 和 proof。图闭包、
非负边权及 consistency 不满足时证书不可用。`TemporalLabelAStar` 仅在显式研究请求
`use_heuristic=True` 且证书 scope 完全匹配时使用该 map 排序；缺失节点、scope/policy/
checkpoint/evaluator 漂移、非 admissible 或未知证据均拒绝并记录原因。启发式只改变出队顺序，
不做 dominance/state-bound pruning，不删除已扩展 label；`TemporalDominancePolicy.disabled()`、
普通 non-FIFO adapter 和正式 `plan()` 默认行为保持不变。`TemporalSessionIdentity` 与
checkpoint 已绑定 `heuristic_policy_digest`，恢复时重新执行 identity fence。

**Synthetic gate。** 独立 runner
`c.p0.2-temporal-certified-heuristic.v1` 在 small/medium/stress ×
fastest/low_risk/recommended × certified/scope-mismatch/incomplete/non-admissible/
unknown-evaluator × 2 repeats 完成 `90/90`。18 个 certified case 与 zero-heuristic
exact-arrival adapter 和独立 Dijkstra oracle 的路线、精确 ETA、业务字段和 semantic digest
一致，并观察到 priority ordering 的扩展/队列改善；72 个拒绝 case 均为
`REJECTED_FAIL_CLOSED`，无 heuristic 生效、无 dominance/state-bound pruning。汇总状态为
`TEMPORAL_CERTIFIED_HEURISTIC_MATRIX_PASS`，且 `semantic_match=true`、`deterministic=true`、
`fail_closed=true`、`production_candidate_enabled=false`。权威构件为
`/root/my_project/.runtime/experiments/c-p02-m8-certified-heuristic-matrix-20260829-r1/`。

**真实 holdout 6h。** 首次直接运行因宿主位于 `/init.scope`、未具备计划要求的 4 GiB/0 swap
cgroup，构件按设计标记 `INVALID/PENDING`，未用于结论。随后在
`MemoryMax=4G`、`MemorySwapMax=0`、固定 CPU=2 的 systemd cgroup 中，以完整 145 帧 holdout、
冻结 `executable_0_6h` route-plan-set、三目标 × 两次重复完成权威诊断；dominance 和
state-bound 均关闭，candidate 仍关闭。权威构件为
`/root/my_project/.runtime/experiments/c-p02-m8-certified-heuristic-real-holdout-6h-20260829-r2/`，
identity `c.p0.2-temporal-certified-heuristic-real.v1-a6b1cd93431a72f8`，汇总状态
`READY_FOR_P0.2-CERTIFIED-HEURISTIC-REAL-REVIEW`，`6/6 PASS`。

每个 case 均 `GOAL_FOUND`，baseline/candidate/reference 的路线、精确 ETA、速度、风险、成本、
confidence、source IDs 和失败语义一致，`semantic_match=true`、`reference_match=true`、
`deterministic=true`；candidate 证书 scope 全部匹配且 `heuristic_rejected=0`。fastest 两次
扩展标签 `18→5`、队列峰值 `26→19`；low_risk 为 `19→10`、队列 `26→26`；recommended
为 `19→5`、队列 `26→19`。所有 case `dominance_pruned=0`、`state_bound_pruned=0`，CPU
affinity、RSS、无 swap、4 GiB cgroup 和 OOM 事件证据完整（`resource_evidence_complete=true`、
`resource_clean=true`）。

**边界与结论。** M8 证明了在已知有限图、scope 完整且非 FIFO exact-arrival 研究适配器中，
证书化反向图 objective 下界可安全改善队列排序并保持路线/业务语义；不证明 FIFO、连续 ETA
或全局生产最优性。真实输入的既有 `REAL_INPUT_FIFO_VIOLATED` 不变，M4 的
`REAL_INPUT_6H_FEASIBLE_24H_RESOURCE_FAIL` 不覆盖、不重跑，且未提高
`50k expansions / 100k labels / 50k queue / 400k edge evaluations` 上限。综合状态为
`READY_FOR_P0.2-CERTIFIED-HEURISTIC-REAL-REVIEW`，仅可作为另立 24h 资源审计或 bounded
state/corridor 计划的证据；candidate、Winter、P2.1、P3、ARA* 和 dominance 继续默认关闭。
完成验证后移除本轮辅助 worktree，保留研究分支和实验构件，不 push。

### 【2026-08-29 | PLANNED】P0.2-M13：actual temporal session resumability and evidence audit

M12/M12.1 已在有限非 FIFO Pareto sidecar 上证明 session/checkpoint 的 identity fence、
恢复等价和 fail-closed 语义；M2-M4 则已将同一边界接到 active exact-arrival
`TemporalSession`。当前缺口是：真实冻结输入的 actual session 仍只有 one-shot runner 证据，
尚未把 one-shot、分片暂停、checkpoint→restore 和取消路径放在同一输入 identity 下逐项比较。
本轮只补齐可恢复研究证据，不新增搜索剪枝、不改变 ETA 策略、不启用 candidate，也不重开
Winter/P2.1/P3/ARA*。

**治理与围栏。** 从 `e7bb916` 建立隔离分支
`research/p02-m13-actual-session-20260829` 与独立 worktree；本段先提交后实现。
主 worktree、B/C 与 C/D 合同、ingress/service、正式 planner、formal latest、replanning
baseline、frozen artifact、M2J/M2K 历史均只读。实验 identity 绑定 implementation、
`uv.lock`、配置树、完整 145 帧 RiskFrame/route-plan-set digest、scope、request、ETA policy、
search limits、evaluator 和 adapter mode；冻结 `50k/100k/50k/400k` 资源上限。

**研究 runner。** 新增独立 `scripts/benchmark_non_fifo_temporal_session.py`，schema
`c.p0.2-temporal-session-real.v1`，复用已冻结 `benchmark_temporal_dominance_real.py`
fixture loader，但不修改既有 real adapter runner。每个 worker 使用固定 CPU、独立进程和
资源快照，支持 `one_shot`、`slice_restore`、`cancelled` 三种模式；`slice_restore` 必须在
同一 session identity 下先暂停、保存 adapter/active checkpoint digest，再恢复并完成，
不得把重新创建 session 当作恢复。每个完成 case 立即 fsync 写入 manifest/cases/session
checkpoint/summary/heartbeat，并支持仅接受完全一致 identity 的 resume。

**语义矩阵。** 对 holdout/development 的 `executable_0_6h`、三个 objective，先 synthetic
adapter fixtures，再按资源预算执行 one-shot 与 slice→restore；每个成功结果比较 route
nodes、exact UTC ETA、speed/risk/cost/confidence/source IDs、failure status 和 semantic
digest，并以独立 zero-heuristic reference 作为正确性证据。取消、evaluator failure、
resource limit、identity/policy/config/limit/checkpoint drift 必须无 partial route/frontier，
且 dominance/state-bound pruning 为零。24h 只在 6h 三目标构件完整且资源稳定时作为单独诊断，
不因任何成功结果自动扩大到 full-voyage。

**收口门。** 仅当 one-shot 与 slice→restore 的成功/失败语义和业务字段完全一致、恢复
session identity fence 生效、取消/资源/漂移 fail-closed、确定性和资源证据完整时标记
`READY_FOR_P0.2-REAL-SESSION-RECOVERY-REVIEW`。真实 FIFO 已知为
`REAL_INPUT_FIFO_VIOLATED`，本轮不重新解释为 dominance 资格；任一 semantic mismatch、
partial frontier、pruning、identity 漂移或资源证据缺失标记 `NO_PERFORMANCE_PROOF/FAIL` 或
`INVALID/PENDING`，保持 adapter/default-off。

**验证与提交。** 先跑 adapter/session/checkpoint、runner contract 和正式默认路径聚焦测试，
再跑 Ruff、`UV_OFFLINE=1 make check`、`uv lock --check`、offline sync、CLI smoke、active/archive
import boundary 与 `git diff --check`。完成后只追加本段 COMPLETED/INVALID 证据，保留 M0-M12.1
历史；本地提交后 fast-forward 正式 C 分支并删除辅助 worktree，实验构件留在
`.runtime/experiments/`，不 push、不自动启用 candidate。

### 【2026-08-29 | PLANNED】P0.2-M10：composed arrival envelope and heuristic ordering

M9 的 phase-isolated 24h 诊断显示：证书化 heuristic 在部分真实目标中能够找到 route，但
baseline/reference 仍在冻结 `queue=50,000` 处失败；仅改变出队顺序不足以形成可审计的完整
24h 语义证据。本轮研究一个更窄的组合：将 M7 已验证的 graph-topological arrival envelope
与 M8 已验证的 reverse-graph objective lower-bound heuristic 同时安装，用 arrival certificate
安全拒绝不可能到达目标的新 label，用 heuristic 只排序剩余 label。两种证书独立验证，任一不完整
均 fail-closed，不把组合效果归因于未经证明的规则。

**治理与边界。** 从 M9 clean tip `b9d711c` 建立隔离分支
`research/p02-m10-composed-bound-20260829` 和独立 worktree，先提交本段计划。复用现有完整
145 帧 RiskFrame、冻结 route-plan-set、配置和 ETA/evaluator；identity 绑定 implementation、
`uv.lock`、证书 digest、scope、segment、search limits 和资源边界。主工作树、B/C 与 C/D 合同、
ingress/service、正式 planner 默认路径、formal latest、replanning baseline、frozen artifact、
candidate/Winter/P2.1/P3/ARA* 均不修改或启用。

**组合安全规则。** 新增 C 内部显式组合 session/adapter，要求：

- `TemporalStateBoundCertificate.arrival_bound_complete` 和 `TemporalHeuristicCertificate.usable`
  同时为真，两个 scope/evaluator/policy/checkpoint digest 完全匹配；
- state bound 只检查新生成 label，边界相等保留，绝不删除已扩展 label；heuristic 只改变队列
  顺序，不改变 label 集合；
- baseline 使用 arrival-bound-only，candidate 使用 arrival-bound + certified heuristic，
  reference 使用独立 zero-heuristic arrival-bound Dijkstra；不使用 FIFO 支配、beam、近似剪枝、
  reference route 注入或任何提高 `50k/100k/50k/400k` 的办法；
- 任一证书拒绝、scope mismatch、evaluator/coverage unknown、pruning counter inconsistency
  或 checkpoint digest drift 均返回明确失败，pruning 不得静默发生。

**Synthetic gate。** 在 small/medium/stress × 三 objective 上比较无界、arrival-bound-only、
组合 candidate 和独立 bounded exact-arrival oracle；覆盖完整证书、arrival incomplete、heuristic
incomplete、各自 scope mismatch、非 admissible/unknown evaluator、边界相等和取消/资源上限。
要求路线、精确 ETA、速度、风险、成本、confidence、source IDs、失败语义和 semantic digest
一致；组合 profile 至少有真实 state-bound pruning 与 heuristic 生效，所有拒绝 profile 的
pruning=0，确定性和 checkpoint slice→restore 通过。只在 synthetic 全门通过且 clean identity
不漂移时执行真实输入。

**真实 24h 诊断。** 沿用 M9 phase-isolated runner，新增
`c.p0.2-temporal-composed-bound-real-24h.v1`，对 holdout/development 的 `rolling_0_24h`
逐 objective 运行 baseline/candidate/reference，各 phase 独立 deadline，CPU=2、4 GiB
`MemoryMax`、0 swap；先跑一个目标探针，再按顺序完成其余目标。记录 route/semantic、证书和
pruning counters、queue/label/expansion、RSS/swap/OOM/timeout；任一目标失败只标记该目标，
不择优形成总通过。24h 仍失败时不重跑 Winter、不提高上限、不自动进入 production。

**收口分支。** 全部目标 baseline/candidate/reference 语义与 bounded oracle 一致、确定、
证书完整、资源合格且观察到组合安全 pruning 时，标记
`READY_FOR_P0.2-COMPOSED-BOUND-REAL-REVIEW`；任一资源/语义/证书门失败为
`REAL_INPUT_24H_RESOURCE_FAIL` 或 `NO_PERFORMANCE_PROOF/FAIL`；构件/identity 不完整为
`INVALID/PENDING`。真实 FIFO `REAL_INPUT_FIFO_VIOLATED`、M4/M9 24h 资源边界和 candidate
默认关闭状态保持不变。完成后追加 SSOT 证据、运行正式 checks、fast-forward 集成并移除辅助
worktree，保留研究分支和实验构件，不 push。

### 【2026-08-29 | COMPLETED】P0.2-M10：composed arrival envelope and heuristic ordering

本轮已在隔离分支 `research/p02-m10-composed-bound-20260829` 的 clean
implementation `1f35a6a` 上完成 synthetic 与真实 24h 诊断。组合路径仍是 C 内部研究
sidecar：arrival envelope 只对新生成 label 做证书化安全拒绝，objective lower-bound
heuristic 只改变队列顺序；`dominance_policy=disabled`，`production_candidate_enabled=false`。
未修改 B/C、C/D 合同、ingress/service 或正式 planner 默认路径。

**Synthetic gate。** `c.p0.2-temporal-composed-bound.v1` 矩阵输出于
`.runtime/experiments/c-p02-m10-composed-bound-synthetic-20260829-r2/`：72/72 cases
完成，完整证书 profile 通过，`deterministic=true`、`fail_closed=true`，观察到
`arrival_bound_pruning=81` 和 `state_bound_pruning=8`；incomplete、scope mismatch、
unknown evaluator、non-admissible、cancelled 与 resource-limit profile 均未授权静默
剪枝。路线、ETA、业务字段、失败语义和 independent exact-arrival oracle 对照通过。

**真实 24h identity 与资源边界。** holdout 和 development 均使用完整 145 帧、冻结
route-plan-set、相同 search limits（`50k/100k/50k/400k`），每个 objective 独立运行
baseline/candidate/reference phase；experiment identity、实现/配置/lock/RiskFrame/route
digest 均记录在 manifest。每个 phase 均运行于 `MemoryMax=4GiB`、`MemorySwapMax=0`、
固定 CPU 0 的 systemd cgroup，resource evidence complete，未发生 swap、OOM 或 timeout。

| input | experiment id | cases | semantic/reference | deterministic | state-bound pruning | status |
| --- | --- | ---: | --- | --- | ---: | --- |
| holdout | `c.p0.2-temporal-composed-bound-real-24h.v1-450f8264acef916a` | 3/3 | PASS/PASS | true | 131,897 | `READY_FOR_P0.2-COMPOSED-BOUND-REAL-REVIEW` |
| development | `c.p0.2-temporal-composed-bound-real-24h.v1-a330a279208919ac` | 3/3 | PASS/PASS | true | 78,268 | `READY_FOR_P0.2-COMPOSED-BOUND-REAL-REVIEW` |

每个 objective 的 candidate heuristic rejection 均为 0（heuristic 不执行剪枝），而
arrival/state certificate 的新 label pruning 可复现；baseline、candidate 和独立
bounded reference 的路线、精确 ETA、速度、风险、成本、confidence、source IDs、失败
语义及 semantic digest 一致。逐 case evidence 保存在对应实验目录的
`manifest.json`、`cases.jsonl`、`resource-frontier.jsonl`、`comparison-summary.json`、
`heartbeat.json` 与 `ALL_DONE` 中。

**收口与边界。** 两个真实输入均独立完成三目标 phase，故组合 bound 达到真实 review
资格，但这不是 performance promotion 或 candidate 晋级。两份 summary 同时记录
`known_fifo_status=REAL_INPUT_FIFO_VIOLATED`；该事实要求另立 P0.2 非 FIFO 计划，不能由
本轮 envelope/heuristic 结果覆盖。M4/M9 的 24h 资源边界历史保持不变；不提高队列/label
上限，不重跑 Winter，不启用 candidate。后续仅可在独立计划中评估非 FIFO 语义、interval
证明或带证明的 corridor/state bound；本轮实验构件不写 formal latest、replanning
baseline 或 frozen artifact，不 push。

### 【2026-08-29 | PLANNED】P0.2-M11：non-FIFO exact-arrival Pareto frontier audit

M10 在 holdout/development 的真实 24h 组合 bound 诊断中保持了路线与独立 reference
一致，并确认真实输入存在 `REAL_INPUT_FIFO_VIOLATED`。下一步只收紧 C 内部 finite
non-FIFO 研究 sidecar 的多目标语义：让 exact-arrival Pareto frontier、终止/取消/资源
失败和 deterministic evidence 可独立复核。该轮不接入真实 runner、正式 planner、ingress
或公共 API，不把 finite fixture 结果解释为连续海洋模型的全局最优性。

**研究边界。** 使用 `non_fifo_feasibility.search_non_fifo_pareto` 的显式研究调用；
`pareto_pruning=False` 仍是默认，启用时只能丢弃“新生成”、同一
`(node, exact UTC arrival)` 状态上被 component-wise 严格支配的 label。不同精确到达、等价
成本或已扩展 label 一律保留。每条边必须严格推进到达时间、成本有限且非负；hard-mask、
evaluator failure、未知结果、取消和任一冻结资源上限均 fail-closed，不返回 partial route。

**证据接口。** 补充可审计的完整 goal/frontier 视图和 canonical frontier digest；结果必须
区分 `GOAL_FOUND`、`EXHAUSTED`、`RESOURCE_LIMIT`、`CANCELLED` 与 `EVALUATOR_FAILURE`，
并保留 route business evidence（speed/risk/confidence/source IDs/hard-mask）。digest 绑定
精确到达、成本向量、路径、transition payload/business evidence 和搜索规则；相同输入即使
邻居以不同可迭代顺序提供，也要通过稳定 canonical tie-break 得到同一证据。此能力只在
C 内部 sidecar 使用，不改变 C→D 合同或正式 `plan()` 默认行为。

**Adversarial matrix。** 独立 test-only runner 使用 schema
`c.p0.2-nonfifo-pareto-frontier.v1`，覆盖 2×2 later-arrival shortcut、同桶不同 exact ETA、
周期/零成本 cycle、相同精确状态的严格支配与 equal-cost 保留、hard-mask、evaluator failure、
取消、expansion/label/queue/edge-evaluation limits、maximum horizon、scope/policy digest
漂移和 business evidence。每个 fixture 与独立 zero-heuristic exact-arrival oracle 对照，
保存 `manifest.json`、`cases.jsonl`、`comparison-summary.json`、`heartbeat.json` 以及
`ALL_DONE`/`STOPPED_HARD`；每个 case 记录 frontier digest、label/expansion/queue counters
和失败语义。

**收口门。** 所有成功 fixture 的 Pareto frontier、选中 route、精确 ETA、成本向量和业务
字段必须与独立 oracle 一致，10 次重复 deterministic；所有 fail-closed fixture 的结果不
得带有成功 label，且 pruning 计数只出现在明确授权的同精确状态新 label 上。任一跨到达误
剪枝、partial-route 泄漏、digest 漂移或邻居顺序依赖标记 `NO_PERFORMANCE_PROOF/FAIL`，不
进入真实输入；全部通过只标记 `READY_FOR_P0.2-NONFIFO-IMPLEMENTATION-REVIEW`，不启用
candidate/Winter。资源上限继续冻结为 `50k/100k/50k/400k`，M4/M9/M10 真实资源和 FIFO
violation 历史不覆盖。

### 【2026-08-29 | COMPLETED】P0.2-M11：non-FIFO exact-arrival Pareto frontier audit

本轮在隔离分支 `research/p02-m11-nonfifo-frontier-20260829` 上完成；计划提交为
`0404dfa`，实现与证据绑定的 clean implementation 为 `910a6fd`（含
`f5f8f0c` 的 sidecar/runner/test 实现及可恢复 identity 修正）。改动仅限 C 内部
`non_fifo_feasibility` research sidecar、独立 runner 和单元/runner contract tests；未修改
B/C、C/D 合同、ingress/service、正式 planner 默认路径或生产接口，`pareto_pruning=False`
继续是默认值，candidate/Winter 仍关闭。

**Synthetic matrix。** 独立 runner `scripts/benchmark_non_fifo_pareto_frontier.py` 使用
schema `c.p0.2-nonfifo-pareto-frontier.v1`，在 12 个有限 fixture ×
`fastest/low_risk/recommended` × baseline/显式 pareto policy × 10 次重复完成 `720/720`
cases。权威构件位于
`.runtime/experiments/c-p02-m11-nonfifo-frontier-synthetic-20260829-r2/`，包含
`manifest.json`、`cases.jsonl`、`comparison-summary.json`、`heartbeat.json` 和 `ALL_DONE`；
manifest 绑定实现文件、commit、`uv.lock`、配置和 fixture/policy digest，resume 复核不会
重复追加 case。

| evidence gate | result |
| --- | --- |
| expected status / deterministic | PASS / `true` |
| route/frontier/ETA/cost/business evidence vs independent exhaustive oracle | PASS |
| fail-closed partial-route suppression | PASS |
| explicit policy/limit frontier digest binding | PASS |
| resource evidence / process swap | complete / clean |
| strict same-exact newly generated pruning | `30` (baseline `0`) |

成功 fixture 为 later-arrival shortcut、同 exact bucket、严格同 exact 支配和 equal-cost
保留；前两类证明不同精确到达不互相支配，后两类分别证明严格同 exact 新 label 可安全
剪枝、等成本路径不被删除。hard-mask、evaluator
failure、non-increasing arrival、objective mismatch、取消、maximum horizon、edge/label
limit 和周期零成本 cycle 均返回明确 `EVALUATOR_FAILURE`、`CANCELLED`、`EXHAUSTED` 或
`RESOURCE_LIMIT`，不带成功 label 或 partial route。固定 CPU=0 的 worker 资源快照均无 swap；
确定性、邻居 canonical ordering、business evidence 和 frontier digest 单元/runner 门均
通过。

**边界与结论。** 汇总状态为 `TEMPORAL_NONFIFO_PARETO_FRONTIER_MATRIX_PASS`，仅表示有限
非 FIFO exact-arrival Pareto 语义、终止/取消/资源失败和安全 pruning 规则已通过研究审计，
不证明连续海洋模型的全局最优性，也不构成真实输入或性能晋级。按 M10 已知
`REAL_INPUT_FIFO_VIOLATED`，本轮不启动真实 runner、不提高
`50k/100k/50k/400k` 资源上限、不重跑 24h/Winter；状态只推进为
`READY_FOR_P0.2-NONFIFO-IMPLEMENTATION-REVIEW`，candidate 继续默认关闭。后续若继续，
须另立有限非 FIFO 实现/真实资源计划，并保留本轮原始构件和历史结论。

### 【2026-08-29 | PLANNED】P0.2-M12：finite non-FIFO Pareto session/checkpoint review

M11 已在有限 fixture 上证明 exact-arrival Pareto frontier、严格同 exact 新 label
pruning、取消和资源失败语义，但入口仍是一次性函数，无法独立审计长任务的切片、暂停和
恢复。本轮把该 sidecar 封装为显式 C 内部 session，不连接真实输入、正式 planner、
ingress/service 或公共 API；`pareto_pruning=False` 和正式 C 默认路径保持不变。

**Session 与身份围栏。** 新增 `NonFifoParetoSession`、`NonFifoParetoCheckpoint` 和
`NonFifoParetoSessionIdentity`（或等价内部类型），identity 必须绑定 schema、start/goal、
UTC departure、objective dimension、Pareto policy、四项冻结 limits、neighbor/evaluator
digest、fixture/config digest。session 只能在 `READY/PAUSED` 状态 checkpoint；checkpoint
保存 exact-arrival labels、frontier、queue、serial、diagnostics 和 lifecycle state，并在
restore 前重新校验 identity/state digest。回调不序列化；恢复必须显式提供与 digest 匹配的
当前 callbacks。identity/policy/limit/evaluator 漂移、篡改 digest 或非法 lifecycle 一律
fail-closed。

**执行语义。** `advance(expansion_slice)` 在未终止时返回暂停标记/空值，只有完整
`GOAL_FOUND`、`EXHAUSTED`、`RESOURCE_LIMIT`、`CANCELLED` 或 `EVALUATOR_FAILURE` 才返回
terminal result；任何非成功 terminal 都不暴露 partial route/frontier。恢复后的完整运行必须
与未切片运行在 selected label、完整 goal frontier、semantic/frontier digest、计数和失败
原因上一致。Pareto pruning 仍只允许同一 `(node, exact UTC arrival)` 的新生成 label 被严格
component-wise 支配时丢弃；不同 arrival、equal-cost 和已扩展 label 永不删除。

**独立证据。** 新 runner 使用 schema `c.p0.2-nonfifo-pareto-session.v1`，在 M11
adversarial fixtures 上交替比较 one-shot、slice-only、slice→checkpoint→restore 和
cancelled runs，至少 10 次重复；每 case 保存 identity/checkpoint digest、selected/frontier
payload、semantic/business evidence、queue/label/edge counters 和资源快照。独立 exhaustive
oracle 继续只作正确性证据，不注入候选答案；必须覆盖成功 frontier、周期/零成本、hard-mask、
evaluator failure、resource limit、cancellation、callback/policy/limit/checkpoint digest
mismatch，并验证 resume 不重复追加 case。

**收口门。** 所有成功 fixture 的 one-shot 与 restored frontier/route/业务字段完全一致且
deterministic；所有失败/cancelled/resource/mismatch 场景无 partial route；恢复身份篡改全部
拒绝；pruning 仅出现在明确同 exact 新 label。全门通过只标记
`READY_FOR_P0.2-NONFIFO-REAL-SESSION-REVIEW`，不启动真实输入、不提高
`50k/100k/50k/400k`、不重开 Winter、不启用 candidate。任一 restore 漂移、跨 arrival 误剪枝、
失败泄漏或资源证据缺失标记 `NO_PERFORMANCE_PROOF/FAIL`，保留 M11 结论不覆盖。

### 【2026-08-29 | COMPLETED】P0.2-M12：finite non-FIFO Pareto session/checkpoint review

本轮在隔离分支 `research/p02-m12-nonfifo-session-20260829` 完成；计划提交为
`ee949e1`，clean implementation 为 `e0b47ab`。改动仅限 C 内部
`NonFifoParetoSession`/`NonFifoParetoCheckpoint`/`NonFifoParetoSessionIdentity`、独立
session runner 与测试；未修改 B/C、C/D 合同、ingress/service、正式 planner 默认路径或
公共 API，`pareto_pruning=False` 和 candidate/Winter 继续关闭。

**Session 与恢复语义。** one-shot `search_non_fifo_pareto(...)` 现在通过显式 session
完成，保留历史调用形状；`advance(expansion_slice)` 只在 `READY/PAUSED` 间切片，完整运行
才返回 `GOAL_FOUND`、`EXHAUSTED`、`RESOURCE_LIMIT`、`CANCELLED` 或
`EVALUATOR_FAILURE`。identity 绑定 schema、start/goal、UTC departure、objective
dimension、Pareto policy、冻结 `50k/100k/50k/400k` limits、neighbor/evaluator callback
digest 和 fixture digest；checkpoint 保存 exact-arrival labels、goal frontier、queue、
serial/counters/diagnostics，并以 state digest 和 callback/identity fence 保护 restore。
只有同一 exact `(node, arrival)` 的新生成且严格 component-wise 被支配 label 才能 pruning；
不同 arrival、equal-cost 或已扩展 label 均保留。所有失败、取消和资源终止均不暴露 partial
route/frontier。

**Synthetic matrix。** runner `scripts/benchmark_non_fifo_pareto_session.py` 使用 schema
`c.p0.2-nonfifo-pareto-session.v1`，在 M11 adversarial fixtures 上对
`one_shot`、`slice_only`、`slice_restore` × 三 objective × 10 次重复完成 `900/900`
cases。权威构件位于
`.runtime/experiments/c-p02-m12-nonfifo-session-synthetic-20260829-r1/`，包含
`manifest.json`、`cases.jsonl`、`comparison-summary.json`、`heartbeat.json` 和 `ALL_DONE`；
manifest 绑定 clean implementation、runner/sidecar file digests、commit、`uv.lock`、配置
和 fixture/mode/policy digest。resume 复跑保持 `900 -> 900` 行，无重复追加。

| evidence gate | result |
| --- | --- |
| expected cases / expected statuses | `900/900` / PASS |
| one-shot vs slice-only/slice-restore route/frontier/semantic digest/counters | PASS |
| deterministic / checkpoint digest | `true` / PASS |
| fail-closed cancellation/resource/evaluator/mismatch | PASS；无 partial route/frontier |
| resource evidence / process swap | complete / clean；固定 CPU=0 |
| strict same-exact newly generated pruning | `90`（非授权场景为 `0`） |

成功 frontier、later-arrival、same-exact dominance 与业务 evidence 均和独立 exhaustive
oracle 一致；周期/资源、evaluator failure、取消以及 callback/policy/checkpoint drift
均返回明确失败或拒绝状态。汇总状态为
`TEMPORAL_NONFIFO_PARETO_SESSION_MATRIX_PASS`，仅证明有限非 FIFO sidecar 的可恢复切片、
身份围栏、终止/取消/资源失败和安全 pruning 语义通过研究审计，不证明连续海洋模型的全局
最优性，也不代表真实输入性能或 candidate 晋级。

**边界与后续。** 本轮不启动真实 runner、不提高冻结资源上限、不重跑 24h/Winter，保留
M10 已知 `REAL_INPUT_FIFO_VIOLATED` 和 M11 结论；P0.2 非 FIFO 真实实现仍需另立计划，
candidate、formal latest、replanning baseline 和 frozen artifact 均不变。

### 【2026-08-29 | PLANNED】P0.2-M12.1：session identity fence completeness audit

M12 的 session/checkpoint 语义矩阵已通过，但审计发现原计划要求的 `limit` identity drift
没有独立 adversarial case，session identity 也只通过 runner manifest 间接绑定 config digest。
本轮只补齐这两个身份围栏，不改变非 FIFO 搜索规则、默认关闭策略或正式路径；仍不接入真实
runner、正式 planner、ingress/service、公共合同或 candidate/Winter。

**围栏补强。** 为 `NonFifoParetoSessionIdentity` 增加显式 `config_digest`，保留旧 one-shot
调用的默认兼容值，并将其纳入 session/policy/checkpoint digest。restore 必须同时拒绝
config、四项冻结 limit、Pareto policy、callback 或 fixture identity 漂移；checkpoint
state digest 篡改和 terminal/非法 lifecycle 仍 fail-closed。冻结
`50k/100k/50k/400k` 上限不变，exact-arrival pruning 仍仅允许同一精确到达状态的新生成
严格支配 label。

**证据矩阵。** 扩展 M12 runner 的 `limit_drift` 场景并覆盖 config drift；在既有 adversarial
fixtures 上继续比较 one-shot、slice-only、slice→restore，三 objective、至少 10 次重复，
恢复身份拒绝必须不产生 route/frontier/pruning。manifest、case、summary、heartbeat 和
resume identity 继续独立 fsync 保存，实验产物只写 `.runtime/experiments/`。

**收口门。** 原 900 cases 加上新增 drift 场景后全部完成，成功/恢复语义与独立 exhaustive
oracle 一致，确定性、资源证据、fail-closed 和合法 pruning 全通过，才追加 COMPLETED；任一
身份漂移被接受、失败泄漏或资源证据缺失则 `NO_PERFORMANCE_PROOF/FAIL`，保留 M12 结论且不
进入真实非 FIFO 实现。完成后在 clean local commit 上 fast-forward 正式分支并移除辅助
worktree，不 push。

### 【2026-08-29 | COMPLETED】P0.2-M12.1：session identity fence completeness audit

本轮在隔离分支 `research/p02-m12-1-session-fence-20260829` 完成；计划提交为
`ae6dcb6`，实现提交为 `8002668`。改动仅补强 M12 C 内部 session identity：新增显式
`config_digest` 并纳入 session/policy/checkpoint digest，保留 one-shot 默认兼容值；runner
新增 `limit_drift` 与 `config_drift` adversarial cases，并为每个 case 输出 identity、policy、
config 和 checkpoint digest。正式 planner、B/C、C/D、ingress/service、candidate/Winter 均未
改变或接入。

**Authoritative matrix。** 在 clean implementation `8002668` 上，runner
`c.p0.2-nonfifo-pareto-session.v1` 完成 `1080/1080` cases（12 scenarios × 3 objectives ×
3 modes × 10 repeats），权威构件位于
`.runtime/experiments/c-p02-m12-1-session-fence-synthetic-20260829-r1/`。summary 状态为
`TEMPORAL_NONFIFO_PARETO_SESSION_MATRIX_PASS`；`deterministic=true`、语义与 independent
oracle 一致、slice/restore 等价、资源证据完整且无 swap，合法同 exact 新 label pruning 为
`90`，所有 mismatch/cancel/resource/evaluator 失败均无 partial route/frontier。新增的
`limit_drift`、`config_drift` 均 `MISMATCH_REJECTED`。resume 复跑保持 `1080 -> 1080`，无重复
追加。

本轮只是身份围栏完整性修复，不证明连续非 FIFO 海洋模型全局最优性，不构成真实输入性能
或 candidate 晋级；M10 的 `REAL_INPUT_FIFO_VIOLATED`、M11/M12 历史及冻结
`50k/100k/50k/400k` 上限保持不变。辅助 worktree 已移除，研究分支保留，未 push。

### 【2026-08-29 | PLANNED】P0.2-M9：certified heuristic long-horizon resource audit

M8 在完整 holdout `executable_0_6h` 上证明了证书化反向图 objective lower bound 可以只改变
exact-arrival label 的出队顺序，同时保持路线、ETA 和业务语义；但 M4 的 `rolling_0_24h`
仍在冻结 queue/label/expansion 预算下超时。本轮只把已通过 M8 synthetic/6h 门的适配器延伸到
真实 24h 资源前沿，验证启发式是否足以降低长时队列压力；不新增未经证明的剪枝规则。

**治理与输入。** 从正式 M8 clean tip `99c4b28` 建立隔离分支
`research/p02-m9-heuristic-24h-20260829` 和 worktree，先提交本段计划。复用现有完整 145 帧
holdout/development RiskFrame、冻结 route-plan-set 和配置，不下载或重建数据；实验 identity
绑定 implementation、`uv.lock`、配置树、RiskFrame/route-plan-set digest、segment、scope、
ETA policy、搜索限制和 evaluator。主工作树、B/C 与 C/D 合同、ingress/service、formal latest、
replanning baseline、frozen artifact、candidate/Winter/P2.1/P3/ARA* 均不得修改或启用。

**算法边界。** 复用 M8 `TemporalHeuristicCertificate` 与显式
`run_non_fifo_temporal_certified_heuristic_search(...)`；baseline 使用 zero-heuristic
exact-arrival，candidate 只使用 scope 完全匹配的反向图 objective lower bound。继续冻结
`50k expansions / 100k labels / 50k queue / 400k edge evaluations`，不使用 FIFO 支配、
state-bound/arrival pruning、beam/近似剪枝或 reference route 注入；所有 dominance/state-bound
剪枝计数必须为零。真实输入已知 `REAL_INPUT_FIFO_VIOLATED`，本轮不重新判定 FIFO、不授权
dominance，Dijkstra 只作正确性 oracle。

**执行顺序。** 新增独立 `c.p0.2-temporal-certified-heuristic-real-24h.v2` runner，支持
`rolling_0_24h`、`--resume`、固定 CPU 和 4 GiB `MemoryMax`/0 swap cgroup，逐 case fsync
保存 manifest/cases/resource-frontier/summary/heartbeat/终态标记。baseline、candidate 和
reference 必须分别在独立 phase worker 中运行，避免 baseline 超时掩盖 candidate 证据。先以
holdout fastest 单目标短探针确认 24h worker 能启动并持续产出证据；短探针通过后按交替顺序
运行 holdout 三目标，随后仅在资源和身份均正常时运行 development 三目标。任何单目标
timeout/RESOURCE_LIMIT 只停止该 case，继续其他 case；身份漂移、语义与 oracle 不一致、
fail-open pruning 或资源污染立即停止全局实验。绝不启动 full-voyage 或 Winter 复测。

**通过与失败。** 每个完成 case 必须同时满足 baseline/candidate/reference 语义一致、
deterministic、heuristic scope match、rejection=0、dominance/state-bound pruning=0，且
CPU/RSS/swap/OOM/timeout 证据完整。所有目标均完成则状态为
`READY_FOR_P0.2-CERTIFIED-HEURISTIC-24H-REVIEW`；若 case 完整但资源/语义门失败为
`NO_PERFORMANCE_PROOF/FAIL`；超时或构件不完整为 `REAL_INPUT_24H_RESOURCE_FAIL` 或
`INVALID/PENDING`，不得将短探针或择优 case 写成性能通过。无论结果如何，candidate 仍默认关闭，
24h 结果不覆盖 M4 的历史失败事实。

**收口。** 只在独立实验完成后追加本段的 COMPLETED/INVALID 证据，保留 M0–M8 历史和原始
构件；运行聚焦测试、正式 `UV_OFFLINE=1 make check`、Ruff、lock/sync、CLI smoke 和
`git diff --check`，本地 fast-forward 集成后移除辅助 worktree，保留研究分支和实验目录，不 push。

### 【2026-08-29 | COMPLETED】P0.2-M9：certified heuristic long-horizon resource audit

本轮从 M8 clean tip `99c4b28` 建立隔离分支
`research/p02-m9-heuristic-24h-20260829` 和 worktree，先提交计划
`eee2529`。24h runner 初版为 `21485e6`，修正冻结 fixture loader 为 `e54ba26`；随后发现
baseline/参考搜索在长时限内会阻塞 candidate，故在不改变算法语义的前提下以
`0279922`、`2f3c389` 将 runner 升级为 v2 phase-isolated：baseline、certified candidate、
zero-heuristic reference 各自独立进程、deadline、RSS/cgroup 快照和失败记录。所有提交均只
涉及 C 内部研究 runner/test/docs；未修改合同、ingress/service、正式 planner 或默认路径，未
启用 candidate/Winter/P2.1/P3/ARA*，未提高 `50k/100k/50k/400k` 限制，也未 push。

**执行围栏。** 复用完整 145 帧 holdout/development RiskFrame 和冻结 24h route-plan-set，
`rolling_0_24h` 起点/目标由 fixture 自动解析；每个 phase 使用 CPU=2、`MemoryMax=4G`、
`MemorySwapMax=0`，所有资源快照 `resource_clean=true`、`resource_evidence_complete=true`。
candidate 仍只安装已通过 M8 的反向图 objective lower-bound certificate，所有记录
`heuristic_scope_match=true`、`heuristic_rejected=0`、`dominance_pruned=0`、
`state_bound_pruned=0`。

**holdout 24h。** 权威 v2 构件分别为
`/root/my_project/.runtime/experiments/c-p02-m9-certified-heuristic-real-holdout-24h-phased-fastest-20260829-r2/`、
`...-phased-low-risk-20260829-r1/` 和
`...-phased-recommended-20260829-r1/`。三项目 baseline 均在冻结 `queue=50,000` 处
`RESOURCE_LIMIT`（expanded 分别为 `8146/8151/8156`），reference 均因同一 queue 上限失败。
candidate fastest 找到 `GOAL_FOUND`（expanded `3645`、queue peak `24867`）；low_risk 和
recommended 分别在 queue 上限处 `RESOURCE_LIMIT`（expanded `7600`、`7366`）。因为 baseline
和 reference 没有完成路线，不能宣称 24h candidate 的语义等价或性能通过；三份汇总均为
`REAL_INPUT_24H_RESOURCE_FAIL`。

**development 24h。** 权威构件为
`/root/my_project/.runtime/experiments/c-p02-m9-certified-heuristic-real-development-24h-phased-all-20260829-r1/`，
三项目均保留完整 phase 证据并汇总为 `REAL_INPUT_24H_RESOURCE_FAIL`。baseline expanded
为 `8459/8562/8499` 且 queue=`50000`；candidate fastest 与 recommended 找到
`GOAL_FOUND`（分别 expanded `2486`/`6763`、queue `16376`/`42503`），low_risk 在 queue
上限处 `RESOURCE_LIMIT`（expanded `8188`）；reference 三项目均触及 queue 上限。由于缺少
baseline/reference 完成路线，development 也不满足语义门。

**结论与下一步。** M9 证明证书化启发式能在部分真实 24h 输入/目标中独立推进并显著降低
queue/expansion，但在冻结资源和非 FIFO 语义下仍不能使所有目标完成，综合状态固定为
`REAL_INPUT_24H_RESOURCE_FAIL`，不覆盖 M4 的历史结论。真实 FIFO
`REAL_INPUT_FIFO_VIOLATED` 不变；不把 candidate-only 路线包装成正确性或性能通过，不启动
full-voyage/Winter，也不启用 candidate。下一步应另立“带完整 baseline/reference 证据的
24h 资源限界”或 proof-carrying corridor/state envelope 计划，继续默认关闭并保持当前资源上限。
完成验证后移除本轮辅助 worktree，保留研究分支和实验构件，不 push。

### 【2026-08-29 | COMPLETED】P0.2-M13：actual temporal session resumability and evidence audit

本轮在隔离分支 `research/p02-m13-actual-session-20260829` 完成，计划提交为
`1c94d8c`，runner 实现与测试提交为 `50786fc`、`5335ec2`、`0ade39f`。新增的
`scripts/benchmark_non_fifo_temporal_session.py` 只调用实际
`non_fifo_temporal_adapter` 的 zero-heuristic、dominance-disabled、state-bound-absent 路径；
不改变正式 planner、默认行为、B/C 或 C/D 合同，也未接入 ingress/service、candidate 或
Winter。首次 r1 构件因 worker 命令漏传 `--output-dir` 被参数解析拒绝，已保留为
`INVALID/PENDING` 工具诊断，不纳入算法证据；修复后所有权威构件均重新绑定最终实现 commit。

**权威真实 6h 矩阵。** holdout 使用完整 145 帧 commit
`risk-window-sha256-115ad3ab6d7034fabc9428f91c14099b02dff8bb2443569a8d3947187fbb5ff9`、
目标 `(7,6)`，development 使用
`risk-window-sha256-bdfd7964df96ffcad7dd78d9830394a0a91d7fbbfde16c0649d2ba2fb68a00ab`、
目标 `(7,7)`；两者均为 `executable_0_6h`、`fastest/low_risk/recommended`、两次重复、固定
CPU 0。权威目录分别为
`.runtime/experiments/c-p02-m13-actual-session-holdout-6h-20260829-r4/` 和
`.runtime/experiments/c-p02-m13-actual-session-development-6h-20260829-r3/`，实验
identity 均绑定最终实现、`uv.lock`、配置树、route-plan-set、RiskFrame frame digest、
scope/request、ETA/search limits 和 evaluator。

**结果。** 每个输入 18/18 cases 完成（3 objective × 2 repetitions ×
`one_shot/slice_restore/cancelled`），摘要均为
`READY_FOR_P0.2-REAL-SESSION-RECOVERY-REVIEW`。两组均
`all_pairs_equivalent=true`、`deterministic=true`、6 个 pair 全部通过；one-shot 与
slice→restore 的路线节点、exact UTC ETA、速度、风险、成本、confidence、source IDs 和
semantic digest 一致，独立 zero-heuristic reference 的成功路线 12/12 匹配。6/6 cancelled
case 均返回 `CANCELLED`，没有 route/frontier；所有 case 的 dominance/state-bound
checks/pruned 均为 0，session identity 与 restored session ID 一致，checkpoint digest 均有
记录。资源快照显示固定 CPU、`resource_clean=true`、无 swap/OOM；cgroup memory events
完整，但宿主 scope 的 memory/swap 上限为 `max`，因此这不是 4 GiB 隔离或性能门证据。

**结论与边界。** M13 只证明实际 exact-arrival session 在冻结真实 6h 输入上的 one-shot、
分片恢复和取消语义可复现、可审计、fail-closed；它不证明连续非 FIFO 海洋模型的全局最优性，
不把 `REAL_INPUT_FIFO_VIOLATED` 转为 dominance 资格，也不覆盖真实 24h 资源前沿。状态标记
为 `READY_FOR_P0.2-REAL-SESSION-RECOVERY-REVIEW`，candidate/Winter 仍关闭；下一步另立
带强制 cgroup/24h 预算的 session 资源计划或 P0.2 非 FIFO label-correcting 实现计划，
不得自动启动。

### 【2026-08-29 | PLANNED】P0.2-M14：actual temporal Pareto label-correcting bridge

M13 已证明实际 `TemporalSession` 在冻结真实 6h 输入上的 exact-arrival 单目标搜索、分片
恢复和取消语义可复现，但该会话仍以单一 objective 的标量 equivalent-hours 成本保存每个
状态。M12.1 的有限 Pareto sidecar 已证明不同 exact arrival 不可交叉剪枝、同一精确状态的
新生成严格 component-wise 支配 label 可以安全丢弃。本轮把两条研究证据接到同一个实际
`TemporalLabelAStar` edge evaluator 上，形成可审计的 C 内部 Pareto bridge；它只供 test/
research 使用，不改变正式 planner、合同、ingress/service 或默认行为。

**实现边界。** 新增未从 `planners.__init__` 导出的
`non_fifo_temporal_pareto.py`。bridge 把实际 `_EdgeTraversal` 映射为固定顺序的可加向量
`travel/risk-exposure/distance/turn/deviation/low-confidence/total-equivalent hours`，
并将 exact `(node, incoming-heading, UTC arrival)` 作为状态。它复用真实 edge geometry、ETA、
RiskSampler、VesselPerformanceModel、CostModel 和业务字段，但调用前强制
`use_heuristic=False`、`TemporalDominancePolicy.disabled()`、无 state-bound/heuristic
certificate。Pareto pruning 只由已有 finite sidecar 执行：仅拒绝同一 exact state 新生成且
严格被支配的 label；不同 arrival、equal vector、已扩展 label、hard-mask/evaluator failure
和资源超限均保留或显式失败。

bridge 的 route/frontier 类型只包含研究证据（states、exact UTC arrivals、cost vector、
speed/risk/confidence/source IDs 和 `CostBreakdown`），不伪造 `PlanningResult` 或 C→D
route contract。session/checkpoint 通过 scope、request、ETA/search-limit/evaluator digest
和 callback identity fence 恢复；one-shot、slice→restore、cancelled 和 identity drift
必须不泄漏 partial route/frontier。冻结 `50k/100k/50k/400k` 上限不变。

**验证与证据。** 增加 actual bridge 的 synthetic adversarial 聚焦测试和独立 runner，覆盖
later-arrival non-FIFO shortcut、same-exact Pareto pruning、周期/重复 label、业务字段、
hard-mask/evaluator failure、resource limit、cancel、checkpoint restore 及 planner/request
scope drift；同一小网格用独立 zero-heuristic exhaustive oracle 对照 route/ETA/业务字段，要求
determinism、slice 等价、合法 pruning 至少一次、非授权场景 pruning 为零。runner 只写
`.runtime/experiments/c-p02-m14-actual-pareto-20260829-r1/`，逐记录 fsync，输出 manifest、
cases、summary、heartbeat 和终态标记；不启动真实 24h，不提高资源限制，不重开 Winter。

**收口分支。** 全部矩阵通过时追加 `READY_FOR_P0.2-REAL-PARETO-REVIEW`，仍不启用
candidate；任一语义、恢复、fail-closed 或资源证据失败则 `NO_PERFORMANCE_PROOF/FAIL`，
保留 M13/M12.1 结论。identity 漂移、构件不完整或 dirty evidence 一律
`INVALID/PENDING`。完成后只在本地集成并移除辅助 worktree，保留研究分支和实验构件，不
push。

### 【2026-08-29 | COMPLETED】P0.2-M14：actual temporal Pareto label-correcting bridge

本轮在隔离分支 `research/p02-m14-actual-pareto-20260829` 完成，计划提交为 `7a7eec7`，
actual edge bridge 为 `4cd33ab`，runner/测试为 `661e6b4`、`359bab4`、`d047692`。新增的
`non_fifo_temporal_pareto.py` 是未从 `planners.__init__` 导出的 C 内部研究 sidecar：它把实际
`TemporalLabelAStar._EdgeTraversal` 接到现有 `NonFifoParetoSession`，保留
`(node, incoming-heading, exact UTC arrival)` 状态、7 维 additive cost vector、业务 edge
evidence 和完整 frontier；不伪造 `PlanningResult`，不修改正式 planner、B/C 或 C/D 合同、
ingress/service、default path、candidate 或 Winter。

**安全与身份围栏。** bridge 创建前强制 `use_heuristic=False`、
`TemporalDominancePolicy.disabled()`、无 state-bound/heuristic certificate，并拒绝未知
evaluator identity。Pareto pruning 委托已有有限 sidecar，只有同一 exact state 的新生成且
严格 component-wise 被支配 label 被丢弃；不同 arrival、equal vector、已扩展 label、
evaluator/hard-mask failure 和 resource/cancel 均不被包装成成功。checkpoint 额外绑定 bridge
schema、`TemporalScope` digest、cost-component digest、callback digest 和 nested state
digest；restore 的 scope/component/callback/state drift 均 fail-closed。

**权威 synthetic evidence。** runner schema 为
`c.p0.2-temporal-pareto-bridge.v1`，构件位于
`.runtime/experiments/c-p02-m14-actual-pareto-20260829-r3/`，包含 `manifest.json`、
`cases.jsonl`、`comparison-summary.json`、`heartbeat.json` 和 `ALL_DONE`。矩阵为
7 scenarios（same-exact dominance、later-arrival、business evidence、evaluator failure、
resource limit、scope drift、checkpoint tamper）× 3 objectives × 3 modes
（one-shot/slice→restore/cancelled）× 2 repetitions，共 `126/126`。摘要为
`TEMPORAL_NONFIFO_ACTUAL_PARETO_BRIDGE_MATRIX_PASS`：deterministic=true，独立 exhaustive
small-fixture oracle 的 route/ETA/cost/source IDs 对照通过，slice→restore 等价，观察到
12 次合法 same-exact pruning，scope/checkpoint drift、cancel、evaluator failure、resource
limit 均无 partial route/frontier，固定 CPU=0 且无 process swap。r1/r2 分别因 runner 的已修复
编排缺陷（初始 failure 场景未正确处理 terminal failure、identity 场景误受 cancelled mode
影响）标记为历史诊断/FAIL，不纳入权威算法证据；r3 绑定最终 clean implementation。

**验证收口。** 新增 9 个 bridge/runner 聚焦测试通过；相关非 FIFO/temporal/session/qualification
聚焦集合 `101 passed`；全仓 `pytest` 为 `527 passed, 3 skipped`，3 个 skip 是缺失
orchestrator archive fixture 的既有测试边界。改动文件 Ruff、lock check、offline sync、CLI
smoke、active/archive import boundary 和 `git diff --check` 均通过；`UV_OFFLINE=1 make check`
因隔离 worktree 没有 `.mamba-env/bin/uv` 被明确阻塞，未修改环境，使用同一锁定 `.venv` 完成
等价校验。实验 runner 未执行真实 6h/24h，宿主 cgroup 仅作观察而非 4 GiB 性能门。

**结论与边界。** 状态为 `READY_FOR_P0.2-REAL-PARETO-REVIEW`：有限 actual-edge bridge 的
Pareto、业务 evidence、恢复/取消和 fail-closed 语义具备继续审计的证据，但不证明连续海洋
模型全局最优性、不授权真实输入 dominance，不覆盖 24h 资源前沿。M10 的
`REAL_INPUT_FIFO_VIOLATED`、M9 的 24h resource fail、M13 的真实 6h session evidence 及
冻结 `50k/100k/50k/400k` 上限保持不变。下一步另立带强制 cgroup/真实输入 scope 的
P0.2-real Pareto 资格或资源计划；candidate、Winter、formal latest、replanning baseline
和 frozen artifact 继续关闭。

### 【2026-08-29 | PLANNED】P0.2-M15：real 6h Pareto qualification audit

M14 的 actual-edge Pareto bridge 已在独立 synthetic 矩阵中证明有限 exact-arrival 状态、
业务 edge evidence、checkpoint 恢复、取消和同一精确状态的安全新 label pruning。M15 将把
该 bridge 接到冻结的真实 145 帧 holdout/development 输入，仅执行 `executable_0_6h` 的
研究资格审计；不执行真实 24h、全航程或 Winter 复测，不启用 candidate。

本轮从 clean `3056fb3` 建立隔离分支
`research/p02-m15-real-pareto-20260829` 与 worktree
`/root/my_project/.runtime/worktrees/c-p02-m15-real-pareto`。新增 runner 只使用
`non_fifo_temporal_pareto` 的 zero-heuristic、dominance-disabled 实际 edge evaluator，
以冻结 route-plan-set 自动解析目标，绑定 implementation/lock/config、RiskFrame 与
route-plan-set digest、TemporalScope/request、搜索限制和 evaluator identity。每个输入和
objective 运行 one-shot、slice→restore、cancelled 以及两次重复，成功路线再与独立
zero-heuristic exhaustive/reference 搜索核对；资源、确定性、身份恢复和 fail-closed 证据
逐记录 fsync 保存到 `.runtime/experiments/`。

通过条件是所有预期 case 完整、重复确定、slice 等价、成功语义与 reference 一致、合法
same-exact pruning 可观测、cancel/失败/资源限制不产生 partial route/frontier，且固定 CPU、
swap/OOM/timeout 证据完整。通过只标记
`READY_FOR_P0.2-REAL-PARETO-REVIEW`，失败标记
`REAL_INPUT_PARETO_RESOURCE_FAIL` 或 `INVALID/PENDING`；任何结果都不授权 dominance、
candidate 或 Winter。完成后仅本地集成并移除辅助 worktree，保留研究分支和实验构件，不 push。

### 【2026-08-29 | COMPLETED】P0.2-M15：real 6h Pareto qualification audit

本轮从 clean `3056fb3` 建立隔离分支
`research/p02-m15-real-pareto-20260829` 与 worktree
`/root/my_project/.runtime/worktrees/c-p02-m15-real-pareto`。提交序列为计划
`5232bce`、实际 bridge 边拒绝围栏 `d6fb5ca`、真实 runner `dfab593`、测试
`a220aed`，以及将显式 ETA method 纳入 identity 的 `cca094e`；权威 r2 evidence 均绑定
clean implementation commit `cca094e1f21933056ca442434e9e36ca33bbcc46`。早期 r1 构件因
manifest identity 未包含 `eta_method` 保留为诊断，不纳入结论。没有修改
B/C、C/D 合同、ingress/service、公共 planner、formal latest、replanning baseline 或
frozen artifact，也没有 push。

**实现与安全围栏。** 新增
`scripts/benchmark_non_fifo_temporal_pareto_real.py`，schema 为
`c.p0.2-temporal-pareto-real.v1`。runner 复用已审计的完整 145 帧真实 fixture loader，
以冻结 route-plan-set 自动解析起点/目标，逐 worker 绑定 implementation/lock/config、
RiskFrame frame digest、route-plan-set、scope/request、bounded ETA policy 和冻结的
`50k/100k/50k/400k` 搜索上限。正式研究调用固定 `use_heuristic=False`、
`TemporalDominancePolicy.disabled()`、无 state-bound certificate，仅显式打开
`pareto_pruning=True`；`--eta-method bounded` 只作用于该研究 worker，正式 planner 的
历史默认 ETA 策略不变。M14 bridge 新增 `skip_expected_rejections` 围栏：仅将已分类的
hard/coverage/sampling/speed/ETA 域拒绝作为 unavailable edge 跳过并写入 planner
diagnostics，未知 evaluator 异常仍进入 `EVALUATOR_FAILURE`；该开关进入
callback/component/checkpoint digest，默认值保持关闭，因此既有 synthetic bridge 语义不变。

**权威真实 6h 构件。** holdout 输出为
`.runtime/experiments/c-p02-m15-real-pareto-holdout-6h-20260829-r2/`，experiment id
`c.p0.2-temporal-pareto-real.v1-d4b0818f302c36d1`；development 输出为
`.runtime/experiments/c-p02-m15-real-pareto-development-6h-20260829-r2/`，experiment id
`c.p0.2-temporal-pareto-real.v1-984dd011b4045963`。两组均为
`executable_0_6h`、`fastest/low_risk/recommended`、`one_shot/slice_restore/cancelled`、
两次重复，共 `18/18` case，manifest/cases/resource-frontier/summary/heartbeat 和
`ALL_DONE` 齐全；resume 以完全一致 identity 重读成功。

| 输入 | status | one-shot 与 restore | reference 语义 | deterministic | Pareto pruning | 搜索峰值 |
|---|---|---|---|---|---:|---|
| holdout `(5,7)→(7,6)` | `READY_FOR_P0.2-REAL-PARETO-REVIEW` | `6/6` 等价 | `12/12` 匹配 | `true` | `0` | expanded `32`、queue `26` |
| development `(5,7)→(7,7)` | `READY_FOR_P0.2-REAL-PARETO-REVIEW` | `6/6` 等价 | `12/12` 匹配 | `true` | `0` | expanded `17`、queue `13` |

成功路线的节点、每个 exact UTC ETA、速度、风险、最大风险、confidence、source IDs、
CostBreakdown 与独立 zero-heuristic reference 一致；slice→restore 的 semantic/frontier
digest 与 one-shot 一致；`6/6` cancelled case 均为 `CANCELLED`，没有 route/frontier。
两输入的实际 `pareto_pruned_total=0`，所以本轮没有把真实输入包装成 Pareto pruning 或
dominance 性能证明；M14 synthetic 的合法 same-exact pruning 证据仍是唯一 pruning 证据。
真实 bounded ETA 仍观察到域拒绝（holdout 每次完整 run 约 `42` ETA/`40` hard，
development 约 `26` ETA/`26` hard），这些均按研究桥接规则保留为显式 diagnostics，未被
静默当作航线。

**资源与边界。** 所有完成 case 固定 CPU `0`，process/host swap 为零，OOM/timeout 为零，
resource-clean 与 cgroup memory events 证据完整；当前宿主 `/init.scope` 的
`memory.max`/`memory.swap.max` 为 `max`，因此这不是 4 GiB 强制 cgroup 性能门，只是可审计
资源观察。未执行真实 24h、full-voyage 或 Winter；已知 `REAL_INPUT_FIFO_VIOLATED` 不变，
本轮不授权 FIFO dominance，也不启用 candidate/Winter。最终状态为
`READY_FOR_P0.2-REAL-PARETO-REVIEW`：实际 6h Pareto bridge 的语义、恢复、取消和
fail-closed 边拒绝具备另立 review 的证据，但不代表连续海洋模型全局最优性、真实 Pareto
性能通过或生产资格。下一步另立带强制 cgroup/更大真实输入的 Pareto 资源计划，或在
明确非 FIFO label-correcting 终止/资源语义后制定 P0.2 implementation plan；保持默认关闭。

### 【2026-08-29 | PLANNED】P0.2-M16：proof-carrying state-bound actual Pareto bridge

M15 已在真实 6h 输入上证明 actual-edge Pareto bridge 的路线语义、恢复、取消和失败围栏，
但实际 `pareto_pruned_total=0`，M9 的真实 24h frozen queue 上限失败仍未解决。本轮只推进
一个可审计的核心算法增量：把已有 `TemporalStateBoundCertificate`/corridor 证书接入
actual Pareto bridge 的新 label 生成点，研究证明不能到达的状态可以在加入 Pareto session
前被丢弃；不改变正式 `TemporalLabelAStar`、合同、ingress/service、默认策略、candidate
或 Winter。

**实现围栏。** `create/restore/run_non_fifo_temporal_pareto_*` 保持默认无证书行为不变；
显式传入证书时，bridge 必须要求 `TemporalDominancePolicy.disabled()`、zero-heuristic、
已知 evaluator、完整 `TemporalScope`，并将证书 digest 纳入 callback/component/session
checkpoint identity。state-bound 只在实际 traversal 已得到 exact arrival 后检查，且只拒绝
“新生成”的 label；已扩展 label、不同 exact arrival、evaluator/hard-mask/coverage 失败和
资源/取消状态均不得被删除或伪装成成功。scope、proof、coverage、evaluator 或 checkpoint
不匹配时 fail-closed：拒绝授权并保持 pruning=0，记录稳定 rejection reason。不能把
reference Dijkstra 路线注入候选，也不能使用 beam/近似剪枝。

**验证。** 扩展 actual bridge synthetic oracle 矩阵：证书有效时至少观察一次合法
state-bound pruning，路线/ETA/7 维 cost/source IDs 与无证书 exhaustive reference 一致；
证书 rejected、scope drift、arrival envelope 不完整、非 FIFO/later-arrival、hard-mask、
evaluator failure、cancel、resource limit 和 checkpoint digest drift 场景 pruning 必须为
零且无 partial route/frontier。验证 one-shot 与 slice→restore 等价、重复 deterministic，
并确认正式默认路径回归不变。实验构件只写入新的 `.runtime/experiments/` 目录并绑定 clean
implementation/lock/config/scope identity；不启动真实 24h，真实输入仍保持
`dominance-disabled`。

**收口。** synthetic 全部通过时仅标记
`READY_FOR_P0.2-REAL-STATE-BOUND-RESOURCE-PLAN`，再另立带强制 cgroup 的真实资源计划；
任一语义、fail-closed、恢复或 identity 失败标记 `NO_PERFORMANCE_PROOF/FAIL`；构件不完整
标记 `INVALID/PENDING`。M9 的 `REAL_INPUT_24H_RESOURCE_FAIL`、M10 的组合证书边界、M13--M15
历史结论保持不变。完成后只本地集成、保留研究分支和构件、清理辅助 worktree，不 push。

### 【2026-08-29 | COMPLETED】P0.2-M16：proof-carrying state-bound actual Pareto bridge

本轮在隔离分支 `research/p02-m16-pareto-state-bound-20260829` 完成。计划提交为
`b749f97`，bridge/测试/runner 实现提交为 `1de6d63`、`4618b28`、`ce54ac1`；最终权威
evidence 绑定 clean implementation commit `ce54ac1ee7a604bf980925dabfa30129aec1ecba`。
早期 r1 构件因启动时 worktree 尚有未提交改动，manifest 的 `implementation.dirty=true`，
按治理规则仅保留为诊断，不纳入结论；r3 为最终权威构件。

**实现边界。** `non_fifo_temporal_pareto.py` 增加显式可选
`state_bound_certificate` 参数，默认 `None` 的 M14/M15 调用行为不变；无显式证书时仍
拒绝 planner 上意外安装的 state-bound。显式证书必须是
`TemporalStateBoundCertificate`，并将证书 digest 纳入 callback/component/session checkpoint
identity。actual edge traversal 先得到 exact arrival，再调用已有
`TemporalLabelAStar._should_prune_state_bound`；只有新生成且超出可用证书的 label 被以
unavailable edge 形式跳过，已扩展 label、不同 exact arrival、evaluator/hard-mask/coverage
失败、取消和资源状态不被删除或包装成成功。scope、proof、coverage、evaluator 或证书
digest 不匹配时保持 pruning=0 并记录拒绝原因。checkpoint 同时保存 state-bound digest 和
累计 checks/pruned/rejection diagnostics，恢复后继续同一证据计数。

**权威 synthetic evidence。** 新增 runner schema
`c.p0.2-temporal-pareto-state-bound.v1`，构件目录为
`.runtime/experiments/c-p02-m16-pareto-state-bound-20260829-r3/`，包含 manifest、cases、
summary、heartbeat 和 `ALL_DONE`。矩阵为
`certified/scope_mismatch/coverage_incomplete/checkpoint_drift/disabled` ×
`fastest/low_risk/recommended` × `one_shot/slice_restore/cancelled` × 2 repetitions，
共 `90/90`。摘要为 `TEMPORAL_NONFIFO_PARETO_STATE_BOUND_MATRIX_PASS`：
`deterministic=true`、独立 exhaustive oracle 语义对照通过、有效证书观察到真实
state-bound pruning、scope/coverage/disabled 场景 pruning 为零、checkpoint certificate
drift 全部拒绝、取消无 partial route/frontier、资源观察 clean。正式默认路径仍为
`state_bound=disabled`，candidate/Winter 均为 false。

**结论与边界。** 本轮证明有限 actual-edge Pareto bridge 可以在完整 scope 与 proof-carrying
state bound 下安全拒绝新 label，同时保留 exact-arrival/业务语义和恢复围栏；不证明真实
连续海洋模型的 corridor 证书、不证明 24h 资源前沿、不改变冻结
`50k/100k/50k/400k` 上限，也不把 M9 的 `REAL_INPUT_24H_RESOURCE_FAIL` 转为通过。真实
145 帧输入本轮没有启用该证书路径；下一步另立带真实 scope、独立 admissible bound 证明和
强制 cgroup 的 Pareto 资源计划。M10、M13、M14、M15、FIFO violation 及 P3/ARA* 历史结论
保持不变；完成后仅本地集成，不 push。

### 【2026-08-29 | PLANNED】P0.2-M17：real actual-Pareto topological state-bound qualification

M16 已证明 proof-carrying state bound 可以安全接入 actual Pareto bridge，但证书和剪枝只
在 synthetic finite graph 上验证。M17 仅把已有、独立实现的 graph-topological maximum-speed
lower-bound/corridor envelope 接到冻结的真实 145 帧 `executable_0_6h` 输入，审计真实 scope
下的语义与资源边界；不执行 24h、full-voyage、Winter 或 candidate。

**实现边界。** 新增独立 real runner，复用已审计的 RiskFrame/route-plan-set loader、完整
frame digest、目标自动解析和 reference Dijkstra。每个 holdout/development、
`fastest/low_risk/recommended` case 固定 `EtaRefinementPolicy(method="bounded")`（仅研究
worker）、`use_heuristic=False`、`pareto_pruning=True` 和 `skip_expected_rejections=True`；
baseline 不传证书，candidate 显式传同 scope 的 topological arrival certificate。证书只基于
完整有限网格邻接、最大船速和保守 reverse lower bound，不能注入 reference route；actual
bridge 仍只丢弃新生成 label，已扩展 label、不同 exact arrival 和 domain rejection 不被
删除。所有 identity 绑定 implementation/lock/config/RiskFrame/route-plan-set/scope/ETA
policy/search-limit/evaluator digest。

**验证与状态。** 每输入/目标运行 one-shot、slice→restore、两次重复，并保存
`manifest.json`、`cases.jsonl`、`resource-frontier.jsonl`、`comparison-summary.json`、
`heartbeat.json` 和终态 marker。要求无证书与证书路线、ETA、7 维 cost、speed/risk/
confidence/source IDs 和失败语义与 reference 一致，恢复 deterministic，证书 scope/身份
完整且拒绝时 pruning=0。实际 pruning 为零不算失败，但不得宣称性能收益。宿主 cgroup 若
`memory.max`/`memory.swap.max` 为 `max`，只记录 `RESOURCE_EVIDENCE_INCONCLUSIVE`，不宣称
4GiB 资源资格；不提高 `50k/100k/50k/400k` 上限。

**收口。** 语义、身份和恢复完整且强 cgroup 存在时标记
`READY_FOR_P0.2-REAL-STATE-BOUND-RESOURCE-REVIEW`；语义通过但强 cgroup 缺失标记
`REAL_INPUT_STATE_BOUND_SEMANTIC_PASS_RESOURCE_INCONCLUSIVE`；语义/identity/fail-closed
失败标记 `NO_PERFORMANCE_PROOF/FAIL`，构件不完整标记 `INVALID/PENDING`。任何状态都不授权
dominance/candidate/Winter；M9 的真实 24h resource fail、FIFO violation、M14--M16 历史
结论保持不变。完成后仅本地集成、保留构件、清理辅助 worktree，不 push。

### 【2026-08-29 | COMPLETED】P0.2-M17：real actual-Pareto topological state-bound qualification

本轮按 `f5cb9bb` 的计划在隔离分支
`research/p02-m17-real-pareto-state-bound-20260829` 完成；权威实现为 clean commit
`bc458f913ef3c27f5be5ff71a85c2324deddda55`。新增独立 runner
`scripts/benchmark_non_fifo_temporal_pareto_state_bound_real.py`，未修改 B/C、C/D 合同、
ingress/service 或正式 planner 默认路径。较早未使用强 cgroup 的 r1/r2 构件保留为诊断；本节
只引用 systemd transient scope 下的 r3 构件。

**输入与身份。** holdout 与 development 均复用完整 145 帧 committed RiskFrame、冻结
four-layer route plan set 的 `executable_0_6h` 目标、共同起点 `(5, 7)` 和自动解析终点；
实现、`uv.lock`、configs tree、RiskFrame content/frame digests、route-plan-set、三目标
`TemporalScope.digest`、bounded ETA policy、evaluator 和冻结
`50k/100k/50k/400k` limits 均写入 manifest。holdout 构件为
`.runtime/experiments/c-p02-m17-real-pareto-state-bound-holdout-6h-20260829-r3/`，experiment
id `c.p0.2-temporal-pareto-state-bound-real.v1-243564c4fcc44429`；development 构件为
`.runtime/experiments/c-p02-m17-real-pareto-state-bound-development-6h-20260829-r3/`，experiment
id `c.p0.2-temporal-pareto-state-bound-real.v1-3d6d0f4670b3d6e0`。

**方法与证据。** 每个输入执行 `fastest/low_risk/recommended` ×
`one_shot/slice_restore` × 2 repetitions，共 12 cases；baseline 和 candidate 都是
actual-edge、`use_heuristic=False`、`pareto_pruning=True`、dominance disabled，candidate
仅显式传入同 scope 的 graph-topological maximum-speed arrival certificate。两输入所有
12/12 case 的 baseline/candidate/reference 路线、精确 ETA、7 维 cost、speed/risk/
confidence/source IDs 和失败语义一致；one-shot 与 slice→restore deterministic，scope
digest 两侧一致，checkpoint 恢复无拒绝漂移，state-bound rejection=0、意外 pruning=0。
holdout 观察 `state_bound_pruned=276`（arrival-bound 216）；development 观察
`state_bound_pruned=300`（arrival-bound 228），证明真实 actual Pareto 路径确实发生了
新 label 的 certified pruning。baseline/candidate 最大 queue 分别为 holdout `26/3`、
development `13/3`，最大 RSS 约 `120084/120044 KiB`，未见 swap、OOM 或超时。

两个权威构件均在 systemd scope `MemoryMax=4G`、`MemorySwapMax=0` 下运行，记录的
`memory.max=4294967296`、`memory.swap.max=0`、`memory.swap.current=0` 且 memory events
全为 0；summary 状态均为
`READY_FOR_P0.2-REAL-PARETO-STATE-BOUND-REVIEW`。这只是“真实输入、真实 scope 下的
研究证书路径可复核”结论，不是性能门、candidate 晋级或 Winter 授权；candidate 和
Winter 仍为 false，`TemporalDominancePolicy.disabled()` 仍是正式默认。

**未执行与下一步。** 依计划未执行 `rolling_0_24h`、full-voyage、Winter、candidate 或
production/frozen/latest 写入；M9 的 24h queue resource fail、FIFO violation、M14--M16、
P3 和 ARA* 历史结论保持不变。下一步仅可另立“带连续 FIFO/interval proof 的 dominance
资格”或“真实 24h corridor/resource frontier”计划；不因本轮 pruning 结果自动启用任何
生产算法。

### 【2026-08-29 | PLANNED】P0.2-M18：real 24h actual-Pareto state-bound resource frontier

M17 已在真实 145 帧 `executable_0_6h` 输入、完整 scope 和强制 `MemoryMax=4G`/
`MemorySwapMax=0` 下证明 graph-topological arrival envelope 可以安全接入 actual Pareto
bridge，并观察到真实新 label pruning；但 M9 的 `rolling_0_24h` frozen queue 上限失败仍未
得到 state-bound 路径下的可审计前沿。本轮只做真实 24h 资源诊断，不提高
`50k/100k/50k/400k` 限制，不调用 `certified_only(...)`，不授权 candidate/Winter。

**隔离与身份。** 从 M17 权威提交 `63bf381` 建立本地分支
`research/p02-m18-real-pareto-24h-20260829` 和独立 worktree；正式
`research-validation-system`、M17 worktree/构件和其他 agent 改动保持只读。runner identity
继续绑定 implementation、`uv.lock`、configs、145 帧 RiskFrame content/frame digests、冻结
route-plan-set、三目标 TemporalScope、bounded ETA、search limits、topological bound/evaluator
digest，并要求 clean implementation worktree。复用 holdout/development 的冻结
`rolling_0_24h` 目标，不下载或替换输入。

**执行。** 扩展 M17 actual-Pareto state-bound runner 仅支持
`rolling_0_24h`（并保留 `executable_0_6h` 兼容性），每输入、目标、one-shot 与
slice→restore 至少一份独立 case；worker 固定 CPU、`MemoryMax=4G`、`MemorySwapMax=0`，每个
case 立即 fsync manifest/cases/resource evidence。baseline 不传证书，candidate 只显式传完整
scope 的 graph-topological arrival certificate；reference zero-heuristic Dijkstra 仅用于路线和
业务字段正确性。某目标触及 queue/label/expansion/edge 上限、timeout、OOM 或资源证据缺失时，
保存该失败并继续其他目标/输入；不择优重跑，不放宽上限。

**通过与失败语义。** 每个完成 case 必须保持 baseline/candidate/reference 的节点、精确 ETA、
速度、风险、成本、confidence、source IDs、失败语义和 deterministic digest 一致；scope、
certificate 或 state-bound rejection 失败时 pruning 必须为零。24h 观察到合法 pruning 只作
资源证据，不构成性能门或连续海洋模型最优性证明。状态固定为：语义完整且强 cgroup 资源干净
时 `REAL_INPUT_24H_STATE_BOUND_RESOURCE_REVIEW`；触及冻结上限时
`REAL_INPUT_24H_STATE_BOUND_RESOURCE_FAIL`；语义通过但 cgroup/构件不完整时
`REAL_INPUT_STATE_BOUND_SEMANTIC_PASS_RESOURCE_INCONCLUSIVE`；身份漂移、fail-open 或不完整
构件为 `INVALID/PENDING`。无论结果如何，FIFO violation、dominance disabled、candidate 和
Winter 边界不变；仅可为后续 corridor/envelope 或 P0.2 implementation 计划提供证据。

### 【2026-08-29 | COMPLETED】P0.2-M18：real 24h actual-Pareto state-bound resource frontier

本轮在隔离分支 `research/p02-m18-real-pareto-24h-20260829` 的 clean commit
`f582e634258a7b18acc15e75657f39b02dc4f265` 完成；新增 M18 schema
`c.p0.2-temporal-pareto-state-bound-24h.v1`，只扩展 M17 actual-edge Pareto state-bound
研究 runner 的 `rolling_0_24h` 输入和冻结资源结果语义。没有修改 B/C、C/D 合同、ingress/service
或正式 planner 默认路径；`TemporalDominancePolicy.disabled()`、candidate/Winter 均保持关闭。

**输入与身份。** holdout/development 均复用完整 145 帧 committed RiskFrame 和冻结
four-layer route-plan-set 自动解析的 `rolling_0_24h` 目标（共同起点 `(5, 7)`；holdout
目标 `(14, 5)`，development 目标 `(14, 6)`）。manifest 绑定 implementation 文件 hashes、
branch/commit、`uv.lock`、configs tree、每帧 content digest、route-plan-set、三目标
TemporalScope digest、bounded ETA、topological bound/evaluator 和冻结
`50k/100k/50k/400k` search limits。权威构件为：

- holdout：`.runtime/experiments/c-p02-m18-real-pareto-24h-holdout-20260829-r1/`，experiment
  id `c.p0.2-temporal-pareto-state-bound-24h.v1-361e5d987a1a1877`；
- development：`.runtime/experiments/c-p02-m18-real-pareto-24h-development-20260829-r1/`，
  experiment id `c.p0.2-temporal-pareto-state-bound-24h.v1-3e261b87102e017c`。

每个输入均完成 `fastest/low_risk/recommended × one_shot/slice_restore × 1`，共 `6/6`
case；`manifest.json`、`cases.jsonl`、`resource-frontier.jsonl`、`comparison-summary.json`、
`heartbeat.json` 和 `ALL_DONE` 齐全，deterministic=true，scope/certificate rejection=0，
unexpected pruning=false。slice→restore 的 checkpoint/session identity 与 one-shot 稳定。

**资源前沿。** holdout 三目标两模式均为 baseline `RESOURCE_LIMIT`（queue peak `50,001`）
而 candidate `GOAL_FOUND`；candidate queue peak 为 fastest `3,369`、low_risk `3,358`、
recommended `3,398`，每 case state-bound checks `81,927`、合法新-label pruning `71,446`，
合计 `428,676`。development 同样三目标两模式均为 baseline queue `50,001` 的
`RESOURCE_LIMIT`、candidate `GOAL_FOUND`；candidate queue peak 为 fastest `1,997`、
low_risk `1,911`、recommended `1,943`，pruning 分别为 `38,382`、`38,366`、`38,382`，
合计 `230,260`，checks 为 `44,056`、`44,040`、`44,056`。两输入所有 case 的候选
state-bound rejection 为 0，未提高任何上限。由于 baseline 在冻结 queue 上限前没有完成，
独立 reference Dijkstra 未进入路线对照；因此本轮不宣称 24h semantic/correctness 或性能
通过，只记录 candidate 可完成与资源边界的研究观察。

**资源证据与结论。** 全部 12 case 在 systemd scope `MemoryMax=4G`、`MemorySwapMax=0`、
固定 CPU 0 下运行；记录 `memory.max=4294967296`、`memory.swap.max=0`、
`memory.swap.current=0`，process/host swap 为零，OOM/timeout/cgroup memory events 全为 0，
`resource_evidence_complete=true`。两份 summary 均为
`REAL_INPUT_24H_STATE_BOUND_RESOURCE_FAIL`，表示冻结 queue limit 仍是资源失败边界，而非
state-bound 语义失败；M9 的 24h resource fail、已知 `REAL_INPUT_FIFO_VIOLATED`、M14--M17、
P3/ARA* 历史结论保持不变。最终状态不授权 dominance/candidate/Winter，也不构成连续海洋
模型的全局最优性或生产资格。下一步只能另立“带独立 reference/semantic proof 的 24h
corridor/envelope”或 P0.2 非 FIFO implementation 计划；不自动放宽 queue、不重跑 Winter。

### 【2026-08-29 | PLANNED】P0.2-M19：certificate-aware independent 24h semantic reference

M18 在真实 24h holdout/development 上观察到 graph-topological state bound 将 candidate
搜索压在较小 queue 内，但 baseline 均触及冻结 `queue=50,000`，因此没有进入独立 Dijkstra
路线对照。本轮只补齐这一审计缺口：以同一份完整、scope-bound、admissible arrival envelope
构造独立 exact-arrival Dijkstra reference；不把 reference 当性能基线、不提高任何资源上限、
不调用 `certified_only(...)`，也不启用 candidate/Winter。

**研究边界。** 从 M18 clean tip `c168c4b` 建立隔离分支
`research/p02-m19-reference-24h-20260829`。新增 runner 必须独立维护 Dijkstra 的
`(node, incoming_heading, exact_UTC_arrival)` 状态表，使用冻结 edge evaluator 和同一
`TemporalStateBoundCertificate` 的 `allows_state(...)` 只拒绝新生成、已证明不可能在
24h horizon 内到达 goal 的状态；不能删除已扩展 label、合并不同 exact arrival、注入
candidate route 或使用 beam/近似剪枝。baseline/candidate/reference 的 scope、ETA policy、
RiskFrame/route-plan-set/config/lock digest 及 `50k/100k/50k/400k` limits 全部写入 identity。

**失败语义。** reference `GOAL_FOUND` 时，逐字段比较 candidate 的节点、精确 ETA、速度、
风险、成本、confidence、source IDs、CostBreakdown、semantic/frontier digest；reference
触及冻结 queue/label/expansion/edge 上限时记录 `REFERENCE_RESOURCE_LIMIT`，不将 candidate
结果包装成 correctness pass；未知 evaluator、scope/proof mismatch、hard-mask 或 coverage
异常为 `REFERENCE_FAILURE` 并保持 fail-closed。每个输入/目标至少保留 one-shot、
certificate-aware reference、resource snapshot、heartbeat 与可恢复 marker。

**收口。** 两输入/三目标的 reference 均完成且逐字段一致时仅标记
`REAL_INPUT_24H_SEMANTIC_REFERENCE_READY`，不代表性能或生产资格；任一 reference resource
limit 标记 `REAL_INPUT_24H_REFERENCE_RESOURCE_FAIL`；semantic mismatch、fail-open 或
identity 漂移为 `NO_PERFORMANCE_PROOF/FAIL`；构件不完整为 `INVALID/PENDING`。M18 的
`REAL_INPUT_24H_STATE_BOUND_RESOURCE_FAIL`、FIFO violation、dominance disabled、P3/ARA* 和
Winter 历史结论保持不变。完成后只保留本地分支与构件，清理 worktree，不 push、不合并正式分支。

### 【2026-08-29 | COMPLETED】P0.2-M19：real 24h independent semantic reference

本轮在隔离分支 `research/p02-m19-reference-24h-20260829` 完成；计划、实现和修正提交分别为
`680f14e`、`84c0cc5`、`f3a6bbc`。新增 runner
`scripts/benchmark_non_fifo_temporal_pareto_reference_24h.py` 是 C 内部研究 sidecar，维护独立
zero-heuristic exact-arrival Dijkstra 状态 `(node, incoming_heading, exact_UTC_arrival)`。
它在完整 `TemporalStateBoundCertificate` 通过 scope 和 arrival-envelope identity 后，才对新
生成 state 调用 `allows_state(...)`；没有调用 `planner.plan()`、`certified_only(...)` 或候选
路线注入。`TemporalDominancePolicy.disabled()`、candidate/Winter 和所有 B/C、C/D、ingress/service
边界保持不变，search limits 仍为 `50k/100k/50k/400k`，每条记录立即 fsync。

**输入与身份。** holdout/development 均复用完整 145 帧 committed RiskFrame、冻结
`rolling_0_24h` route-plan-set 和 M18 topological arrival certificate；manifest 绑定最终
implementation commit `f3a6bbc01fb6571ef5d932fd69ced1853e424574`、实现文件 hashes、`uv.lock`、
configs tree、RiskFrame commit/content/frame digests、route-plan-set、三目标 TemporalScope、
bounded ETA、reference policy、state-bound proof 和固定 limits。权威构件为：

- holdout：`.runtime/experiments/c-p02-m19-reference-24h-holdout-20260829-r1/`，experiment id
  `c.p0.2-temporal-pareto-reference-24h.v1-a31a67f489f2809b`；
- development：`.runtime/experiments/c-p02-m19-reference-24h-development-20260829-r1/`，
  experiment id `c.p0.2-temporal-pareto-reference-24h.v1-33ac6a2800cd5f41`。

两份构件均完成 `fastest/low_risk/recommended × 1`，共 `6/6` case，`ALL_DONE`、manifest、
cases、reference frontier、summary 和 heartbeat 齐全。每个 case 的 reference 与 actual-Pareto
candidate 均为 `GOAL_FOUND`，节点、精确 ETA、CostBreakdown、速度、距离、风险、maximum risk、
confidence 和 source IDs 逐字段一致（`reference_match=true`）。reference 观察到合法的
arrival-envelope pruning：holdout 三目标均 `68,213`，development 三目标均 `35,641`；candidate
pruning 分别为 holdout `71,446`、development `38,366/38,382`，certificate rejection 和
unexpected pruning 均为零。runner 的 `deterministic=true` 是单次完成记录的稳定签名；本轮不是
重复次数性能门，不据此宣称统计性能收益。

**资源证据与结论。** 全部 6 个 worker 在 systemd scope 固定 CPU 0、`MemoryMax=4G`、
`MemorySwapMax=0` 下运行；host `/proc/swaps` 和 `free -h` 均为 `Swap: 0B`，scope
`memory.swap.max/current=0`，memory events 的 OOM/kill/high/low 全为 0，resource evidence
complete=true。cgroup memory peak 最大约 `192,372,736` bytes（约 183.5 MiB），reference queue
peak holdout 为 `3,347–3,388`、development 为 `1,836–1,922`，均未触及冻结上限。两输入
summary 均为 `REAL_INPUT_24H_SEMANTIC_REFERENCE_READY`，表示独立语义 reference 缺口已补齐；
这不是 candidate 性能通过、continuous FIFO proof、production 资格或 Winter 重启授权。已知
`REAL_INPUT_FIFO_VIOLATED`、M18 resource-frontier、P3/ARA* 和 Winter 历史结论保持不变。

**验证。** 全 pytest 为 `551 passed, 3 skipped`；跳过项是已知的
`test_p21_m2j_diagnostic_profile.py`（隔离 worktree 没有 orchestrator `winter_p2_shadow.py`）。
M19/M18 聚焦 Ruff、CLI help、`uv lock --check`、offline sync、active/archive
`temporal_session` import boundary 和 `git diff --check` 均通过。全仓 Ruff 仍只有既有无关的
`scripts/benchmark_bc_coupling.py:721` E501；原样 `UV_OFFLINE=1 make check` 因隔离 worktree
缺少 `.mamba-env/bin/uv` 返回阻塞，未修改环境。M19 辅助 worktree 已在验证后移除，分支和
`.runtime/experiments/` 构件保留，未 push、未合并正式 `research-validation-system`。

**后续分支。** M19 只允许制定下一份“带 interval/continuous proof 的真实 dominance 资格”或
“非 FIFO label-correcting/Pareto 实现”计划；不得自动启用 candidate、提高资源上限、进入
full-voyage/Winter、写 formal latest/replanning baseline/frozen artifact，或把本轮
`SEMANTIC_REFERENCE_READY` 解读为性能晋级。

### 【2026-08-29 | PLANNED】P0.2-M20：complete non-FIFO Pareto frontier proof

M19 已经用独立 exact-arrival Dijkstra 对真实 24h 的**选中路线**完成逐字段语义对照，但
仍未把“完整 goal Pareto frontier 是否一致”做成可复核的研究证书。本轮只补齐这一正确性
证据缺口，不改变非 FIFO 搜索规则，不把任何结果包装成 production planner 或性能门。

**研究边界。** 从 M19 clean tip 建立隔离分支和 worktree；新增的 frontier certificate/
verifier 只能位于 C 内部 `non_fifo_feasibility` sidecar。证书绑定 session identity、scope、
policy、evaluator/config、fixture、冻结四项资源上限和 frontier digest；只有完整耗尽且
`GOAL_FOUND`、无 evaluator error/cancel/resource limit 的结果才可标记 complete。不同精确
到达时刻永远不互相支配；只允许在同一 `(node, exact_UTC_arrival)` 状态丢弃新生成的向量支配
label，已扩展 label 不删除。

**验收矩阵。** 增加独立的完整 frontier 比较器和 test-only runner，逐个比较 candidate/reference
的所有 goal labels（节点、精确 ETA、路径、向量成本、转移业务 evidence/source IDs），而不
只比较 selected route。覆盖同桶不同 ETA、同一 exact state 的可安全支配/不可支配标签、非 FIFO
后缀反例、邻接顺序变化、checkpoint identity/policy/limit/evaluator drift、取消、evaluator
failure、资源上限、hard-mask 和重复运行 deterministic。任何不完整结果、scope/identity
漂移或 frontier mismatch 均 fail-closed；不得使用容差、beam、候选路线注入或 Dijkstra 结果
替换 candidate。

**收口。** synthetic finite fixtures 全部通过时，只标记
`READY_FOR_P0.2-IMPLEMENTATION-REVIEW`；任一 frontier 缺失、误剪枝、语义不一致或资源证据
不完整则标记 `NO_FRONTIER_PROOF/FAIL`。本轮不执行真实 24h 重跑、不提高
`50k/100k/50k/400k` 上限、不调用 `certified_only(...)`，不启用 candidate/Winter；M19
真实 reference、M18 queue resource fail、FIFO violation、P3/ARA* 历史结论全部保留。

### 【2026-08-29 | COMPLETED】P0.2-M20：complete non-FIFO Pareto frontier proof

本轮在隔离分支 `research/p02-m20-frontier-proof-20260829` 的 clean tip
`7eb7d4248d57307ddc0ef9be775292a15240a6be` 完成。新增内容仍是 C 内部研究 sidecar：

- `NonFifoParetoFrontierCertificate`：只有 terminal `GOAL_FOUND`、无 evaluator error、无
  resource/cancel、search limits 与 session identity 完全一致且 goal frontier 非空时才
  `usable=true`；证书绑定完整 callback session digest、独立比较用 input/config digest、
  scope digest、Pareto policy、冻结四项资源上限和 canonical frontier digest；
- `NonFifoParetoFrontierComparison` / `compare_non_fifo_pareto_frontiers`：对独立实现的全部
  goal labels 做多重集合精确比较，包含节点、精确 UTC 到达时间、完整路径、向量成本和每条
  transition business/source evidence；scope、输入 identity、frontier 不一致或证书不完整
  分别返回 `IDENTITY_MISMATCH`、`FRONTIER_MISMATCH`、`INCOMPLETE`，没有数值容差或 selected
  route 替代；
- `NonFifoTemporalParetoResearchSession.frontier_certificate`：仅 terminal session 暴露证书，
  paused/ready 状态直接拒绝；既有 `NonFifoParetoSession` checkpoint/restore 的 callback
  digest fence 保持不变。

**权威 synthetic 构件。** `c.p0.2-nonfifo-pareto-frontier.v1` runner 已接入 certificate
并记录其 digest、scope、policy、goal/frontier counts 和 rejection reason；代码提交后在 clean
tip 重新执行（此前 dirty 代码下的 r3 构件不作为证据）。权威目录为
`.runtime/experiments/c-p02-m20-frontier-proof-synthetic-20260829-r4/`，manifest 的
implementation commit 与 clean tip 一致，`cases.jsonl` 共 `72/72`（12 fixture × 3 objective ×
2 policy × 1 repetition），并包含 `manifest.json`、`cases.jsonl`、`comparison-summary.json`、
`heartbeat.json`、`ALL_DONE`。

**结果。** summary 为 `TEMPORAL_NONFIFO_PARETO_FRONTIER_MATRIX_PASS`：
`deterministic=true`、`semantic_match=true`、`fail_closed=true`、
`frontier_certificate_complete=true`、`policy_digest_bound=true`、
`resource_evidence_complete=true`、`resource_clean=true`、`worker_errors=false`；所有失败
fixture 均保持无 partial route，严格同 exact state 的 candidate pruning 观察到 `3` 次，
不同 exact arrival、周期/后缀反例、hard-mask、evaluator failure、取消、资源上限和维度/到达
错误均未被误判为成功。全量 C 测试 `557 passed, 3 skipped`；跳过仍是隔离 worktree 缺少
orchestrator `winter_p2_shadow.py` 的既有 M2J 诊断项。Ruff、lock check、offline sync、CLI
smoke、active/archive `temporal_session` import boundary、`git diff --check` 均通过；直接
`UV_OFFLINE=1 make check` 仍因 Makefile 期待隔离 worktree 自带 `.mamba-env/bin/uv` 而不可执行，
已用正式 C `.mamba-env/bin/uv` 逐项等价复现并通过。

**边界与下一步。** 本轮没有真实 24h 重跑、没有提高 `50k/100k/50k/400k`、没有调用
`certified_only(...)`，`TemporalDominancePolicy.disabled()`、candidate/Winter、B/C 与 C/D
合同、ingress/service、formal latest/replanning baseline/frozen artifact 均未改变。M20 只证明
有限非 FIFO sidecar 的完整 frontier 证据链可审计；不构成真实连续海洋模型最优性、性能晋级或
生产资格。已知真实 `FIFO_UNCERTAIN/VIOLATED`、M18 queue resource fail、M19 independent
24h semantic reference、P3/ARA* 历史结论继续保留；后续才可另立带真实 scope 的 P0.2
implementation review 或 ETA interval proof 计划。

### 【2026-08-29 | PLANNED】P0.2-M21：real 6h Pareto frontier equivalence

M20 只在有限 fixture 上证明了完整 frontier certificate；M19 的真实 24h 对照则只比较了
selected route。本轮补齐一个有限、可承受的真实输入证据：在冻结 holdout/development 的
`executable_0_6h` 上，同一完整 `TemporalScope` 下分别运行不做 Pareto 剪枝的 reference
sidecar 与只允许同一 `(node, exact_UTC_arrival)` 新 label 组件支配的 candidate sidecar，
对完整 goal frontier 做精确多重集合比较。它不证明 FIFO，也不启用 production candidate。

**隔离和身份。** 从 M20 clean tip 建立 `research/p02-m21-real-frontier-equivalence-20260829`
隔离 worktree。新增 runner 只支持完整 145 帧 holdout/development 的
`executable_0_6h`，三目标 `fastest/low_risk/recommended`，每个 policy/objective/repetition
独立 worker；每 cell 默认两次重复，顺序交替。manifest/cases/frontier-comparison/resource
evidence/heartbeat/终止 marker 绑定 implementation commit、实现文件、`uv.lock`、配置树、
RiskFrame commit/content/frame digests、冻结 route-plan-set、节点/出发时间/ETA policy/scope、
Pareto policy 和 `50k/100k/50k/400k` limits。启动前要求 clean implementation worktree；
systemd scope 固定 CPU、`MemoryMax=4G`、`MemorySwapMax=0`，不并行 worker。

**正确性规则。** baseline 使用 `pareto_pruning=False`，candidate 使用显式
`pareto_pruning=True`；两者均 `TemporalDominancePolicy.disabled()`、无 state-bound、无 heuristic
和无 `certified_only(...)`。每个 terminal successful session 生成 M20 frontier certificate；
`compare_non_fifo_pareto_frontiers` 要求 shared input/config/scope digest 完全相同，并逐项比较
节点、精确 UTC ETA、完整路径、向量成本、transition business evidence 和 source IDs。资源
超限、取消、evaluator/coverage failure、certificate incomplete、scope/identity drift 或
frontier mismatch 均 fail-closed；不使用数值容差、不注入 reference route、不删除已扩展 label。
selected route 另与既有独立 zero-heuristic point oracle 对照，但不把 oracle 当性能基线。

**收口分支。** 所有 policy/objective/repetition 的 certificate complete、frontier exact match、
point reference match、deterministic 和资源证据通过时，只标记
`READY_FOR_P0.2-REAL-FRONTIER-IMPLEMENTATION-REVIEW`；任一 frontier/语义/fail-closed 失败为
`NO_FRONTIER_PROOF/FAIL`；资源或构件不完整为
`REAL_INPUT_FRONTIER_EQUIVALENCE_INCONCLUSIVE`。本轮不执行 24h/full-voyage、不提高资源上限、
不写 formal latest/replanning baseline/frozen artifact，不改变 FIFO violation/uncertain、
candidate/Winter、P3/ARA* 或正式合同状态。

### 【2026-08-29 | PLANNED】P0.2-M22：real 24h state-bound Pareto frontier equivalence

M18 已在真实 `rolling_0_24h` 上观察到 graph-topological arrival envelope 能将 actual
Pareto candidate 压在冻结 queue 上限内，但 baseline 未完成，M19 只补齐了 selected route 的
独立 reference，M20/M21 分别补齐了 synthetic/真实 6h 的完整 frontier 证据。本轮把三者合并
为一个有限的真实 24h 正确性审计：同一完整 `TemporalScope`、同一 proof-carrying
`TemporalStateBoundCertificate` 下，比较 `pareto_pruning=False` 的 certified reference
sidecar 与 `pareto_pruning=True` 的 candidate sidecar 的完整 goal frontier，并以独立
zero-heuristic Dijkstra 做 selected-route 业务字段对照。

**边界和资源。** 只运行冻结 holdout/development 的 `rolling_0_24h`，三目标，每个 policy/
objective/mode 至少一次；mode 为 one-shot 与 slice→restore。两边均保持
`TemporalDominancePolicy.disabled()`、`use_heuristic=False`、无 `certified_only(...)`，仅显式
使用同一 graph-topological arrival bound；不提高 `50k/100k/50k/400k`，不执行 full-voyage、
Winter 或 candidate。每个 worker 置于 `MemoryMax=4G`、`MemorySwapMax=0`、固定 CPU 的独立
systemd scope；任何 queue/label/expansion/edge limit、timeout、OOM、scope/proof mismatch
或 evaluator failure 都记录为资源/证据失败，不择优重跑。

**正确性。** reference/candidate 的完整 frontier 必须证书 complete、scope/comparison
identity 相同，并逐项比较节点、exact UTC arrival、路径、7 维成本、速度、风险、confidence、
source IDs 和 CostBreakdown；selected route 继续与独立 Dijkstra 对照。只允许把派生
`semantic_digest` 的稳定序列化差异标为 `SEMANTIC_MATCH` 诊断接受，不能忽略业务字段或使用
数值容差；实际 exact match 仍单独记录。state-bound 只能拒绝新生成 label，已扩展 label、
不同 exact arrival、失败/取消/资源状态不得删除或伪装成功。

**收口。** 完整 frontier、selected-route 语义、slice→restore、determinism、fail-closed 和
强 cgroup 全部通过时标记 `READY_FOR_P0.2-REAL-24H-FRONTIER-IMPLEMENTATION-REVIEW`；任一
语义/证书/fail-closed 失败为 `NO_FRONTIER_PROOF/FAIL`；冻结资源触顶为
`REAL_INPUT_24H_STATE_BOUND_FRONTIER_RESOURCE_FAIL`；构件不完整或身份漂移为
`INVALID/PENDING`。无论结果如何，不授权 dominance、candidate/Winter 或生产接口；完成后
只保留本地分支与实验构件并清理辅助 worktree。

### 【2026-08-29 | COMPLETED】P0.2-M21：real 6h Pareto frontier equivalence

本轮在隔离分支 `research/p02-m21-real-frontier-equivalence-20260829` 完成；实现提交为
`b1d61ea`，聚焦测试提交为 `690741e`，实验启动时 worktree clean，实验身份绑定的
implementation commit 为 `690741e7e6352835f1b1aad35f62c325eecd5aa5`。新增
`scripts/benchmark_non_fifo_temporal_pareto_frontier_real.py` 仍是 C 内部研究 runner：它从已审计
real-input fixture loader 读取完整 145 帧 RiskFrame 和冻结 route-plan-set，分别以
`pareto_pruning=False/True` 运行 actual temporal Pareto sidecar；两边都保持
`TemporalDominancePolicy.disabled()`、无 heuristic、无 state-bound、无 `certified_only(...)`，
不修改正式 planner、合同或 ingress/service。

**身份、输入和执行。** holdout/development 均只运行 `executable_0_6h`，三目标
`fastest/low_risk/recommended`，两种 policy、两次重复，共每输入 `12/12` 独立 worker；重复顺序
交替，CPU 固定为 0，systemd scope 为 `MemoryMax=4G`、`MemorySwapMax=0`。每个目录均具备
`manifest.json`、`cases.jsonl`、`frontier-comparison.jsonl`、`resource-frontier.jsonl`、
`comparison-summary.json`、`heartbeat.json` 和 `ALL_DONE`。权威构件为：

- holdout：`.runtime/experiments/c-p02-m21-real-frontier-equivalence-holdout-20260829-r1/`，
  experiment id `c.p0.2-temporal-pareto-frontier-real.v1-7722d09c1070b35a`，RiskFrame 145 帧、
  start `[5,7]`、goal `[7,6]`、departure `2026-02-22T00:00:00Z`；
- development：`.runtime/experiments/c-p02-m21-real-frontier-equivalence-development-20260829-r1/`，
  experiment id `c.p0.2-temporal-pareto-frontier-real.v1-b48ef4c188a3d6b7`，RiskFrame 145 帧、
  start `[5,7]`、goal `[7,7]`、departure `2026-03-22T00:00:00Z`。

两份 manifest 的 implementation digest 均为
`78ad48eb93467e81c5da7ec0af040f76700794e9e61c92e4e2c5db1db6e22aef`，`uv.lock` digest 均为
`8893cb8387825ca4890ed808f4a98b02ba938337752971dacc3e77f859164f22`；RiskFrame commit/content/frame
digest、route-plan-set 和 configs tree 均逐实验绑定，未使用新下载数据。

**正确性结果。** holdout 与 development summary 均为
`READY_FOR_P0.2-REAL-FRONTIER-IMPLEMENTATION-REVIEW`：`12/12` case terminal
`GOAL_FOUND`、`deterministic=true`、`point_reference_match=true`、`frontier_pair_count=6`、
`frontier_pairs_match=true`、`strict_frontier_pairs_match=true`、
`semantic_frontier_pairs_match=true`、`no_unexpected_pruning=true`。每个 policy/objective 的
两次运行均保留相同完整 goal frontier；六个 pair 的节点、精确 UTC arrival、路径、向量成本、
transition business evidence/source IDs 和证书 scope/comparison identity 均严格 MATCH。为响应
“适当放宽正确性门禁”的授权，runner 同时记录一个仅去除派生 `semantic_digest` 字段的
`SEMANTIC_MATCH` 诊断层；它不使用数值容差、不忽略任何业务字段，且只有 identity/certificate
完整时才可接受。此次真实构件实际全部为严格 `MATCH`，未使用放宽分支。

独立 zero-heuristic point oracle 的 selected route 对照在两输入、三目标、两 policy、两重复
全部通过。真实输入本轮 `pareto_pruned_total=0`（queue 尚未形成可安全消除的同 exact-arrival
新 label）；这不是性能收益证明，也不把 synthetic M20 的 3 次安全 pruning 外推到真实输入。

**资源与边界。** holdout 每 case `expanded=32`、`queue_peak=26`、`edge_evaluations=240`，
compute 约 `1204–1680 ms`，观测最大 RSS 约 `120300 KiB`；development 每 case
`expanded=17`、`queue_peak=13`、`edge_evaluations=128`，compute 约 `645–753 ms`，最大 RSS
约 `120324 KiB`。两输入均为 `resource_clean=true`、`resource_evidence_complete=true`，
cgroup memory events 的 OOM/kill/high/low 均为 0，`memory_swap_max/current=0`，host
`/proc/swaps` 和 `free -h` 均为 `Swap: 0B`。未提高 `50k/100k/50k/400k` 搜索上限，未启动
24h/full-voyage。

**结论。** M21 只证明冻结真实 6h 输入上，研究 Pareto sidecar 在当前有限状态域内与未剪枝
reference 的完整 frontier 可复核等价，状态为
`READY_FOR_P0.2-REAL-FRONTIER-IMPLEMENTATION-REVIEW`。它不证明 continuous FIFO、interval
proof、性能回归门、生产资格或 candidate/Winter 授权；`TemporalDominancePolicy.disabled()`、
P2.1/P3/ARA*、M18 queue resource fail、M19 24h reference、B/C/C/D 合同及 formal latest/
replanning baseline/frozen artifact 全部保持不变。后续只能另立带真实 scope、资源和 pruning
证据的 P0.2 implementation review，不自动重跑 Winter 或启用 candidate。

**验证。** M21 runner 的聚焦测试为 `5 passed`；其余 C 测试、Ruff、lock/offline sync、CLI
smoke、active/archive import boundary 和 `git diff --check` 将在本地收口提交前复核。原样
`UV_OFFLINE=1 make check` 若仍因隔离 worktree 缺少 `.mamba-env/bin/uv` 阻塞，使用正式 C 的
`.mamba-env/bin/uv` 执行等价离线检查并如实记录。完成验证后删除 M21 辅助 worktree，保留分支
和 `.runtime/experiments/` 构件，不 push、不合并正式 `research-validation-system`。

### 【2026-08-29 | COMPLETED】P0.2-M22：real 24h state-bound Pareto frontier equivalence

本轮在隔离分支 `research/p02-m22-real-frontier-state-bound-20260829` 完成，实验启动时
worktree clean；实现与修正提交为 `32a2137`、`fad1264`、`d25e643`、`ece5b0e`、
`954259d`。新增 `scripts/benchmark_non_fifo_temporal_pareto_frontier_state_bound_real.py`
仍为 C 内部研究 sidecar，使用同一完整 `TemporalScope` 和
`graph-topological-arrival-envelope-v1` `TemporalStateBoundCertificate`，对照
`pareto_pruning=False` certified reference 与 `pareto_pruning=True` candidate 的完整 goal
frontier；独立 zero-heuristic Dijkstra 只作 selected-route 业务语义证据。正式
`TemporalDominancePolicy.disabled()`、candidate/Winter、B/C 与 C/D 合同、ingress/service、
formal latest/replanning baseline/frozen artifact 均未改变。

**身份、输入和执行。** holdout 与 development 均只运行冻结 `rolling_0_24h`，三目标
`fastest/low_risk/recommended`，各执行 `one_shot` 与 `slice_restore`，每输入 `6/6` 独立
worker，重复数为 1，CPU 固定为 0，worker timeout 1800 秒，cgroup 为
`MemoryMax=4G`、`MemorySwapMax=0`。两份权威目录为：

- holdout：`.runtime/experiments/c-p02-m22-real-frontier-state-bound-holdout-20260829-r2/`，
  experiment id `c.p0.2-temporal-pareto-state-bound-frontier-real.v1-630374134b7c225d`，
  start `[5,7]`、goal `[14,5]`、departure `2026-02-22T00:00:00Z`、145 帧；
- development：`.runtime/experiments/c-p02-m22-real-frontier-state-bound-development-20260829-r1/`，
  experiment id `c.p0.2-temporal-pareto-state-bound-frontier-real.v1-d034cfd7b289cdb3`，
  start `[5,7]`、goal `[14,6]`、departure `2026-03-22T00:00:00Z`、145 帧。

两输入均绑定 implementation commit `954259d5a01ecb37c9eb13258189a1afdd4a6c7e`、实现
tree digest `dfdd93a45c71ed75df0e0c320745576eb4eff47e2943805b01483f293bed0143`、`uv.lock`
digest `8893cb8387825ca4890ed808f4a98b02ba938337752971dacc3e77f859164f22`、configs digest
`537e1a1d1ef3f8015402e9b57556518b92a2524993074b4ecc1ccf58143cded4`、各自 RiskFrame
commit/content/frame digest、route-plan-set digest、scope digest 和三目标 state-bound
certificate digest。manifest 中 `dirty=false`、`production_candidate_enabled=false`、
`winter_enabled=false`；已知真实 FIFO 状态继续记录为 `REAL_INPUT_FIFO_VIOLATED`。

**正确性结果。** holdout 与 development summary 均为
`READY_FOR_P0.2-REAL-24H-FRONTIER-IMPLEMENTATION-REVIEW`，各 `6/6` case 为 `PASS`，
`complete=true`、`all_case_gates=true`、`certificate_usable=true`、`fail_closed=true`、
`deterministic=true`、`frontier_equivalence=true`、`semantic_match=true`、
`all_resource_clean=true`、`resource_evidence_complete=true`。12 个 frontier pair 全部为
严格 `MATCH`：baseline/candidate 均 `GOAL_FOUND`，完整 frontier 数量、节点、exact UTC
arrival、路径、7 维成本、速度、风险、confidence、source IDs、CostBreakdown、scope 和
comparison identity 均一致；selected-route 与独立 Dijkstra 对照全部通过。计划允许的
仅去除派生 `semantic_digest` 的 `SEMANTIC_MATCH` 诊断层未被实际使用，未采用数值容差或
忽略业务字段。

**资源与剪枝。** holdout 每个 candidate case 的 `state_bound_pruned=71,446`、baseline
`71,521`，candidate Pareto pruning 为 5；6 个 case 合计 candidate state-bound pruning
`428,676`。baseline/candidate expanded 分别为 `10,487/10,477`，queue peak 约
`3,358–3,400/3,358–3,398`，edge evaluations `83,336/83,256`，最大 RSS 约
`120,192/153,596 KiB`。development 每个 candidate case 的 state-bound pruning 为
`38,366–38,382`，baseline 为 `38,552`，6 个 case 合计 candidate 为 `230,260`；
expanded 分别为 `5,681/5,657–5,659`，queue peak `1,911–1,997`，edge evaluations
`45,368/45,176–45,192`，最大 RSS 约 `120,392/130,188 KiB`。两输入 cgroup memory
events 的 high/low/max/oom/kill 均为 0，`memory_swap_max/current=0`，每条记录 CPU
affinity 为 `[0]`，process swap 为 0，主机 `free -h` 与 `/proc/swaps` 均为 `Swap: 0B`；
未提高 `50k/100k/50k/400k` 搜索上限，未发生 queue/label/expansion/edge limit、
timeout、OOM 或 swap。

**运行记录与结论。** 早期 holdout `r1` 的 6 个超时是父进程 PIPE 读取造成的 worker 大型
frontier JSON 管道死锁，保留为诊断构件，不纳入结论；`954259d` 改为临时文件收集 stdout/
stderr 后，holdout `r2` 和 development `r1` 均完整收口。M22 因此仅证明冻结真实 24h
有限状态域中，带同一 proof-carrying state bound 的 Pareto sidecar 与未剪枝 reference
具有可审计的完整 frontier 等价，并观察到真实安全 pruning；状态为
`READY_FOR_P0.2-REAL-24H-FRONTIER-IMPLEMENTATION-REVIEW`。这不是 continuous FIFO、
interval proof、性能回归门、生产资格或 candidate/Winter 授权；真实 FIFO violation、
M18 queue resource fail、M19 reference、M20/M21、P2.1/P3/ARA* 历史结论保持不变。后续
只能另立实现审查、corridor/envelope 或非 FIFO 计划，不自动启用 candidate 或重开 Winter。
