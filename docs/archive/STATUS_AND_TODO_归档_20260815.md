> [!NOTE]
> **二次文档治理归档声明**
> - 本文件角色：2026-08-15 改造前的 C 状态与待办快照，仅供历史追溯。
> - 归档时间：2026-08-15（Asia/Shanghai）。
> - 现行文件：[STATUS_AND_TODO.md](STATUS_AND_TODO.md)。
> - 归档原因：科学校准不再作为挑战杯工程演示完成门槛，且不再要求跨专业签字。

<!-- ORIGINAL CONTENT START -->

> [!NOTE]
> **文档治理声明**
>
> - 文件角色：工作包 C 的当前进度、缺口、依赖和无日历待办真源。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 原文件去向：旧综合指南已归档为
>   `../工作包C项目整体认识与继续开发指南.archive-20260814-pre-governance.md`。
> - 改造原因：把会变化的状态与稳定架构/操作说明分开，并移除已过时的 v1、B 原型和
>   逐日冲刺排期叙述。

# 工作包 C 进度与待办

## 状态快照

| 字段 | 当前值 |
|---|---|
| 快照时间 | 2026-08-14（Asia/Shanghai） |
| 包版本 | `0.4.0` |
| 审计基线提交 | `703b1da071754585e3c1bfceeeadf6647c795072` |
| 工程复验 | `UV_OFFLINE=1 make check`：`138 passed`，Ruff/lock/sync/CLI 均通过 |
| 整体状态 | 进行中：工程主线完成，实源/科学/D 验收未完成 |
| 安全口径 | 科研演示；calibration 为 `demo_unvalidated` |

用户已确认旧 `交付包.zip` 是已退役的 B 历史制品。C 已移除其硬编码路径和外部制品测试，
保留不依赖该 ZIP 的 legacy 显式开发模式门禁。

## 已完成

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

## 当前缺口

### P0：系统闭环

1. **真实来源 A→B→C 尚未形成当前验收制品。**需以 A、B 各自当前 handoff 为准，选择
   同一 RunContext 的真实 DatasetBundle 和 B commit；旧综合指南中的 A/B 快照不得复用为现状。
2. **真实窗口覆盖尚未验收。**窗口必须逐小时覆盖路线实际 ETA；未来、陈旧、缺帧、网格或
   上下文不匹配必须显式失败。
3. **D 消费尚未验收。**D 需明确消费 v2 或 v3，验证 Schema、atomic latest、身份字段和
   不完整整组拒绝语义。

### P1：科学可信度

1. 风险模型、`environment_speed_factor` 与真值的校准/误差评估由 B/领域团队牵头，C 只消费。
2. C 船舶性能曲线、燃耗/成本参数需以可追溯真船或认可资料校准。
3. 需定义数据划分、指标阈值、版本冻结和领域签字流程；完成前保持 `demo_unvalidated`。
4. 需保存一次正式初始规划和一次事件/时间推进重规划的输入、摘要、输出与日志证据。

### P2：产品化与维护

1. 按 D 的稳定需求确定候选路线保留、历史制品和可观测性策略。
2. 若引入等待动作、短窗终端代价或新图结构，须升级状态/算法/合同，不能通过配置暗改。

## 依赖闸门与执行顺序

```text
当前 A bundle + RunContext
          ↓ 身份、时域、provenance 合格
当前 B formal commit
          ↓ 完整逐小时窗口 + execution lease
C formal v2/v3 规划与重规划
          ↓ Schema + atomic latest + failure semantics
D 消费验收
          ↓
科学校准与产品化增强
```

任一上游闸门不满足时，应保留 formal fixture/synthetic 工程证据并把实源状态标为阻塞，
不能降低校验或把 synthetic 输出改名为 formal。

## 完成判据

- 工程基线：以 [`ACCEPTANCE.md`](ACCEPTANCE.md) 的闸门为准；
- 系统闭环：A/B/C/D handoff 对同一运行身份、合同版本和证据制品表述一致；
- 科学闭环：参数、数据集、指标、阈值和评审记录齐全后才可改为 `calibrated`；
- 文档闭环：状态变化同步本文件和
  [`../work_package_c_handoff.md`](../work_package_c_handoff.md)，日期排期只更新
  [`ABC_10_DAY_SPRINT.md`](../../ABC_10_DAY_SPRINT.md)。

## 待人工确认

1. 系统验收指定的真实 A bundle/B commit 及制品保管位置；
2. D 下一轮主路径选择 v2 还是 v3；
3. 科学校准的负责人、数据授权、指标和签字阈值；
