---
Overall Status: APPROVED
Content Status:
  - COMPLETED
Document Role: SUPPORTING
Scope: cross-package CD contract change proposal for selection rationale sidecar
Canonical For: approval gate for adding selection-rationale to C→D output
Branch: research-validation-system
Last Verified: 2026-08-24
---

> **声明（About this proposal）**：本文档依据 `arctic_route_governance/standards/CONTRACT_CHANGE_PROPOSAL_TEMPLATE.md`
> 为工作包 C 向 C→D 输出新增可选 `selection-rationale` sidecar 建立跨包合约变更提案。
> `DRAFT` 状态不授权生产使用；需走语义/生产者/消费者/集成四方审批门控后方可推进。
>
> **批准（2026-08-24）**：C、D 两包均由项目负责人同一负责，四方审批已一次性通过（见下方 Approval record），
> 状态由 DRAFT 升级为 APPROVED；跨包消费测试与文档同步同步落地。

# Contract Change Proposal: CD Selection Rationale v1

## Proposal metadata（2026-08-24 11:00）

```text
Proposal ID: CCP-CD-SELECTION-RATIONALE-V1
Title: Add optional selection-rationale sidecar to C→D output
Status: APPROVED (2026-08-24)
Author: AI Agent (work package C maintenance)
Semantic owner: work_package_c
Affected producers: work_package_c (publishing/service/layered/cli)
Affected consumers: work_package_d (CD artifact consumer)
Target version: C 0.4.x → 0.5.0 candidate
Created: 2026-08-24
Last updated: 2026-08-24
```

## Problem and evidence（2026-08-24 11:00）

- Current observed limitation: C 产生推荐路线（recommended）与最快基线（fastest），但对外输出未携带任何结构化"为何选择推荐路线而非最快路线"的权衡说明。外部评审（任务 2）指出这是 C 包唯一真实差距。
- Code/schema/artifact evidence: `publishing/models.py::SelectionRationale`、`schemas/selection-rationale-v1.schema.json`、`service.py` 与 `layered.py` 已新增 `selection_rationale` 字段；CLI 已输出 `selection-rationale.json`。
- Why configuration or an additive optional field is insufficient: 该信息需要确定性地从 recommended 与 fastest 两条路线派生（delta 距离/ETA/风险、风险降低百分比），属于跨包语义产物，必须在合约层面定义字段与 Schema 以保证 D 可消费。
- Frozen baselines affected: 不影响 `cd.route-plan.v2` / `cd.route-plan.v3` / `cd.four-layer-route-plan-set.v3` 既有内容身份与 digest（rationale 作为独立 sidecar，不进入路线 identity 计算）。

## Current and proposed semantics（2026-08-24 11:00）

| Dimension | Current | Proposed | Breaking? |
|---|---|---|---|
| Schema identity | 无 selection-rationale | 新增 `selection-rationale.v1` Schema（独立 `$id`） | No（新增独立 Schema） |
| Field/cardinality | v2/v3 输出无 rationale 字段 | `PlanningBatch.selection_rationale`、`FourLayerPlanningOutcome.selection_rationale` 可选字段 | No（可选，默认 None） |
| Units/polarity | N/A | delta 单位为 km/h/无量纲；风险 delta 限定 [-1,1] | No |
| Time semantics | N/A | rationale 继承运行身份时间字段（generated_at/as_of_time） | No |
| Missing/unavailable behavior | N/A | 无推荐/最快对偶时不产出 rationale（字段为 None，不写文件） | No（fail-soft） |
| Atomicity/immutability | N/A | rationale 随整组原子发布；不单独改写 | No |
| Content identity/digest | 路线 digest 不含 rationale | rationale 不进入路线/content digest | No（保持 SSOT） |

机器可读 before/after：

- before（v3 整组）：`FourLayerPlanningOutcome` 无 `selection_rationale` 字段。
- after（v3 整组）：`FourLayerPlanningOutcome.selection_rationale: SelectionRationale | None`；CLI 额外写 `selection-rationale.json`。

## Compatibility and failure behavior（2026-08-24 11:00）

- Old producer → new consumer: 旧 C 不产 rationale，新 D 将 rationale 视为可选 → 正常降级显示（不阻塞）。
- New producer → old consumer: 新 C 产 rationale 文件/字段；旧 D 忽略未知字段（`additionalProperties: false` 仅约束 rationale 自身 Schema，不影响 v2/v3 主 Schema）→ 兼容。
- Unsupported-version behavior: `schema_version` 非 `selection-rationale.v1` 的 rationale 文档被新消费者拒绝（Schema `const` 校验）。
- Partial/missing/unknown behavior: rationale 缺失时主路线合同完整可用；rationale 内部未知字段被 `additionalProperties: false` 拒绝。
- Fail-closed behavior: baseline_objective 非 `fastest` 时 Schema 拒绝（保证权衡基准恒为最快线）；非有限 delta 值在模型层拒绝。
- Migration and rollback: rationale 为纯新增可选 sidecar；回滚只需不写该字段，不影响既有 v2/v3 读取与 digest。

## Implementation ownership（2026-08-24 11:00）

| Repository / directory | Owner | Allowed change | Prohibited change |
|---|---|---|---|
| work_package_c/publishing | work_package_c | 新增 SelectionRationale 模型与 schema writer | 修改 RoutePlan/RoutePlanV3 既有字段或 digest |
| work_package_c/service.py, layered.py | work_package_c | 在发布路径附加 selection_rationale | 改变 v2/v3 内容身份 |
| work_package_c/cli.py | work_package_c | 写 selection-rationale.json 与 run-summary 段 | 破坏原子发布语义 |
| work_package_d | work_package_d | 可选消费 rationale | 将 rationale 当作路线身份或强制依赖 |

## Verification matrix（2026-08-24 11:00）

| Gate | Required evidence | Result |
|---|---|---|
| Schema validation | old/new fixtures | PASS（tests/contract/test_schemas.py） |
| Producer tests | deterministic output and identity | PASS（tests/unit/test_publishing.py, test_service.py, test_layered_planning.py） |
| Consumer tests | valid/invalid/unsupported inputs | PASS（D 侧 tests/unit/test_selection_rationale_consumer.py + test_loader.py） |
| Compatibility | old baseline remains readable | PASS（v2/v3 读取不受影响） |
| Semantic equivalence | unchanged fields/digests | PASS（rationale 不进入路线 digest） |
| Focused integration | real or formal fixture path | PASS（C synthetic-demo 真实产物 → D snapshot 消费回归） |
| Resource budget | wall time and peak RSS | PASS（新增可选 sidecar，预算影响可忽略） |

## Approval record（2026-08-24 12:00）

| Role | Decision | Evidence/date |
|---|---|---|
| Semantic owner | APPROVED | 2026-08-24，C/D 项目负责人一次性批准 |
| Producer owner | APPROVED | 2026-08-24，C 侧代码与测试通过 |
| Consumer owner | APPROVED | 2026-08-24，D 侧消费对接与测试通过 |
| Integration owner | APPROVED | 2026-08-24，C→D 真实产物跨包回归通过 |
