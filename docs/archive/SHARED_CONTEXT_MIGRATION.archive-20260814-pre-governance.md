> [!WARNING]
> **文档治理归档声明**
>
> - 文件角色：工作包 C 0.4.0 治理前的共享上下文与 v2/v3 迁移说明。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 现行去向：同路径 `SHARED_CONTEXT_MIGRATION.md` 继续作为稳定迁移操作说明。
> - 改造原因：保留共享 RunContext 和 v2/v3 技术顺序，移除冲刺日历职责。
> - 完整性：下方标记之后为归档前原正文，逐字保留；归档前 SHA-256 为 `67054b72ff0ae04df66e66d2c5557863d8ba72b6f740520cd9ba04f2910e3b13`。
>
> 归档正文中的 Day 标签仅供历史追溯，不再用于现行操作排期。
>
<!-- ORIGINAL CONTENT START -->
# 共享配置与 v2/v3 合同迁移

1. 先运行 A，并从其不可变 DatasetBundle 创建 `arctic_route_contracts.RunContext`。
2. B 读取同一 RunContext，发布 `bc.risk-frame.v2` 和独立 `model_config_digest`。
3. C 同时读取 RunContext、共享 Scenario/Corridor/Vessel 与 C 本地算法配置；场景请求的
   航程时域不得超过 C 216 h 硬上限，也不得超过 RiskFrame 实际覆盖。
4. Day 7 基线可发布 `cd.route-plan.v2`；v3 推广后，C 发布一个原子的
   `cd.four-layer-route-plan-set.v3`，内部含四层 × 三目标共 12 条 `cd.route-plan.v3`。
   两条路径都原样传播 run 与公共/B/C 三类身份；一次运行显式选择其中一个，不双写。
5. v3 的四层必须共享同一 B committed window 和 execution lease；任一层失败时不得发布
   部分整组。D 按完整运行身份、generation 和 `layer_set_id` 缓存。

兼容原则：v2 保留历史读取和回归，不自动升级为 v3；旧 v1 只能走显式
`legacy_unverified` 适配。禁止根据同名 scenario、船型或相近时间猜测其属于当前运行。
A、B、C、D 演示前必须以同一时间窗重新运行。

当前主线设计窗为 168 h、测试线为 96 h，航区上限仍为 216/144 h。最多 10 个自然日仅是
开发冲刺期限，不改变这些运行时域。
