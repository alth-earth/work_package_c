> [!NOTE]
> **文档治理声明**
> - 文件角色：工作包 C 当前进度、挑战杯待办和延期项真源。
> - 改造时间：2026-08-15（Asia/Shanghai）。
> - 原文件去向：[STATUS_AND_TODO_归档_20260815.md](archive/STATUS_AND_TODO_归档_20260815.md)。
> - 改造原因：科学校准不再作为工程演示门槛，并取消跨专业签字依赖。

# 工作包 C 进度与待办

## 状态快照

| 字段 | 当前值 |
|---|---|
| 包版本 | 0.4.0 |
| 当前基线提交 | `b5bcb7e456afafedaedc126ed17957eee2e40c94`（记录时 clean） |
| 工程复验 | 2026-08-25：`UV_OFFLINE=1 make check` 为 `272 passed`，Ruff/lock/sync/CLI 通过 |
| 工程主线 | v2/v3、重规划、围栏和原子发布已实现 |
| 挑战杯状态 | **Demo RC1 已建立（2026-08-16）**：mur/dikson v3 四层 + 6h 重规划 PASS，D 消费 PASS，r7 复现 PASS |
| 科学状态 | `demo_unvalidated`；保留接口，不阻塞演示 |

用户已确认旧 `交付包.zip` 是已退役的 B 历史制品；C 已移除其硬编码路径和外部制品测试，
保留不依赖该 ZIP 的 legacy 显式开发模式门禁。

## 已完成（源自：[STATUS_AND_TODO_归档_20260815.md](archive/STATUS_AND_TODO_归档_20260815.md)）

| 范围 | 证据 |
|---|---|
| 共享配置与 `RunContext.v2` 对齐 | `config.py`、`context_validation.py`、`tests/contract/test_configuration.py` |
| 有界、可审计的 corridor 端点映射 | `endpoints.py`、`tests/unit/test_endpoints.py` |
| canonical、逐小时、原子 B 风险窗与执行 lease | `contracts/windows.py`、`contracts/sources.py`、`ingress.py` |
| 按 ETA 的风险采样和时间依赖 A* | `risk/sampler.py`、`planners/time_dependent_astar.py` |
| C 侧最终速度、ETA 和多目标成本 | `cost/`、`service.py` |
| RoutePlan v2 三目标基线 | `service.py`、`schemas/route-plan-v2.schema.json` |
| RoutePlan v3 四层十二路线整组 | `layered.py`、`contracts/layered.py`、v3 Schema |
| 重规划、竞态围栏和原子 latest | `replanning/`、`publishing/` |
| JSON/GeoJSON 往返和 Schema 验证 | `contracts/codec.py`、`publishing/`、`tests/contract/test_schemas.py` |
| 旧 B/v1 显式隔离 | `adapters/legacy_b.py`；只允许 `legacy_unverified` |
| 任务2 gap：推荐选择理由（SelectionRationale + `selection-rationale.v1` Schema + v2/v3 可选 `selection_rationale` 字段 + CLI 输出 `selection-rationale.json`；跨包提案 APPROVED，CD_CONTRACT.md 同步）—— `publishing/`、`service.py`、`layered.py`、`cli.py`、提案与测试 | ✅ 完成（代码+测试+提案+文档+D 消费对接） | 无（D 消费对接与跨包回归已落地） |

## P0：挑战杯闭环

1. 用同一冻结 RunContext 与 B committed window 运行 v2 三目标。
2. 检查端点、穿陆、ETA、风险采样、绕行和失败语义无明显错误。
3. 保存初始计划和至少一次风险/时间触发的重规划。
4. 让 D 只读当前 CD 制品显示风险、路线和指标。
5. 完成两次断网复现和答辩讲解材料。

## P1：工程增强

1. 为 RiskSampler/A* 建立阶段耗时、内存、取消点和性能预算。
2. 主线稳定后再复验 v3 四层十二路线。
3. 迁移至特罗姆瑟—伊斯峡湾外部入口做 smoke。
4. 根据 D 需要完善候选/历史制品保留。

## P2：接口保留，不阻塞

- 海洋/气象、冰情、船舶、航运/法规、数据/模型五类科学接口；
- 真船性能、风险概率、净水深、法规 hard mask；
- 等待、方向性精细模型、新路径算法。

必需参数优先公开典型值，其次透明拟合，最后使用明确标注的演示值。无需跨专业签字。

## 完成定义

工程门禁通过；冻结场景的风险—路线变化可解释；路线基本合理；重规划可运行；D 能展示完整
当前代次；所有演示参数和限制明确。科学闭环不是挑战杯完成条件。

## 保留风险

真实长窗、科学校准、D 长期合同、v3 性能、bathymetry/法规、等待和新算法继续记录，不在本轮
自动展开。核心算法下一轮固定选择 P2.1 Winter M2；其门禁、延期项和证据边界只见
[`CORE_ALGORITHM_IMPROVEMENT_PLAN.md`](CORE_ALGORITHM_IMPROVEMENT_PLAN.md)，本文不复制算法方案。

## 依赖闸门与执行顺序（源自：[STATUS_AND_TODO_归档_20260815.md](archive/STATUS_AND_TODO_归档_20260815.md)）

```text
当前 A bundle + RunContext
          ↓ 身份、时域、provenance 合格
当前 B formal commit
          ↓ 完整逐小时窗口 + execution lease
C formal v2/v3 规划与重规划
          ↓ Schema + atomic latest + failure semantics
D 消费验收
          ↓
科学校准与产品化增强（非挑战杯门槛）
```

任一上游闸门不满足时，保留 formal fixture/synthetic 工程证据并把实源状态标为阻塞；不能
降低校验或把 synthetic 输出改名为 formal。

## 完成判据补充（源自：[STATUS_AND_TODO_归档_20260815.md](archive/STATUS_AND_TODO_归档_20260815.md)）

- 工程基线：以 [`ACCEPTANCE.md`](ACCEPTANCE.md) 的闸门为准；
- 系统闭环：A/B/C/D handoff 对同一运行身份、合同版本和证据制品表述一致；
- 科学闭环：参数、数据集、指标和阈值记录齐全后才可改为 `calibrated`；批准由项目负责人
  决定，不依赖不存在的领域签字流程；
- 文档闭环：状态变化同步本文件和
  [`../work_package_c_handoff.md`](../work_package_c_handoff.md)，日期排期只更新
  [`ABC_10_DAY_SPRINT.md`](../../ABC_10_DAY_SPRINT.md)。

当前日历见 [ABC_10_DAY_SPRINT.md](../../ABC_10_DAY_SPRINT.md)，详细交接见
[work_package_c_handoff.md](../work_package_c_handoff.md)。
