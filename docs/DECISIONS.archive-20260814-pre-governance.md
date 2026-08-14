> [!WARNING]
> **文档治理归档声明**
>
> - 文件角色：工作包 C 0.4.0 治理前的决策记录。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 现行去向：同路径 `DECISIONS.md` 继续作为 C 稳定决策真源，架构摘要见 `ARCHITECTURE_AND_DECISIONS.md`。
> - 改造原因：将长期架构决策与一次性冲刺日历分离，并明确 formal 来源与科学校准的双维语义。
> - 完整性：下方标记之后为归档前原正文，逐字保留；归档前 SHA-256 为 `41d7910a631daa3c98eedc95263781ff486e421545aa766b945cb96f26fba4bc`。
>
> 归档正文中的 Day 排期按历史快照保留，当前日程以顶层冲刺文档为准。
>
<!-- ORIGINAL CONTENT START -->
# 工作包 C 决策记录

> 更新日期：2026-08-14；适用于 `0.4.0` BC v2 / CD v3 工程基线。
> 本系统仅用于科研演示，不得用于真实航行安全决策。

## 已冻结的边界

1. A 发布规范环境帧；B 完成环境时间处理、预测和风险融合；C 不直接读 A 的数据库或缓存。
2. B 通过 `RiskFrame` 提供风险、硬约束、置信度和
   `environment_speed_factor`。该因子对正式帧必需；B 不发布最终船速。
3. C 把 B 的环境影响系数应用到版本化船型的静水巡航速度，检查最低安全系数和操舵速度，得到最终有效航速。C 不再从 `risk_score`
   或 `confidence` 推导速度损失，避免重复计权。
4. C 的 ETA 采样只在两帧已发布、合同兼容的风险之间进行；它不是 B 的预测或补帧。不外推，不跨场景、代次、船型、配置、模型或网格插值。
   `provenance` 也是窗口身份的一部分，不得把合成或旧帧与正式帧混合。
5. 规划器核心对硬掩膜中的起终点严格失败，绝不暗中修改坐标。历史制品如需坐标吸附，只能由调用者显式开启、给出有限距离，并在输出中报告调整量。
6. `corridor_id` 表示数据裁剪/允许航区，`plan_id` 表示具体计划；
   `objective_mode` 与 `plan_kind` 分离。
7. `generation_id` 隔离 seek/重置前后的任务；`planning_request_id` 和
   `input_revision` 阻止同代次的较早请求迟到覆盖新结果。
8. v1 不允许等待动作；风险时域必须覆盖实际 ETA。合成演示生成足够长的时域；稀疏旧制品缺帧时明确拒绝，不把 24 h 冒充为 2–5.5 天全航程覆盖。
9. `RoutePlan v2` 显式携带输入风险的 `provenance`；只有规划器提供与请求一致的
   `risk_identity` 时才能发布 `formal`，无身份的开发 planner 只能输出明示的非正式来源。
10. v2 的 `risk_level` 不是第二套模型输出：有限 `risk_score` 固定按
    `min(5, floor(risk_score*5)+1)` 派生，未知风险固定为保守等级 5。
11. 正式 `risk_id` 为排除 `risk_id` 本身、包含其余全部规范传输字段的 SHA-256：
    `risk-sha256-<64hex>`；来源引用先按规范 JSON 排序，网格和 payload 数组顺序有意义。
12. 正式 C 入口只消费显式原子提交的 `CommittedRiskWindow`。窗口绑定完整身份、知识截止、
    严格逐小时闭区间、帧数和完整帧内容；普通 `get_window()` 结果不能冒充已提交窗口。
13. `prepare` 建立可审计输入；`execute` 必须持有 source 的 execution lease，复核同一
    commit，并把帧规范编码/解码为私有快照后重建规划组件，让 generation 围栏和内容身份
    覆盖规划与最终发布。同一 ingress 对同一 `(run_id, scenario_id)` 复用一个
    `PlanningCoordinator`，不能为每个请求创建相互隔离的迟到发布围栏；不同 run 必须隔离，
    不能互相取消。
14. `PreparedRiskPlanning` 不公开可绕过租约执行的 prepare 阶段 `PlanningService`；唯一正式
    执行入口是 `.execute()`。
15. 正式 ingress 接收完整 `PlanningConfiguration`，从实际 vessel model、planner 与
    replanning 对象重算 `planner_config_digest`，并用同一重规划配置构造运行策略；不接受
    与执行对象脱离的摘要声明。
16. 正式端点映射只能在 Corridor 声明的 allowed region 内选择首帧 hard mask 可通航节点，
    同时满足显式距离上限和起终点连通性；规划核心仍不暗中修改坐标。
17. v3 四层不是四次独立运行：它们共享同一 B committed window/execution lease、运行身份、
    全航程推荐线和发布 token。每层恰好三目标，完整 12 条路线才允许原子发布。
18. 主通道、滚动和可执行层分别以全航程推荐线在 72/24/6 h 及之前的最后一个非起点航点
    为目标；航程提前结束使用业务终点，无非起点锚点则 `layer_not_materializable`。
19. v2 保留历史读取、回归和 Day 7 门槛；v3 推广后一次正式运行只选一个输出合同，不双写。
20. 10 个自然日是开发期限，不是运行时域；两条航区 216/144 h 上限保持不变。

## 全系统共享配置

场景、航区、船舶事实和 `RunContext` 由相邻独立包
`arctic_route_contracts` 提供，A/B/C/D 读取同一份版本化配置。C 通过兼容
re-export 暴露共享类型，但不复制其实现。

```text
arctic_route_contracts/configs/{corridors,scenarios,vessels}
work_package_c/configs/{vessel_models,planner,replanning}
```

公共 `config_digest` 只绑定共享场景（含航区事实）、共享船型和 A 的精确
DatasetBundle；不包含 B/C 算法配置。B 发布 `model_config_digest`，C 发布
`planner_config_digest`。`generation_id` 是模拟时钟 seek 围栏，不参与任何 digest。

## 当前默认值的性质

共享 `nordic_odyssey_reference_v1` 是公开资料事实基线，不等于已校准的性能模型。
C 的经济速度、最低操舵速度、转弯尺度、净空和阈值仍是
**演示、未经真实校准**的可替换算法参数；水深接口保留但核心硬约束关闭。

## 主线、测试线与四层规划

- 主线为摩尔曼斯克外海—迪克森外海；测试线为特罗姆瑟外海—伊斯峡湾外部入口。朗伊尔城仅是 AIS 参考点。
- 先在主线冻结 B 风险参数与 C 规划目标权重，再把同一套算法与参数迁移到测试线；测试线重新调参必须作为独立实验披露。
- C 0.4.0 已编排全航程参考线、24–72 h 主通道、0–24 h 滚动优化和 0–6 h 可执行线；
  四层复用现有时间依赖 A*，没有修改风险采样、网格和成本算法。
- 0–2 h 高可信、2–4 h 推荐、4–6 h 预测的科学可信度分级仍延期，不能从当前 0–6 h
  `focus` 字段推断已经实现概率校准。
- 工程四层实现不等于真实数据验收；当前仍等待主航区真实 12 类、168 h A bundle。
- 当前冲刺先在 Day 7 冻结 v2 三目标主线，Day 8–9 再推广 v3，Day 10 只做验收或阻断修复。
