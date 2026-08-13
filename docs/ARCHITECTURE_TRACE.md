# 架构追踪与矛盾处理

实现依据（当前 `0.2.x` 合同迁移基线）：

- `北极航线预测驱动动态规划系统架构设计与实施方案 V2.0`（2026-07-15）；
- `/root/my_project/工作包C项目整体认识与继续开发指南.md`（2026-08-09 审阅版）；
- 用户冻结决策：B 给环境影响、C 算最终有效航速；场景、航区、船舶事实已迁移到独立共享包 `arctic_route_contracts`。

| 需求/矛盾 | 本版处理 | 证据位置 |
|---|---|---|
| B 尚未有正式逐小时 BC 输出 | 合同优先：`RiskSource` + 合成夹具 + 严格旧制品适配；核心不知道旧文件名 | `contracts/`, `adapters/`, `tests/contract/` |
| 旧 `route_cost_grid` 与当前风险不一致 | 适配器明确拒绝；C 从 `RiskFrame` 重新计算边成本 | `adapters/legacy_b.py` |
| 有效航速责任不清 | 正式 B 帧必须给 `(0,1]` 综合环境因子；C 应用船型底线和速度曲线。风险/置信度不映射为物理减速 | `contracts/models.py`, `cost/vessel.py` |
| 24 h BC 窗口不足以覆盖 2–5.5 天 | 规划核心必须有 ETA 全覆盖；合成演示生成全场景时域，稀疏旧制品明确失败。未实现伪外推或隐式背景场 | `risk/sampler.py`, `cli.py` |
| 长航线是主线、短航线是迁移测试线 | 共享包维护两条 Corridor；CLI 默认测试线快速冒烟，主线冻结调参后同核回归，测试线不得重调 | `arctic_route_contracts/configs/`, `tests/contract/test_configuration.py` |
| 共享配置同名但内容已被原地修改 | 服务与 CLI 统一重算 Scenario/Corridor/Vessel 内容摘要及公共 `config_digest`，并与 `RunContext` 严格比对；仅 ID/version 相同也会失败 | `context_validation.py`, `tests/integration/test_service.py`, `tests/integration/test_cli.py` |
| 场景、上下文和风险窗口可能跨越不同模拟时段 | Scenario 的起止必须与 `RunContext` 完全相等；出发和最大搜索时域不得越过上下文；CLI 对任何越界 RiskFrame 选择 fail closed，不静默裁剪 | `service.py`, `cli.py`, `tests/integration/` |
| 历史最佳估计的知识时间晚于模拟时间 | `retrospective_best_estimate` 允许晚到知识；`frozen_forecast` 仍要求 `as_of_time <= start_time`。风险帧知识截止不得晚于请求截止 | `service.py`, `risk/sampler.py` |
| 合成/旧风险的路线可能被误当正式输出 | `provenance` 进入 RiskIdentity 和 RoutePlan；formal planner 缺可验证身份或身份不匹配均拒绝 | `risk/sampler.py`, `service.py`, `contracts/models.py` |
| 旧网格的场景坐标与硬掩膜/连通分量不完全吻合 | 规划核心严格拒绝；CLI 只在显式距离上限内映射并原子写出审计文件 | `grid/regular.py`, `cli.py`, `endpoint-mapping.json` |
| seek 向过去跳转会保留未来发布的 A 静态帧 | A 跨代只保留 `issue_time <= simulation_time` 的 static；缺省时刻安全清空 | `work_package_a/src/arctic_route_data/cache.py` 及回归测试 |
| 演示需要可运行，但粗网格会产生较大端点调整 | `make demo` 优先在 5×5 网格快速冒烟，输出完整调整距离。它不是科学基线；细网格应通过 CLI 参数显式选择并单独做性能验证 | `README.md`, `cli.py`, `output/demo/` |

## 未假装完成的部分

- 正式 B 的预测/补帧、完整硬约束和速度影响尚未交付；
- 演示散货船未经冰级、POLARIS/RIO、历史航行或真船性能校准；
- 转弯半径已进入船型合同，v1 基线仅计算方向变化惩罚，还没有几何航迹平滑/可操纵性求解；
- v1 不允许等待，不包含 D* Lite、MPC 或真实海事导航规则；
- 5×5 默认网格只是快速工程冒烟，不是路线质量评估网格。
- development-only 合成/旧制品上下文由 C 的隔离工厂构造，不调用 A 正式 DatasetBundle 验收入口，也不得作为正式上下文发布。
