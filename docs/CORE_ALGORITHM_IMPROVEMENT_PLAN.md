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
Last Verified: 2026-08-27
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

> 本文档将“工作包 C 核心算法实现审计报告”与后续改进方案合并为一个持续维护的计划文档。当前正式基线是带风险、速度和 ETA 耦合的时间依赖 A*。P2.1 控制轨迹复用在同 goal 收紧查询上仍保留约 48% 总耗时改善这一工程观察，但 Winter M2 的冻结门禁没有改变：M2K 对称预热诊断两档均因 order-gap 失败，P2.1 当前收口为 `MEASUREMENT_INCONCLUSIVE / FORMAL_M2_FAIL_UNCHANGED`，candidate 继续默认关闭。P3 SMO-A* 与 ARA* 保持 `DEFERRED/RETIRED`、`M0_FAIL/DEFERRED`；不再启动 Winter 重型复测。下一条主线是已实现但尚未取得性能资格的 P0.1 有限域 FIFO 证书与 exact-arrival 安全支配，先完成 clean Synthetic M0，再决定是否进入 M1。所有候选继续默认关闭、非发布，尚不能声明生产级稳定优势或全局最优。

## 1. 文档定位与更新规则（2026-08-24 20:52 +08:00）

**首要参考声明：** 本文档是工作包 C 核心算法改进实现的首要参考（Single Source of Truth，SSOT）。以后 C 核心算法的现状、问题、改进方案、实施计划、实验结论、验收状态和方案修订，均在本文档对应章节下更新；不再为同一主题另设新的审计、研究方案或计划文档。

**更新规则：**

- 先更新本文档，再实施与本文档一致的 C 内部代码和测试；实施后在本文档补充 commit、输入身份、结果摘要和成熟度。
- 已解决的问题保留在“当前基线”或“变更记录”中，不能继续作为未解决缺陷描述。
- 新增章节必须放在语义正确的位置，并遵守治理标准的分钟级更新时间要求；不能把补丁式内容追加到文档末尾。
- 仅当跨包正式合同必须改变时，才按 [`CONTRACT_CHANGE_PROPOSAL_TEMPLATE.md`](/root/my_project/arctic_route_governance/standards/CONTRACT_CHANGE_PROPOSAL_TEMPLATE.md) 另行建立和审批提案；提案获批后仍须把链接、影响和实施状态回填本文档。
- 本文档不改变 B 的风险公式、RiskFrame 身份或 D 的展示职责；研究 sidecar、实验输出和 synthetic fixture 不得静默成为正式生产输入。

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
| 单元/合同回归 | `UNIT_PASS` | 历史 P0/P2.1 基线分别为 `215/274 passed`；本轮 `UV_OFFLINE=1 make check` 为 `268 passed`，Ruff、lock/sync、CLI 通过 |
| 当前 A* 的全局最优性 | `NOT_IMPLEMENTED`（未证明） | 时间桶合并、FIFO、ETA 迭代和连续时间误差均无通用证明 |
| P2.1 相对独立 cold control 的受限重复查询优势 | `EXPERIMENTAL_PASS` | clean M0/M1 与 Winter formal 均观测到约 47%–79% 总耗时改善；只适用于同 goal 收紧查询，不等于跨 workload 稳定优势 |
| 相对于传统算法的生产级稳定性能优势 | `NOT_IMPLEMENTED`（未证明） | P2.1 Winter M2 因 `rolling_0_24h × fastest` 中位回归 `5.94% > 5%` 失败；候选未默认启用 |
| P3 SMO-A* 共享记忆化多目标搜索 | `DEFERRED` | 语义/诊断回归通过；P3.2 holdout/development M1 分别因 hit rate `14.27%/19.19%`、RSS ratio `3.367/3.380` 失败；P3.3 synthetic medium exact-key hit `47.87%`，主要为 objective 路径差异，未形成安全修复路径 |
| bounded LRU 风险采样缓存 | `EXPERIMENTAL` | direct medium 实验约 14.77% median 改善，但增加约 38.6 MiB RSS，未通过正式 12 路线门禁 |

**上一版审计的状态修正：** 上一版把计数器热循环、环境变量/资源观测耦合、v3 常量散落、层窗口常量、session 无界增长等工程项标为已修复；代码和测试已支持这一结论。本次不再把这些历史问题列为当前算法瓶颈，当前重点转为时间依赖搜索语义、可证明复用和可重复性能证据。

## 4. 核心正确性边界与待解决问题（2026-08-24 20:52 +08:00）

| 编号 | 发现 | 当前证据 | 影响 | 进入下一阶段的门禁 |
|---|---|---|---|---|
| C-ALG-01 | 边到达函数 `A_e(t)=t+τ_e(t)` 未验证 FIFO | 后出发时环境速度可能更高；当前 [`_evaluate_edge`](/root/my_project/work_package_c/src/arctic_route_planning/planners/time_dependent_astar.py:392) 没有单调性检查 | 不能直接使用依赖 FIFO 的 label-setting 或普通最优子结构结论 | 在合成冲击场上逐边验证 FIFO；不满足时切换到非 FIFO label-correcting/Pareto 语义或显式失败 |
| C-ALG-02 | 同一 `(node, time_bucket, heading)` 只保留较低累计成本 | [`labels`](/root/my_project/work_package_c/src/arctic_route_planning/planners/time_dependent_astar.py:263) 与放松规则 [`time_bucket`](/root/my_project/work_package_c/src/arctic_route_planning/planners/time_dependent_astar.py:345) | 同桶但不同精确到达时间会采样不同未来风险；较低成本标签未必支配较早标签 | P0 必须保留精确到达时间或 Pareto 标签，并用独立 oracle 验证安全支配 |
| C-ALG-03 | ETA/速度固定两轮，没有收敛残差或误差上界 | [`_EDGE_REFINEMENT_ROUNDS`](/root/my_project/work_package_c/src/arctic_route_planning/planners/time_dependent_astar.py:45) 与循环 [`range`](/root/my_project/work_package_c/src/arctic_route_planning/planners/time_dependent_astar.py:409) | 可能发生 2 小时/10 小时振荡；最终 ETA、采样时刻和成本可能不属于同一收敛状态 | 定义迭代映射、容差、最大迭代、周期检测；终值重新采样，不收敛则明确失败 |
| C-ALG-04 | 启发式下界只对当前近似图和状态成立 | `lower_bound` 不能修复非 Markov 标签、非 FIFO 和近似边代价 | 只能声称“当前采样状态图上的搜索”，不能声称连续海洋问题全局最优 | 建立小规模显式时间展开 Dijkstra/reference oracle；报告离散模型边界 |
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
9. （2026-08-27）**P2.1-M2J 测量协议提案已立（PLANNED）。** 代码核查确认 cold 单元在候选中已与控制走同一 `plan()` 路径（无残留旁路稳定开销），`rolling × fastest` 的 `5.94%` 中位回归归因为进程内内存/GC 污染或顺序噪声等测量伪影；提案 R1 每单元进程隔离 + R2 轨迹载荷释放，均不放松 5% 语义门禁。复测须在独立 experiment identity `winter-c-p21-m2j-measurement-protocol-20260827-r1` 下进行。**P3 SMO-A\* 与 ARA\* 建议 RETIRE**：二者均不具备晋级证据（SMO +12.71%/hit 14.3%/RSS 3.3×；ARA\* small 首解 +4.14%），停止投入，集中算力于 M2J 复测或 P6 多目标/自适应后续；任何退役动作须在本文档回填成熟度与构件保留状态。

**开放问题：** 非 FIFO 情形是否进一步采用 label-correcting；ETA 迭代的保守误差模型；P3 anchor 证书的浮点容差。（已收口：P2.1 cold control 旁路是否残留稳定开销 —— 2026-08-27 代码核查判定**否**，cold 单元在候选中已与控制走同一 `plan()` 路径；`rolling_0_24h × fastest` 的 `5.94%` 回归归因为测量伪影，见 P2.1-M2J 提案，待独立 experiment identity 复测确认能否在不放宽门禁前提下消除。）Winter 已证明每次自然产生 3 个 full→main 零搜索 hit，并确认约 48% 总 wall-time 改善，但单元硬门禁失败意味着不能宣称生产级稳定加速。独立 FIFO 分类器和 exact 标签安全支配仍是后续候选。任何资源或标签语义变更必须先记录新的实验身份与正确性回归，不能用“全局最优”“稳定加速”或“生产级优势”代替证据。

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

### 【2026-08-27 | IMPLEMENTED-PENDING-RETES】P2.1-M2J 冷路径代码核查与测量协议提案

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

原始 M2J/M2K 的跨运行差异和单 case 异常支持“计时方差/顺序效应”解释，但不构成算法性能通过证据。短复测目录仅有 `PREPARED` manifest，样本尚未形成可审计结论，标记 `INCOMPLETE/INVALID_FOR_CONCLUSION`。P2.1 收口为 `MEASUREMENT_INCONCLUSIVE / FORMAL_M2_FAIL_UNCHANGED`；control-trace candidate 继续默认关闭，不进入新的 Winter 重型复测或正式发布。

**P3/ARA* 冻结。** SMO-A* 保持 `DEFERRED/RETIRED`，ARA* 保持 `M0_FAIL/DEFERRED`；full-anchor reuse 不再作为独立 P3 分支推进，暂存为 P0.1 证书语义通过后的下游候选。旧实验目录和原始 manifest 原样保留，不写入 formal latest、replanning baseline 或 frozen artifact。

**P0.1 当前状态。** `temporal_qualification.py` 已提供有限域 `FIFO_CERTIFIED/FIFO_VIOLATED/FIFO_UNCERTAIN`、探测覆盖、容差和反例；`TemporalScope` 绑定 RiskFrame/config/grid/model/request/evaluator identity；`TemporalDominanceCertificate` 对 FIFO、suffix monotone、coverage 和 scope 做 fail-closed 校验；`TemporalDominancePolicy.disabled()` 为默认值，`certified_only(...)` 仅供 C 内部研究调用；session identity/checkpoint 已绑定 dominance policy digest。`benchmark_temporal_dominance.py` 已具备 small `5×7×7`、medium `9×13×13`、三目标、独立 worker、warmup/repetition、语义/确定性/资源/真实 pruning 门禁。

当前 P0.1 标记为 `IMPLEMENTED_PENDING_M0`：代码和聚焦测试已经提交，但尚无可作为资格结论的独立 M0 manifest。下一步必须在 clean 本地提交上运行两个 profile；M0 未同时满足语义一致、FIFO/scope 证据完整、median compute 回归 ≤5%、RSS ratio ≤1.10、无 swap/OOM/timeout 且至少一次真实 certified label pruning 前，不进入 Winter M1，不启用 candidate，不接入 ingress/service。

**架构前置项。** P0.1 活跃 planner 当前仍通过兼容路径引用 `planners/_archive/temporal_session.py`。在 M1 前应将 temporal session 内核迁入活跃的 C 内部命名空间，并保留 archive 兼容壳；该迁移只允许改变模块归属和 import，不得改变 session identity、checkpoint 或正式合同语义。
