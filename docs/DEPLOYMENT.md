# 车端部署指南

## 前置条件

- Jetson AGX Orin 宿主机可正常登录。
- Docker 容器 `autoware_ai_orin` 存在。
- 容器中已安装 ROS 1 Melodic 和 Autoware.AI 1.14。
- `/opt/ros/melodic/setup.bash` 和 `/root/autoware_1.14.0/install/setup.bash` 存在。
- Hesai 雷达、YHS 底盘驱动以及 CAN0 已按实车配置。
- Jetson 图形桌面使用 X11，默认 `DISPLAY=:0`。
- 宿主机已安装 Python 3、FFmpeg 和 x11vnc，`x11vnc.service` 监听本机 `5900`。

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

安装脚本会把控制令牌更新为项目约定的 `801801801`，并补齐 VNC、上次文件和运行参数配置。`runtime/config.json` 权限为 `0600`，不应提交到 Git。对外网络部署时必须换成强令牌。

将 `runtime/config.json` 中的 `vnc_password` 设为车端 x11vnc 密码。安装脚本不会把真实 VNC 密码写入仓库。

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
3. **`dist/` 也要一起同步**：只更新 `backend/` 会出现「后端已经是新版、页面还是旧包」的
   错位（页面按文件哈希找 JS，旧 bundle 里没有新界面）。
4. 重启 Web 服务：

```bash
sudo systemctl restart bigcar-console.service
```

重启 Web 服务不会自动重启或停止已运行的 ROS 节点。如需重置整套车辆流程，应在控制台中使用“重启流程”。

### 实时画面相关的车端依赖

实时地图/雷达靠容器内的 `python2` 跑 `backend/ros_bridge.py`：

- 服务以 root 运行，`LiveBridge` 自己执行
  `docker exec -i autoware_ai_orin bash -lc "<source ROS>; exec python2 ros_bridge.py"`，
  不需要额外的 systemd 单元；容器里的 `python` 是 Python 3 且没 source ROS，必须走 `bash -lc`。
- 容器内需要 `numpy`（Jetson 镜像自带 1.13）才能让单帧点云从 ~1.3 s 降到 ~30 ms；
  没有 numpy 时会自动退回纯 Python。
- 只有浏览器打开实时视图时才启动桥接，页面全部关闭后桥接会收到 `stop` 并进入空闲。
- 排查：

```bash
curl -s http://127.0.0.1:8765/api/state | python3 -m json.tool | grep -A 8 '"live"'
tail -f /home/nvidia/Desktop/bigcar-console/runtime/logs/console.log   # 找 live/tuning 两栏
```

## 6. VNC 开机后无法连接

若 `journalctl -b` 出现 `ordering cycle` 并删除 `x11vnc.service/start`，检查服务是否同时配置 `After=graphical.target` 和 `WantedBy=multi-user.target`。这会形成启动依赖循环。可用仓库内修正后的服务替换：

```bash
sudo install -m 0644 deploy/x11vnc.service /etc/systemd/system/x11vnc.service
sudo systemctl daemon-reload
sudo systemctl enable --now x11vnc.service
```

该服务沿用 `/etc/x11vnc.pass`，共享物理桌面 `:0`，连接端口为 `5900`。旧的 `vnc-resolution.service` 将屏幕降为 `1366x768`，1920×1080 车载屏应禁用该旧服务。`5901` 是独立虚拟桌面，不是物理车载屏幕。

若网页屏幕监看缺少 VNC 凭证，可在车端执行 `sudo python3 deploy/sync_vnc_credentials.py`，随后重启 `bigcar-console`。该脚本依赖车端 Python 的 `Crypto.Cipher.DES`，读取现有 VNC 密码文件并写入权限为 `0600` 的运行配置，不更改或显示密码。

## 7. 上车检查

- 物理急停可用，遥控器可立即接管。
- 车辆周围无人员和障碍物。
- CAN0、雷达、TF、NDT 和 `/current_pose` 正常。
- CSV 方向与车头方向一致。
- `/final_waypoints` 速度不超过当前安全上限。
- 循迹启动后 `/ctrl_cmd` 持续更新。
