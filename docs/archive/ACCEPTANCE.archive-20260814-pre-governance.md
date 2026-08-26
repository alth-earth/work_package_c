> [!WARNING]
> **文档治理归档声明**
>
> - 文件角色：工作包 C 0.4.0 治理前的验收清单。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 现行去向：同路径 `ACCEPTANCE.md` 为现行技术验收真源；日历安排统一见 `../../ABC_10_DAY_SPRINT.md`。
> - 改造原因：将稳定验收闸门与 Day 7/8–9/10 日历职责分离。
> - 完整性：下方标记之后为归档前原正文，逐字保留；归档前 SHA-256 为 `d6726f9890da8cd14285dcbddd144c0b2e950e4675a81396b2861722b9473daa`。
>
> 归档正文中的日历门槛仅供历史追溯，不再承担现行排期职责。
>
<!-- ORIGINAL CONTENT START -->
# 验收清单

## 自动验收

```bash
make env-create
make lock
make sync
make check
make demo
git diff --check
```

`make check` 必须同时通过 Ruff、pytest、`uv lock --check`、`uv sync --check` 和 CLI help。
测试通过只证明工程基线，不能替代真实数据、船舶和风险模型的科研有效性验证。

## v2 基线不变量

- 非 UTC 时间、超范围/形状错误、上下文不匹配、未来信息或不完整正式来源明确拒绝。
- 正式入口只接受 canonical、原子提交、严格逐小时闭区间的 `RiskFrame v2` 窗口。
- ETA 整点、帧间、超窗口、超间隔和 hard mask 行为有手算测试；风险不外推，缺失不当零。
- 小网格 A* 与零启发式 Dijkstra 同成本；路线按 ETA 而非单帧风险变化。
- 三目标路线的 ETA 严格递增、硬约束违规为 0，距离/ETA/风险和来源 risk IDs 可复算。
- generation、request、revision、取消和切换门槛阻止旧任务覆盖 latest。

## v3 四层不变量

- `map_corridor_endpoints` 只在起终点 allowed region 内映射未阻断节点，满足显式最大距离，
  并证明两点属于同一可通航连通分量；失败不能静默换点。
- `RoutePlanV3` 与 `FourLayerRoutePlanSet` 的 Python、JSON、GeoJSON round-trip 通过四份 v3
  Schema；额外字段、非法整数/bool、身份或 canonical digest 篡改必须拒绝。
- 四层固定按全航程、24–72 h 主通道、0–24 h 滚动、0–6 h 可执行顺序出现；每层恰好
  三目标，共 12 条路线。
- 全航程到业务终点；下层终点是全航程推荐线在 72/24/6 h 截止时刻及之前的最后一个
  非起点航点。提前到达使用业务终点；无锚点时返回 `layer_not_materializable`。
- 所有层共享 run/scenario/corridor/vessel、三类摘要、provenance、generation、request、
  revision、B committed window 和一次 execution lease。
- 任一层失败、代次/修订过期、取消、ID 篡改或发布冲突时，layered latest 不留下部分
  结果；成功重规划用新完整整组原子替换旧整组。
- 正式重规划要求已有同代次计划、严格递增 `input_revision`、观测/风险时刻等于新请求
  `start_time`，且 `risk_revision` 等于当前 B 窗口 commit ID。
- v2 历史 Schema/解析保持可用；一次新运行显式选择 v2 或 v3，不双写。

## 10 个自然日门槛

1. Day 7 前：真实 12 类、168 h A bundle；B 恰好 169 帧 formal canonical 风险窗；C v2
   三目标和一次 6 h 时间触发重规划，可断网重复。
2. Day 8–9：主线稳定后，以同一运行身份验收 v3 四层 12 条路线和原子整组重规划。
3. Day 10：只做短航区验收或阻断修复，不再增加功能。

10 天是开发期限，不是预测/规划运行时域；主线/测试线 216/144 h 上限保持不变。

## 当前未完成的验收

- 截至 2026-08-14，指定主航区的真实 12 类、168 h A `DatasetBundle v2` 尚未取得；
  当前工程夹具不得表述为实源闭环。
- B 风险权重、C 船型性能和规划权重仍是 `demo_unvalidated`，未完成真实标签、冰级、
  AIS/事故数据或真船航次校准。
- 旧 B 稀疏、不规则制品只能经隔离适配器读取，永久保持 `legacy_unverified`；它不能替代
  正式 v2/v3 验收。
- 当前 hard mask 不能证明净水深、法律限制区或完整海事规则；系统不得用于真实航行。
- 等待动作、非 FIFO 标签设置、D* Lite、MPC、方向相关风浪流和 0–2/2–4/4–6 h 科学
  可信度分级仍未实现。
