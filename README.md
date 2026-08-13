# 北极航线工作包 C

工作包 C 是一个独立、可运行、可测试的 Python 3.13 项目：它消费 B → C
`RiskFrame v2` 时间序列，按船舶到达每条边的 ETA 采样风险，运行时间依赖 A*，
生成最快、低风险和综合推荐路线，并通过最新值缓存向 D 发布 `RoutePlan`。

当前版本为 `0.3.0`；在共享 `RunContext.v2` 与 BC/CD v2 身份合同之上，新增规范
RiskFrame JSON codec、逐小时原子窗口提交合同和正式 `RiskSource` 规划入口。四层规划仍是
后续增量路线图。逐版本变化、兼容性和验收证据见
[CHANGELOG.md](CHANGELOG.md)。

> 安全声明：当前场景、船型、风险和成本参数都是科研演示基线，未经真实标定，
> 不得用于真实航行或安全决策。

## 快速开始

原生 NetCDF/HDF5 库由 Mamba 管理，Python 包和锁文件由 uv 管理：

```bash
cd /root/my_project/work_package_c
make env-create
make lock
make sync
make check
make demo
```

`make demo` 使用确定性合成风险帧运行三种目标，将 JSON、GeoJSON 和摘要写入
`output/demo/`。它不需要 A、B 或外部网络即可完成。
为把默认冒烟运行控制在数十秒内，该命令默认使用 5×5 粗网格，因此端点映射可达百公里级；调整距离会被完整报告，结果不应用作路线质量基线。需要更细的工程演示时，显式增加 `--rows/--columns` 并重新设定可接受的 `--max-snap-km`。

## 责任边界

```text
A StandardDataFrame
        │
        ▼
B 时间处理/预测/风险融合
        │ RiskFrame + environment_speed_factor（正式帧必需）
        ▼
C RiskSampler → Grid/Cost/Vessel → TimeDependentAStar
        │                         │
        │                         └─ Replanning/Cancel/Revision
        ▼
  RoutePlan（显式 provenance）+ CD latest
        │
        ▼
D 只读渲染
```

- B 提供环境影响；C 把它应用到版本化船型，计算最终有效航速。
- C 不读 A 内部数据库/缓存，不调用 B 内部函数，不修改 B 的风险权重。
- C 不从 `risk_score` 或 `confidence` 重复推导速度损失。
- 核心不外推风险、不把缺测当安全、不暗中吸附被硬掩膜阻断的起终点。

更完整的取舍见 [决策记录](docs/DECISIONS.md)。

## 命令行

### 合成演示

```bash
.mamba-env/bin/uv run arctic-route-plan synthetic-demo \
  --scenario tromso_isfjorden_july_2026_retrospective_v1 \
  --output-dir output/demo
```

可将 `--scenario` 替换为
`murmansk_dikson_july_2026_retrospective_v1`。输出中会记录配置摘要、
演示警告、起终点到离散网格的有限距离映射以及三种路线指标。

### 隔离读取旧版嵌套 B 样例（可选）

```bash
.mamba-env/bin/uv run arctic-route-plan legacy-inspect \
  --scenario tromso_isfjorden_july_2026_retrospective_v1 \
  --archive '/mnt/c/Users/asd233/Desktop/挑战杯/挑战/交付包.zip' \
  --as-of 2026-07-31T00:00:00Z \
  --allow-unverified-legacy \
  --acknowledge-valid-time
```

这两个 acknowledgement 参数不能省略。此适配器只接受早期“外层 ZIP + 嵌套
综合风险.zip”的已知结构。当前用户提供的 `工作包B.zip` 是另一种直接成果目录，已在
`work_package_b_handoff/` 审计，C 不为它扩展新的半正式适配路径；当前
`work_package_b/` 已按 v2 committed-source 合同重建。旧数据没有可证明的 `issue_time`，
只能用于开发联调，不能进入正式历史
回放。`legacy-plan` 还要求显式 `--max-snap-km`；旧帧间隔超限时明确失败。

## 共享运行上下文与当前演示配置

- 场景、航区和船舶事实来自独立的 `arctic_route_contracts`，C 不再复制这些配置。
- 主线为摩尔曼斯克外海—迪克森外海；测试线为特罗姆瑟外海—伊斯峡湾外部入口。朗伊尔城只保留为 AIS 航次识别参考点，不是规划终点。
- 参考船为公开资料可核查的 `nordic_odyssey_reference_v1`；C 的最低操舵速度、环境阈值、转弯和 UKC 仍是 `demo_unvalidated` 算法参数。
- 默认时间桶 60 min、8 邻接、每边 3 个时空采样点；216 h 是硬上限，实际时域由已物化场景/`RunContext` 请求。
- 默认不允许等待动作。

全航程时域不是固定 9 天。冻结运行前由共享包按候选航线距离、参考船速和缓冲评估：

```bash
cd /root/my_project/arctic_route_contracts
.venv/bin/arctic-route-context recommend-horizon \
  --corridor offshore_murmansk_to_offshore_dikson \
  --vessel nordic_odyssey_reference_v1 \
  --candidate-route-distance-nm 1250
```

C 只消费随后物化且已绑定 A bundle 的具体 `RunContext`，不会自行延长场景；若所需
时域超过共享来源/设备上限，流程在 A 下载和 C 规划前就以
`forecast_coverage_insufficient` 失败。
正式运行 C 时应把完全相同的开始时刻与候选距离传入，并提供 A 生成的 RunContext：

```bash
.mamba-env/bin/uv run arctic-route-plan synthetic-demo \
  --scenario murmansk_dikson_frozen_forecast_template_v1 \
  --simulation-start 2026-08-12T00:00:00Z \
  --candidate-route-distance-nm 1250 \
  --run-context /path/from-shared/run-context.json \
  --output-dir output/frozen-contract-smoke
```

上例的 `synthetic-demo` 仍只用于合同/规划冒烟。当前 `work_package_b/` 已通过下面的正式
Python 入口完成 A→B→C 工程夹具联调；指定共享场景的完整真实 A bundle 尚未取得，因此
不能把该结果称为实源联调。

### 正式 B 风险入口

B 不需要继承 C 的实现类，只需结构化实现
`CommittedRiskSource.get_committed_window(query)`，并返回公共
`CommittedRiskWindow`。C 用完整 run/场景/航区/船型/代次/公共配置/B 模型配置和
`as_of` 查询严格逐小时闭区间，随后通过公共入口装配现有规划组件：

```python
from arctic_route_planning.ingress import RiskSourcePlanningIngress

ingress = RiskSourcePlanningIngress(
    b_risk_source,
    configuration=configuration,
)
prepared = ingress.prepare(service_request)
batch = prepared.execute()
```

`prepare()` 会重算窗口 content digest、正式帧 canonical `risk_id`，核对完整身份、知识
截止、首尾、帧数、60 min 间隔和起终点网格归属。`execute()` 再通过
`lease_committed_window(query)` 复核同一 commit，并让租约贯穿规划和 RoutePlan 发布；B
对同一 run 切换 generation 必须与共享执行租约互斥；同代次新修订和不同 run 则可并发，
以便 coordinator 及时取消旧工作。租约内的帧还会经 canonical encode→decode 形成私有
快照并重建 sampler/planner，避免 prepare 返回的可检查 xarray 对象被替换后进入规划。
缺帧、未提交窗口、过期代次、不同模型/网格或越过 `as_of` 都会失败。同一 ingress 对同一
`(run_id, scenario_id)` 复用 `PlanningCoordinator`，使新修订取消并阻止较旧请求迟到发布；
不同 run 使用独立 coordinator，互不误取消。

入口接收完整、已验证的 `PlanningConfiguration`，并从其中实际执行的 vessel model、
planner 与 replanning 配置重算 `planner_config_digest`；摘要与对象不一致会在读取 B 窗口
前拒绝。

`PreparedRiskPlanning` 不暴露可直接执行的 prepare 阶段 `PlanningService`；正式调用必须走
`.execute()`，从而不能绕过 source lease、commit 复核和 canonical 私有快照。

`RunContext` 把 A 的精确 `DatasetBundle`、场景和船型锁成公共 `config_digest`；
B 和 C 分别另发 `model_config_digest`、`planner_config_digest`。三者不可混称。
C 本地 `configs/` 只保存规划、重规划和船舶性能模型参数。正式 ingress 是边界装配层，
本次未修改 `risk/grid/cost/planners/replanning/service/publishing` 核心模块。
服务与 CLI 都会重算共享事实的内容摘要和公共 `config_digest`，并要求场景起止与
`RunContext` 完全相等。出发、搜索时域和 RiskFrame 窗口只要越过上下文就明确失败，
不会静默裁剪。历史最佳估计允许知识时间晚于模拟时间；冻结预报仍禁止使用出发后知识。
正式 B 来源的 `data_id/issue_time/valid_time/checksum` 缺任一项都会拒绝；
RiskFrame 窗口的来源级别必须一致，并原样写入 RoutePlan。合成/旧数据输出因此不会被冒充为正式路线。

## 后续四层规划路线

导师提出的四层结构纳入 C 的后续路线图，但本次合同迁移不重写现有规划器：

1. 全航程参考线：覆盖完整实际航程（并非固定 9 天），判断总体航道与大尺度通道；
2. 24–72 h 主通道：判断未来进入哪个冰区通道；
3. 0–24 h 滚动优化：面向高精度气象导航和冰区避险；
4. 0–6 h 可执行线：细分为 0–2 h 高可信、2–4 h 推荐、4–6 h 预测区。

四层必须共享同一个 `RunContext`。先在主线上冻结 B 风险参数和 C 目标权重，再原样迁移到测试线；若针对测试线重新调参，不能称为迁移能力验证。

## 项目导航

| 位置 | 用途 |
|---|---|
| `../arctic_route_contracts/configs/` | 全系统唯一的场景、航区和船舶事实 |
| `configs/` | C 自有船舶性能模型、规划和重规划配置 |
| `schemas/` | B→C 与 C→D 的 JSON Schema |
| `contracts/` | 不可变 Python 合同和 `RiskSource` 协议 |
| `ingress.py` | 已提交正式 BC 窗口到现有 C 规划组件的公共入口 |
| `adapters/` | 确定性夹具与隔离的旧 B 适配层 |
| `risk/` | 严格时空 ETA 采样 |
| `grid/`, `cost/` | 规则网格、显式吸附工具、船模和等价小时成本 |
| `planners/` | 隐式时空状态上的时间依赖 A* / Dijkstra oracle |
| `replanning/` | 五类触发、防抖、迟滞、取消和修订围栏 |
| `publishing/` | RoutePlan JSON/GeoJSON 与 CD latest |
| `service.py` | 三目标求解、候选路线和原子发布编排 |

合同和算法细节：

- [版本变更记录](CHANGELOG.md)
- [B→C 合同](docs/BC_CONTRACT.md)
- [C→D 合同](docs/CD_CONTRACT.md)
- [航速与成本模型](docs/COST_MODEL.md)
- [验收清单与已知限制](docs/ACCEPTANCE.md)
- [架构追踪与矛盾处理](docs/ARCHITECTURE_TRACE.md)

## 开发规则

```bash
make lint
make test
make check
```

当前 `../work_package_b/` 已结构化实现 `CommittedRiskSource`，使用公共 canonical codec
发布通过验证的 `RiskFrame` 和原子 `CommittedRiskWindow`；
`RiskSampler`、网格、成本、规划和重规划源码不应因输入实现切换而改动。
