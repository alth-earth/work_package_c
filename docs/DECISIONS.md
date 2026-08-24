> [!NOTE]
> **文档治理声明**
> - 文件角色：工作包 C 当前稳定架构、挑战杯定位和参数治理决策真源。
> - 改造时间：2026-08-15（Asia/Shanghai）。
> - 原文件去向：[DECISIONS_归档_20260815.md](archive/DECISIONS_归档_20260815.md)。
> - 改造原因：补入项目负责人已定的工程演示优先、参数来源和权责口径。

# 工作包 C 决策记录

## 项目与权责

1. 本项目是挑战杯演示；工程链稳定、风险与路线大体合理即为成功。
2. 科学校准、真船适航和业务化不是验收项；所有结果禁止用于真实导航。
3. 项目负责人拥有 A、B、C 的全部决策权，不要求子项目或跨专业人员签字。
4. 五类专业接口保留；必需参数优先公开典型值、次选透明拟合、最后用演示默认值。

## 包与数据边界

5. A 下载、标准化并持久化环境数据；B 生成风险；C 生成航线；D 只读展示。
6. C 不导入 A/B 私有实现，只经 contracts 和版本化公共接口集成。
7. 稳定演示默认读取预置本地制品；历史回放继续执行未来信息门禁。

## 时间、身份与来源

8. issue/valid/ingest/as-of/generated 五类时间不可互换。
9. generation 隔离 seek/reset；request/revision 隔离同代次旧任务。
10. `formal` 和 `demo_unvalidated` 可同时成立；前者不推出 scientific/calibrated。

## 风险、船速与规划

11. B 输出环境速度因子，C 组合演示散货船参数得到最终速度，不重复折减。
12. C 按候选边 ETA 读取对应风险，不外推、不把缺测当安全。
13. 正式端点只在 allowed region 内有界映射并留证。
14. v3 四层 × 三目标（12 路线整组）是挑战杯演示主线（2026-08-15 确认）；v2 三目标保留为
    强制后备；任何主线运行必须完整原子发布，不能发布不完整 v3。
15. 当前无等待动作；引入等待或非 FIFO 成本时必须升级算法和合同。

补充技术决策（源自：[DECISIONS_归档_20260815.md](archive/DECISIONS_归档_20260815.md)）：

- 公共 `config_digest` 绑定共享 Scenario/Corridor/Vessel 和 A DatasetBundle；B 发布
  `model_config_digest`；C 发布 `planner_config_digest`；`generation_id` 不进入 digest；
- 软风险按时间线性插值；hard mask 取逻辑 OR；confidence 和环境速度因子取保守最小值；
  `risk_level` 由插值后 risk 重算；
- 正式 C ingress 只消费 canonical、完整逐小时闭区间的 `CommittedRiskWindow`；普通
  `get_window()` 结果不得冒充 commit；
- prepare 只生成可审计输入；execute 必须持有 source execution lease、复核 commit，并从
  canonical 私有快照重建规划组件；
- 当前使用 8 邻接规则网格、Haversine 距离、边内采样和等价小时成本；A* 启发式只使用可证明
  下界；
- 正式调用方只能在 allowed region 内选择首帧 hard mask 可通航节点，同时满足显式距离上限
  和连通性；规划核心不暗中改坐标；
- C 检查最低安全因子和操舵速度后计算最终航速、边耗时和 ETA。

## 航线与船型

16. 先摩尔曼斯克外海—迪克森外海，后迁移特罗姆瑟外海—伊斯峡湾外部入口。
17. 朗伊尔城只作为 AIS 航次参考；峡湾内部不进入气象路线优化评价。
18. 当前使用演示散货船参数集；Ice Class 1A 不等于 PC6，未补齐参数不写成真船结果。

## 延期项

19. 科学校准、净水深/法规 hard mask、等待、D* Lite/LPA*/MPC 保留接口，不阻塞比赛。
20. 实验 B 仅在真实主线稳定后做 I001、lock、Mamba 和 `make check`，不接入 C。
21. 其余风险与决策本轮只记录，不自动扩展开发范围。

运行时事实与延期边界（源自：[DECISIONS_归档_20260815.md](archive/DECISIONS_归档_20260815.md)）：

- 共享 `nordic_odyssey_reference_v1` 是事实参考，不等于 C 性能模型已校准；C 的航速、操舵、
  转弯、净空和阈值仍为 `demo_unvalidated`；
- 水深接口保留，但核心 bathymetry 硬约束当前关闭；
- 主线/测试线的设计窗和 216/144 h 航区上限是运行时事实；开发日程不得改变它们；
- 真实数据验收、真船校准、D 消费、0–2/2–4/4–6 h 科学可信度分级、等待、D* Lite 和 MPC
  均不属于 0.4.0 已验收范围。

系统级完整依据见 [ARCTIC_ROUTE_SYSTEM.md](../../ARCTIC_ROUTE_SYSTEM.md)。
