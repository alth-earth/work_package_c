# 共享配置与 v2 合同迁移

1. 先运行 A，并从其不可变 DatasetBundle 创建 `arctic_route_contracts.RunContext`。
2. B 读取同一 RunContext，发布 `bc.risk-frame.v2` 和独立 `model_config_digest`。
3. C 同时读取 RunContext、共享 Scenario/Corridor/Vessel 与 C 本地算法配置；场景请求的
   航程时域不得超过 C 216 h 硬上限，也不得超过 RiskFrame 实际覆盖。
4. C 发布 `cd.route-plan.v2`，原样传播 run 与公共/B/C 三类身份；D 按全身份缓存。

兼容原则：旧 v1 只能走显式 `legacy_unverified` 适配；禁止根据同名 scenario、船型或
相近时间猜测其属于当前运行。A、B、C、D 演示前必须以同一时间窗重新运行。
