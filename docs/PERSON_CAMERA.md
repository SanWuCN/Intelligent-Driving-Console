# 01/03车深度相机人体停车开发记录

03车已接入实际速度出口；启动循迹前门控保持未使能，持续输出零速度。相机节点只发布判断，独立门控负责底盘输出。

## 已验证

- 车辆：03车，192.168.31.232。
- Orbbec Astra：深度 USB 2bc5:060f，彩色 USB 2bc5:050f。
- 厂商驱动 `orbbec/ros_astra_camera`，提交 210f530144314da7c70072215bacba12130d9b20。
- backward_ros 使用 kinetic-devel（0eaa663），避免 ROS 2 分支不兼容。
- 独立工作目录 `/root/person_camera_ws`；libuvc 安装至 `/root/person_camera_deps/install`。
- 编译环境需要 CPATH 指向上述 install/include，LIBRARY_PATH 与运行时 LD_LIBRARY_PATH 指向 install/lib，以及 libdw-dev。
- 深度对齐开启：depth_align:=true；color_depth_synchronization:=false，使用消息时间戳软件同步。
- 彩色和深度均 640×480，深度 16UC1，毫米单位，frame_id 为 person_camera_color_optical_frame。
- 初步模型：chuanqi305/MobileNet-SSD 的 deploy.prototxt、mobilenet_iter_73000.caffemodel，VOC person 类15。模型只用于初步验证，尚未完成漏检评估。
- 宿主机 Python 3 / OpenCV 4.5.4 推理，经本机127.0.0.1:8771向 ROS Python 2观察节点返回检测框。

## 当前进程

持久 systemd 模板单元（已启用开机启动）：bigcar-person@camera、bigcar-person@detector、bigcar-person@observer、bigcar-person@gate。
观察节点仅发布 `/person_guard/status`；1.5m触发、1.8m解除距离、连续2秒清空、0.6秒数据超时。
未校准时状态强制为 calibration_required，不允许据此自动恢复。
检测框取躯干区域有效深度的20百分位；有效深度不足则判定未知，不能当作清空。

## 接入控制前待完成

1. 确认1.5m从相机还是车头前缘计算；若相机在车头后方d米，车头净距离=深度-d。
2. 静止状态验证近距离人员、多人、遮挡、转身、蹲下、离开，以及彩色与深度边缘对应。
3. 校验该型号深度盲区、实际安装俯仰角和视场，不能保证摄像头看不到的位置有人也会停车。
4. 故障测试：相机断流、推理超时、无效深度、重复时间戳，必须保持停止。
5. 使用唯一的底盘命令出口串联速度门控，禁止用另一个零速度发布者与现有 /ctrl_cmd 竞争；保留人工急停，解除人员停车不得解除人工急停。
6. 停车距离及恢复速度在封闭赛道验证后再启用自动恢复。

本地测试：`python3 -m unittest discover -s tests -p test_person_stop_policy.py`。

## 现场静态验证结果

用户确认相机位于车头前沿，camera_to_front_m=0。彩色/深度叠加图的人体轮廓基本吻合，观察节点 alignment_verified=true。
连续30秒采样包含 person_near、person_in_release_margin、confirming_clear、clear 四种状态，人体测距范围1.342–2.076m。
用户退到约2m时测得躯干表面1.93–1.95m，状态解除。此前近距离采样约0.98–1.02m。
这些是有限场景观察，不能代替遮挡、多人、漏检、断流和实际制动距离测试。

`person_command_gate.py` 默认使用隔离测试话题，不向 `/ctrl_cmd` 发布。它对相机状态或上游命令超时输出零速度，恢复时限制加速，且前向相机模式禁止倒车输出。
01车（192.168.31.134）和03车（192.168.31.232）的 runtime/config.json 中 person_guard_enabled=true；02/04车未启用。
用户确认01车与03车相机安装相同，前沿偏移均为0。各车 `runtime/person-camera.env` 单独保存 `PERSON_CAMERA_TO_FRONT_M=0.0` 和 `PERSON_ALIGNMENT_VERIFIED=true`；未配置时启动脚本默认不放行。
正式底盘输出固定订阅 `/person_guard/status`，不接受遗留的测试话题参数。`/person_guard/check` 只检查使能前条件，不使能、不下发起步指令。
控制台使用 person_guard_tracking.launch 将原 /ctrl_cmd 重映射到 /person_guard/ctrl_cmd_input，实际 /ctrl_cmd 仅由 person_command_gate 发布。
开始循迹先解除门控使能，待上游启动后校验相机时效及发布者拓扑，再调用 /person_guard/arm 使能；急停先锁住门控，再停止上游。服务重启后默认未使能。
用户在03车现场确认识别人、停车和恢复正常后，取消门控额外的0.2m/s试运行上限。
门控跟随上游速度，保留控制台原有0至2.0m/s有效范围校验、约0.1m/s²恢复加速和禁止倒车；前向摄像头不覆盖倒车方向。

## 接入验证

- 03车 /ctrl_cmd 发布者仅 /person_command_gate，底盘订阅者 /yhs_can_control_qt_node。
- 实际输出与底盘反馈均为零；未启动循迹或下发正速度。
- tests/ros_person_gate_probe.py 在隔离话题验证启动锁、恢复加速、人员停车及解除、人工停用锁、识别和指令断流。
- 解除试运行限速后，该测试还验证0.5m/s指令能够通过原0.2m/s上限，随后人员停车仍输出零速度。
- 测试脚本不向实际 /ctrl_cmd 发布，也不使能实际 /person_guard/arm。
- 尚未验证实车运动中的制动距离；现阶段为原型辅助保护，不是经过认证的人员防护设备。
