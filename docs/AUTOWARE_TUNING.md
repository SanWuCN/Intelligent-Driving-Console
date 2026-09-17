# Autoware.AI 可调参数与功能边界

本车使用 ROS 1 Melodic + Autoware.AI 1.14。前端目前开放三个可在运行中安全下发的参数：

| 前端参数 | ROS 配置话题 | 当前范围 | 作用 |
| --- | --- | ---: | --- |
| 限速 | `/config/waypoint_replanner` | `0.05–0.50 m/s` | 重新生成路径点速度上限 |
| 前视距离 | `/config/waypoint_follower` | `0.5–5.0 m` | 改变 Pure Pursuit 追踪点距离 |
| 障碍停车距离 | `/config/velocity_set` | `0.05–3.0 m` | 改变点云障碍物前停车距离 |

参数会同时写入 `runtime/config.json`，因此 Web 服务或 Jetson 重启后仍保留。

## 激光雷达避障当前为关闭状态

`backend/controller.py` 顶部的 `LIDAR_OBSTACLE_AVOIDANCE_ENABLED = False` 是激光雷达避障总开关。关闭时：

- `detection_range` = `0.0`（检测半径归零）
- `threshold_points` / `points_threshold` = `2000000000`（int32 上限，点数阈值不可达）

`velocity_set` 仍然运行并继续发布 `/final_waypoints`，只是不再因为点云把目标速度压到 0。判断依据见 `/obstacle_waypoint`：`-1` 表示没有障碍停车，`>= 0` 表示在对应航点索引处停车。

开关为 `True` 时恢复 Autoware.AI 原厂阈值：检测半径 `1.3 m`、点数阈值 `10`，即路径点周围 1.3 m 内超过 10 个点就判为障碍。这也是 03 车此前“开头两个航点速度被置 0、底盘目标速度为 0”的直接原因。

车端还有两处持久化位置，改回时必须一起改，否则重启后会恢复避障：

- 容器内 `waypoint_planner/velocity_set.launch` 的 `detection_range` / `points_threshold` 默认值（`src/` 和 `install/` 两份）。
- 控制台后端 `controller.py`：`_publish_live_parameters()` 每次点“循迹运行”都会重发 `/config/velocity_set`。

一键脚本见 `bigcar/diagnostics/disable_lidar_avoidance.sh` 与 `enable_lidar_avoidance.sh`（后者用于回滚）。

**障碍停车距离参数只在避障开启时才有意义**，关闭期间在前端调整它不会改变车辆行为。


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

- `detection_range`、`points_threshold`：检测半径和最少点数。**当前分别为 0.0 和 2000000000，即避障关闭。**
- `detection_height_top` / `detection_height_bottom`：障碍点高度窗口（当前 `0.2 / -1.7`）。
- `remove_points_upto`：忽略车身附近 2.3 m 内的点。
- `deceleration_range`：为 0 时不搜索减速区，只做停车判定。
- `deceleration_obstacle` / `deceleration_stopline`：障碍物与停止线减速强度。
- `stop_distance_obstacle` / `stop_distance_stopline`：障碍物与停止线停车预留距离。

### A* 避障

- `enable_avoidance`：开启 Hybrid-A* 绕障。当前为 `false`（`astar_avoid` 节点在运行，但只直通航点）。
- `robot_length` / `robot_width` / `robot_base2back`：碰撞检查车身外形。
- `minimum_turning_radius`：最小转弯半径。
- `replan_interval`、`search_waypoints_size`：重规划频率和搜索范围。

A* 避障不应只靠开关直接启用；必须先校验车身尺寸、转弯半径、占据格地图和安全区域。本车 `robot_length` 为 2.0 m，而实车更长，直接开启会低估碰撞风险。

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
