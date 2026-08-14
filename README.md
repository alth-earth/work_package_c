# 北极航线工作包 C

工作包 C 是独立、可运行、可测试的 Python 3.13 航线规划项目。它消费 B 原子提交的
逐小时 `bc.risk-frame.v2` 风险窗，按船舶到达每条边的 ETA 采样风险，运行既有时间依赖
A*，并生成最快、低风险和综合推荐路线。

当前包版本为 `0.4.0`。本版在不改 A*、风险采样、规则网格和成本算法的前提下，新增：

- 公共、可审计的 allowed-region 端点映射；
- `cd.route-plan.v3` 与 `cd.four-layer-route-plan-set.v3`；
- 全航程、24–72 h 主通道、0–24 h 滚动、0–6 h 可执行四层编排；
- 每层三目标，共 12 条路线的 JSON/GeoJSON codec 和原子 latest 发布；
- 正式 ingress 的 v2/v3 初始规划与重规划入口。

工程实现和合同测试不能代替实源验收：指定主航区的真实 12 类、168 h A
`DatasetBundle v2` 尚待取得，因此 C 0.4.0 仍是 `demo_unvalidated` 科研基线，不得用于
真实航行或安全决策。逐版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 当前口径

| 项目 | 当前状态 |
|---|---|
| B→C 输入 | 正式入口只接受完整、逐小时、原子提交的 `RiskFrame v2` 窗口 |
| C 核心算法 | 时间依赖 A*、ETA 风险采样、网格和成本算法沿用既有实现 |
| C→D 正式新输出 | `RoutePlanV3` + 原子 `FourLayerRoutePlanSet` |
| v2 兼容 | Schema/codec/历史结果继续可读；v2 基线入口保留作回归与 Day 7 门槛 |
| 发布策略 | 一次运行选择 v2 或 v3，不双写；v3 只在 12 条路线齐全后原子替换 |
| 实源证据 | A→B→C 工程夹具已覆盖合同；真实主航区长窗验收仍待完成 |

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

`make demo` 使用确定性合成风险帧运行 v2 三目标冒烟，将 JSON、GeoJSON 和摘要写入
`output/demo/`。它不需要 A、B 或外部网络。默认 5×5 粗网格用于快速流程检查，端点调整
可能达到百公里级，不能作为路线质量基线；更细演示应显式增加网格行列并重新设定
`--max-snap-km`。

## 责任边界

```text
A DatasetBundle v2 + RunContext v2
                │
                ▼
B CommittedRiskWindow / RiskFrame v2
                │  同一 execution lease
                ▼
C endpoint mapping → ETA sampling → time-dependent A*
                │
                ├─ v2 三目标基线（兼容/回归）
                └─ v3 四层 × 三目标 → atomic layered latest
                                      │
                                      ▼
                                  D 只读消费
```

- A 拥有环境数据获取、归一化、归档和精确回放；C 不读 A 内部数据库或缓存。
- B 提供风险、硬约束、置信度和 `environment_speed_factor`；C 不调用 B 私有函数。
- C 把环境因子应用到版本化船型并计算最终速度、ETA、成本、路线和重规划。
- C 不从 `risk_score` 或 `confidence` 重复推导物理减速。
- C 不外推风险、不把缺测当安全、不跨 run/代次/配置混合输入。
- D 只读消费已发布合同，不参与规划。

完整决策见 [决策记录](docs/DECISIONS.md)。

## 公共端点映射

正式调用方应使用 `map_corridor_endpoints(...)`，而不是自行把经纬度取整到网格：

```python
from arctic_route_planning import map_corridor_endpoints

mapping = map_corridor_endpoints(
    configuration,
    committed_window.frames[0],
    max_adjustment_km=80.0,
)
```

该入口先在共享 Corridor 声明的起点/终点 allowed region 内选择未被首帧 hard mask 阻断的
网格节点，再要求两点位于同一可通航连通分量，并严格执行调用方给定的最大调整距离。
返回的 `EndpointMapping` 显式记录请求坐标、解析坐标、调整距离、网格形状和连通分量大小。
allowed region 无网格点、无可航节点、无同分量节点、超距离或起终点解析为同一节点都会
明确失败。

## 正式 B 风险入口

B 结构化实现
`CommittedRiskSource.get_committed_window(query)` 与
`lease_committed_window(query)`。C 用完整 run/场景/航区/船型/代次、公共配置摘要、B 模型
摘要和 `as_of` 查询严格的 60 min 闭区间：

```python
from arctic_route_planning.ingress import RiskSourcePlanningIngress

ingress = RiskSourcePlanningIngress(
    b_risk_source,
    configuration=configuration,
)
prepared = ingress.prepare(service_request)

# 二选一：同一次正式运行不能双写。
result = prepared.execute()  # Day 7 v2 基线
# result = prepared.execute_four_layer()  # v3 整组
```

一次正式运行只能选择其中一个发布路径。`prepare()` 会验证窗口 content digest、commit ID、
canonical `risk_id`、完整身份、知识截止、首尾、帧数、60 min 间隔和起终点节点。
执行阶段在 B 的同一 execution lease 内重新复核 commit，并把帧经 canonical
encode→decode 形成私有快照，再构造 sampler、grid、vessel model 和 planner；租约保持到
RoutePlan 或完整 v3 整组发布结束。

同一 ingress 对同一 `(run_id, scenario_id)` 复用协调器和重规划状态。新 generation、较新
`input_revision`、取消或发布冲突会阻止旧任务迟到覆盖；不同 run 相互隔离。

## RoutePlan v3 四层语义

四层共享同一 `RunContext`、B committed window、execution lease、generation、revision、
三类配置摘要和全航程推荐线：

| 层 | 目标与关注时间窗 | 锚点规则 |
|---|---|---|
| `full_voyage` | 到业务终点；关注全航程 | 业务终点 |
| `main_corridor_24_72h` | 主通道；关注 24–72 h | 全航程推荐线在 72 h 及之前的最后一个非起点航点 |
| `rolling_0_24h` | 滚动优化；关注 0–24 h | 全航程推荐线在 24 h 及之前的最后一个非起点航点 |
| `executable_0_6h` | 可执行线；关注 0–6 h | 全航程推荐线在 6 h 及之前的最后一个非起点航点 |

若全航程在某个截止时刻前已经结束，该层以业务终点为锚点，并显式标记
`destination_reached=true`。若截止时刻及之前不存在非起点航点，整组以
`layer_not_materializable` 失败，不发布部分结果。

每层必须恰好包含 `fastest`、`low_risk`、`recommended` 三目标。`RoutePlanV3` 在 v2 路线
字段上新增 `planning_layer`、`layer_set_id`、关注时间窗、`reference_plan_id`、
`layer_goal_reached` 和 `destination_reached`。三个下层的全部路线都引用全航程推荐路线。

JSON/GeoJSON Schema 位于：

- `schemas/route-plan-v3.schema.json`
- `schemas/route-plan-v3.geojson.schema.json`
- `schemas/four-layer-route-plan-set-v3.schema.json`
- `schemas/four-layer-route-plan-set-v3.geojson.schema.json`

codec 会严格拒绝额外字段并重算路线和整组的规范内容身份。`LayeredRoutePlanLatestStore`
只有在四层、12 条路线、身份、锚点和 canonical ID 全部通过后才原子发布；任一层失败、
代次/修订过期、取消或冲突都不会留下部分 v3 结果。

## 初始规划与重规划

正式 ingress 同时提供 v2 和 v3 入口：

```python
initial_v2 = ingress.execute(service_request)
replanned_v2 = ingress.replan_if_needed(next_request, observation)

initial_v3 = ingress.execute_four_layer(service_request)
replanned_v3 = ingress.replan_four_layer_if_needed(next_request, observation)
```

重规划要求已存在同 run、同 scenario、同 generation 的已发布计划；新请求的
`input_revision` 必须严格递增，`observed_at` 和 `risk_valid_time` 必须等于新请求的
`start_time`，`risk_revision` 必须等于当前 B 窗口 commit ID。v3 接受重规划时以新完整整组
原子替换旧整组。正式冲刺在 `simulation_start + 6 h` 验证一次时间触发重规划。

## v2 兼容策略

- `cd.route-plan.v2` 的 Python 模型、Schema 和解析链继续保留，用于历史结果读取、回归和
  Day 7 的三目标稳定门槛。
- v3 通过完整门槛后成为新正式运行输出；运行器必须显式选择合同版本，禁止同一次运行
  同时发布 v2 与 v3。
- v1 仅作审计/显式迁移；它缺少完整 run/model/planner 身份，不能进入正式 latest。

## 场景、时域与 10 日冲刺

- 主线：摩尔曼斯克外海—迪克森外海，默认设计窗 168 h；运行时上限仍为 216 h。
- 测试线：特罗姆瑟外海—伊斯峡湾外部入口，默认设计窗 96 h；运行时上限仍为 144 h。
- 默认窗来自保守设计航时加至少 48 h 缓冲；C 只消费已物化并绑定 A bundle 的
  `RunContext`，不会自行延长时域。
- 10 个自然日是开发冲刺期限，不是运行时域，也不改变 216/144 h 航区上限。
- Day 7 门槛是可重复的真实 v2 三目标主线和一次重规划；Day 8–9 才在主线稳定后推广
  v3；Day 10 只做验收或阻断修复。

截至 2026-08-14，v3 工程实现已落地，但真实 12 类、168 h A bundle 尚未交付，因此不能
声称完成实源四层验收。若实源受阻，可保留正式夹具链，但必须明确标注“未完成实源验收”。

## 旧 B 制品

旧嵌套 B 样例只能经 `LegacyBArchiveAdapter` 隔离读取，并要求显式确认未知
`issue_time`/`valid_time`。它永久标记 `legacy_unverified`，不能升级为 formal，也不能用来
替代当前 12 类 A bundle 或正式 B committed window。

## 项目导航

| 位置 | 用途 |
|---|---|
| `../arctic_route_contracts/configs/` | 全系统唯一的场景、航区和船舶事实 |
| `configs/` | C 自有船舶性能、规划和重规划配置 |
| `schemas/` | B→C 与 C→D JSON Schema |
| `src/arctic_route_planning/contracts/` | 不可变 Python 合同和 RiskSource 协议 |
| `src/arctic_route_planning/endpoints.py` | allowed-region 端点映射 |
| `src/arctic_route_planning/ingress.py` | 正式 v2/v3 初始规划与重规划入口 |
| `src/arctic_route_planning/layered.py` | 四层应用编排，不改规划核心 |
| `src/arctic_route_planning/publishing/` | v2/v3 codec 与原子 latest |
| `src/arctic_route_planning/adapters/` | 确定性夹具与隔离的旧 B 适配层 |

进一步阅读：

- [B→C 合同](docs/BC_CONTRACT.md)
- [C→D 合同](docs/CD_CONTRACT.md)
- [航速与成本模型](docs/COST_MODEL.md)
- [验收清单](docs/ACCEPTANCE.md)
- [架构追踪与矛盾处理](docs/ARCHITECTURE_TRACE.md)
- [继续开发指南](工作包C项目整体认识与继续开发指南.md)

## 开发检查

```bash
make lint
make test
make check
git diff --check
```

最终交付应同时报告自动测试结果、真实/夹具来源等级、配置摘要、B commit ID、端点调整、
路线指标以及未完成的实源或科学验收，不能用“测试全绿”替代科研有效性证明。
