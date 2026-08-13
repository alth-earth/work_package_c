# Legacy scenario fixtures

本目录中的两个 TOML 只为旧 B 制品适配与历史回归保留，不再是 C 的正式场景事实。
它们使用旧 ID、旧时间窗和旧终点语义，禁止复制到新运行或原地修改后沿用旧摘要。

正式场景、航区和时域唯一来自：

```text
/root/my_project/arctic_route_contracts/configs/scenarios/
/root/my_project/arctic_route_contracts/configs/corridors/
```

新 C 运行必须读取 `RunContext.v2`。若旧适配器需要本目录，它必须持续标为
`legacy_unverified`，不能发布 `provenance=formal`。
