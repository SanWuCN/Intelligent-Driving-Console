# Changelog

## 未发版（车端已应用，`VERSION` 仍为 1.3.0）

- 关闭激光雷达避障：`velocity_set` 的 `detection_range` 由 `1.3 m` 改为 `0.0`、
  `threshold_points` / `points_threshold` 由 `10` 改为 `2000000000`，点云不再把目标速度压到 0。
- 新增 `LIDAR_OBSTACLE_AVOIDANCE_ENABLED` 总开关，`True` 可一键恢复 Autoware 原厂阈值。
- 同一组阈值同时写入 `_publish_live_parameters()` 和 `velocity_set.launch` 启动参数，
  避免“循迹运行”或节点重启后避障被悄悄恢复。
- RViz 启动改为后台运行并写入宿主机日志，避免阻塞控制台请求。

## 1.3.0

- 控制令牌调整为 `801801801`。
- 记住地图和路径文件，并限制为配置数据目录中的 PCD/CSV。
- 增加闭环校验和 200 圈连续航点，实现实训演示的自动循环跑圈。
- 支持在前端实时下发速度、前视距离和障碍停车距离。
- 用可交互的 noVNC “屏幕监看”取代只读 RViz 图片视图。
- 固定 1920×1080 主页布局不产生页面滚动，日志区改为内部滚动并显示全部条目。

## 1.2.8

- Pure Pursuit 最小前视距离设为 2 m。
- 循迹速度上限设为 0.2 m/s。
- Autoware `velocity_set` 障碍停车距离设为 5 cm。
- 打通 `/ctrl_raw → /ctrl_cmd → CAN` 链路。
- 只有 `/ctrl_cmd` 有实时消息时才标记循迹运行。

## 1.2.2

- 收到 RViz `2D Pose Estimate` 后再启动 NDT。
- 标定前提供临时 TF，使点云地图和实时雷达同时显示。
- Runtime Manager 开关同步真实 ROS 节点状态。

## 1.0.0

- 完成 React 前端、Python 后端和六步启动流程。
- 增加学校品牌、RViz 实时画面、路径预览、控制令牌、急停和日志。
