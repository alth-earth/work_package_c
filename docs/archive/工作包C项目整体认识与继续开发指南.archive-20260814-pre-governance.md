> [!WARNING]
> **文档治理归档声明**
>
> - 文件角色：2026-08-09 至 2026-08-14 期间形成的 C 接手说明、A/B 历史审计、架构解释和开发计划混合文档。
> - 改造时间：2026-08-14（Asia/Shanghai）。
> - 现行去向：`work_package_c_handoff.md`、`docs/PROJECT_OVERVIEW.md`、`docs/STATUS_AND_TODO.md`、`docs/ARCHITECTURE_AND_DECISIONS.md`、`docs/DEVELOPMENT_GUIDE.md`。
> - 改造原因：原文混合当前事实与历史 v1/B 原型状态，存在旧版本号、旧测试数和已完成事项仍列为待办等冲突。
> - 完整性：下方标记之后为归档前原正文，逐字保留；归档前 SHA-256 为 `5551e5d62bce303f28d0603f732d8ce1b736afeddaafdf301339b5b828d7e208`。
>
> ⚠️ 与现状不符：归档正文中的 2026-08-09/10 A/B 状态、BC/CD v1 字段、74 tests、“B 尚未工程化”及“四层尚待实现”等表述仅供历史追溯，不得作为当前实现、接口或进度依据。
>
<!-- ORIGINAL CONTENT START -->
# 北极航线预测驱动动态规划系统：工作包 C 项目整体认识与继续开发指南

> 文档定位：面向工作包 C 的接手开发者，先建立完整项目心智模型，再说明当前真实进度、A/B/C/D 交付边界、可用接口、已知矛盾，以及 C 下一步如何安全开发。  
> 首次审阅日期：2026-08-09（Asia/Shanghai）；最近实施状态更新：2026-08-14
> 核心依据：《北极航线预测驱动动态规划系统架构设计与实施方案 V2.0》  
> 重要声明：本项目当前是科研演示系统，不是业务化海上导航系统，不能直接用于真实航行安全决策。

---

## 0. 如何阅读这份文档

如果只想迅速接手，建议按以下顺序阅读：

1. 先看本节“0.1 当前状态”，了解现在到底做到哪里；
2. 再看“1. 执行摘要”，并把“3. 项目整体心智模型”和“5. 当前进度矩阵”
   作为带有 **[历史审计]** 标签的演进背景；
3. 重点阅读工作包 A、工作包 B 和三段接口契约；
4. 最后按“工作包 C 阶段化开发路线”核对已完成项；C9 工程夹具联调已经完成，当前从
   指定共享场景的实源 A → B → C 验收继续。

本文用下列标签区分信息性质，避免把“设计建议”误写成“已完成”：

| 标签 | 含义 |
|---|---|
| **[架构规定]** | V2.0 架构要求，是目标系统的主要设计口径 |
| **[用户补充]** | 本次任务中用户明确说明的现状或目标 |
| **[代码确认]** | 已从当前源码直接确认 |
| **[产物确认]** | 已从交付的 NetCDF、图片、CSV 或压缩包直接确认 |
| **[当前复验]** | 在本机当前快照中实际运行或诊断得到 |
| **[建议契约]** | 已写进设计/接手文档，但还没有正式代码实现 |
| **[建议]** | 本文为便于继续开发提出的实施建议，不代表团队已经决定 |
| **[待确认]** | 资料矛盾或参数缺失，必须由相关负责人明确 |
| **[已冻结]** | 已在当前 A/C 代码、配置、Schema 和测试中落地的口径 |
| **[历史审计]** | 2026-08-09 首次审计时的原始发现；保留用于追溯，但不再代表当前状态 |

### 0.1 2026-08-14 当前状态（优先于下文历史叙述）

当前有效工程已经变为：

```text
arctic_route_contracts 0.3.0 → A 0.4.2 → B 0.2.0 → C 0.4.0 → D 延期
                              ▲              ▲              ▲
                              └── orchestrator 0.1.0 ───────┘
```

- [`work_package_b/`](../work_package_b/) 已建立独立 Mamba + uv 工程，交付确定性的
  `demo_unvalidated` 逐小时风险基线、canonical `RiskFrame`、持久 committed window、
  generation fence/lease 和 C 正式入口；旧 ZIP 继续永久隔离为 `legacy_unverified`。
- A 现在向 B 交付深拷贝 `PreparedWindow` 与逐 data ID payload attestation，并提供公共
  exact-bundle resolver；B 在输入信封和 build 前独立重算，不能只凭 manifest 元数据相信
  live payload。
- C 通过公共 `RiskSourcePlanningIngress` 消费 B committed window；入口接收完整
  `PlanningConfiguration` 并从实际执行对象重算摘要，execution lease 覆盖规划和发布，执行时
  从 canonical 私有快照重建规划输入。同 run 的修订共享 coordinator，不同 run 隔离。
- C 0.4.0 已新增 allowed-region 端点映射、`RoutePlanV3`、原子
  `FourLayerRoutePlanSet`、四层 × 三目标编排和正式 v2/v3 重规划入口；A*、风险采样、网格
  和成本核心未改。一次运行必须显式选择 v2 或 v3，不能双写。
- 工程夹具已验证 12 类、96/168/216 h（97/169/217 帧）、两走廊相同
  `model_config_digest`、A→B→C formal RoutePlan；归档夹具还验证 A 发布→重启→公共精确恢复
  →B，以及恢复后 payload 篡改拒绝。
- 上述是**工程合同夹具证据**。当前真实 A 长窗仍是历史 v1、旧 corridor、9 类，不能创建
  当前正式 RunContext；真实完整场景的 A→B→C 联调、风险/船舶校准和 D 仍未完成。

除明确标为 2026-08-14 当前状态的段落外，下文大量 B“尚未实现”、旧版本号和旧测试数是
2026-08-09/10 的历史审计证据，保留用于追溯，不再代表当前工作区。当前入口依次为
[`ARCTIC_ROUTE_SYSTEM.md`](../ARCTIC_ROUTE_SYSTEM.md)、
[`work_package_b/README.md`](../work_package_b/README.md) 和各包当前 README/合同。

### 0.2 2026-08-10 修订说明（历史）

本次修订**保留原文件、原审计证据和历史问题描述**，同时加入当前实施结果。阅读时遵循：

1. 本节、第 1、5、6、7、12、14～16 章的 2026-08-10 状态优先于旧的“尚未实现”表述；
2. 第 8、9 章的原始审计过程继续保留，已修复项会明确标记“已解决”；
3. 当前 A 提交为 `2d38819`，C 提交为 `67f0322`，两个工作树均干净；
4. A 的向过去 seek/static 问题已修复，最新 `make check` 为 32 tests passed；
5. C 已成为独立、可运行、可测试的 Python 3.13 + Mamba + uv 项目，最新 `make check` 为 74 tests passed；
6. 本条是历史状态：当时 B 没有被修改；当前实现位于 [`work_package_b/`](../work_package_b/)，
   原交接材料保留在 [`work_package_b_handoff/`](../work_package_b_handoff/)。

当前最小闭环状态是：

```text
A 正式实现 ──► B 正式工程基线 ──► C 正式边界/核心 ──► D 尚未实现
                    │
                    ├── 当前模型仍为 demo_unvalidated
                    └── 旧制品只经隔离适配器，不能冒充正式 BC
```

---

## 1. 执行摘要：现在到底到哪了

### 1.1 一句话结论

这是一个“**离线准备环境数据，但按模拟时钟在线执行风险计算、动态规划和渲染**”的科研演示系统。工作包 A、B、C 现均已形成独立工程；B 已生成逐小时、可追溯的正式形状
`RiskFrame` 并通过 C committed ingress，但风险规则仍未经科学校准。A→B→C 工程合同闭环
已由可复核夹具证明；C 的 v3 四层工程合同也已实现。真实完整场景闭环、科学校准和 D
仍未完成。

### 1.2 最重要的五个判断

1. **工作包 A 不是简单下载脚本。**它已经承担采集适配、发布时间取证、逐时次拆帧、规范化、质检、归档、manifest、模拟时钟和 AB 缓存等完整数据入口职责。
2. **旧 B ZIP 仍只是静态/批处理原型；当前 B 工程不是它的包装。**新 B 已实现确定性逐小时
   连续化和正式 BC store，但没有训练权重、Q50/Q90 或真实标签校准，必须保持
   `demo_unvalidated`。
3. **两份 `route_cost_grid` 只能作为历史原型样例。**它们内嵌的综合风险与同交付包当前的综合风险文件不一致，生成源码也未随包交付；其 `passable_mask` 实际只等于广播后的 `sea_mask`，不能代替正式 `hard_mask`。
4. **C 已按“合同优先、夹具驱动”完成 0.4.0 工程基线。**正式 `RiskFrame`、v2/v3
   `RoutePlan`、时空采样、三目标时间依赖 A*、四层整组、重规划和竞态围栏已经由测试固定；
   旧文件结构和 `route_cost_grid` 被隔离在适配层之外。
5. **当前最高优先级是实际完成真实源统一运行。**先按共享场景重采 A 的完整 v2 bundle，
   再用根级运行器验收 B 的 169 帧、C v2 三目标、`+6 h` 重规划和 v3 四层 12 路线；随后
   才做风险/船舶科学校准。C 的采样、图、成本和规划核心未因四层编排修改。

### 1.3 各工作包的当前定位

| 工作包 | 目标职责 | 当前状态摘要 | C 应如何对待 |
|---|---|---|---|
| A | 获取/接收、规范化、索引、回放、AB 发布 | 主体能力已实现并工程化；向过去 seek 的 static 未来泄漏缺口已修复，仍有科学 QC/真实数据等限制 | B 通过稳定 AB 契约消费；C 不直接读取 A 内部数据库或缓存 |
| B | 时间处理、连续化、风险分量、融合、BC 发布 | v0.2.0 已配置化全部数值规则并支持 full/suffix commit；科学规则仍 `demo_unvalidated` | 只经公共 A 输入和 C 合同；旧 ZIP 仅作历史证据 |
| C | 时间依赖航线规划、候选路线、四层整组、滚动重规划、CD 发布 | v0.4.0 已实现 canonical BC ingress、v2 基线、v3 四层和原子发布；实源/科学验收待完成 | 通过 committed source 接 B；一次运行显式选择 v2 或 v3 |
| D | 地图、风险、路线、船位和指标渲染 | 当前十日冲刺明确延期 | 以后只读消费稳定合同，不与 C 内部逻辑耦合 |

---

## 2. 资料范围、证据优先级与审计口径

### 2.1 本次使用的资料

| 资料 | 位置/版本 | 用途 |
|---|---|---|
| 架构设计压缩包 | `/mnt/c/Users/asd233/Desktop/挑战杯/挑战/北极航线预测驱动动态规划系统架构设计.zip` | 目标架构、模块边界、算法路线、缓存和接口的最高层依据 |
| 架构正文 | 压缩包内 `a2dabb9c40fc421fb317303282202f09_md_full.md`，文档版本 V2.0，日期 2026-07-15 | 逐章追踪架构要求 |
| 工作包 B 交付包 | `/mnt/c/Users/asd233/Desktop/挑战杯/挑战/交付包.zip` | 审计 B 当前代码、说明、测试和结果制品 |
| 工作包 A 项目 | [`work_package_a/`](../work_package_a/)；版本 `0.2.0`；当前提交 `2d38819` | 2026-08-09 历史审计时的实现快照 |
| 工作包 C 项目 | [`work_package_c/`](./)；当前提交 `67f0322` | 2026-08-10 历史审计时的实现快照 |
| B 完善交接包 | [`work_package_b_handoff/`](../work_package_b_handoff/) | 提供给 B 负责人的矛盾、字段、任务、AI 约束和验收文档 |
| 用户补充说明 | 本次对话 | 确认 B 尚未开发完成；完整 B 还应包含类似插帧的预测模型并输出逐小时风险序列；场景初步考虑两条航线和散货船 |

压缩包完整性检查均通过。为保证资料快照可追踪：

```text
架构设计.zip  SHA-256  55cb00b6aba300df9635f2b2bb3f49fc97987933508b756142303d3acd1ec775
交付包.zip    SHA-256  de171f0ac40d2f0102a01da015f32af91d989d97745ce0d9431d2704a333ff90
```

### 2.2 发生矛盾时如何裁决

本文采用“双轴裁决”，而不是简单规定某个来源永远优先：

- 判断“**目标应该是什么**”时：用户明确补充和 V2.0 架构优先；
- 判断“**现在实际有什么**”时：当前源码、当前产物和本机复验优先于说明文档；
- 工作包 A 的 `BCD_HANDOFF.md` 对 BC/CD 的内容明确属于建议契约，不能写成已经实现；
- 工作包 B 的说明如果与代码、CSV 或 NetCDF 不一致，正文同时保留两者并列为冲突；
- 无法从资料证明的字段不猜测，例如不能从文件修改时间伪造 `issue_time`，也不能把普通 `sea_mask` 改名后冒充完整 `hard_mask`。

### 2.3 当前资料不能证明什么

当前资料不能证明：

- 风险权重或阈值已经过正式专家评审、POLARIS/RIO 校准或历史数据标定；
- 当前风险模型适用于某一具体冰级、吃水和装载状态的散货船；
- “交付的风险文件在历史回放中不存在未来信息泄漏”这一结论；文件缺少完整 `issue_time/as_of_time/source_summary`，无法形成所需证据链；
- 现有 `route_cost_grid` 能被当前交付代码重复生成；
- 当前路线场景能直接用于真实航行安全决策。

---

## 3. 项目整体心智模型

### 3.1 项目的真正目标

系统不是“给一张风险图片，计算一次最短路”。它要演示一条完整闭环：

```text
网站/API/历史文件
        │
        ▼
独立下载与预处理 / 工作包 A 数据入口
        │  StandardDataFrame，带 issue_time / valid_time / generation_id
        ▼
AB 缓存
        │
        ▼
工作包 B：时间处理 + 预测/插帧 + 风险分量 + 融合
        │  RiskFrame 时间序列，默认 1 h 一帧
        ▼
BC 缓存
        │
        ▼
工作包 C：按预计到达时刻采样风险 + 时间依赖规划 + 滚动重规划
        │  RoutePlan + 候选路线 + 指标
        ▼
CD 最新值缓存
        │
        ▼
工作包 D：固定帧率渲染路线、船位、风险和指标
```

其中“离线数据”只说明当前不依赖现场网络，**不代表可以预先把最终风险图和航线固化好**。B 和 C 在演示运行时仍要按模拟时钟执行。

### 3.2 项目边界

**本阶段要做：**

- 在离线资料条件下复现“数据何时可见—风险如何变化—路线为何更新”；
- B 默认生成未来至少 24 h 的逐小时风险序列；
- C 根据船舶到达每个区域的预计时刻使用相应风险，而非用当前风险图覆盖全航程；
- 展示最短/最快、低风险、综合推荐和动态重规划等方案及指标；
- 保留未来接入实时源、缩短风险输出间隔和升级算法的接口。

**本阶段不做或不能承诺：**

- 真实船舶的业务化导航与安全决策；
- 在没有冰级、吃水和可靠性能模型时声称路线具备工程航行可行性；
- 将所有变量统一线性外推；
- 让 D 直接调用 B/C 内部函数，或让 UI 等待规划计算；
- 在历史回放中提前使用模拟时刻之后才发布的数据。

### 3.3 两种运行模式

| 模式 | 数据规则 | 主要用途 |
|---|---|---|
| 历史回放/验证 | 严格要求输入 `issue_time <= simulation_time` | 检查未来信息泄漏，计算可比较的预测/规划指标 |
| 稳定演示 | 数据集预先放到本地，但仍按模拟时钟、版本和代次运行 | 无网络、可重复地展示风险变化和至少一次重规划 |

### 3.4 三类时间绝不能混用

| 时间 | 白话含义 | 主要用途 |
|---|---|---|
| `issue_time` | 从什么时候起系统才被允许知道这份数据 | 防未来信息泄漏；查询门禁 |
| `valid_time` | 数据描述的环境/风险状态发生在什么时候 | B 的时间轴；C 对应船舶预计通过时刻 |
| `ingest_time` | A 实际何时收到、登记数据 | 运维延迟和审计，不替代前两者 |
| `as_of_time` | 本次 B/C 计算允许知道的数据截止时刻 | 证明这次结果使用了哪一时刻可见的信息 |
| `generated_at` | 算法真实完成计算的墙钟时间 | 性能与审计，不参与模型时间推进 |

必须始终保持：

```text
input.issue_time <= as_of_time <= 当前允许使用的 simulation_time
```

`valid_time` 可以晚于当前模拟时刻，因为已发布的预报本来就可能描述未来；关键是该预报的 `issue_time` 必须已经可见。

### 3.5 `scenario_id` 与 `generation_id`

- `scenario_id` 标识一次完整演示或试验，例如某条航线、船型、参数和数据快照的组合；
- `generation_id` 标识模拟时钟的一次连续代次；每次跳转/重置都递增；
- A、B、C、D 都不得发布、消费或显示旧代次迟到结果；
- `schema_version` 描述接口结构，`model_version` 描述算法和参数，二者不能互换。

### 3.6 三组缓存的不同语义

| 缓存 | 内容 | 组织/保留原则 | 关键目的 |
|---|---|---|---|
| AB | 标准化环境帧 | 按类型/变量/时间分区；静态、缓变、动态、事件采用不同策略 | 为 B 提供可追溯、无未来泄漏的输入 |
| BC | 风险帧序列 | 按 `valid_time` 排序的滑动窗口，至少覆盖当前规划窗口 | 让 C 读取未来风险随时间的变化 |
| CD | 路线和指标 | 最新值覆盖，保留当前、上一版和候选路线 | D 永不因 C 计算变慢而阻塞 |

---

## 4. 航线和船型场景

### 4.1 当前两条航线

| `route_id` | 当前配置范围 | 起点 | 终点 | 建议用途 |
|---|---|---|---|---|
| `tromso_to_svalbard` | 经度 10–22°，纬度 68.5–79° | 18.95°E, 69.65°N | 15.63°E, 78.22°N | 约 2–3 天，适合首个端到端联调、演示多次更新 |
| `offshore_murmansk_to_offshore_dikson` | 经度 30–85°，纬度 67.5–75° | 33.05°E, 69.45°N | 80.55°E, 73.75°N | 约 3.5–5.5 天，适合规模、性能和更长滚动规划验证 |

**[用户补充]** 当前设想的顺序是先完成摩尔曼斯克外海—迪克森外海，再把同一套风险与规划程序迁移到特罗姆瑟—斯瓦尔巴。  
**[架构规定]** V2.0 同时指出特罗姆瑟—斯瓦尔巴航程较短，适合快速联调和演示。**[已冻结]** 当前 C 同时交付两套场景配置：默认快速 smoke 使用短航线，长航线也执行同一套合成回归；这不改变团队可优先完善长航线正式数据和科学模型的产品顺序。

### 4.2 当前船型口径

**[用户补充]** 当前倾向选择散货船，原因是散货船在北极货运场景中常见、港口和航线相对固定，适合研究航路、海况和冰况的联合优化。

但“散货船”还不足以形成可计算的船舶模型。以下参数必须补齐：

| 参数 | 为什么影响 C/B |
|---|---|
| 冰级/Polar Class 或等效能力 | 决定哪些冰况属于硬不可通行，以及冰区航速折减 |
| 满载/压载状态、吃水、船宽和安全富余水深 | 决定浅水硬约束和搁浅风险，不能只用固定 10/50 m 阈值 |
| 静水经济航速和最大/最小航速 | 决定边通过时间、ETA 和时间依赖风险采样 |
| 风、浪、流相对航向的性能曲线 | 决定有效航速和方向性风险；当前 B 原型只使用部分矢量的模长 |
| 转向能力、最小转弯尺度 | 决定邻接方式和转向惩罚是否合理 |
| 等待/减速策略是否允许 | 影响时间依赖最短路是否需要“等待”动作 |

在这些参数确定前，本文建议使用明确标注的“演示散货船参数集”，不能将其称为真实船型校准结果。

---

## 5. 当前进度矩阵

状态定义：✅ 已实现并有当前代码；🟡 有原型/部分制品；📐 只有建议契约或设计；❌ 当前资料未交付；⚠️ 已发现缺口。

| 能力 | A | B | C | D |
|---|---:|---:|---:|---:|
| 多源数据入口/旧下载器适配 | ✅ | — | — | — |
| `issue_time/valid_time/ingest_time` | ✅ | ❌ 未传播到当前产物 | ✅ 消费 `as_of/valid_time` 并检查来源 | 应展示时间/版本 |
| 多时次拆帧与规范化 | ✅ | 使用旧清洗变量名 | — | — |
| manifest / 可追溯索引 | ✅ | ❌ | ✅ 传播风险/路线来源身份 | ❌ |
| 模拟时钟和 `generation_id` | ✅ | 📐 | ✅ 代次、请求 ID 和 revision 围栏 | 📐 |
| AB 分区有界缓存 | ✅ | — | — | — |
| 单因素风险 | — | 🟡 线性规则原型 | — | 可显示 |
| 综合风险融合 | — | 🟡 权重 + 主导因子修正 | — | 可显示 |
| 逐小时预测/插帧 | — | ❌ 尚未交付 | 依赖此时间序列 | 可选择时刻显示 |
| `RiskFrame` / BC 缓存 | — | ❌ 正式生产者未实现 | ✅ 合同、Schema、内存源和旧适配 | 可按需读取 |
| 时间依赖 A* | — | — | ✅ 时空状态、Dijkstra 对照 | — |
| 多模式路线和滚动重规划 | — | — | ✅ 三目标、五类触发、防抖/取消 | 📐 展示 |
| `RoutePlan` / CD 缓存 | — | — | ✅ JSON/GeoJSON/latest | 依赖 |
| UI/动画/交互 | — | 生成静态 PNG | — | ❌ |

因此当前阶段更准确的说法是：

```text
A 数据入口主体链和回放修复已经成形
        +
B 已完成一版批处理风险原型和结果样例
        +
C 已完成独立规划框架和核心，可接合成/旧适配/未来正式 RiskSource
        ≠
A→B→C→D 动态闭环已经完成
```

---

## 6. 技术栈与工程现状

### 6.1 工作包 A

| 层次 | 技术 | 用途 |
|---|---|---|
| 语言/运行时 | Python 3.13 | 主实现 |
| 原生环境 | Mamba、ecCodes、NetCDF、HDF5 | 管理 GRIB/NetCDF 所需本地库 |
| Python 依赖 | uv + `uv.lock` | 锁定和同步 Python 包 |
| 数组/数据 | xarray、NumPy、SciPy、h5netcdf/h5py | NetCDF 读取、规范化和多维数组 |
| 索引 | SQLite | manifest 查询与版本/时间过滤 |
| 接口对象 | frozen dataclass、Protocol | `ManifestRecord`、`StandardDataFrame`、`DataSource` |
| 测试/质量 | pytest、Ruff、Makefile | 单元、集成、静态检查和统一验收 |
| CLI | `arctic-data` | 初始化、摄取、扫描、查询、回放、诊断、演示和旧下载器入口 |

当前复验环境的主要版本为：Python `3.13.14`、uv `0.12.3`、NumPy `2.5.1`、SciPy `1.18.0`、xarray `2026.7.0`、h5py `3.16.0`、h5netcdf `1.8.1`、cfgrib `0.9.15.1`、Copernicus Marine Toolbox `2.4.1`、Ruff `0.16.2`。这些是当前快照，不应替代 `pyproject.toml` 和 `uv.lock` 的正式约束。

在当前受限环境中应优先使用 Makefile；直接调用 uv 时需把 `UV_CACHE_DIR`、`UV_PYTHON_INSTALL_DIR` 指向项目内目录，并设置 `ECCODES_DIR` 指向 `.mamba-env`，否则可能因默认缓存只读或找不到 ecCodes 动态库而失败。

### 6.2 当前工作包 B 交付原型

| 层次 | 技术 | 当前情况 |
|---|---|---|
| 语言 | Python 脚本 | 三个独立目录，使用 `sys.path` 和约定目录拼接 |
| 数组/文件 | xarray、NumPy、NetCDF4/h5netcdf/SciPy | 读取/写入风险网格与底图数据 |
| 绘图 | Matplotlib | 输出彩色图和二值图 |
| 报告 | CSV；单因素 requirements 还列出 pandas | 输出风险统计与权重 |
| 依赖管理 | 三份未锁版本的 `requirements.txt` | 无 `pyproject.toml`、无 lock、无统一环境 |
| 测试 | 名为 `test_*.py` 的运行脚本 | 主要打印或运行主流程，不是具备断言的正式 pytest 套件 |
| 发布接口 | `comprehensive_risk_interface.py` | 读取固定的 60 天 NetCDF；不是 `RiskSource`/BC 缓存 |

### 6.3 工作包 C 已落地的工程基线

**[已冻结][代码确认][当前复验]** C 已沿用 A 的 Mamba + uv + pytest + Ruff 工程方式，使用 Python 3.13、xarray/NumPy、frozen dataclass/Protocol 和 `heapq` 隐式时空图。当前未引入 NetworkX，也没有为每个时刻显式构图。

| 层次 | 当前实现 |
|---|---|
| 环境 | Mamba `.mamba-env` + uv `.venv` + `uv.lock` |
| 合同 | `RiskFrame`、`RiskSource`、`RoutePlan`、JSON Schema |
| 核心 | `RiskSampler`、规则网格、船模、等价小时成本、时间依赖 A* |
| 运行时 | 三目标服务、五类重规划触发、迟滞、取消和 input revision |
| 适配 | 合成 `FixtureRiskSource`、严格 `LegacyBArchiveAdapter` |
| 输出 | CD latest、JSON、GeoJSON、摘要和审计字段 |
| 验收 | Ruff、74 tests、uv lock/sync、CLI help 全部通过 |

默认合成 smoke 使用 5×5 粗网格以控制运行时间，端点映射距离会完整报告；它只验证闭环，不是路线质量或真实导航基线。

---

## 7. 当前项目结构与建议目标结构

### 7.1 当前工作区

与本项目直接相关的正式代码当前包括工作包 A 和 C：

```text
/root/my_project/
├── work_package_a/                 # 环境数据入口，当前提交 2d38819
├── work_package_c/                 # 规划框架和核心，当前提交 67f0322
│   ├── configs/{scenarios,vessels,planner,replanning}/
│   ├── schemas/
│   ├── docs/
│   ├── src/arctic_route_planning/
│   ├── tests/
│   ├── pyproject.toml
│   ├── uv.lock
│   ├── environment.yml
│   └── Makefile
└── work_package_b_handoff/         # 发给 B 负责人的开发交接文档
```

工作包 B 目前仍位于外部交付压缩包中，没有形成正式 `work_package_b/` 工程，也没有正式安装到当前工作区；本轮没有修改 B。

### 7.2 B 交付包结构

> **[历史审计]** 本节记录 2026-08-09 的旧 ZIP 结构。上段“尚无正式
> `work_package_b/`”已失效；当前工程见 [`work_package_b/`](../work_package_b/) 和本文件 0.1 节。

```text
交付包/
├── 基础背景图.zip
│   └── 基础背景图/
│       ├── base_map.py
│       ├── 水深数据/*.nc
│       ├── downloads/基础背景图/*.png
│       └── 使用说明 + requirements.txt
├── 单因素风险.zip
│   └── 单因素风险/
│       ├── calculate_single_factor_risk.py
│       ├── configs/risk_config.py
│       ├── utils/
│       ├── downloads/{risk_dataset,color_maps,binary_maps,reports}/
│       ├── test_calculate_single_factor_risk.py
│       └── 使用说明 + requirements.txt
├── 综合风险.zip
│   └── 综合风险/
│       ├── calculate_comprehensive_risk.py
│       ├── comprehensive_risk_interface.py
│       ├── configs/
│       ├── utils/
│       ├── downloads/{risk_dataset,color_maps,binary_maps,reports}/
│       ├── 两个运行/接口测试脚本
│       └── 两份说明 + requirements.txt
├── route_cost_grid_offshore_murmansk_to_offshore_dikson.nc
└── route_cost_grid_tromso_to_svalbard.nc
```

### 7.3 建议的整体目标结构

模块可以处于同一仓库或独立仓库，但依赖方向必须保持单向：

```text
project/
├── work_package_a/                 # 环境数据入口
├── work_package_b/                 # 时间处理、风险模型、RiskFrame/BC
├── work_package_c/                 # 时间依赖规划、RoutePlan/CD
├── work_package_d/                 # 只读渲染与交互
├── contracts/                      # 或各包发布版本化契约包
├── demo_scenarios/                 # 固定数据快照、船型和参数
└── integration_tests/              # A→B→C→D 合同及回放测试
```

这里的 `contracts/` 不是要求所有包共享内部源码，而是建议把跨包对象、Schema 和合同测试做成明确版本；禁止通过导入对方内部模块来“省事”。

---

## 8. 工作包 A 深度认识

### 8.1 当前状态与本次复验

工作包 A 当前提交为 `2d3881965ba153077f49a8e4fc33db67367a9796`，包版本为 `0.2.0`。它是可安装、可运行、有 CLI 和自动化测试的正式 Python 项目，不只是设计文档。

本次执行：

```bash
cd /root/my_project/work_package_a
env \
  CONDA_PREFIX=/root/my_project/work_package_a/.mamba-env \
  PATH=/root/my_project/work_package_a/.mamba-env/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  make check
```

当前结果：

| 检查 | 结果 |
|---|---|
| Ruff | `All checks passed!` |
| pytest | 收集 32 项，`32 passed in 1.20s` |
| 锁文件检查 | `uv sync --check --extra acquisition` 通过，检查 78 个包 |
| CLI | `arctic-data --help` 成功，8 个子命令可加载 |
| 警告/失败 | 本轮 pytest 无警告、无失败 |
| 工作树 | 复验前后均干净 |

历史覆盖率诊断在 29 tests 版本约为 `67%`；本轮未重新以 coverage 门槛运行。当前 32 tests 能守住主体链和 seek 回归，但仍不能将“测试全绿”等同于“所有科学语义和真实数据源均已验证”。特别是 CLI、通用旧下载器适配、异常分支和真实网络链仍需增强。

### 8.2 A 的实际数据链

```text
旧下载器 / 新来源 / 手工发布文件
        │
        ▼
取得 issue_time 及证据
        │
        ▼
识别所有 valid_time，并把多时次文件拆成单时次帧
        │
        ▼
payload.nc.part → payload.nc → payload.metadata.json
        │
        ▼
FolderWatchSource / IngestionPipeline
        │
        ├── 变量、坐标和部分单位规范化
        ├── 结构质检与 SHA-256
        ├── 原始资料及证据进入 raw/
        ├── 规范帧原子写入 ready/
        └── 记录 SQLite manifest
                    │
                    ▼
LocalArchiveSource 按 issue_time 和模拟时刻查询/加载
                    │
                    ▼
StandardDataFrame → AB 分区缓存 → B
```

失败的 sidecar 会进入 `quarantine/`，缺测发布 `MissingDataAlert`；A 不用全零网格假装存在数据。

### 8.3 A 支持的数据类型与标准变量

| `data_type` | 分类 | A 的标准变量 | B 当前是否直接使用该命名 |
|---|---|---|---|
| `sea_ice_concentration` | slow | `ice_concentration` | 否，B 原型使用旧清洗名 |
| `sea_ice_type` | slow | `ice_type` | 当前 B 未使用 |
| `sea_ice_edge` | slow | `ice_edge` | 当前 B 未使用 |
| `sea_ice_drift` | slow | `ice_drift_u/v` | 当前 B 未使用 |
| `sea_ice_thickness` | slow | `ice_thickness` | 否，B 原型使用旧清洗名 |
| `wave` | dynamic | `significant_wave_height`、`mean_wave_direction`、`peak_wave_period` | 只使用旧名浪高，未使用方向/周期 |
| `ocean_current` | slow | `ocean_current_u/v` | 使用旧清洗名 |
| `water_level` | slow | `sea_surface_height` | 使用旧清洗名 |
| `wind_field` | dynamic | `wind_u10/v10` | 使用旧清洗名 |
| `temperature` | dynamic | `air_temperature_2m` | 当前 B 未使用 |
| `visibility` | dynamic | `visibility` | 使用旧清洗名 |
| `bathymetry` | static | `elevation` | 使用旧清洗名 |
| `long_term_restricted_area` | event | `restricted_area`（GeoJSON） | 当前 B 未使用 |

这说明 A 与 B 之间仍需要一个**单一、版本化的变量适配层**。不能让 B 的旧清洗变量名反向污染 A 的标准契约，也不能在 B 多处重复写映射。

### 8.4 A→B 已实现对象

#### `ManifestRecord`

```text
data_id, data_type, category, route_id, variables,
issue_time, valid_time, ingest_time,
bbox, crs, resolution,
source, quality_flag, version, checksum,
relative_path, size_bytes, media_type, metadata
```

代码特征：

- frozen dataclass；构造时把三类时间统一为 UTC；
- 路径解析拒绝逃逸出 archive root；
- checksum 必须是小写 SHA-256；
- `is_available_at(t)` 实现 `issue_time <= t`；
- `metadata.issue_time_evidence` 可保存取时方法、权威机构、原始值和引用位置。

#### `StandardDataFrame`

```python
@dataclass(frozen=True)
class StandardDataFrame:
    record: ManifestRecord
    payload: xr.Dataset | dict
    generation_id: int
```

需要注意：frozen 只冻结外层引用，`xarray.Dataset`、字典及 metadata 的嵌套对象并没有深度只读保护。“发布后不可变”目前主要靠合同约定。

#### B 可调用的入口

| 入口 | 用途 |
|---|---|
| `WorkPackageA.prefetch(...)` | 围绕当前模拟时刻加载历史和已发布未来预报帧 |
| `WorkPackageA.latest_for_b(data_type)` | 读取某类缓存中的最新帧 |
| `WorkPackageA.window_for_b(...)` | 读取按 `valid_time` 排序的窗口 |
| `DataSource.list_available(...)` | 按类型、航线、时段和 `as_of` 查询 |
| `DataSource.get_latest_before(...)` | 取得目标有效时刻之前的最新可见版本 |
| `DataSource.get_bracketing(...)` | 取得目标时刻前后帧，供 B 时间处理 |
| `DataSource.load_frame(...)` | 校验发布时间和 SHA 后加载标准帧 |
| `cache.lease(data_id)` | 防止 B 使用期间帧被回收 |
| `EventBus.subscribe(...)` | 订阅到达、缺测和代次变化事件 |

正常查询链有两层未来信息门禁：SQLite 查询使用 `issue_time <= as_of`；实际加载时 `LocalArchiveSource` 再检查一次。

### 8.5 模拟时钟与 AB 缓存

`SimulationClock` 支持播放、暂停、倍速、按实际秒数推进和跳转。模型时间与 UI 墙钟时间分离。每次 `seek()` 都提升 `generation_id`，以拒绝旧任务迟到写入。

| 类别 | 默认缓存策略 |
|---|---|
| static | 每类 1 帧；设计上可在跳转后复用 |
| slow | 每类型至少最近 2 帧 |
| dynamic | 默认每类型最多 64 帧 |
| event | 根据 `metadata.end_time` 淘汰 |

缓存还具备全局内存上限、LRU 式回收倾向和 `lease` 引用计数。同一个帧即使进入多个变量分区也只计算一次内存。

### 8.6 A 当前已有但容易被误解的能力

| 说法 | 更准确的解释 |
|---|---|
| “13 类下载器已经完成” | 13 类旧入口已登记并有统一 Runner；当前测试验证登记表和一个假下载器链，不等于本轮真实联网验证了 13 个源 |
| “规范化已经完成” | 已统一变量别名、坐标和部分单位；不包含统一目标网格，也不代表完整科学质检 |
| “历史回放已实现” | 正常 manifest 查询和加载有双重门禁；向过去 seek 的静态缓存缺口已于 `2d38819` 修复并有回归测试 |
| “A 有正式数据” | 主 `data/manifest` 当前为 0 条，只有合成 demo 数据，尚无可直接供 B/C 联调的正式 A 数据集 |
| “配置已存在” | `configs/*.toml` 当前主要是示例/说明，源码没有统一配置加载器 |
| “Schema 已存在” | sidecar JSON Schema 已交付，但摄取链没有实际调用完整 JSON Schema 验证器 |

独立 demo 在 `/tmp` 中复验得到：

```json
{
  "visible_versions": ["analysis", "forecast"],
  "future_observation_hidden": true,
  "cache": {"generation_id": 0, "frames": 2}
}
```

这证明正常演示路径能隐藏尚未发布的未来观测，但不覆盖所有跳转边界。

### 8.7 A 的已知缺口与优先级

#### 已解决（原 P0）：向过去跳转可能保留未来发布的静态帧

**[历史审计]** 2026-08-09 审计时，`cache.reset_generation()` 会无条件保留 static 帧并换成新代次，没有重新检查该帧的 `issue_time` 是否早于跳转后的模拟时刻。

已用最小诊断复现：

```text
静态帧 issue_time = 2026-07-15T00:00:00Z
初始模拟时刻       = 2026-07-16T00:00:00Z
向过去 seek 到     = 2026-07-14T00:00:00Z

结果：该静态帧仍留在缓存，issue_time 晚于新的 simulation_time。
```

该历史行为违反全链路 `issue_time <= simulation_time` 不变量。当时缓存测试只验证跳转后静态帧仍在，没有覆盖向过去跳转时的发布时间门禁。

当时建议的 A 修复方式：

1. 最稳妥方式是跳转时清空全部缓存，再按新模拟时刻预取；或
2. 给 `reset_generation()` 传入新 `simulation_time/as_of`，只复用 `issue_time <= new_time` 的静态帧；
3. 增加“向过去 seek 不得保留未来发布 static”回归测试；
4. 修复前 B 对所有帧（包括 static）继续执行防御性发布时间断言。

**[已冻结][代码确认][当前复验]** A 已在提交 `2d38819` 采用第 2 种方式完成修复：

- `PartitionedABCache.reset_generation(generation_id, *, simulation_time=None)` 只跨代复用 `issue_time <= simulation_time` 的 static；
- 未传 `simulation_time` 时安全清空 static，不假定资料可见；
- `WorkPackageA._on_seek` 传入 `snapshot.current_time`；
- 新增“未来 static 丢弃”“缺省时刻清空”和真实摄取/回放集成回归；
- A 最新 `make check` 为 32 tests passed。

C 没有读取 A 缓存，也没有在 C 内补丁式复制修复；B 仍应对所有来源执行 `issue_time <= as_of_time` 防御性断言。

#### P1：未知单位可能只改标签、不换算数值

目前仅显式实现少量转换，例如 `% → 1`、摄氏度 → K、km → m、knot → m/s。对于未识别单位，当前逻辑可能保留原数值却覆盖成标准单位。诊断中 `10 mph` 变成数值仍为 `10`、标签却成为 `m s-1`。这会制造科学语义错误。

建议未知单位严格拒绝、标为 degraded，或通过明确的单位库换算；B 不应默认所有 A 数值都已经科学校准。

#### P1：当前质检偏结构，尚非完整科学 QC

当前会检查变量、坐标、数组大小、时间、路径和 SHA，但不会完整检查全 NaN、物理范围、异常比例、航线覆盖和多变量联合有效区。最小诊断中，全 NaN 海冰浓度仍可通过。

B 必须继续处理缺测、质量和置信度，不能把“进入 ready”直接等同于“可安全作为零风险输入”。

#### P2：工程与合同收尾项

- sidecar Schema 和 TOML 配置尚未真正接入统一运行时；
- `normalizer_version` 仍写 `arctic-route-data/0.1.0`，与包版本 `0.2.0` 不一致；
- payload 和嵌套 metadata 不具备深度不可变性；
- 事件过期只在新帧 `put()` 时触发，不是纯时钟驱动；
- 无统一目标网格、常驻 watcher、对象存储和异步/并行预取；
- 主归档为空，真实源仍受账号、权限和网站可用性影响；
- `make check` 没有覆盖率门槛，异常分支覆盖仍有提升空间。

### 8.8 A 对 B/C 的交接结论

**给 B：**

- 只消费 `StandardDataFrame`，不扫描 A 的 `incoming/raw`；
- 按 `valid_time` 做 `PASSTHROUGH/HOLD/INTERPOLATE/EXTRAPOLATE`，不按文件顺序；
- 对所有帧防御性断言 `issue_time <= as_of_time`；
- 自己计算 `confidence`，不能把 A 的 `quality_flag` 直接当预测置信度；
- 保存 `data_id/source/version/quality_flag/issue_time/valid_time` 到 `source_summary`；
- 创建并传播 `scenario_id`，继续传播 `generation_id`；
- 在统一目标网格未落地时，显式处理不同源网格；
- 不假定 A 的单位和科学 QC 已百分之百可靠。

**给 C：**

- 只依赖正式 BC 契约，不导入 A 内部模块；
- 不访问 A 的 SQLite、目录或 AB 缓存；
- 不重新解析原始 NetCDF 时间；
- 检查 `scenario_id/generation_id/as_of_time`；
- A 的问题由 A 修复，不能在 C 中复制一条私有数据链来绕开。

---

## 9. 工作包 B 交付物深度审计

### 9.1 总体判断

**[代码确认][产物确认][用户补充]** 当前 B 应定位为：

> 已交付一套离线批处理风险原型，包括底图、单因素风险、综合风险、静态图片、统计报告、一个简易读取接口和两份历史代价网格；但尚未交付 V2.0 要求的运行时逐小时预测/融合模块、正式 `RiskFrame`、BC 缓存和全链路时间/版本证据。

它已经证明“可以把一批清洗后的多变量网格按规则转成风险值并保存”，但还没有证明“可以在任意模拟时刻只使用当时可见资料，稳定生成未来规划窗的逐小时、船型化风险序列”。

### 9.2 当前交付了什么

#### 基础背景图

- `base_map.py`：读取 GEBCO 等水深 NetCDF，拆分海域/陆地并生成静态底图；
- 两条航线的水深文件和 PNG；
- 主要用于展示背景，不应直接成为 C 的规划契约；
- 它没有下载水深数据，也没有形成版本化静态约束对象。

随包原始水深约为 15 arc-second（约 0.0041667°）：长航线文件约 `1176×11520`，短航线约 `2160×2040`；而风险产物是 0.05° 网格。这表明中间发生过裁剪/重采样，但负责该过程的完整“数据清洗”代码和输入边界没有交付，C 不能仅凭结果属性审计空间转换是否正确。

#### 单因素风险

- `calculate_single_factor_risk.py`：遍历风险因子，读取旧清洗数据，调用规则公式并保存；
- `configs/risk_config.py`：路径、两条航线、因子配置和二值阈值；
- `utils/`：公式、NetCDF I/O、绘图和 CSV 报告；
- 交付了两条航线的 7 天和 60 天 NetCDF、彩色图、二值图和报告；
- 输出中的 8 个实际风险变量均为 `0–1` 浮点值，另有 `sea_mask`。

#### 综合风险

- `calculate_comprehensive_risk.py`：从单因素风险计算综合风险并输出 NetCDF、图片和报告；
- `comprehensive_risk_interface.py`：按固定文件名读取两条航线的 60 天文件，返回 `xarray.Dataset`；
- `configs/comprehensive_risk_config.py`：权重、阈值、主导因子修正和绘图参数；
- 交付了两条航线的 7 天、60 天综合风险 NetCDF 及 CSV/PNG；
- NetCDF 主变量为 `comprehensive_risk`，辅助变量为 `sea_mask` 和 `bathymetry_risk`。

#### 路线代价网格

- 两份顶层 `route_cost_grid_*.nc`；
- 含 `cost_grid`、`passable_mask`、`comprehensive_risk`、`sea_mask`、`bathymetry_risk`；
- 生成代码未随包交付；
- 文件可用于理解旧原型如何把风险转成软代价，但不能视为正式 BC 接口。

### 9.3 当前单因素风险公式

| 因子 | 当前输入变量 | 原型规则 | 关键限制 |
|---|---|---|---|
| 海冰密集度 | `sea_ice_concentration_ice_conc` | 15% 以下低，70% 以上高，中间线性 | 不区分冰型、冰级和 POLARIS |
| 海冰厚度 | `sea_ice_thickness_sithick` | 0.05 m 以下低，1.0 m 以上高 | 未结合船舶冰级 |
| 风 | `wind_field_u10/v10` | 模长 8–20 m/s 线性 | 忽略相对船艏方向 |
| 浪 | `wave_VHM0` | 1–5 m 线性 | 未使用浪向、周期和船舶响应 |
| 海流 | `ocean_current_uo/vo` | 模长 0.3–1.2 m/s 线性 | 忽略顺/逆流方向和航速影响 |
| 能见度 | `visibility_vis` | 10,000–1,000 m 反向线性 | 不是船型或操纵规则校准结果 |
| 水深 | `bathymetry_elevation` | 50–10 m 反向线性 | 未结合散货船吃水和安全富余 |
| 水位 | `water_level_zos` | 0 至 -1 m 反向线性 | 没有与实际水深联合形成净水深约束 |
| 船舶交通 | `vessel_traffic_traffic_risk` | 直接裁剪/透传上游 0–1 风险 | 只在代码声明，交付数据中不存在；并未实现 AIS 计算 |

当前没有使用 A 可提供的冰型、冰缘、冰漂、温度和长期限制区；海浪的方向/周期也未进入公式。因此“支持多源风险”应理解为已完成一部分示范因子，不是环境/法规/船型因子的完整集合。

### 9.4 综合风险的当前实现

综合风险说明文档前半仍保留旧权重：

```text
0.25, 0.20, 0.10, 0.15, 0.10, 0.10, 0.07, 0.03
```

旧 `route_cost_grid` 内嵌风险正好对应这组 8 因素简单加权和。同一说明文档后半、当前配置和新版产物已经改为下列权重与非线性修正，因此必须把二者视为不同模型版本，不能混用。

当前代码权重为：

```text
海冰密集度 0.30    海冰厚度 0.22
风场       0.11    浪高       0.13
海流       0.08    能见度     0.07
水深       0.07    水位       0.02
船舶交通   0.06（当前产物没有该变量）
```

实际算法不是说明文档前半部分写的单纯加权平均，而是：

```text
1. 只保留数据集中实际存在的风险变量，并重新归一化其权重；
2. 计算加权平均；
3. 取关键动态因子的逐网格最大值；
4. 综合值 = 加权平均 × 0.72 + 关键因子最大值 × 0.28；
5. 对结果做 0.90 次幂敏感度修正并裁剪到 0–1。
```

当前 8 个实际因子的权重正好合计 1.0，所以报告中仍是上述 8 个值。若未来加入代码已声明的船舶交通因子，原始权重总和将变成 1.06，`normalize_weights()` 会自动缩小其他所有因子的有效权重。由于当前没有 `model_version`，这种行为改变无法被下游可靠识别。

另一个重要风险是：综合公式对每个因子先执行 `fillna(0)`。这会把海域内未声明的缺测临时当成零风险，然后再做融合；而输出又没有 `confidence` 或缺测掩膜。正式 B 必须改为显式缺测/降级策略，不能把“不知道”编码成“安全”。

### 9.5 风险阈值并不统一

| 用途/代码位置 | 阈值 | 当前语义 |
|---|---:|---|
| 单因素二值图 | 0.60 | 单因素黑白图阈值 |
| 综合风险显示 | 0.22 | 展示和报告中的可见高风险阈值 |
| 综合风险规划 | 0.45 | 报告中所谓规划高风险阈值 |
| 综合图层可见性 | 约 0.20 | 绘图透明度阈值 |
| `route_cost_grid.passable_mask` | 未使用风险阈值 | 实际仅等于 `sea_mask` |

这些阈值服务的对象不同，不能相互替换。特别是：

- PNG 中的黑色/灰色区域不是正式 `hard_mask`；
- 0.45 当前只参与报告/绘图语义，没有进入已交付 `passable_mask`；
- C 不应读取图片做规划；
- 是否将某一风险阈值升级为不可通行，必须结合船型、冰级和来源明确决定。

综合风险黑白图还存在实现与说明冲突：绘图代码内部会计算百分位阈值，但当前 `RISK_OVERLAY_STRENGTH=0.0`，只有大于 0 时才叠加风险层。因此当前黑白图实质主要是陆地/海域参考背景，不能按说明文档理解为已经表达高风险障碍。

### 9.6 NetCDF 实物审计

#### 7 天文件实际上只有 81 小时

两条航线的 7 天单因素、综合风险和旧代价网格使用相同时间坐标：

```text
起点：2026-07-31 00:00 UTC
终点：2026-08-03 09:00 UTC
总跨度：81 h
帧数：11
相邻间隔：12 h × 5、6 h × 2、3 h × 3
```

所以文件名中的 `7days` 不是连续覆盖 7 天，更不是逐小时序列。

若按上述 81 h 闭区间逐小时应有 82 帧，当前只有 11 帧，时间点覆盖率约 13.4%。

#### 60 天文件是跨 59 天的稀疏样本

```text
起点：2026-06-07 00:00 UTC
终点：2026-08-05 00:00 UTC
跨度：1,416 h（59 天）
帧数：38
相邻间隔：3、6、12、18、24、42、48、60 和 924 h
最大空洞：924 h（38.5 天）
```

因此 60 天文件也不能直接当作连续规划风险窗口。

若按 1,416 h 闭区间逐小时应有 1,417 帧，当前 38 帧，时间点覆盖率约 2.68%。

#### 空间网格

| 航线 | 形状 | 纬度范围 | 经度范围 | 分辨率 | 海域占比 |
|---|---|---|---|---|---:|
| 摩尔曼斯克外海—迪克森外海 | `151 × 1101` | 67.5–75° | 30–85° | 0.05° | 约 41.36% |
| 特罗姆瑟—斯瓦尔巴 | `221 × 241` | 68.5–79.5° | 10–22° | 0.05° | 约 51.87% |

当前文件没有独立 `crs` 变量，`lat/lon` 坐标也缺少完整单位/标准名属性；部分数据变量却保留 `grid_mapping="crs"`，形成悬空引用。C 的正式合同必须显式校验 CRS、坐标方向、范围和网格一致性。

#### 当前综合风险产物摘要

| 文件 | 帧/网格 | 海域风险范围 | 海域均值 | 说明 |
|---|---|---:|---:|---|
| 7 天，摩尔曼斯克—迪克森 | `11×151×1101` | 约 0.0100–0.4965 | 约 0.0657 | 新版融合算法 |
| 7 天，特罗姆瑟—斯瓦尔巴 | `11×221×241` | 约 0.0134–0.4478 | 约 0.0810 | 新版融合算法 |
| 60 天，摩尔曼斯克—迪克森 | `38×151×1101` | 约 0.0097–0.7991 | 约 0.1410 | 报告对应新版算法 |
| 60 天，特罗姆瑟—斯瓦尔巴 | `38×221×241` | 约 0.0087–0.4432 | 约 0.1022 | 报告对应新版算法 |

`time` 坐标很可能想表达风险的有效时刻，但文件没有用正式契约明确它就是 `valid_time`，也没有每帧 `as_of_time/issue_time`。因此可做空间算法样例，不能用于无未来泄漏的历史评估。

单因素数组在陆地也保留数值，CSV 的单因素均值/缺测率没有先按海域过滤。综合风险 CSV 的低/中/高比例又以整个矩形网格为分母，陆地 NaN 仍进入总格点数，导致这些比例之和约等于海域占比而不是 100%。正式报告应明确分母是“全部网格”还是“有效海域”。

### 9.7 `route_cost_grid` 的定量追踪结果

文件声明的软代价公式是：

```text
cost_grid = 1 + 99 × comprehensive_risk^1.25
不可通行单元 cost = 1,000,000
```

本次逐值复验得到：

- 公式与文件内嵌的旧 `comprehensive_risk` 一致，浮点最大误差约 `10^-6`；
- `passable_mask` 与 `sea_mask` 沿 time 维广播后逐值完全相等；
- 即“海域全部可通行、陆地不可通行”，没有冰区、禁航区、浅水安全余量或风险阈值硬约束；
- 两份代价文件内嵌的风险与当前同名 7 天综合风险文件不一致，最大绝对差分别约 `0.2626` 和 `0.2602`；
- 代价文件内嵌风险精确对应旧版简单加权结果，当前 7 天文件则对应新版“主导因子 0.28 + 0.90 次幂”算法；
- 代价文件生成于当前 7 天综合风险文件之前，`source_file` 仍指向后来被覆盖的同名 Windows 路径；
- 文件没有 `model_version`、源文件 checksum 或生成程序，无法从当前交付包重建同一结果。

可通行海域中的软代价范围约为：长航线 `1.413–21.216`，短航线 `1.619–15.649`；陆地统一为 `1,000,000`。这些值仅描述旧模型公式，不是经过船型和时间依赖规划共同校准的通用代价。

因此它们属于**陈旧/孤儿制品**：可以保留做旧算法样例和连通性烟雾测试，但不能作为当前 B 正式输出、不能证明当前综合风险已转换成最新代价，也不能让 C 围绕其字段设计核心 API。

代价网格还有元数据污染：`cost_grid` 继承了上游风险/GRIB/能见度等属性，甚至保留 `units="0-1"`，但其实际范围包含 1–100 和 `1,000,000`，显然不是 0–1 风险。

### 9.8 当前接口和测试能证明什么

`comprehensive_risk_interface.py` 能：

- 按两个固定 `route_name` 打开模块本地 `downloads/risk_dataset`；
- 固定选择文件名中的 `60days`；
- 返回一个或两个 `xarray.Dataset`；
- 汇总变量、坐标、维度和风险 min/max/mean；
- 要求调用者关闭文件。

它不能：

- 按 `scenario_id/generation_id/as_of_time` 查询；
- 返回单时次不可变 `RiskFrame`；
- 获取覆盖任意规划窗的排序序列；
- 选择同一 `valid_time` 的可见最新版本；
- 发布或缓存新帧；
- 表达置信度、硬掩膜和来源摘要。

三个 `test_*.py` 实际是脚本式入口：两个调用整套计算，一个打印接口元数据和形状；没有 `test_*` 函数和断言，不构成正式 pytest 合同测试。计算脚本还依赖交付包外部的旧“数据清洗”目录和固定中文层级，随包没有原始 cleaned dataset，因此无法仅靠当前交付包重跑完整单因素/综合风险生成链。接口读取已有 60 天结果可以运行，但这只验证文件可打开。

本次具体复验：接口示例脚本成功读取两条 60 天文件；单因素和综合公式的合成数据 smoke test 通过；在当前可用 A 环境中执行两个目录的 `pytest -q` 会在收集期因没有安装 B 的 `matplotlib` 依赖而失败。即使按未锁版本 requirements 补装依赖，这些脚本仍没有断言，完整计算仍缺 cleaned dataset。

接口说明本身已经漂移：文档写的是 `time=11` 和旧风险统计，而接口源码固定加载 `60days` 文件，当前实际返回 `time=38` 和新版风险结果。

### 9.9 工程可移植性与可重复性问题

- 三份 `requirements.txt` 都未锁版本；
- 无统一 `pyproject.toml`、lock、CLI 或包安装方式；
- 目录通过 `parents[...] / "深度学习" / ...` 拼接，要求特定外部布局；
- 文档和 NetCDF 属性保留 `E:\python项目\...` 绝对 Windows 路径；
- 交付包含 `__pycache__/*.pyc` 等生成物；
- 7 天和 60 天制品并存，但当前接口只读取 60 天；
- 产物没有稳定 `schema_version/model_version/checksum`；
- 报告对应 60 天新版数据，而接口说明中的数值对应旧代价文件内嵌风险，文档已经漂移。
- 风险 NetCDF 未启用明显的 chunk/压缩，最大单文件约 177.8 MB；I/O 函数调用 `dataset.load()`，会把整份文件读入内存；
- 保存函数先删除目标文件，再从临时目录复制，不是同目录原子替换；
- `comprehensive_risk`、`sea_mask` 和 `cost_grid` 继承了无关 GRIB、能见度、水深等属性，存在悬空 `grid_mapping`、错误单位和语义污染；
- 单因素 requirements 列出未使用的 pandas；综合绘图可选使用 Pillow，但 requirements 未明确声明。

### 9.10 B 距离正式目标还差什么

| 目标能力 | 当前状态 | 需要补齐 |
|---|---|---|
| 消费 A 标准帧 | 使用外部旧 cleaned dataset | 实现 A 变量适配、时间/质量/来源传播 |
| 按变量类别处理时间 | 未实现 | PASSTHROUGH/HOLD/INTERPOLATE/EXTRAPOLATE 策略和上限 |
| 逐小时未来序列 | 未实现 | 用户说明中的预测/插帧模型，默认至少未来 24 h |
| 船型化风险 | 通用固定阈值 | 冰级、吃水、航向、船速和版本化船型参数 |
| `risk_score` | 有 `comprehensive_risk` 原型 | 统一命名、范围、缺测和模型版本 |
| `risk_level` | 仅图片/报告阈值 | 正式离散规则和版本 |
| `hard_mask` | 只有陆地 `sea_mask/passable_mask` | 陆地、禁航区、船舶能力、净水深等硬约束 |
| `confidence` | 无 | 综合质量、龄期、缺帧、预测时长和模型置信度 |
| `source_summary` | 只有绝对源文件字符串 | 保存 A 的数据 ID、时间、版本、质量和 checksum |
| `scenario/generation` | 无 | 创建/传播场景和代次，拒绝旧任务 |
| `RiskFrame` | 无 | 固化 dataclass/Schema/不可变发布对象 |
| BC 缓存 | 无 | 按时间窗口查询、版本选择、容量和代次隔离 |
| 合同/回放测试 | 无正式断言 | 单元、合同、无未来泄漏、逐小时连续性和回归测试 |
| 可重复环境 | 不完整 | 统一工程、锁文件、参数和产物版本 |

### 9.11 对 C 的直接结论

C 当前可以安全复用的是：

- 两条航线的坐标范围和网格样例；
- `comprehensive_risk` 作为 0–1 软风险样例；
- `sea_mask` 作为“陆地/非海域”的初步测试掩膜；
- 现有时间坐标作为稀疏时序测试夹具；
- 旧代价公式用于测试“风险变化会改变软代价”，但不作为正式接口。

C 当前不能直接相信或固化的是：

- `7days/60days` 文件名等于连续覆盖；
- `passable_mask` 等于完整可通行性；
- 旧 `route_cost_grid` 等于当前风险模型结果；
- `time` 已具备无未来泄漏语义；
- 当前风险对选定散货船有效；
- 缺测已经正确处理；
- 当前权重、阈值和接口已经冻结。

---

## 10. 三段交付接口：已实现与目标契约

### 10.1 单向依赖原则

```text
A -- StandardDataFrame --> B -- RiskFrame --> C -- RoutePlan --> D
```

- B 不扫描 A 的 `incoming/raw`；
- C 不调用 B 的模型内部函数，也不读取 A；
- D 不调用 C 求解器，不持有计算锁；
- 跨包只通过版本化对象、Schema、缓存协议和事件通信；
- 发布后的对象视为不可变。

### 10.2 A→B：`StandardDataFrame` 已实现

这一段已经有当前代码、测试和文档，详见第 8 章。它提供环境帧和来源证据，不提供风险、预测、规划代价或路线。

### 10.3 B→C：历史 `RiskFrame v1`（当前正式合同已为 v2）

> **[历史审计]** 下述字段是 2026-08-09 的 v1 基线。当前正式真源为
> [`docs/BC_CONTRACT.md`](docs/BC_CONTRACT.md) 与
> [`schemas/risk-frame-v2.schema.json`](schemas/risk-frame-v2.schema.json)；
> 当前 B 已发布 canonical v2 committed window。

**[已冻结][代码确认]** Python 真源为 `work_package_c/src/arctic_route_planning/contracts/models.py`，跨语言结构为 `work_package_c/schemas/risk-frame-v1.schema.json`。当前顶层对象是：

```python
@dataclass(frozen=True)
class RiskFrame:
    schema_version: str
    risk_id: str
    scenario_id: str
    corridor_id: str
    vessel_profile_id: str
    config_digest: str
    generation_id: int
    valid_time: datetime
    as_of_time: datetime
    generated_at: datetime
    model_version: str
    payload: xr.Dataset
    source_summary: tuple[SourceReference, ...]
    provenance: ProvenanceKind
```

`payload` 为单时次二维网格。C 依赖四个始终必需变量，正式 B 还必须提供第五个环境影响变量：

| 变量 | 类型/范围 | C 中的语义 |
|---|---|---|
| `risk_score` | `float32 [0,1]` | 软风险代价输入 |
| `risk_level` | `uint8 [1,5]` | 离散展示/解释等级，不宜直接作为核心连续代价 |
| `hard_mask` | `bool` | `True` 表示绝对不可扩展 |
| `confidence` | `float32 [0,1]` | 数据、预测和模型可信度，用于降级/拒绝决策 |
| `environment_speed_factor` | `float32 (0,1]` | B 给出的综合环境影响；C 用版本化船型计算最终有效航速 |

可选保留 `risk_ice/risk_wave/risk_wind/...` 等解释分量，但 C 的核心不应强制依赖它们。

#### `RiskFrame` 必须满足的不变量

- 所有时间均为带时区 UTC；单帧只描述一个 `valid_time`；
- `risk_score/confidence` 范围正确，NaN/缺测有显式规则；
- `hard_mask=True` 无论风险值多少都不可扩展；
- v1 坐标为一维、有限、严格递增的 `latitude/longitude`，CRS 固定 `EPSG:4326`，同一窗口网格身份兼容；
- `scenario_id/generation_id` 不匹配时拒绝读写；
- 同一 `valid_time` 多版本只能选择当前 `as_of_time` 可见的最新可靠版本；
- C 不假设固定 1 h，未来 30/10 min 帧仍按时间戳工作；
- 来源摘要能追到 A 的 `data_id/issue_time/valid_time/source/version/quality_flag/checksum`；
- 合成/临时降级数据必须可识别，不能伪装成正式 B 输出。
- `provenance=formal` 时所有来源 `issue_time` 非空且不晚于 `as_of_time`；正式帧必须携带 `environment_speed_factor`。

#### 目标 BC 读取协议

```python
class RiskSource(Protocol):
    def publish(self, frame: RiskFrame) -> None: ...

    def get_window(
        self,
        start: datetime,
        end: datetime,
        *,
        scenario_id: str,
        generation_id: int,
        vessel_profile_id: str,
        config_digest: str,
        as_of: datetime,
    ) -> Sequence[RiskFrame]: ...

    def latest_before(...) -> RiskFrame | None: ...
```

### 10.4 C 的 ETA 风险采样不是 B 的预测/插帧

需要明确区分：

- **B 的时间处理/预测**：把稀疏环境数据转换成正式风险帧序列；
- **C 的 ETA 采样**：船在 18:30 进入某网格时，从 18:00 和 19:00 两个正式风险帧估算 18:30 的规划风险。

首版 ETA 采样规则已经由代码和测试固定：

| 字段 | 建议时间处理 |
|---|---|
| `risk_score` | 相邻兼容帧之间线性插值 |
| `confidence` | 取两帧较保守值，或按明确龄期规则衰减 |
| `hard_mask` | 不做数值插值；对包围 ETA 的帧取逻辑 OR |
| `risk_level` | 由插值后的 `risk_score` 重新分级 |
| `environment_speed_factor` | 空间参与值和相邻时间帧取保守最小值 |

不得跨场景、代次、不兼容网格或未声明兼容的模型版本插值；时间间隙超过阈值时，应请求补算、明确拒绝或进入保守降级。

### 10.5 C→D：已固化并实现的 `RoutePlan v1`

```python
@dataclass(frozen=True)
class Waypoint:
    longitude: float
    latitude: float
    eta: datetime
    recommended_speed_mps: float

@dataclass(frozen=True)
class RoutePlan:
    schema_version: str
    scenario_id: str
    corridor_id: str
    vessel_profile_id: str
    config_digest: str
    generation_id: int
    plan_id: str
    plan_version: str
    planning_request_id: str
    input_revision: int
    generated_at: datetime
    as_of_time: datetime
    start_time: datetime
    objective_mode: ObjectiveMode
    plan_kind: PlanKind
    waypoints: tuple[Waypoint, ...]
    metrics: RouteMetrics
    replan_reasons: tuple[ReplanReason, ...]
    source_risk_ids: tuple[str, ...]
    planner_version: str
    destination_reached: bool
```

当前已实现 GeoJSON/JSON 序列化：路线为 `LineString`，航点 ETA/速度和完整指标可审计。CD 使用原子激活/latest 语义，并用代次、请求 ID、input revision 和配置摘要阻止迟到结果覆盖；D 只读。

#### `RoutePlan` 必须满足的不变量

- 至少包含起点和终点，ETA 严格递增；
- 坐标、速度、距离、风险和时间均为有限值且单位明确；
- 硬约束违规数为 0；
- 距离、ETA、平均/最大风险可由航点和引用风险帧在容差内复算；
- 候选路线必须属于相同场景、代次、起终点和 `as_of_time`；
- 发布前再次检查取消状态和任务修订，不能只在任务开始时检查；
- `source_risk_ids` 应引用实际 `risk_id`，而不是只写一个模糊模型名。

### 10.6 原四个接口矛盾的当前处理

1. **`route_id` 复用冲突已解决**：使用 `corridor_id` 表示数据/允许走廊，`plan_id` 表示具体路线计划。
2. **`mode` 混合已解决**：目标函数使用 `objective_mode=fastest/low_risk/recommended`，生命周期使用 `plan_kind=initial/replanned`。
3. **同代次任务竞态已解决**：`generation_id` 隔离 seek，`planning_request_id/input_revision` 隔离同代次请求修订。
4. **速度责任已解决**：正式 B 必须输出 `environment_speed_factor`，C 把它应用到版本化船型计算最终速度；C 不从风险/置信度重复折减。

### 10.7 24 小时风险窗与 2–5.5 天航程的矛盾

架构一处要求 BC 至少保留未来 24 h 风险，另一处要求 C 的规划窗口覆盖剩余航程或采用滚动窗口。两条候选航线预计 2–5.5 天，因此必须在以下策略中明确选择：

- B 一次输出覆盖全航程的逐小时风险；
- C 只精细规划当前风险窗，使用滚动窗口与终端代价；
- 超出 24 h 使用明确的低置信度背景风险/气候场；
- 分段航行，只承诺发布当前可证实的部分路线。

**[已冻结]** C v1 不外推、不等待，风险时域必须覆盖实际 ETA；合成场生成足够长时域，旧制品缺帧会明确失败。当前正式 B 联调应覆盖完整搜索时域，默认上限为 168 h。未来若实现短窗滚动/终端代价，需要形成新的一致设计，不能把 24 h 直接冒充 5 天全航程覆盖。

---

## 11. 工作包 C 应该怎样理解和实现

### 11.0 当前实现状态

本章最初用于指导实现，现保留为设计解释；截至 2026-08-10，以下能力已经落地：

- `RiskFrame/SourceReference/RiskSource`、`RoutePlan` 和两份 JSON Schema；
- 严格的风险身份、网格、来源、时间窗和覆盖校验；
- 风险时空采样：软风险线性、hard OR、置信度/环境因子保守最小值；
- 8 邻接规则网格、Haversine 距离、边内采样和显式限距端点映射；
- C 侧船型模型，只应用 B 的环境因子，不从风险二次折速；
- 时间依赖 A*，状态包含节点、时间桶和入射方向，并有 Dijkstra 对照测试；
- fastest、low_risk、recommended 三种目标；
- 五类重规划触发、最小间隔、收益阈值、迟滞、取消、请求修订和原子发布；
- JSON/GeoJSON/CD latest、合成夹具、严格旧 B 适配器和三个 CLI 子命令；
- 两个场景和一份明确标注 `demo_unvalidated` 的演示散货船配置。

尚未完成的是正式 B `RiskSource` 联调、真实科学/船型校准、D 接入、等待动作、方向相关速度响应和基于实测的性能算法升级。

### 11.1 C 的职责

C 应负责：

- 读取覆盖规划时域的标准 `RiskFrame` 序列；
- 接收起终点、当前船位/时刻、船舶性能、允许航区和目标模式；
- 构建规则网格或导航图；
- 按节点/航段预计到达时刻采样风险；
- 计算有效航速、航段时间、软代价和硬约束；
- 输出最快/最短、低风险和综合推荐候选路线；
- 评估当前路线未来风险并滚动重规划；
- 固定已航行段，只重算剩余航段；
- 计算航程、ETA、平均/最大风险、硬约束违规、转向和耗时；
- 发布不可变 `RoutePlan`，传播场景、代次、信息截止时刻和风险版本；
- 响应取消标记，拒绝旧任务发布。

### 11.2 C 明确不负责

C 不应：

- 下载、解析或规范化原始环境文件；
- 直接读取 A 的 SQLite、`incoming/raw/ready` 或 AB 内部缓存；
- 从文件名/mtime 猜 `issue_time`；
- 承担 B 的稀疏环境数据预测、插帧和风险融合；
- 私自修改 B 的风险权重、阈值或置信度；
- 把缺失风险当零风险；
- 直接调用 B 模型内部函数；
- 执行 UI 渲染或让 D 阻塞等待；
- 为绕过不完整 BC 而把旧 NetCDF 字段散落到求解器核心。

### 11.3 时间依赖状态模型

首版建议使用可验证的时空状态：

```text
(导航节点, 到达时间桶[, 入射方向])
```

原因：

- 同一网格在不同到达时刻的风险不同；
- 有转向惩罚时，入射方向会改变下一步成本；
- 如果允许等待、风险可能下降或成本不满足 FIFO 性质，只给每个空间节点一个标签可能不正确；
- 显式时空状态比“普通 A* + 隐式当前图”更容易做小网格穷举验证。

时间桶不能硬编码为 1 h；应由风险帧间隔、船速和空间分辨率共同决定。

### 11.4 每条边的评估顺序

1. 取得边长、方向和空间采样点；
2. 根据当前节点到达时间估计进入、中点和离开该边的时间；
3. 从相邻风险帧采样 `risk_score/confidence/hard_mask`；
4. 任一必要采样点命中硬约束则禁止扩展；
5. 根据船型和环境计算有效航速；
6. 得到航段耗时和下一个状态的到达时间；
7. 累积时间、风险暴露、距离、转向和偏离代价。

长边不能只检查起点；至少应按空间网格或风险变化尺度检查边穿越的单元，避免从两个安全端点跨过中间陆地/禁航区。

### 11.5 代价模型

架构给出的组成包括：

```text
有效航速：v_eff = v0 × α_ice × α_wave × α_wind × α_other

边代价：航段时间
      + 风险暴露
      + 距离
      + 转向惩罚
      + 偏离参考通道/当前路线的惩罚
```

不同分量量纲不同，不能直接用未经解释的常数相加。建议先归一化，或转换为“等价时间成本”，再使用版本化配置。风险指标建议同时保存按航行时间/距离加权的平均风险和最大风险。

A* 启发式必须是可证明的下界：例如终点直线距离除以物理上最大速度；无法证明风险下界时，风险启发项取 0。若后续允许等待、非 FIFO 成本或复杂速度控制，应采用时空图/标签设置或 MPC，而不是假定普通空间 A* 仍最优。

### 11.6 规划模式

| 目标模式 | 主要目标 | 始终保留 |
|---|---|---|
| fastest/shortest | 提高时间或距离权重 | 全部硬约束和基本安全底线 |
| low_risk | 提高风险权重，可设置最大允许风险 | 硬约束、ETA 和可解释指标 |
| recommended | 平衡时间、风险、速度损失、转向和偏离 | 版本化政策和权重 |

“动态重规划”不是第四种目标函数，而是从当前船位按上述某一目标模式重新求解剩余航段。

### 11.7 重规划触发与防抖

架构定义五类触发：

- 时间触发：跨过新的 B 输出时刻；
- 数据触发：发布新风险帧或旧时刻风险被修订；
- 风险触发：当前路线未来最大/平均风险或硬约束比例越界；
- 偏航触发：实际船位偏离计划；
- 事件/人工触发：禁航区、障碍、船舶状态或用户请求变化。

还应具备：最小重规划间隔、风险变化阈值、路线切换收益阈值、迟滞机制和同一时刻事件合并，避免路线不断来回切换。

任务启动时建议冻结：

```text
scenario_id
generation_id
planning_request_id
as_of_time
ship_state_revision
vessel_profile_version
planner_config_version
```

发布前原子核对这些版本；同一 `generation_id` 中也必须用 `planning_request_id` 防止较早请求迟到覆盖较新请求。

### 11.8 B 未完成时的已实现临时隔离方案

```text
现有 B NetCDF / 合成时空风险场
                │
                ▼
LegacyBAdapter / FixtureRiskSource
                │  只有这一层知道旧路径和变量名
                ▼
ContractValidator
                │
                ▼
统一 RiskFrame / RiskSource
                │
                ▼
RiskSampler → C 的图、代价、规划和重规划核心
```

未来 B 完成后只替换输入实现：

```text
BCRiskSourceV1 → 同一个 ContractValidator / RiskSampler / C 核心
```

#### 临时适配的严格规则

- 不从文件名或 mtime 猜发布时间；
- 旧文件 `time` 只有经配置明确后才能映射为开发用 `valid_time`；
- 当前 `comprehensive_risk` 可映射成软 `risk_score` 夹具；
- `sea_mask` 只能提供陆地初始约束，不能宣称完整 `hard_mask`；
- 当前 `route_cost_grid` 不映射为 `risk_score`，也不进入核心接口；
- 缺少 `confidence/source_summary/model_version/as_of_time` 时，严格模式拒绝；开发模式必须显式标记低可信/合成，不伪装成正式 B 输出；
- 临时逐小时插值只用于 C 的测试夹具，不能冒充 B 的正式预测结果；
- 不静默重投影或猜网格；需要重采样时只放在适配层并记录方法；
- 所有适配结果通过和正式 BC 相同的合同验证。

当前 `work_package_c/src/arctic_route_planning/adapters/legacy_b.py` 已按上述规则实现：只读取嵌套 `综合风险.zip` 中唯一综合风险 NetCDF，不读取 `route_cost_grid`；要求显式开发模式和 time 语义确认；只从 `sea_mask` 得到陆地 hard；置信度上限 0.40；缺失环境速度影响时提供带警告的中性 1.0；来源始终标为 `legacy_unverified`。

#### 临时适配器退出条件

- B 能连续输出规划时域内的 `RiskFrame`；
- 四个必需变量及时间、版本、来源、场景、代次字段完整；
- BC 生产者—消费者合同测试和历史回放测试通过；
- C 切换输入工厂后，图、成本、规划、重规划源码无需修改；
- 旧文件路径和旧变量名不再出现在 C 核心和核心测试中。

### 11.9 已落地的 C 项目结构

```text
work_package_c/
├── README.md
├── pyproject.toml
├── uv.lock
├── environment.yml
├── configs/
│   ├── scenarios/
│   ├── vessels/
│   ├── planner/
│   └── replanning/
├── schemas/
│   ├── risk-frame-v1.schema.json
│   └── route-plan-v1.schema.json
├── docs/
│   ├── BC_CONTRACT.md
│   ├── CD_CONTRACT.md
│   ├── COST_MODEL.md
│   └── ACCEPTANCE.md
├── src/arctic_route_planning/
│   ├── contracts/          # RiskFrame、RoutePlan、协议和验证
│   ├── adapters/           # fixture、legacy B、正式 BC
│   ├── domain/             # 场景、船舶、船位
│   ├── risk/               # 时间/空间采样、缺帧策略
│   ├── grid/               # 网格、邻接、距离、坐标
│   ├── cost/               # 航速、硬约束、目标函数
│   ├── planners/           # 首版时间依赖 A*
│   ├── replanning/         # 触发、迟滞、取消和控制器
│   ├── evaluation/         # 路线指标和对比
│   ├── publishing/         # CD 缓存与 GeoJSON/JSON
│   └── service.py
└── tests/
    ├── contract/
    ├── unit/
    ├── integration/
    ├── regression/
    └── fixtures/
```

BC/CD 契约最好由一个小型共享契约包或共享 Schema 维护，而不是在 B/C 中各复制一份后逐渐漂移。

---

## 12. 工作包 C 阶段化开发路线

| 阶段 | 状态 | 工作内容 | 当前结果/后续条件 |
|---|---:|---|---|
| C0 决策冻结 | ✅ | 处理核心接口/场景/船型问题 | 决策记录已固定 v1；科学参数仍标演示未校准 |
| C1 合同骨架 | ✅ | RiskFrame、RoutePlan、RiskSource、CD latest | dataclass/Protocol、两份 Schema、合同测试和合成夹具已落地 |
| C2 风险采样 | ✅ | 精确时刻、帧间 ETA、hard、缺帧与置信度 | 整点、帧间、过期、超窗和异常均有测试 |
| C3 图与代价 | ✅ | 网格、邻接、距离、船模和成本 | 8 邻接、边采样、显式 snap、等价小时成本已实现 |
| C4 基线规划 | ✅ | 时间依赖 A*，三目标 | 时空状态、取消和 Dijkstra 对照已实现 |
| C5 CD 输出 | ✅ | RoutePlan、指标、GeoJSON/JSON、latest | 序列化、反序列化、不可变发布和指标校验已实现 |
| C6 滚动重规划 | ✅ | 五类触发、最小间隔、迟滞 | policy/coordinator 已实现并有测试 |
| C7 取消与竞态 | ✅ | seek、场景/配置切换、同代次修订 | generation/request/revision 围栏和原子激活已实现 |
| C8 临时 B 联调 | ✅ | LegacyBArchiveAdapter 读取现有制品 | 低可信、未知 issue time、中性 factor 和显式确认已隔离 |
| C9 正式 BC 联调 | ✅/⏳ | 切换正式 RiskSource | 工程夹具已通过且 C 核心零改动；真实完整场景仍待验收 |
| C10 双航线回归 | ✅/⏳ | 固定配置执行两场景 | 两场景合成 smoke 已通过；正式 B 数据对比仍待完成 |
| C11 性能优化 | ⏳ | 以 benchmark 决定 LPA*/D* Lite/分层网格 | 当前先保留正确基线，待真实网格指标和团队阈值 |

当前仍不应无基准地引入 D* Lite、MPC 或复杂多目标算法。首版时间依赖 A* 已提供可解释正确基线；优化应由正式 B 网格和重规划性能数据驱动。

---

## 13. 测试与验收体系

### 13.1 合同和时间测试

| 类别 | 关键测试 | 验收标准 |
|---|---|---|
| RiskFrame 字段 | UTC、范围、形状、网格、版本、来源 | 不合格帧明确拒绝，不静默修补 |
| 防未来泄漏 | 来源 `issue_time`、`as_of_time`、模拟时刻 | 结果不包含当时不可见数据 |
| 时间窗口 | 排序、重复版本、30/60 min、窗口边界 | C 不依赖固定 1 h |
| ETA 采样 | 整点、帧间、超窗、过期 | 结果与手算一致，缺帧不当安全 |
| 硬约束 | 两帧变化、边跨越障碍 | 任一必要时空点命中即拒绝 |
| 场景/代次 | mismatch、seek、重置 | 旧帧和旧计划拒绝读写 |

### 13.2 算法正确性测试

| 类别 | 关键测试 | 验收标准 |
|---|---|---|
| 成本分量 | 无风险、速度折减、风险、转向、偏离 | 非负、有限、单位和版本明确 |
| 小网格最优性 | 与 Dijkstra/穷举结果比对 | 已知场景返回已知最优解 |
| 时间依赖性 | 当前图相同、未来图不同 | 路线随 ETA 风险合理改变 |
| 模式差异 | fastest vs low_risk vs recommended | 结果差异可解释，硬违规均为 0 |
| 长边检查 | 两端安全、中间阻塞 | 不得跨越障碍“跳过去” |
| RoutePlan 复算 | 航点、速度、风险版本 | 距离/ETA/风险在容差内一致 |

### 13.3 重规划和并发测试

| 类别 | 关键测试 | 验收标准 |
|---|---|---|
| 五类触发 | 时间、数据、风险、偏航、事件 | 应触发时触发，不应触发时保持 |
| 防抖 | 最小间隔、收益阈值、迟滞 | 小扰动不造成路线频繁切换 |
| seek 取消 | 旧任务在各阶段完成 | 旧结果不进入 CD |
| 同代次竞态 | 请求 1 比请求 2 晚完成 | 请求 1 不能覆盖请求 2 |
| CD 非阻塞 | C 计算变慢/失败 | D 可继续读取同代次最近有效路线 |
| 代次切换 | generation 改变 | D 立即停止展示旧代次路线 |

### 13.4 端到端与科学验证

- 初始风险 → 三种路线 → 新风险/偏航 → 至少一次可解释重规划；
- 固定数据快照、船型、起终点、参数、随机种子和算法版本；
- 比较最短/最快、低风险和动态重规划路线；
- 记录航程、ETA、平均/最大风险、硬约束违规、速度损失、重规划次数/延迟、路线稳定性、扩展节点、计算时间和内存峰值；
- 两条航线均执行回归，避免场景硬编码；
- B 的预测验证必须使用“当时可见资料预测、事后与真实有效时刻比较”的回放方式。

---

## 14. 资料矛盾与处理结论

| 编号 | 矛盾/不明确点 | 当前证据 | 本文处理 | 仍需决定 |
|---|---|---|---|---|
| X1 | A 文档称主体完成，但向过去 seek 曾保留 static | 历史诊断复现；`2d38819` 已修复；32 tests passed | **已解决**：按新 simulation time 过滤，缺省时安全清空 | B 仍保留 as-of 防御断言 |
| X2 | A 规范化是否等于完整科学 QC | 代码偏结构校验；全 NaN 可通过 | 明确不等价 | QC 阈值和责任边界 |
| X3 | A 配置/Schema 是否运行时生效 | 文件存在，源码未统一加载/验证 | 标为说明/未接入 | 后续是否正式接入 |
| X4 | B 说明前半写旧权重，后半和代码写新算法 | 文档、代码、CSV 不一致 | 以当前代码/产物说明现状，旧版保留历史标签 | 正式模型版本和校准依据 |
| X5 | 船舶交通是否已经实现 | 代码声明并给权重，产物/报告无变量；公式只透传 | 标为预留，不计入当前完成度 | AIS 数据源、公式和版本 |
| X6 | `7days/60days` 是否连续 | 实物为 11 帧/81 h 和 38 帧/59 天，最大间隔 924 h | 文件名仅作历史命名 | B 正式覆盖窗和输出频率 |
| X7 | `route_cost_grid` 是否对应当前风险 | 内嵌旧风险，与当前文件最大差约 0.26，生成器缺失 | 标为陈旧孤儿制品 | 是否归档/重新生成 |
| X8 | `passable_mask` 是否等于 `hard_mask` | 逐值等于广播 `sea_mask` | 明确不等价 | 冰、净水深、禁航区等硬约束 |
| X9 | 风险缺测如何处理 | 当前融合 `fillna(0)`，无 confidence | 认定不满足正式 BC | 正式缺测/降级策略 |
| X10 | 两条航线的先后顺序 | 长航线符合产品计划；短航线适合快速 smoke | **已处理**：默认 smoke 用短航线，两场景均有配置和合成回归 | 正式数据验收顺序由团队安排 |
| X11 | 24 h BC 窗能否覆盖 2–5.5 天 | C v1 不外推/不等待，默认搜索上限 168 h | **C 侧已冻结**：必须覆盖实际 ETA；B 输出场景剩余时域与 168 h 上限取最小的完整请求窗 | 未来短窗滚动/终端代价另行设计 |
| X12 | `route_id` 的含义 | 上游走廊与具体计划语义冲突 | **已解决**：`corridor_id/plan_id` | B 按 v1 输出 corridor_id |
| X13 | `mode=replanned` 是否合理 | 目标函数与生命周期混用 | **已解决**：`objective_mode/plan_kind` | 无 |
| X14 | 有效航速由谁提供 | 风险不能重复当物理减速 | **已解决**：B 输出 `environment_speed_factor`；C 计算最终速度 | B 完成正式因子模型和校准 |
| X15 | 正式/旧/合成数据是否混淆 | 旧包无 issue time、confidence、速度影响 | **已解决 C 侧隔离**：`formal/legacy_unverified/synthetic` | B 正式源只发布 `formal` |
| X16 | 场景/船型是否各包复制 | 当前唯一可运行配置在 C | **过渡方案已冻结**：注入 `config_root` 和稳定 digest，未来迁共享目录 | 指定共享配置维护人 |

---

## 15. 团队决策清单与当前状态

### 15.1 原 P0 决策

1. **部分解决**：BC/CD 当前由 C 的 Python 合同、Schema 和合同测试作为临时真源；未来共享 `contracts/` 的维护人仍需指定。
2. **已解决**：`corridor_id` 与 `plan_id` 分开。
3. **已解决**：`RiskFrame` 含 `vessel_profile_id` 和正式必需的 `environment_speed_factor`。
4. **已解决**：B 提供环境影响，C 计算最终有效航速，双方不从风险重复折速。
5. **C 侧已解决、B 待实现**：v1 要求风险覆盖实际 ETA，当前正式联调请求窗为场景剩余时域与 168 h 上限取最小；未来短窗方案另行设计。
6. **已解决 C 默认**：超窗/大间隔/低置信度明确拒绝，不当安全；B 仍需定义自己的降级和补算策略。
7. **已解决**：软风险时间线性；hard OR；confidence/environment factor 取保守最小；等级按插值风险重算。
8. **已解决 v1**：EPSG:4326、严格递增一维经纬度、规则网格、8 邻接、显式无效规则；分辨率由场景/输入配置。
9. **工程接口已解决、科学值未解决**：当前使用 `demo_bulk_carrier_v1` 且明确 `demo_unvalidated`；真实参数/性能曲线仍需领域校准。
10. **已解决配置**：两场景端点、bbox 和 ID 已版本化；默认 smoke 用短航线，正式验收顺序仍可由团队安排。
11. **已解决演示基线**：成本统一为等价小时分量并用 TOML 版本化；真实政策权重仍需校准。
12. **已解决 v1**：不允许等待；后续若加入需升级状态/算法和合同。
13. **已解决**：配置 digest 隔离场景/船型/参数，`generation_id` 隔离 seek；切换时隐藏不兼容 current。
14. **已解决**：`planning_request_id + input_revision` 防止同代次迟到覆盖。

### 15.2 原 P1 决策

15. **已解决演示基线**：最小间隔、风险/偏航/收益阈值和迟滞参数进入 TOML；真实阈值待校准。
16. **已解决**：`objective_mode` 与 `plan_kind` 分开。
17. **已解决**：内部船速为 knots/km·h⁻¹，RoutePlan 航点对外使用 `recommended_speed_mps`。
18. **已解决**：保存实际 `source_risk_ids`，风险帧自身携带 `model_version`。
19. **已解决**：六个 `ReplanReason` 枚举，可保存多个触发原因。
20. **当前已解决**：发布从当前起点到终点的完整剩余计划，已航段由上层状态管理。
21. **已解决 v1**：JSON/GeoJSON 和原子 latest；候选路线由服务批次返回，持久保留策略未来按 D 需求扩展。
22. **已解决**：旧源必须显式开发模式、低可信、未知 issue time、中性 factor 警告；正式 B 满足第 11.8 节退出条件后停用。

### 15.3 P2：实测后决定

23. 目标单次规划耗时、重规划延迟和内存阈值；
24. 网格分辨率和规划时间桶精度；
25. 是否需要 LPA*/D* Lite、多目标 Pareto 或 MPC；
26. 两条航线最终演示指标、基线和脚本。

---

## 16. 推荐的近期行动顺序

### 已完成（2026-08-10）

1. A 修复“向过去 seek 保留未来 static”并补回归测试；
2. C 固化 `RiskFrame v1`，明确 hard、confidence、环境速度因子、风险窗和来源语义；
3. C 建立独立 Mamba + uv 项目、合同测试和 `FixtureRiskSource`；
4. C 完成 `RiskSampler`、图/成本、时间依赖 A* 和 Dijkstra 对照；
5. C 完成三目标、RoutePlan、JSON/GeoJSON 和 CD latest；
6. C 完成滚动重规划、迟滞、取消、代次和同代次 revision 围栏；
7. C 通过严格 Legacy Adapter 联调当前 B 制品并保持核心隔离；
8. 两个场景均有合成 smoke，A/C 最新 `make check` 分别 32/74 tests passed。

### 当前立即执行（真实源与科学主线）

9. 按共享具体场景重新采集 A 的 12 类必需层，并尽可能同包验收 2 类可选层，形成完整
   `a.dataset-bundle.v2` 与 doctor 记录；
10. 使用同一 RunContext 运行当前 B/C 正式入口，记录真实帧数、时域、来源、性能和失败项；
11. B 负责人审阅旧交接书中仍未签字的科学/产品决策，校准风险、confidence、方向相关
   风浪流和船舶适用域；
12. 保持 B `demo_unvalidated`，直到真实标签、训练/权重（若采用模型）和独立回放指标交付；
13. C 增量实现四层路线，D 再消费稳定 RoutePlan。

### 随后执行

15. C 只替换输入工厂接入正式 B，采样、图、成本、规划和重规划核心保持零改动；
16. 用正式 A/B 数据完成双场景三目标对比、性能和重规划报告；
17. D 消费当前 `RoutePlan` JSON/GeoJSON/CD latest；
18. 取得真实船舶、冰级、环境性能和风险校准证据后，再讨论去除 `demo_unvalidated` 标记和算法性能升级。

---

## 附录 A：关键证据导航

### 工作包 A

- 项目入口与当前边界：[`work_package_a/README.md`](../work_package_a/README.md)
- AI/工程不变量：[`work_package_a/AGENTS.md`](../work_package_a/AGENTS.md)
- AB 已实现接口：[`work_package_a/docs/AB_INTERFACE.md`](../work_package_a/docs/AB_INTERFACE.md)
- BC/CD 现行交接契约：[`work_package_a/docs/BCD_HANDOFF.md`](../work_package_a/docs/BCD_HANDOFF.md)
- 架构追踪：[`work_package_a/docs/ARCHITECTURE_TRACE.md`](../work_package_a/docs/ARCHITECTURE_TRACE.md)
- 发布时间规则：[`work_package_a/docs/ISSUE_TIME_POLICY.md`](../work_package_a/docs/ISSUE_TIME_POLICY.md)
- 旧下载器迁移：[`work_package_a/docs/LEGACY_MIGRATION.md`](../work_package_a/docs/LEGACY_MIGRATION.md)
- AB 对象：[`work_package_a/src/arctic_route_data/models.py`](../work_package_a/src/arctic_route_data/models.py)
- manifest：[`work_package_a/src/arctic_route_data/manifest.py`](../work_package_a/src/arctic_route_data/manifest.py)
- 数据源：[`work_package_a/src/arctic_route_data/sources.py`](../work_package_a/src/arctic_route_data/sources.py)
- 模拟时钟：[`work_package_a/src/arctic_route_data/clock.py`](../work_package_a/src/arctic_route_data/clock.py)
- AB 缓存：[`work_package_a/src/arctic_route_data/cache.py`](../work_package_a/src/arctic_route_data/cache.py)
- A 编排服务：[`work_package_a/src/arctic_route_data/service.py`](../work_package_a/src/arctic_route_data/service.py)

### 工作包 C 与 B 交接

- C 项目入口：[`README.md`](README.md)
- C 决策记录：[`docs/DECISIONS.md`](docs/DECISIONS.md)
- 正式 BC 合同：[`docs/BC_CONTRACT.md`](docs/BC_CONTRACT.md)
- BC Python 模型：[`src/arctic_route_planning/contracts/models.py`](src/arctic_route_planning/contracts/models.py)
- `RiskSource` 协议：[`src/arctic_route_planning/contracts/sources.py`](src/arctic_route_planning/contracts/sources.py)
- 正式 BC v2 JSON Schema：[`schemas/risk-frame-v2.schema.json`](schemas/risk-frame-v2.schema.json)
- 历史 BC v1 JSON Schema：[`schemas/risk-frame-v1.schema.json`](schemas/risk-frame-v1.schema.json)
- B 交接目录：[`work_package_b_handoff/README.md`](../work_package_b_handoff/README.md)
- B 矛盾、字段和开发任务：[`工作包B矛盾与完善开发交接书.md`](../work_package_b_handoff/工作包B矛盾与完善开发交接书.md)
- B 的 AI 约束模板：[`work_package_b_handoff/AGENTS.md`](../work_package_b_handoff/AGENTS.md)

### 架构与 B 交付包

由于这两份资料仍位于外部压缩包，证据路径按“压缩包/内部文件”表示：

```text
北极航线预测驱动动态规划系统架构设计.zip/
└── a2dabb9c40fc421fb317303282202f09_md_full.md

交付包.zip/交付包/
├── 基础背景图.zip/基础背景图/...
├── 单因素风险.zip/单因素风险/...
├── 综合风险.zip/综合风险/...
├── route_cost_grid_offshore_murmansk_to_offshore_dikson.nc
└── route_cost_grid_tromso_to_svalbard.nc
```

---

## 附录 B：术语表

| 术语 | 含义 |
|---|---|
| AB/BC/CD | A→B、B→C、C→D 之间的缓存/接口边界 |
| frame | 某一个有效时刻的网格数据对象 |
| as-of | 本次计算允许知道的信息截止时刻 |
| hard constraint | 不可违反的约束，命中后节点/边不得扩展 |
| soft cost | 可以权衡但会增加代价的风险、时间、距离等 |
| time-dependent planning | 边代价取决于船舶实际到达该边的时刻 |
| rolling replanning | 随时间、风险、船位或事件变化只重算剩余航段 |
| future leakage | 在历史模拟中提前使用当时尚未发布的数据 |
| fixture | 为测试构造的小型、确定性输入，不冒充真实业务数据 |
| contract test | 验证生产者和消费者对字段、单位、时间和错误语义理解一致的测试 |

---

## 附录 C：本次清单覆盖索引

| 原清单范围 | 本文位置 |
|---|---|
| 资料基线、冲突裁决与复验 | 第 2、8、9、14 章 |
| 项目目标、全链路、场景和进度 | 第 1、3、4、5 章 |
| 技术栈和项目结构 | 第 6、7 章 |
| A 当前实现和 AB 接口 | 第 8、10 章 |
| B 代码、制品、NetCDF、缺口和矛盾 | 第 9、10 章 |
| BC/CD 正式目标接口与临时适配 | 第 10、11 章 |
| C 模型、算法、重规划和目录 | 第 11 章 |
| C 开发阶段、测试和验收 | 第 12、13 章 |
| 团队决策、风险和近期行动 | 第 14、15、16 章 |

---

## 结语

当前项目最有价值的基础是：A 已经把“数据何时可以被系统知道”和 payload 内容证明工程化；
B 已把确定性逐小时风险、来源、置信度、代次和 committed source 落成独立工程；C 已把
canonical BC/CD 边界、严格时空采样、时间依赖规划、重规划和发布做成可运行、可测试的
项目，旧风险原型仍安全隔离。下一条主线不是再包装旧 ZIP，而是取得同一共享场景的真实完整
A bundle，复用当前 A→B→C 公共链路验收，并在证据充分后开展科学校准和四层规划。
