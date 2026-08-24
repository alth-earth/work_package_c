---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
  - IN_PROGRESS
  - PLANNED
Document Role: CANONICAL
Scope: C 核心算法现状、证据、正确性边界、改进设计与实施计划
Canonical For: 工作包 C 核心算法改进实现的首要参考
Branch: research-validation-system
Last Verified: 2026-08-24
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

> 本文档将“工作包 C 核心算法实现审计报告”与后续改进方案合并为一个持续维护的计划文档。当前基线是带风险、速度和 ETA 耦合的时间依赖 A*；截至本次审计，C 已具备正式路线生产能力，但尚未证明相对于常规 A*、Dijkstra、D* Lite/LPA* 或其他独立基线具有稳定的速度、资源或全局最优优势。

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
- 四层编排当前为 `full_voyage`、`main_corridor`、`rolling`、`executable`，每层三个目标，核心执行仍是 12 次相互独立的 A* 查询；共享多目标搜索和增量重规划尚未实现。

**当前成熟度与证据：**

| 能力 | 当前等级 | 证据与限制 |
|---|---|---|
| 正式 B→C 输入与 fail-closed | `AUTHORITATIVE_PASS` | committed-window lease、identity/digest 校验、覆盖和硬约束拒绝 |
| Winter 四层三目标生产 | `AUTHORITATIVE_PASS` | 145 个正式小时帧、4 层 × 3 目标、12/12 route integrity、hard violation 0 |
| C→D 路线合同 | `FROZEN_BASELINE` | route v2 / four-layer v3 schema、digest 和来源字段保持冻结 |
| 单元/合同回归 | `UNIT_PASS` | P0 clean/synced 基线为 `215 passed`；当前 P1 研究工作树执行 `UV_OFFLINE=1 make check` 为 `238 passed`，Ruff 通过 |
| 当前 A* 的全局最优性 | `NOT_IMPLEMENTED`（未证明） | 时间桶合并、FIFO、ETA 迭代和连续时间误差均无通用证明 |
| 相对于传统算法的稳定性能优势 | `NOT_IMPLEMENTED`（未证明） | 目前没有同输入、同边评估器、重复运行的独立 baseline 对比 |
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

**命名与定位：** 采用“分层目标证书复用的时间依赖 A*”（Layered Target-Certified Reuse Time-Dependent A*，简称 **LTCR-TDA***）。它不是给普通 A* 改名，而是围绕 C 当前最大可观测瓶颈设计的、可回退的搜索会话复用算法：在同一目标、同一输入身份下保留 OPEN、标签和前驱，对 full 与 anchor 两个查询分别取得最优性下界证书，证书成立才复用。

**核心数据结构：**

```text
Session_m = (
    OPEN,
    temporal_labels,
    CLOSED,
    predecessor,
    incumbent_for_full,
    incumbent_for_anchor,
    lower_bound_for_full,
    lower_bound_for_anchor,
    certificate_status,
    input/config/model/generation digests,
)
```

每个 `objective`（`fastest`、`low_risk`、`recommended`）独立维护一个 session；不跨目标共享标签，不跨 RiskFrame、generation、revision 或 planner digest 复用。

**计划流程：**

```text
1. 用当前 A* control 产生 full route，并保留该 objective 的搜索会话。
2. 从 full route 选择 main/rolling/executable 的 anchor 候选。
3. 在同一 session 中继续搜索，分别计算 anchor 和 full 的 incumbent/lower bound。
4. 仅当 anchor 证书成立，才重建并复用 full session 中对应 prefix。
5. 证书缺失、输入身份变化、时间语义不满足或资源预算不足时，回退独立 A*。
6. 将 control/candidate 的结果作为新 experiment identity 输出，不覆盖冻结基线。
```

**证书条件：** 对固定 objective、RiskFrame、船模和规划参数，令 `U_A` 为当前到达 anchor 的最好可行成本，`LB_A` 为所有仍可能到达 anchor 的 OPEN 标签下界，则只在

```text
U_A <= LB_A + epsilon
```

且同时满足同一 `start_time`、网格、时间桶、边采样、最大时域、generation、revision、config/model digest、hard mask/coverage/failure 语义和 anchor ETA 上限时，记录 `CERTIFIED_REUSABLE`。没有独立的 `U_A/LB_A` 证书，full route 到达 anchor 并不等于 prefix 最优。

**必要的正确性边界：** P0 已解决候选实现的精确到达时间标签、ETA 收敛检查、终值重采样和独立 oracle 对照，并通过其离散语义 `UNIT_PASS`。LTCR-TDA* 后续仍只能作为受限离散模型上的语义保持工程复用实验；在 P1 会话围栏、P2/P3 证书和正式 paired benchmark 完成前，不得写成对原连续问题的全局最优算法或稳定性能优势。

**预期优势与诚实边界：** 该算法有机会直接消除 full/main 的重复搜索，优势指标是相同语义下的 wall time、expanded/generated、边评估次数和峰值 RSS；预计收益是待验证假设，不是现有结论。证书失败时退回 baseline 是算法设计的一部分，不是异常掩盖。

**C-only 影响：** 第一阶段只改 `work_package_c/src`、配置、测试和本文档，不改变 B/C 或 C/D 正式合同；内部的 session/certificate metrics 作为诊断 sidecar，不进入现有 route semantic digest。

## 7. 候选方向、取舍与暂缓事项（2026-08-24 20:52 +08:00）

所有候选都必须共享 C 当前 `evaluate_edge(state, neighbor)` 的风险采样、船模、hard mask、coverage、provenance 和失败语义；只能改变队列、标签、复用或剪枝。外部仓库只作为论文/接口思想参考，不直接复制许可证不清晰或非标准许可代码。

| 方向 | 作用 | 与 C 的适配 | 决策 |
|---|---|---|---|
| LTCR-TDA* | 同一 objective 的 full/anchor 会话、下界证书和安全回退 | 直接针对已观测的 full/main 重复，C-only | **第一优先级** |
| SIPP-like safe-interval search | 将 hard-mask 时间区间显式放入状态，可表达等待 | 需先定义保守 safe interval，软风险不能误判为绝对安全 | P0 后小规模实验 |
| ARA*/Anytime weighted A* | 先求可行解，再用 epsilon 收敛换取时间预算内的质量 | 可在 C 内独立实现，但必须报告相对 oracle 的代价差 | 备选基线/时间预算实验 |
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
| P2 exact same-goal reuse | 先实现同一目标查询的证书化继续搜索，保留 baseline 回退 | M0/M1 与 control 语义一致；证书可重算；失败自动回退 | `PLANNED` |
| P3 full/anchor reuse | 为 anchor 计算 `U_A/LB_A`，证书成立才复用 prefix；将四层集成到影子分支 | M1 至少 5 次 paired run；无单层硬回归；无证书误用 | `PLANNED` |
| P4 formal shadow | Winter 正式 ingress、4×3、12 路线，control/candidate 双轨 | M2 通过确定性、合同、资源和性能阈值；不覆盖冻结 artifact | `PLANNED` |
| P5 默认启用评审 | 仅在重复正式证据支持时改变默认开关，并更新本文档/CHANGELOG | 通过审批、回滚演练和新 experiment identity；否则保持 baseline | `PLANNED` |
| P6 多目标/自适应后续 | NAMOA*/MOPBD*/自适应网格等独立提案 | 必须先证明 P0/P3 的收益不足且合同必要性成立 | `DEFERRED` |

**实施顺序硬规则：** 先 P0，再 P1/P2；P3 证书失败必须回退；P4 以前不得将候选算法命名为正式生产 planner；P6 不得倒灌到当前合同。

**P0 实施规格冻结（2026-08-24 22:12 +08:00）：** 本轮保留现有 `TimeDependentAStar` 为正式 control；新增候选不从公共包导出、不接入 ingress/service。候选标签身份固定为 `(node, heading, exact UTC arrival_time)`，不同到达时刻禁止相互支配；只有精确状态相同时才保留较低成本。候选资源上限为 50,000 expansions、100,000 labels、50,000 queue 和 400,000 edge evaluations，超限显式失败。

**P1 实施规格冻结（2026-08-24 23:04 +08:00）：** 本阶段只实现 C 内部的可恢复搜索会话骨架，不实现 P2 同目标复用、P3 full/anchor 证书复用、独立 FIFO 分类器或上一版 2.2.2 自适应/非均匀网格方向。新增内部 `TemporalSessionIdentity`，其规范身份必须绑定 RiskWindow content digest、commit/revision、generation/input revision、RiskIdentity、planner/model/config digest、objective、起终点、出发时间、最大时域、风险阈值、网格/边采样、ETA policy、搜索限制和启发式设置；会话 ID 由该身份规范序列化后的 SHA256 确定生成。

P1 会话状态固定为 `READY → PAUSED → GOAL_CERTIFIED | EXHAUSTED | CANCELLED | FAILED`；`CANCELLED` 是终态，不得作为普通暂停继续。内部接口固定为 `create_session`、`advance_session`、`checkpoint_session` 和 `restore_session`；每个 objective 通过 `TemporalSessionBundle` 创建完全隔离的 session，不共享标签、OPEN、前驱或搜索可变状态。planner 继承的边几何缓存只做观察等价的 memoization，不包含 objective/session 搜索状态。checkpoint 为进程内不可变快照；恢复前必须执行全身份 fence，任一输入、配置、模型、目标或策略身份不匹配即拒绝恢复并要求新建会话。expansion、label、queue、edge-evaluation 等硬资源限制在暂停/恢复间累计，不得通过恢复重置；取消、资源超限、coverage/ETA 等失败继续 fail-closed。现有 `plan()` 仅作为“创建临时会话并推进至终态”的兼容包装，正式 control、ingress/service、B/C 与 C/D 合同均不接入该候选。本阶段不宣称任何性能优势，性能结论留待后续 paired benchmark。

候选 ETA 使用 `damped_fixed_point_v1`：静水 ETA 初值、最多 12 次迭代、阻尼 0.5，容差为 `max(1 秒, 1e-6 × max(1 小时, guess, raw ETA))`；周期、超迭代和终值不一致均拒绝该边。初步收敛后必须按 terminal ETA 重采样并再次验证，最终风险、速度、成本和 arrival time 必须来自终值采样。独立 oracle 使用单独的零启发式精确时间搜索，不调用 control/candidate 的 `plan()` 或 `_evaluate_edge()`；它只用于 M0 synthetic，不进入正式发布链。

**P0 实施与证据（2026-08-24 22:31 +08:00）：** 已新增内部 `eta_refinement.py`、未从公共 planners 包导出的 `temporal_label_astar.py`，以及 test-only `tests/reference_temporal_oracle.py`。候选实现 exact UTC arrival label、无跨到达时刻支配、goal incumbent/OPEN 下界终止、四类硬资源上限和 terminal ETA 重采样；独立 oracle 不导入生产规划器。静态小图三方差分测试证明 control、candidate、oracle 的路径、ETA 和代价一致；非 FIFO、同桶不同精确 ETA、exact-state replacement、周期/超迭代、取消和资源超限反例均显式通过或失败关闭。

可重复入口为 `scripts/validate_temporal_semantics.py`。干净运行基线为 Git `37627fdc2b37bbb3c8b06392e09b1b91a2d6ea2f`、clean/synced worktree；实验 `c-p0-temporal-semantics-v1-37627fdc` 在 5×7×7 synthetic 静态 fixture 上串行执行 10 次，10/10 semantic digest 一致，原始构件位于 `/root/my_project/.runtime/experiments/c-p0-temporal-semantics-v1-37627fd/`；manifest 的 `experiment_id` 为 `c-p0-temporal-semantics-v1-37627fdc`，并记录 `git_worktree_dirty=false`。control/candidate median wall time 分别为 11.108/16.661 ms，候选约慢 50.0%，因此 P0 保持正确性 `UNIT_PASS`，**没有通过 M0 性能晋级门禁，也不构成算法优势声明**。性能优势仍须由后续证书化复用和正式 paired benchmark 建立。

验证基线为 Git `37627fdc2b37bbb3c8b06392e09b1b91a2d6ea2f`，clean/synced worktree、`uv.lock` SHA256 `8893cb8387825ca4890ed808f4a98b02ba938337752971dacc3e77f859164f22`、Python 3.13.14；P0 聚焦测试 40 项通过，`UV_OFFLINE=1 make check` 为 215 项通过，Ruff、lock/sync 与 CLI smoke 均通过。未修改 B/C、C/D schema/digest、正式默认 planner 或 frozen artifact。

**P1 实施与证据（2026-08-24 23:33 +08:00）：** 已新增内部 `temporal_session.py`，并把 `TemporalLabelAStar.plan()` 收敛为 create/advance 到终态的兼容包装。session 独占 exact-arrival labels、OPEN、前驱、incumbent、诊断与启发式缓存；checkpoint 保留 stale queue entry、微秒级 ETA 和累计计数，清除 cancel callback，并以 state digest 拒绝篡改。恢复只接受 `READY/PAUSED`，重新计算当前 sampler、planner、request、model、policy 和 evaluator 身份；内部 sampler digest 与可选正式 committed-window digest 明确区分，正式 pair 必须满足 `commit_id = risk-window-sha256-<content_digest>`。显式伪造 identity、风险窗口内容变化、evaluator 变化、终态恢复、取消、四类资源上限重置和跨 objective 状态共享均有负例。

当前 P0/P1 聚焦回归为 63 项通过，完整 `UV_OFFLINE=1 make check` 为 238 项通过。显式 P1 runner 在 5×7×7 synthetic fixture 上串行执行 10 次；每次 session 均经历 8 次 pause/checkpoint/restore，10/10 control、one-shot candidate 与 session candidate 路线 semantic digest 一致，one-shot/session 离散 metrics 与 diagnostics 一致。最终代码哈希对应的原始构件位于 `/root/my_project/.runtime/experiments/c-p1-temporal-session-v1-37627fd-dirty-r3/`，manifest `experiment_id` 为 `c-p1-temporal-session-v1-37627fdc-dirty`。该运行来自 Git `37627fdc2b37bbb3c8b06392e09b1b91a2d6ea2f` 上的未提交研究工作树，manifest 如实记录 `git_worktree_dirty=true`，因此只支持 P1 `UNIT_PASS`，不构成 clean、formal、authoritative、frozen 或性能优势证据。未修改 B/C、C/D schema/digest、正式默认 planner、ingress/service 或 frozen artifact。

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

**开放问题：** 非 FIFO 情形是否进一步采用 label-correcting；ETA 迭代的保守误差模型；anchor 证书的浮点容差；M1/M2 的最终资源预算。P1 会话身份、状态机、恢复围栏和累计资源语义已冻结并达到 `UNIT_PASS`，独立 FIFO 证书分类器留待后续阶段，不属于本轮 P1。P0 exact-arrival label 上限已冻结并实现，任何放宽须先记录新实验身份。这些问题在方案冻结前不得用“全局最优”“稳定加速”或“生产级优势”表述代替。

**本次更新记录：** 将旧的“实现审计报告”重整为现状 + 证据 + 正确性边界 + LTCR-TDA* 方案 + 分阶段计划 + benchmark/回滚门禁；已完成 P0 exact-time candidate、独立 oracle、ETA 收敛器和 P1 per-objective 可恢复 session 骨架的 `UNIT_PASS`。下一步进入 P2 exact same-goal certificate/reuse，不提前接入四层正式链。后续修改直接在本文档对应章节更新，不再创建同主题文档。
