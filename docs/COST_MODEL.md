# 航速与成本模型 v1

## 有效航速

```text
B: environment_speed_factor ∈ (0, 1]
C: v_effective = min(vessel.max_speed, vessel.cruise_speed × factor)
```

C 在因子低于 `vessel.min_speed_factor` 或计算速度低于
`vessel.min_speed_knots` 时拒绝该边，不将速度暗中抬高到可航值。
演示船模是可替换的线性基线，不是真实船舶性能曲线。

## 等价小时成本

每条边的可解释原始分量是：

```text
travel_hours
risk_exposure_hours = travel_hours × risk_score
distance_equivalent_hours = distance_km / physical_max_speed_km_h
turn_equivalent_hours
low_confidence_hours = travel_hours × (1 - confidence)
```

`fastest`、`low_risk` 和 `recommended` 通过版本化 TOML 为这些等价小时分量设权。
风险只作为政策成本，不再作为物理减速系数。A* 启发式只使用直线距离/物理最大速度的下界，风险、转向和不确定性下界取 0。

`low_risk` 当前最小化按航行时间积分的风险成本，不等于按 `max_risk`
字典序最小化。因此某条路线可能平均风险更低而局部峰值略高；若业务规则要求峰值上限，应通过 `maximum_risk` 硬门槛显式传入，而不应仅调整名称或隐式阈值。

## 边评估

每条相邻边至少采样起点、中点和终点。采样时刻由进入边的实际 ETA 与迭代后的有效航速决定；任一采样点触发硬约束、置信度底线、用户风险上限或船速底线时，该边不可扩展。
