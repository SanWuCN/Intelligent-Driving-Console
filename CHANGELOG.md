# Changelog

## 未发版（车端已应用，`VERSION` 仍为 1.3.0）

### 一键启动与流程页

- 流程页只留「序号 + 名称 + 状态」：删掉顶部说明、每步说明和重复的「已完成 / 等待上一步」，
  只保留需要动手或有异常时的提示。第 6 步统一改名 **自主巡航**。
- 右下角主按钮改成 **一键启动**：按顺序自动跑完六步，从当前进度接着往下走；
  第 4 步等 RViz 的 `2D Pose Estimate`（收到位姿后 `localization_bootstrap` 自己拉起
  `ndt_matching`，无需再点），等待期间按钮显示「标定完成，继续」；第 6 步自动弹出安全确认窗。
  执行中可在旁边「停止自动」，失败或急停会主动中止。
- 每一步的手动按钮保留（可以和以前一样单步执行），紧凑排布下六步刚好铺满、不滚动。

### 人体停车门控

- 新增 `person_command_gate` / `person_camera_observer` / `person_stop_policy` 与
  `person_guard_tracking.launch`：深度相机发现车前方有人时拦住 `/ctrl_cmd` 让车停下，
  默认 `observe_only` 只观察不拦截。状态经 `/person_guard/control_status` 上报，
  控制台在「路径规划/跟踪」模块里显示门控是否已使能。详见 [人体停车门控](docs/PERSON_CAMERA.md)。
- 新增 `deploy/bigcar-person@.service`、`deploy/person-guard-run.sh` 及其单元测试。

### 实时地图与雷达布局与运维

- 「系统状态」6 个模块不再被裁掉：状态面板按内容占高、模块表可内部滚动，
  1366×768 下六行全部可见，窄窗口下参数面板也不再被压成一条缝。
- 新增 `deploy-to-car.sh`：自动备份 → 同步后端+前端 → 重启服务，离线车自动跳过。
- 实时桥接补齐孤儿进程防护：`SIGTERM` 干净退出、stdin EOF 自退、90 秒看门狗、
  启动清扫，且无人观看时不再解析点云。

### 实时地图与雷达（RViz 式画面）

- 新增 `backend/ros_bridge.py`：容器内 Python 2.7 常驻桥接，把 `/bms_flag_Infor_fb`、
  `/bms_Infor_fb`、`/current_pose`、`/final_waypoints`、`/points_raw` 和地图 PCD
  以二进制帧推给控制台；只在有浏览器观看时运行。
- 新增 `/api/live` WebSocket 推流（JSON + float32 点云）与 `/api/live-ticket` 一次性票据
  （只读遥测，不需要解锁控制）。前端新增 `useLiveScene` 与 `LiveMap`，用 Canvas 2D
  画出点云地图、路线、实时位姿、激光雷达点云和行驶轨迹，取代原来的静态 CSV 预览图。
- 点云解析走 numpy 快路径：Jetson 上单帧 **1326 ms → 32 ms**；无 numpy 时退回纯 Python。
- 修掉两个链路缺陷：`struct.Struct("<BI")` 原生对齐导致帧头错位；
  实时流里缺 `docker` 或读文件出错会抛异常、握手后立刻断开。

### 运行参数

- 「限速」改为**设置速度**：范围 0.2–2.0 m/s，滑杆 + 0.2/0.4/0.6/0.8/1.0/1.5/2.0 档位；
  改动即时下发 `/config/waypoint_replanner`（velocity_max，单位 km/h）与
  `/config/waypoint_follower`，同时同步已生成的循环路径航点速度。
- 下发改为后台线程 + 250 ms 合并窗口：请求线程不再被 3 次 `rostopic pub` 阻塞
  （实测单次请求 19 s → 0.3 s），连续拖动只下发最后一次。
- 界面移除「前视距离」「障碍停车」两个输入，后端保留固定值（2.0 m / 0.05 m）。

### 电量与排版

- 顶部遥测条、「系统状态」标题栏、机组管理车辆卡片都显示剩余电量
  （`/bms_flag_Infor_fb` 的 SOC），低压与充电有独立配色提示。
- 电量优先取实时桥接缓存；没人订阅时由 `ros_probe.py` 在 5 秒一次的话题探针里顺带读 BMS
  （`AnyMsg` 需要按类型动态反序列化：`str(AnyMsg)` 会直接抛异常）。
- 修复「文件与运行参数」面板在 1366×768 下的排版：速度改成单列卡片，主操作按钮吸底常驻，
  参数面板不再横向溢出、页面不出现滚动条。

### 其它

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
