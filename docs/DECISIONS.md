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
5. 规划器核心对硬掩膜中的起终点严格失败，绝不暗中修改坐标。历史制品如需坐标吸附，只能由调用者显式开启、给出有限距离，并在输出中报告调整量。
6. `corridor_id` 表示数据裁剪/允许航区，`plan_id` 表示具体计划；
   `objective_mode` 与 `plan_kind` 分离。
7. `generation_id` 隔离 seek/重置前后的任务；`planning_request_id` 和
   `input_revision` 阻止同代次的较早请求迟到覆盖新结果。
8. v1 不允许等待动作；风险时域必须覆盖实际 ETA。合成演示生成足够长的时域；稀疏旧制品缺帧时明确拒绝，不把 24 h 冒充为 2–5.5 天全航程覆盖。

## 全系统共享配置的过渡方案

当前唯一可运行实现是 C，因此场景和船型先放在 `work_package_c/configs/`。
`load_configuration(config_root, ...)` 要求调用者传入配置根目录，核心代码不依赖 C 的物理路径。
配置对象具有 `schema_version`、稳定 ID、版本号和内容 SHA-256，因此未来可原样迁移到：

```text
demo_scenarios/
├── scenarios/
├── vessels/
├── planner/
└── replanning/

contracts/
├── schemas/
└── Python 合同包（或生成的 SDK）
```

迁移时保持 ID/版本/字段不变，只改变传入的 `config_root`。A/B/C/D 应使用同一份发布快照，不应各复制后独立修改。

## 当前默认值的性质

`demo_bulk_carrier_v1` 的冰级、装载状态、吃水、航速和转弯尺度都是
**演示、未经真实校准**的可替换默认值。`ScenarioDefinition.default_vessel_profile_id`
只是默认选择，不限制调用者选择其他合法船型。在获得真实船舶曲线、冰级规则、安全富余和标定证据前，不得把它改标为 `calibrated`。
