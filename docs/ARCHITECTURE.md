# 系统架构

## 设计目标

控制台把“手工输入一组 ROS 命令”改成“每一步都有真实反馈的受控流程”。页面不会因为用户点击过按钮就宣布成功，而是依据 ROS 节点存活状态和关键话题的实时消息推进流程。

## 运行边界

- React 前端运行在浏览器中。
- Python 3 HTTP 服务运行在 Jetson 宿主机上。
- ROS 1 和 Autoware 运行在 `autoware_ai_orin` Docker 容器中。
- 宿主机代码挂载为容器内的 `/from_host/bigcar-console`。
- Python 后端只执行 `controller.py` 中定义的白名单动作。

## 状态流

`useConsole.ts` 周期请求 `/api/state`。后端聚合：

- Docker 容器状态。
- ROS Master 中的节点和已发布话题。
- `/points_raw`、`/points_no_ground`、`/current_pose`、`/final_waypoints`、`/ctrl_cmd` 是否真正收到消息。
- CAN0 状态、CPU、内存、温度和日志。

## 六步状态机

### 1. 环境检查

确认 Docker 可用；容器未运行时尝试启动。

### 2. 底盘与雷达

启动 `yhs_can_control_qt_node` 和 Hesai 驱动。只有底盘节点、雷达节点和 `/points_raw` 实时数据同时存在时才进入下一阶段。

### 3. Autoware 与 TF

启动车辆模型、外参 TF、点云体素滤波和地面滤波。`managed_runtime_manager.py` 每秒根据真实 ROS 节点刷新 Runtime Manager 开关状态。

### 4. 地图、RViz 和 NDT

1. 加载 PCD 地图并启动 RViz。
2. `localization_bootstrap.py` 在标定前临时发布 `map → base_link`，使地图和实时雷达同时显示。
3. 等待 RViz `2D Pose Estimate` 产生 `/initialpose`。
4. 收到人工位姿后启动 `ndt_matching`，并重复发布初始位姿。
5. `/current_pose` 持续更新后标记定位完成。

### 5. 路径与规划

读取选定 CSV，启动 Waypoint Loader、Lane Rule、Lane Stop、Lane Select、A* Avoid 和 Velocity Set。只有 `/final_waypoints` 持续发布时才允许启动控制。

### 6. 循迹控制

```text
/final_waypoints + /current_pose + /current_velocity
                         │
                         ▼
                    pure_pursuit
                         │ /ctrl_raw
                         ▼
                     twist_filter
                         │ /ctrl_cmd
                         ▼
                yhs_can_control_qt_node
                         │
                         ▼
                        CAN0
```

Pure Pursuit 必须使用 `publishes_for_steering_robot:=true`，否则只发布 `/twist_raw`，不会产生底盘需要的 `/ctrl_cmd`。控制台以 `/ctrl_cmd` 真实消息作为“循迹运行中”判据。

## RViz 画面

Python 服务使用 FFmpeg `x11grab` 捕获 `1920×1080` X11 桌面，缩放至 960 像素宽并以 4 FPS MJPEG 输出到 `/api/rviz.mjpeg`。视频流只用于观察，RViz 交互仍在车载桌面完成。

## 进程、急停与重启

- 长期进程在 `runtime/` 中保存 PID，日志写入 `runtime/logs/`。
- 急停不要求 Web 控制令牌。
- 急停会停止控制节点，并以 `autoware_msgs/ControlCommandStamped` 向 `/ctrl_cmd` 连续发布零速。
- “重启流程”在零速后停止定位、规划、雷达、底盘和 RViz 节点，但保留 Docker 容器。
