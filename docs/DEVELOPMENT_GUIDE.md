> [!NOTE]
> **文档治理声明**
>
> - 文件角色：工作包 C 的继续开发与本地操作手册。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文件去向：旧综合指南已归档为
>   `docs/archive/工作包C项目整体认识与继续开发指南.archive-20260814-pre-governance.md`。
> - 改造原因：把环境、运行、修改和验收步骤从历史状态/架构长文中独立出来，便于人和 AI
>   在不误用 synthetic/legacy 的前提下继续开发。

# 工作包 C 继续开发指南

## 1. 接手阅读顺序

1. [`../README.md`](../README.md)：入口与当前口径；
2. [`../work_package_c_handoff.md`](../work_package_c_handoff.md)：状态、缺口和依赖；
3. [`DECISIONS.md`](DECISIONS.md)：不可破坏的不变量；
4. [`BC_CONTRACT.md`](BC_CONTRACT.md) 与 [`CD_CONTRACT.md`](CD_CONTRACT.md)：接口；
5. [`ACCEPTANCE.md`](ACCEPTANCE.md)：完成证据；
6. [`CORE_ALGORITHM_IMPROVEMENT_PLAN.md`](CORE_ALGORITHM_IMPROVEMENT_PLAN.md)：核心算法现状、下一轮计划和门禁（唯一 SSOT）。

跨包修改前再读 `../arctic_route_contracts/` 和 orchestrator 的当前 handoff。不要从归档综合
指南复制旧 v1 字段、旧 B 状态或旧测试数。

## 2. 环境与验证

项目要求 Mamba 提供 Python/native 环境，uv 管理 Python 依赖和锁文件：

```bash
cd ${ARCTIC_ROUTE_ROOT}/work_package_c
make env-create
make lock
make sync
UV_OFFLINE=1 make check
```

`make check` 依次验证 Ruff、pytest、`uv lock --check`、`uv sync --check` 和 CLI help。当前 C
基线 HEAD `b5bcb7e456afafedaedc126ed17957eee2e40c94`（记录时 clean），
`UV_OFFLINE=1 make check` 为 `272 passed`。已退役旧 B `交付包.zip` 的硬编码外部回归，不再要求本机存在该文件。

只改文档时至少运行：

```bash
git diff --check
UV_OFFLINE=1 make check
```

不要把另一个 `.venv` 的 pytest 结果等同于 Mamba + uv 锁环境可复现。

## 3. 三种运行模式

### Synthetic smoke

```bash
make demo
```

它调用 `arctic-route-plan synthetic-demo`，只生成 v2 三目标 synthetic 输出到
`output/demo/`。适合工程冒烟，不是正式 A→B→C、v3 四层或科学验收。

### Formal 系统运行

正式入口是根级 [`arctic_route_orchestrator`](../../arctic_route_orchestrator/)；若在 Python
集成测试中直接调用 C，使用公共 `RiskSourcePlanningIngress`：

```python
from arctic_route_planning import RiskSourcePlanningIngress

# source 必须实现 CommittedRiskSource；request 必须携带完整共享上下文与规划配置。
ingress = RiskSourcePlanningIngress(source=source, configuration=configuration)
prepared = ingress.prepare(request)
batch_v2 = prepared.execute()                 # v2 三目标
# 或：layered_v3 = prepared.execute_four_layer()  # v3 四层整组；同一运行不要双写
```

构造 `request` 前必须由 orchestrator 调用 `map_corridor_endpoints(...)`。精确构造方式以
`tests/integration/test_formal_ingress.py` 和 orchestrator 当前 service 为准；不要发明正式 CLI。

### Legacy 审计

`legacy-inspect`/`legacy-plan` 只用于显式旧制品审计与迁移。输出永久标记
`legacy_unverified`，不得写入 formal latest，也不得用来证明当前 B→C 合同通过。

## 4. 代码导航

| 位置 | 修改责任 |
|---|---|
| `contracts/` | C 本地不可变请求、RiskFrame、RoutePlan、window/source 协议 |
| `config.py`、`context_validation.py` | 共享/本地配置加载及摘要一致性 |
| `endpoints.py` | corridor 端点有界映射与审计文档 |
| `ingress.py` | 正式 source、窗口、lease、私有快照和会话围栏 |
| `risk/sampler.py` | 按 ETA 采样、缺测/边界拒绝 |
| `grid/` | 规则网格与邻接 |
| `planners/` | 时间依赖搜索 |
| `cost/` | C 所有的最终航速、燃耗和目标成本 |
| `service.py` | v2 三目标应用服务 |
| `layered.py` | v3 四层应用编排，不改规划核心 |
| `replanning/` | 事件策略与协调器 |
| `publishing/` | 序列化、canonical digest、latest 和并发围栏 |
| `adapters/` | synthetic/legacy 隔离适配器 |
| `schemas/` | 跨语言机器合同 |
| `tests/` | contract、unit、integration 验证 |

## 5. 安全修改流程

1. 用 `git status --short --branch` 确认现有改动，保留不属于本任务的用户修改。
2. 先定位所属边界；跨包字段优先改 `arctic_route_contracts`，不要在 C 复制共享模型。
3. 修改合同前同时检查 Python 模型、Schema、codec、文档和消费者测试。
4. 修改采样、速度、成本、搜索、重规划或发布时，先写失败用例并保持所有围栏。
5. 正式路径测试使用 committed source；synthetic/legacy 适配器不得渗入 formal 入口。
6. 运行聚焦测试，再运行完整 `UV_OFFLINE=1 make check` 和 `git diff --check`。
7. 更新 `CHANGELOG.md`、相关合同文档、状态/handoff；日期与负责人只更新顶层 sprint。

必须保持的不变量：

- 不导入 A/B 私有实现；
- 不使用未来、陈旧、缺帧或上下文不匹配数据；
- 不外推风险窗，不把缺测当零风险；
- B 提供环境因子，C 计算最终速度；
- formal provenance 不等于 calibrated；
- 不绕过 generation/request/revision/cancellation/publication 围栏；
- v3 任一层失败时不发布部分整组。

## 6. 常用聚焦验证

```bash
.mamba-env/bin/uv run --locked pytest tests/integration/test_formal_ingress.py
.mamba-env/bin/uv run --locked pytest tests/unit/test_time_dependent_astar.py
.mamba-env/bin/uv run --locked pytest tests/unit/test_layered_planning.py
.mamba-env/bin/uv run --locked pytest tests/unit/test_replanning.py
.mamba-env/bin/uv run --locked pytest tests/unit/test_publishing.py
.mamba-env/bin/uv run --locked pytest tests/contract/test_schemas.py
```

涉及正式跨包链路时，还需运行根级 orchestrator 的验收命令，并在各包 handoff 中记录同一
run/context/commit/plan 身份。C 单包测试不能替代系统级实源验收。

## 7. 故障定位

| 现象 | 优先检查 |
|---|---|
| `make check` 找不到 uv | 是否先成功执行 `make env-create`，`.mamba-env/bin/uv` 是否存在 |
| lock/sync check 失败 | `pyproject.toml`、`uv.lock` 与相邻 editable contracts 是否一致 |
| formal prepare 拒绝 | RunContext、digest、generation、issue/valid time、60 min 完整窗口、commit |
| 搜索在时域边界失败 | B 窗是否覆盖实际 ETA；不要用等待/外推绕过 |
| 无可行路线 | hard mask、confidence=0、端点映射、速度下限和 allowed regions |
| 旧结果未发布 | request/revision/generation/cancellation token 是否已被更新结果淘汰 |
| v3 整组失败 | 四层 anchor/focus window、12 路线完整性和同一 lease |
| legacy adapter 拒绝输入 | 确认显式 development mode、`legacy_unverified` 与用户提供的实际路径 |

## 8. 文档维护规则

- `README.md` 只保留短入口；详细交接更新 `work_package_c_handoff.md`。
- 稳定架构/决策更新本目录相应文档；进度只更新 `STATUS_AND_TODO.md`。
- 日历、人员和跨包总计划只更新 `../../ABC_10_DAY_SPRINT.md`。
- 系统级模块/数据流变更同步 `../../ARCTIC_ROUTE_SYSTEM.md`。
- 归档文件只用于审计，不编辑其标记后的原正文，也不重新成为现状入口。
- 新增或重命名文档后，检查所有相对链接；不得留下指向旧综合指南的活动链接。
