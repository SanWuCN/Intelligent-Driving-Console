# Autoware.AI 可调参数与功能边界

本车使用 ROS 1 Melodic + Autoware.AI 1.14。前端目前开放三个可在运行中安全下发的参数：

| 前端参数 | ROS 配置话题 | 当前范围 | 作用 |
| --- | --- | ---: | --- |
| 限速 | `/config/waypoint_replanner` | `0.05–0.50 m/s` | 重新生成路径点速度上限 |
| 前视距离 | `/config/waypoint_follower` | `0.5–5.0 m` | 改变 Pure Pursuit 追踪点距离 |
| 障碍停车距离 | `/config/velocity_set` | `0.05–3.0 m` | 改变点云障碍物前停车距离 |

参数会同时写入 `runtime/config.json`，因此 Web 服务或 Jetson 重启后仍保留。

## 还可扩展的调参项

### Pure Pursuit

- `velocity_source`：使用航点速度或固定速度。
- `lookahead_ratio`：前视距离随车速增长的比率。
- `is_linear_interpolation`：是否在航点之间插值。
- `add_virtual_end_waypoints`：终点虚拟点行为。

### Waypoint Replanner

- `velocity_min` / `velocity_max`：曲线和直线速度上下限。
- `accel_limit` / `decel_limit`：加减速限制。
- `replan_curve_mode`、`radius_thresh`、`radius_min`：按曲率降速。
- `resample_mode` / `resample_interval`：航点重采样。
- `replan_endpoint_mode`、`braking_distance`、`end_point_offset`：终点减速和停车。

### Velocity Set 障碍物处理

- `detection_range`、`points_threshold`：检测宽度和最少点数。
- `detection_height_top` / `detection_height_bottom`：障碍点高度窗口。
- `deceleration_obstacle` / `deceleration_range`：障碍物减速强度和范围。
- `stop_distance_stopline`：停止线停车距离。

### A* 避障

- `enable_avoidance`：开启 Hybrid-A* 绕障。当前关闭。
- `robot_length` / `robot_width` / `robot_base2back`：碰撞检查车身外形。
- `minimum_turning_radius`：最小转弯半径。
- `replan_interval`、`search_waypoints_size`：重规划频率和搜索范围。

A* 避障不应只靠开关直接启用；必须先校验车身尺寸、转弯半径、占据格地图和安全区域。

### NDT 定位和点云预处理

- NDT 分辨率、步长、收敛阈值、最大迭代数。
- Voxel Grid 体素尺寸和量程。
- Ring Ground Filter 的雷达高度、地面斜率和分割阈值。
- `base_link → velodyne` 六自由度外参。

这些参数会直接影响定位稳定性，建议停车后调整，不放在运行中的普通控制面板。

## 感知与底盘现状

- **行人识别：当前没有。** Hesai 点云已参与障碍物减速/停车，但它不会把障碍物分类为“人”。车上虽然有 USB 摄像头和 Autoware 视觉/点云感知包，但未看到相机标定、行人模型和感知话题在当前流程中运行。
- **转向灯：底盘面板有“转向灯”状态显示，但当前控制链路未实现。** `/ctrl_cmd` 只携带速度、加速度和转角；要自动打灯，还需要核对 FR-07 Pro/YHS 的 CAN 命令字段并修改底盘驱动，不能猜测报文直接下发。

Autoware.AI 已停止上游维护，新功能建议单独分支、仿真回放和封闭场地逐级验证。
