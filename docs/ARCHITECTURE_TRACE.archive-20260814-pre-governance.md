> [!WARNING]
> **文档治理归档声明**
>
> - 文件角色：工作包 C 0.4.0 治理前的架构追踪与矛盾处理表。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 现行去向：同路径 `ARCHITECTURE_TRACE.md` 继续承担需求到实现证据追踪。
> - 改造原因：原文把已归档综合指南当作当前依据，并混入了冲刺日历语义。
> - 完整性：下方标记之后为归档前原正文，逐字保留；归档前 SHA-256 为 `081b9cd8dee51ba92786ec520448ebde35401e13f564512db6c78c784fa60759`。
>
> 归档正文中的旧指南路径和日历口径按历史快照保留，不作为现行导航。
>
<!-- ORIGINAL CONTENT START -->
# 架构追踪与矛盾处理

实现依据（当前 `0.4.0` 工程基线）：

- `北极航线预测驱动动态规划系统架构设计与实施方案 V2.0`；
- [工作包 C 继续开发指南](../工作包C项目整体认识与继续开发指南.md)；
- 用户冻结决策：A 提供正式运行数据，B 给环境影响，C 算最终有效航速；共享场景、航区、
  船舶和 `RunContext` 来自 `arctic_route_contracts`；开发冲刺最多 10 个自然日。

| 需求/矛盾 | 0.4.0 处理 | 证据位置 |
|---|---|---|
| 正式 B 不能复用旧 ZIP 或依赖 C 私有核心 | B 只实现公共 canonical codec、committed-window source；旧制品保持隔离 | `contracts/`, `ingress.py`, `adapters/` |
| B 窗口可能在规划期间切换 | prepare 后执行仍持有同一 source execution lease，复核 commit 并使用 canonical 私有快照 | `ingress.py` |
| 起终点取整可能落在 allowed region 外、陆地或断开的海区 | 公共 endpoint mapping 先检查 allowed region、hard mask、距离，再要求同一连通分量 | `endpoints.py`, `tests/unit/test_endpoints.py` |
| 有效航速责任不清 | B 提供 `(0,1]` 环境因子；C 应用船型速度曲线和底线。风险/置信度不重复映射为物理减速 | `contracts/models.py`, `cost/vessel.py` |
| 24 h 风险窗不足以覆盖长航程 | 正式入口要求 ETA 全覆盖的逐小时闭区间；不外推、不隐式补安全背景场 | `risk/sampler.py`, `ingress.py` |
| 四层可能被实现成四次无关运行 | 四层共享全航程推荐线、同一 request/revision、B 窗口和 execution lease；下层锚点由全航程线派生 | `contracts/layered.py`, `layered.py`, `ingress.py` |
| 下层提前到终点时 `destination_reached` 语义不清 | 截止前全航程结束则用业务终点并标真；否则仅表示到达分层锚点 | `contracts/layered.py` |
| 任一层失败仍可能留下部分结果 | 先内存生成四层 12 路线，完整校验和 canonical ID 后，由 layered latest 一次原子发布 | `layered.py`, `publishing/layered_store.py` |
| 旧任务可能迟到覆盖新四层整组 | generation/request/revision、取消状态和 canonical digest 在发布时再次校验 | `publishing/layered_store.py`, `replanning/` |
| v2 与 v3 双写会产生两个“正式最新值” | v2 保留历史读取、回归和 Day 7 门槛；新运行由上层显式选择一个合同，v3 推广后不双写 v2 | `ingress.py`, `docs/CD_CONTRACT.md` |
| 配置摘要可与实际执行参数脱离 | ingress 从完整 `PlanningConfiguration` 重算摘要并与请求核对 | `ingress.py`, `tests/integration/test_formal_ingress.py` |
| 同名共享配置内容可能原地变化 | 服务和 CLI 重算 Scenario/Corridor/Vessel 内容摘要及公共 `config_digest` | `context_validation.py`, `tests/contract/test_configuration.py` |
| 主线设计窗、航区上限和开发期限被混用 | 主线/测试线默认 168/96 h，上限仍 216/144 h；10 天只表示开发冲刺 | `../README.md`, `../工作包C项目整体认识与继续开发指南.md` |
| 工程夹具可能被误报为实源验收 | provenance 贯穿输入/输出，文档明确真实 12 类、168 h A bundle 尚未交付 | `contracts/`, `docs/ACCEPTANCE.md` |

## 未假装完成的部分

- A→B→C 公共接口和 v3 四层工程能力已实现，但主航区真实 12 类、168 h bundle 尚未验收；
- B 仍为确定性 `demo_unvalidated` 工程基线，未交付真实标签校准、Q50/Q90、概率模型或
  方向相关物理响应；
- C 的船型、操舵、转弯、净空、成本权重和重规划阈值未经真船/冰级/航次校准；
- 当前 hard mask 不能证明净水深、法律限制区或完整海事规则；
- 0–2/2–4/4–6 h 可信度分级、等待、D* Lite、MPC 和 D 消费不属于 0.4.0 已验收范围；
- 默认 5×5 合成 smoke 只验证流程，不是路线质量或性能基线。
