# 车端部署指南

## 前置条件

- Jetson AGX Orin 宿主机可正常登录。
- Docker 容器 `autoware_ai_orin` 存在。
- 容器中已安装 ROS 1 Melodic 和 Autoware.AI 1.14。
- `/opt/ros/melodic/setup.bash` 和 `/root/autoware_1.14.0/install/setup.bash` 存在。
- Hesai 雷达、YHS 底盘驱动以及 CAN0 已按实车配置。
- Jetson 图形桌面使用 X11，默认 `DISPLAY=:0`。
- 宿主机已安装 Python 3 和 FFmpeg。

## 1. 构建与测试

```bash
npm ci
npm test
npm run build
```

`dist/` 是需要同步到车端的构建产物，但不提交到 Git。

## 2. 同步代码

```bash
ssh nvidia@VEHICLE_IP 'mkdir -p /home/nvidia/Desktop/bigcar-console'
rsync -az --delete \
  --exclude node_modules \
  --exclude runtime/ \
  ./ nvidia@VEHICLE_IP:/home/nvidia/Desktop/bigcar-console/
```

不要用开发机的 `runtime/` 覆盖车端配置、PID 和日志。

## 3. 安装服务

```bash
ssh nvidia@VEHICLE_IP
sudo bash /home/nvidia/Desktop/bigcar-console/deploy/install.sh
```

首次安装会在 `/home/nvidia/Desktop/bigcar-console/runtime/config.json` 中生成控制令牌。该文件权限为 `0600`，不应提交到 Git 或发送到公开聊天。

## 4. 检查服务

```bash
sudo systemctl status bigcar-console.service
curl http://127.0.0.1:8765/api/health
sudo journalctl -u bigcar-console.service -f
```

安装后桌面会出现“智能驾驶控制台”快捷方式。车端访问 `http://127.0.0.1:8765`，局域网设备使用 Jetson IP 访问 `8765` 端口。

## 5. 升级

1. 在开发机执行测试与构建。
2. 用 rsync 同步代码，继续排除 `runtime/config.json`。
3. 重启 Web 服务：

```bash
sudo systemctl restart bigcar-console.service
```

重启 Web 服务不会自动重启或停止已运行的 ROS 节点。如需重置整套车辆流程，应在控制台中使用“重启流程”。

## 6. 上车检查

- 物理急停可用，遥控器可立即接管。
- 车辆周围无人员和障碍物。
- CAN0、雷达、TF、NDT 和 `/current_pose` 正常。
- CSV 方向与车头方向一致。
- `/final_waypoints` 速度不超过当前安全上限。
- 循迹启动后 `/ctrl_cmd` 持续更新。
