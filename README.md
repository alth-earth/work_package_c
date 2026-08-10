# 北极航线工作包 C

工作包 C 是一个独立、可运行、可测试的 Python 3.13 项目：它消费 B → C
`RiskFrame` 时间序列，按船舶到达每条边的 ETA 采样风险，运行时间依赖 A*，
生成最快、低风险和综合推荐路线，并通过最新值缓存向 D 发布 `RoutePlan`。

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
  RoutePlan + CD latest
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
  --scenario demo_tromso_to_svalbard_v1 \
  --output-dir output/demo
```

可将 `--scenario` 替换为
`demo_offshore_murmansk_to_offshore_dikson_v1`。输出中会记录配置摘要、
演示警告、起终点到离散网格的有限距离映射以及三种路线指标。

### 审计现有旧 B 交付包

```bash
.mamba-env/bin/uv run arctic-route-plan legacy-inspect \
  --scenario demo_tromso_to_svalbard_v1 \
  --archive '/mnt/c/Users/asd233/Desktop/挑战杯/挑战/交付包.zip' \
  --as-of 2026-07-31T00:00:00Z \
  --allow-unverified-legacy \
  --acknowledge-valid-time
```

这两个 acknowledgement 参数不能省略。旧数据没有可证明的 `issue_time`，所以只能用于开发联调，不能进入正式历史回放。`legacy-plan` 还要求调用者显式提供
`--max-snap-km`；旧帧间隔超出配置上限时，规划会明确失败，不做伪预测。

## 当前演示配置

- 场景：特罗姆瑟—斯瓦尔巴，摩尔曼斯克外海—迪克森外海。
- 默认船型：`demo_bulk_carrier_v1`，`calibration_status=demo_unvalidated`。
- 关键航速：最低 3 kn、巡航 13.5 kn、最大 15 kn；最低环境系数 0.2。
- 默认时间桶 60 min、8 邻接、每边 3 个时空采样点、最大规划时域 168 h。
- 默认不允许等待动作。

这些配置当前位于 `configs/`，但加载器通过 `config_root` 注入路径；未来迁移到全系统共享
`demo_scenarios/contracts` 时不需要改规划核心。

## 项目导航

| 位置 | 用途 |
|---|---|
| `configs/` | 场景、演示船型、规划和重规划配置 |
| `schemas/` | B→C 与 C→D 的 JSON Schema |
| `contracts/` | 不可变 Python 合同和 `RiskSource` 协议 |
| `adapters/` | 确定性夹具与隔离的旧 B 适配层 |
| `risk/` | 严格时空 ETA 采样 |
| `grid/`, `cost/` | 规则网格、显式吸附工具、船模和等价小时成本 |
| `planners/` | 隐式时空状态上的时间依赖 A* / Dijkstra oracle |
| `replanning/` | 五类触发、防抖、迟滞、取消和修订围栏 |
| `publishing/` | RoutePlan JSON/GeoJSON 与 CD latest |
| `service.py` | 三目标求解、候选路线和原子发布编排 |

合同和算法细节：

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

新的正式 B 只需实现 `RiskSource` 并发布通过合同验证的 `RiskFrame`；
`RiskSampler`、网格、成本、规划和重规划源码不应因输入实现切换而改动。
