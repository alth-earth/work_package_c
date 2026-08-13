# 工作包 C 决策记录

> 冻结日期：2026-08-09；适用于 `0.1.x` 演示基线。
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
- 后续 C 依次编排全航程参考线、24–72 h 主通道、0–24 h 滚动优化和 0–6 h 可执行线；最后一层再标注 0–2 h 高可信、2–4 h 推荐、4–6 h 预测。
- 本版本只准备身份、时域和发布合同，不重写现有时间依赖 A*。
