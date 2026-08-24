---
Overall Status: ACTIVE
Content Status:
  - COMPLETED
Document Role: CANONICAL
Scope: C 核心算法实现的治理合规与工程质量审计
Canonical For: 核心算法当前实现质量、治理合规差距、改进优先级
Branch: research-validation-system
Last Verified: 2026-08-24
---

# C 核心算法实现审计报告

## 1. 审计范围与方法

### 范围

审计对象为 `work_package_c/src/arctic_route_planning` 下的核心算法实现，覆盖以下模块（共 46 个源文件，重点审计 12 个）：

| 模块 | 职责 | 行数 |
|---|---|---|
| `planners/time_dependent_astar.py` | 时间相关 A\* 核心搜索 | ~684 |
| `layered.py` | 四层规划原子编排与发布 | ~575 |
| `ingress.py` | BC 正式入口、prepare/execute lease | ~452 |
| `cost/model.py` / `cost/vessel.py` | 等效小时成本模型 / 船舶性能 | ~104 / ~95 |
| `risk/sampler.py` | ETA-aware 风险采样 | ~432 |
| `replanning/policy.py` / `coordinator.py` | 重规划触发与发布协调 | ~247 / ~120 |
| `grid/regular.py` | 矩形经纬度网格 | ~234 |
| `publishing/serialization.py` / `models.py` | 序列化与发布模型 | ~362 / ~299 |
| `domain/models.py` | 不可变共享事实与配置 | ~192 |

非范围：CLI 层（`cli.py`）、适配器（`adapters/`）、性能基准（`coupling_benchmark.py`）、演示脚本。

### 治理依据

对照 `arctic_route_governance/standards/` 三份规范：

1. `ENGINEERING_GOVERNANCE_STANDARD.md` — 文档生命周期、语义化放置、SSOT、归档三步法、报告 15 区块、成熟度等级、术语标准（`replan_decided != replan_adopted`）。
2. `AGENT_DOCUMENTATION_RULES.md` — SSOT、代码变化同步文档、不随意新建文档、信息进入正确章节。
3. `CONTRACT_CHANGE_PROPOSAL_TEMPLATE.md` — 跨包合约变更提案模板与四方审批门控。

### 方法

逐文件静态阅读核心算法实现，按「治理合规 / 算法正确性 / 工程质量 / 性能」四维度评估，问题按严重度分级（P0–P3），改进建议按优先级排序。

---

## 2. 治理合规核查

### 2.1 单一事实来源（SSOT）— 基本良好，存在一处瑕疵

**合规项：**

- `domain/models.py` 集中定义全部枚举（`ObjectiveMode`/`PlanKind`/`ReplanReason`/`ModelCalibrationStatus`）与配置数据类（`CostWeights`/`PlannerConfig`/`ReplanningConfig`/`VesselModelConfig`），每处带 `schema_version` 常量与 `__post_init__` 校验。
- `publishing/models.py` 集中定义发布层模型，并维护 `ROUTE_PLAN_SCHEMA_VERSION = "cd.route-plan.v2"` 与 `SELECTION_RATIONALE_SCHEMA_VERSION = "selection-rationale.v1"` 常量。
- 内容身份采用 digest：`plan_id = "route-v3-sha256-" + semantic_digest`、`layer_set_id` 同理，保证 digest 与内容强绑定。

**瑕疵 AUDIT-SSOT-01（v3 schema_version 未提取为常量，散落 7 处）：**

`"cd.route-plan.v3"` 与 `"cd.four-layer-route-plan-set.v3"` 以裸字符串散落在：

```text
contracts/layered.py:70, 195        校验逻辑
publishing/layered_serialization.py:108, 122, 169  序列化/反序列化
layered.py:195, 483                   构造
```

而 v2 已有 `ROUTE_PLAN_SCHEMA_VERSION` 常量统一管理。v3 缺失等价常量，违反 SSOT「每个事实领域只有一份规范」——若 schema 演进，需手动改 7 处。

### 2.2 语义化放置 — 基本良好，存在一处越界

**合规项：** 测试 → `tests/`、Schema → `schemas/`、文档 → `docs/`、源码 → `src/`，目录划分清晰；`docs/` 内 `ARCHITECTURE_AND_DECISIONS` / `COST_MODEL` / `CD_CONTRACT` / `BC_CONTRACT` / `STATUS_AND_TODO` 各司其职。

**越界 AUDIT-PLACE-01（运维观测逻辑泄漏进核心算法）：**

`planners/time_dependent_astar.py` 在 `plan()` 主循环内联了进度打印（约 28 行，含长字符串拼接、RSS 取值、`print(..., file=sys.stderr)`）。按治理标准「性能 → 性能/非功能」「缓存 → 环境/缓存/构件」，这类运维观测属"性能/非功能"关注点，不应内联在核心搜索算法中。

### 2.3 代码变化同步文档 — 合规

`docs/` 体系完整，且 `CD_CONTRACT.md`、`COST_MODEL.md`、`ARCHITECTURE_AND_DECISIONS.md` 与代码对应关系可追溯；上一轮 selection-rationale 已同步提案与 CHANGELOG。本次审计未发现代码与文档明显的版本漂移。

### 2.4 Schema 验证 — 合规

`schemas/` 含 9 份 JSON Schema（risk-frame v1/v2、route-plan v1/v2/v3、四层 v3、selection-rationale v1，含 .geojson 变体）；`tests/contract/test_schemas.py` 覆盖校验；`build_selection_rationale` 强制 `baseline_objective = fastest` 且校验 shared identity。符合「新增 Schema 需在 test_schemas.py 验证」约束。

### 2.5 术语标准 — 合规

`replanning/policy.py` 严格区分 `triggered` / `suppressed_by_min_interval` / `retry_at`，`ReplanDecision` 不含糊使用单一 `accepted`；`SwitchDecision.accepted` 与 `reason` 分离。符合治理标准「禁止含糊使用单一 accepted，须区分 candidate_generated/replan_decided/replan_adopted」精神。

---

## 3. 核心算法实现评估

### 3.1 时间相关 A\*（`time_dependent_astar.py`）— 正确性高，工程质量有改进空间

**正确性优点：**

- 状态 `(node, time_bucket, heading_code)` 三元组正确建模时间扩展图；`heading_code` 用于转弯惩罚。
- 启发式 admissible：`CostModel.lower_bound` 仅取 `(travel_time + distance) * fastest_hours`，风险/转弯/偏差下界为 0，保证最优性。
- 失败关闭：`RiskCoverageError` / `UnnavigableSpeedError` / `_RejectedEdge("hard"|"risk")` 四类拒绝独立计数，不静默降级。
- 边采样 ETA-aware：`_evaluate_edge` 两轮精化使 ETA 与环境相关速度自洽；`_trapezoidal_average` 梯形积分风险。
- 确定性：`serial = count()` 打破堆平局，保证可复现。
- zero-heuristic 模式（`use_heuristic=False`）退化为 Dijkstra，可作为小 fixture 的正确性 oracle。

**问题 AUDIT-PERF-01（P0，热循环 frozen dataclass replace 开销）：**

`_Counters` 是 `@dataclass(frozen=True, slots=True)`，但 `plan()` 主循环里几乎每步都用 `counters = replace(counters, ...)` 重建整个对象（约 15+ 处）：

```python
counters = replace(counters, heap_pops=counters.heap_pops + 1)
counters = replace(counters, expanded=counters.expanded + 1)
counters = replace(counters, unique=len(labels))
...
```

`dataclasses.replace` 每次创建新对象并复制全部字段，在 `max_expansions=250_000` 量级的热循环里开销显著。不可变性应体现在**最终快照**，而非循环中的临时累加器。

**问题 AUDIT-COUPLING-01（P1，核心算法直接耦合环境变量与 Unix 资源模块）：**

- `line 238`：`os.environ.get("C_ASTAR_PROGRESS_SECONDS", "0")` 在核心算法直接读环境变量。`PlanningRequest` 已有 `progress_interval_seconds` 字段，env 回退应在 CLI/服务上层解析后注入。
- `line 6`：`import resource`（Unix-only），`line 288` `resource.getrusage(...)` 直接在算法内取 RSS。`resource` 在 Windows 不存在，且这是观测关注点泄漏进算法核心。

**问题 AUDIT-MAINT-01（P1，进度打印内联且字符串拼接冗长）：**

`plan()` 内联约 28 行进度格式化（`expanded_line` / `set_line` / `reopen_line` 拼接 + `print`），可读性差且混杂算法与观测。应提取为独立 `_emit_progress(counters, ...)` 方法。

**问题 AUDIT-MAGIC-01（P2，浮点容差与迭代次数硬编码）：**

- `line 362`：`tentative_cost >= previous[0] - 1e-12` 浮点容差裸写。
- `line 422`：`for _ in range(2)` 两轮 ETA/速度精化，固定次数 2 缺少收敛判断与文档说明"为何 2 次足够"。

### 3.2 四层规划编排（`layered.py`）— 原子性正确，常量与类型可改进

**正确性优点：**

- 原子发布：要么四层全发布、要么 `switch_decision` 拒绝时不发布（`published=False`）。
- 两阶段 set_id 模式正确：先用占位 `layer-set-sha256-0*64` 构造，再用 `semantic_digest` 替换回填各 bundle，保证 digest 与内容强绑定。
- `selection_rationale` 作为 sidecar 不参与 `four_layer_route_plan_set_semantic_digest`，符合「sidecar 不进入 digest、保持 SSOT 与失败关闭」合约。
- 各层校验完整：目标数 == 3、objective 一致、起点匹配、到达 layer goal、不超层时间上限。

**问题 AUDIT-SSOT-02（P1，层时间窗口硬编码）：**

`layer_specs`（72h / 24h / 6h）以裸元组硬编码在 `execute()` 方法内：

```python
(PlanLayer.MAIN_CORRIDOR, timedelta(hours=72), ...)
(PlanLayer.ROLLING, timedelta(hours=24), ...)
(PlanLayer.EXECUTABLE, timedelta(hours=6), ...)
```

应提取为类常量或配置（`PlannerConfig` 已有 `max_search_hours` 等，可扩展层窗口字段），并文档化为何是 72/24/6。

**问题 AUDIT-TYPE-01（P2，类型注解过宽与 RuntimeError 滥用）：**

- `line 74`：`planner_config: object` 应为 `PlannerConfig`。
- `line 99`：`raise RuntimeError("maximum_elapsed was not resolved")` 应改更具体的 `ContractError` 或自定义错误。

### 3.3 BC 正式入口（`ingress.py`）— 失败关闭严格，存在 assert 与泄漏风险

**正确性优点：**

- 失败关闭极严格：formal provenance 校验、窗口完整性（逐小时闭区间）、`prepare` 与 `execute` 间窗口不变性校验（`commit_id` / `content_digest` 双校验）。
- 私有 planner 在 `source.lease_committed_window` 内重建，不从 prepare-time 可检视帧走捷径。
- generation 单调（`enter_generation` 拒绝回退）。

**问题 AUDIT-ROBUST-01（P2，assert 做运行时类型检查）：**

`line 91`：`assert isinstance(result, PlanningBatch)` 与 `line 111`：`assert isinstance(result, FourLayerReplanningOutcome)`。`assert` 在 `python -O` 下被剔除，不应用于运行时契约校验。应改 `if not isinstance(...): raise TypeError(...)`。

**问题 AUDIT-LEAK-01（P3，session 字典无界增长）：**

`_sessions: dict[...]`（`line 244`）以 `(run_id, scenario_id)` 为 key 累积，无清理或上限。长时间运行多 run 的常驻进程会内存泄漏。应加 LRU 或显式 `close_session`。

### 3.4 成本模型与船舶性能（`cost/`、`vessel.py`）— 实现干净，无显著问题

- 等效小时成本可解释（`CostBreakdown` 分项 + 加权总计）。
- `lower_bound` admissible 正确（风险/转弯/偏差下界为 0）。
- 输入校验完整（有限性、非负、`[0,1]` 区间）。
- `vessel.py` docstring 明确「不从 risk_score 推断速度损失，避免把策略风险重复计为物理」，设计意图清晰。

**结论：无需改进。**

### 3.5 风险采样（`risk/sampler.py`）— 实现质量高

**正确性优点：**

- 严格 ETA-aware：只内插不外推（`_bracket` 拒绝越界）。
- 保守聚合：`hard_mask` 逻辑 OR、`confidence` 取 min、`environment_speed_factor` 取 min。
- 未知风险在可通航点视为不安全（`line 329` `raise RiskSamplingError`），符合 fail-closed。
- identity 校验防跨场景/走廊/generation 混插。

**小问题 AUDIT-DOC-01（P3，保守选择缺注释）：**

`line 332` `risk_score = 1.0`（hard_mask 命中但 risk 未知时）是合理的保守值，但缺注释说明"硬阻塞点风险值无关紧要因该边会被拒绝"。读者可能误以为是真值。

### 3.6 重规划策略（`replanning/policy.py`、`coordinator.py`）— 术语清晰，存在同名异常与写法不一致

**正确性优点：**

- 五类触发（TIME/DATA/RISK/DEVIATION/EVENT/MANUAL）枚举顺序保留并合并去重。
- 去抖（`min_interval`）+ 迟滞（`risk_trigger_high`/`risk_clear_below` + `_risk_latched`）+ 开关门（`min_switch_improvement` + `risk_hysteresis`）三层策略完整。
- `mark_replanned` 仅在已发布计划后更新基线，符合「baseline 只在 committed plan 后变化」。

**问题 AUDIT-NAME-01（P2，`PlanningCancelled` 同名三处）：**

```text
errors.py:24             PlanningCancelledError(PlanningError)
planners/errors.py:26   PlanningCancelled(PlanningCancelledError)
replanning/coordinator.py:20  PlanningCancelled(PlanningCancelledError)
```

`planners/errors.py` 与 `replanning/coordinator.py` 各自定义同名 `PlanningCancelled`，均继承 `PlanningCancelledError`。两者语义相近但分属不同子包，导入时易混淆，且若捕获方写 `except PlanningCancelled` 可能接错包。建议统一为单一 `PlanningCancelled`（置于 `errors.py` 或 `planners/errors.py`），另一处复用。

**问题 AUDIT-STYLE-01（P3，UTC 校验写法不一致）：**

`policy.py` `line 79`：`value.utcoffset() != UTC.utcoffset(value)` 写法绕；项目其他处（如 `sampler.py:66`、`layered.py:390`）统一用 `value.utcoffset() != timedelta(0)`。应统一。

### 3.7 网格（`grid/regular.py`）— 正确，snap 性能可优化

**正确性优点：** 严格单调校验、haversine/initial_bearing 标准、`snap_to_navigable` 显式不隐式、连通分量 BFS。

**问题 AUDIT-PERF-02（P2，snap_to_navigable O(n²) 全网格遍历）：**

`snap_to_navigable`（`lines 177-188`）遍历全网格每个节点算 haversine 取最小。大网格（如 30×30+）下 O(rows×cols) 每次 snap 开销显著。可优化为从 `nearest_node` 出发做 BFS 扩圈，命中首个可通航即返回。

### 3.8 发布与序列化（`publishing/`）— 原子写优秀

**正确性优点：**

- `atomic_write_json`：`mkstemp` + `fsync` + `os.replace` + 目录 `fsync`，崩溃安全。
- `allow_nan=False` 防 NaN 入 JSON。
- `PublicationToken` / `SelectionRationale` `__post_init__` 校验 digest 为 64 位小写 hex。
- round-trip 一致（`to_dict` / `from_dict` 对称）。

**小问题 AUDIT-ROBUST-02（P3，from_dict 默认 schema_version 可能掩盖缺失）：**

`serialization.py:128` `value.get("schema_version", ROUTE_PLAN_SCHEMA_VERSION)`：若文档缺失 `schema_version` 字段，静默回退 v2 默认。对已知格式可接受，但更严的做法是缺失即报错（schema_version 是必需字段）。

---

## 4. 问题汇总与改进优先级

> 状态说明（2026-08-24 全部已修复，详见 `CHANGELOG.md` Unreleased — Core algorithm audit fixes）：✅ = 已修复，定向 + 全量测试通过。

| ID | 严重度 | 模块 | 问题 | 改进动作 | 状态 |
|---|---|---|---|---|---|
| AUDIT-PERF-01 | **P0** | `planners/time_dependent_astar.py` | frozen `_Counters` 在 250k 热循环用 `replace` 重建 15+ 次/迭代 | 计数器改可变（普通类或非 frozen dataclass），循环结束快照为不可变 `SearchMetrics` | ✅ |
| AUDIT-COUPLING-01 | **P1** | `planners/time_dependent_astar.py` | 核心算法直接 `os.environ` + `import resource` | env 回退移至 CLI/服务层注入；RSS 观测移至独立 observer | ✅ |
| AUDIT-MAINT-01 | **P1** | `planners/time_dependent_astar.py` | 进度打印 28 行内联主循环 | 提取 `_emit_progress` 方法 | ✅ |
| AUDIT-SSOT-01 | **P1** | `contracts/layered.py`、`publishing/layered_serialization.py`、`layered.py` | v3 schema_version 裸字符串散落 7 处 | 提取 `ROUTE_PLAN_V3_SCHEMA_VERSION` / `FOUR_LAYER_SET_V3_SCHEMA_VERSION` 常量 | ✅ |
| AUDIT-SSOT-02 | **P1** | `layered.py` | 层时间窗 72/24/6h 硬编码方法内 | 提取为 `PlannerConfig` 字段或类常量并文档化 | ✅ |
| AUDIT-ROBUST-01 | **P2** | `ingress.py` | `assert isinstance(...)` 做运行时检查 | 改 `if not isinstance: raise TypeError` | ✅ |
| AUDIT-PERF-02 | **P2** | `grid/regular.py` | `snap_to_navigable` O(n²) 全网格遍历 | 改 BFS 扩圈从 `nearest_node` 起步 | ✅（采用 numpy 矢量化 + `np.lexsort`，tie-break 语义不变） |
| AUDIT-NAME-01 | **P2** | `planners/errors.py`、`replanning/coordinator.py` | `PlanningCancelled` 同名两处 | 统一为单一类，另一处复用 | ✅ |
| AUDIT-TYPE-01 | **P2** | `layered.py` | `planner_config: object` + `RuntimeError` 滥用 | 收紧为 `PlannerConfig` + `ContractError` | ✅ |
| AUDIT-MAGIC-01 | **P2** | `planners/time_dependent_astar.py` | `1e-12` 容差 + `range(2)` 迭代次数裸写 | 提取常量并文档化两轮精化理由 | ✅ |
| AUDIT-LEAK-01 | **P3** | `ingress.py` | `_sessions` 无界增长 | 加 LRU 或 `close_session` | ✅（`OrderedDict` + `_MAX_SESSIONS=64`） |
| AUDIT-STYLE-01 | **P3** | `replanning/policy.py` | UTC 校验写法与项目不一致 | 统一为 `!= timedelta(0)` | ✅ |
| AUDIT-DOC-01 | **P3** | `risk/sampler.py` | `risk_score=1.0` 保守值缺注释 | 加注释说明硬阻塞点风险值无关 | ✅ |
| AUDIT-ROBUST-02 | **P3** | `publishing/serialization.py` | `from_dict` schema_version 缺失静默回退 | 缺失即报错 | ✅ |

---

## 5. 推荐改进路线图

### 第一阶段（P0–P1，性能与治理核心）

1. **AUDIT-PERF-01**：将 `_Counters` 改为可变累加器（`@dataclass(slots=True)` 不加 `frozen`，或普通 `class`），`plan()` 直接 `counters.heap_pops += 1`，循环结束构造不可变 `SearchMetrics`。预期消除热循环每迭代 15+ 次对象重建，大网格搜索吞吐提升。
2. **AUDIT-COUPLING-01 + AUDIT-MAINT-01**：剥离进度观测为独立 `_ProgressEmitter`（或纯函数 `_emit_progress`），`resource.getrusage` 与 `os.environ` 从核心算法移除；env 回退由 CLI 层解析后通过 `PlanningRequest.progress_interval_seconds` 注入。
3. **AUDIT-SSOT-01**：在 `publishing/models.py` 或 `contracts/layered.py` 新增 `ROUTE_PLAN_V3_SCHEMA_VERSION` / `FOUR_LAYER_SET_V3_SCHEMA_VERSION` 常量，替换 7 处裸字符串。
4. **AUDIT-SSOT-02**：`PlannerConfig` 增加层窗口字段（`main_corridor_hours` / `rolling_hours` / `executable_hours`，默认 72/24/6），`layered.py` 读取配置而非硬编码。

### 第二阶段（P2，健壮性与一致性）

5. **AUDIT-ROBUST-01**：`ingress.py` 两处 `assert isinstance` 改为显式 `raise TypeError`。
6. **AUDIT-NAME-01**：合并两个 `PlanningCancelled` 为单一类。
7. **AUDIT-PERF-02**：`snap_to_navigable` 改 BFS 扩圈。
8. **AUDIT-TYPE-01 + AUDIT-MAGIC-01**：收紧类型注解、改具体异常、提取容差/迭代常量并补注释。

### 第三阶段（P3，清理）

9. **AUDIT-LEAK-01 / AUDIT-STYLE-01 / AUDIT-DOC-01 / AUDIT-ROBUST-02**：session LRU、UTC 校验统一、补注释、schema_version 缺失报错。

### 不要做

- 不要重写 A\* 核心搜索逻辑——其正确性（admissible 启发式、失败关闭、确定性）已扎实，改动会引入回归风险。
- 不要把 `selection_rationale` 并入 digest——这会破坏「sidecar 不进入 SSOT」的 C→D 合约（已 APPROVED）。
- 不要为性能过早引入 Cython/numba——先消除 P0 的 `replace` 开销，再据实测决定。

---

## 6. 验证成熟度评估

| 维度 | 当前等级 | 证据 |
|---|---|---|
| 核心算法正确性 | `AUTHORITATIVE_PASS` | admissible 启发式 + zero-heuristic oracle + 失败关闭 + 171 unit passed |
| SSOT 治理 | `UNIT_PASS` | v2 常量良好，v3 散落 7 处待收敛（AUDIT-SSOT-01） |
| 跨包合约 | `FROZEN_BASELINE` | selection-rationale 提案 APPROVED，sidecar 不入 digest |
| 性能工程 | `UNIT_PASS` | P0 计数器开销未优化，`make check` 全绿但未做大规模基准 |
| 失败关闭 | `AUTHORITATIVE_PASS` | ingress/sampler/vessel 三层 fail-closed 严格 |

**整体 verdict：核心算法实现质量高、治理合规度良好；主要改进空间在性能工程（P0 计数器）与观测/常量的治理剥离（P1）。**
