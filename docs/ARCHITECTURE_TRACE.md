# 架构追踪与矛盾处理

实现依据：

- `北极航线预测驱动动态规划系统架构设计与实施方案 V2.0`（2026-07-15）；
- `/root/my_project/工作包C项目整体认识与继续开发指南.md`（2026-08-09 审阅版）；
- 用户冻结决策：可修改 A、不修改 B；B 给环境影响、C 算最终有效航速；先在 C 中交付未标定演示散货船配置，后续迁移到共享目录。

| 需求/矛盾 | 本版处理 | 证据位置 |
|---|---|---|
| B 尚未有正式逐小时 BC 输出 | 合同优先：`RiskSource` + 合成夹具 + 严格旧制品适配；核心不知道旧文件名 | `contracts/`, `adapters/`, `tests/contract/` |
| 旧 `route_cost_grid` 与当前风险不一致 | 适配器明确拒绝；C 从 `RiskFrame` 重新计算边成本 | `adapters/legacy_b.py` |
| 有效航速责任不清 | 正式 B 帧必须给 `(0,1]` 综合环境因子；C 应用船型底线和速度曲线。风险/置信度不映射为物理减速 | `contracts/models.py`, `cost/vessel.py` |
| 24 h BC 窗口不足以覆盖 2–5.5 天 | 规划核心必须有 ETA 全覆盖；合成演示生成全场景时域，稀疏旧制品明确失败。未实现伪外推或隐式背景场 | `risk/sampler.py`, `cli.py` |
| 长航线是团队当前优先，短航线更适合快速联调 | 同时交付两个配置；CLI 默认短航线做快速冒烟，长航线使用同一核心完成回归 | `configs/scenarios/`, `tests/contract/test_configuration.py` |
| 真实场景/船型应全系统共享，但当前无共享包 | 先放 C 且使用路径注入、Schema、稳定 ID/版本与 SHA-256；默认船型只是默认，可显式替换 | `config.py`, `configs/`, `docs/DECISIONS.md` |
| 旧网格的场景坐标与硬掩膜/连通分量不完全吻合 | 规划核心严格拒绝；CLI 只在显式距离上限内映射并原子写出审计文件 | `grid/regular.py`, `cli.py`, `endpoint-mapping.json` |
| seek 向过去跳转会保留未来发布的 A 静态帧 | A 跨代只保留 `issue_time <= simulation_time` 的 static；缺省时刻安全清空 | `work_package_a/src/arctic_route_data/cache.py` 及回归测试 |
| 演示需要可运行，但粗网格会产生较大端点调整 | `make demo` 优先在 5×5 网格快速冒烟，输出完整调整距离。它不是科学基线；细网格应通过 CLI 参数显式选择并单独做性能验证 | `README.md`, `cli.py`, `output/demo/` |

## 未假装完成的部分

- 正式 B 的预测/补帧、完整硬约束和速度影响尚未交付；
- 演示散货船未经冰级、POLARIS/RIO、历史航行或真船性能校准；
- 转弯半径已进入船型合同，v1 基线仅计算方向变化惩罚，还没有几何航迹平滑/可操纵性求解；
- v1 不允许等待，不包含 D* Lite、MPC 或真实海事导航规则；
- 5×5 默认网格只是快速工程冒烟，不是路线质量评估网格。

