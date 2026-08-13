# Legacy vessel fixtures

本目录只为旧 C 测试和适配器保留。全系统正式船舶事实唯一来自
`/root/my_project/arctic_route_contracts/configs/vessels/`；C 自己的船速、操舵、
转弯和净空算法参数放在相邻 `configs/vessel_models/`，并进入
`planner_config_digest`。

禁止把本目录旧“同名船型”与共享 `VesselProfile` 混用。
