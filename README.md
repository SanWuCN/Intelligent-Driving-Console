# 智能驾驶控制台

面向 Jetson AGX Orin、ROS 1 Melodic 和 Autoware.AI 1.14 的车载网页控制台。它将原本需要在多个终端和 Runtime Manager 中手工完成的启动、定位、路径加载和循迹操作，整合为带真实状态校验的六步流程。

> [!WARNING]
> 这是实车控制软件。网页急停不能替代物理急停或遥控接管。首次运行必须在封闭场地低速测试。

## 主要功能

- 六步引导：环境检查、底盘与雷达、Autoware、地图与标定、路径配置、循迹运行。
- 根据真实 ROS 节点、话题和话题消息推进流程，不把“按过按钮”当成“启动成功”。
- 在车载桌面打开 Runtime Manager、控制终端和 RViz。
- 网页内嵌可交互的 Jetson 屏幕监看，可直接操作 Runtime Manager 和 RViz。
- 人工 `2D Pose Estimate` 完成后才启动 NDT。
- 只列出配置数据目录中的 PCD/CSV，并跨浏览器重启记住上次选择。
- 支持闭环轨迹自动连续跑圈，非闭环路径会被安全拒绝。
- 运行中可调整限速、Pure Pursuit 前视距离、障碍停车距离和循环开关。
- 监控 Docker、CAN、Hesai 激光雷达、TF、NDT、路径和控制话题。
- 控制令牌、启动前安全确认、常驻软件急停和结构化日志。
- 安全重启全部流程：先发布零速指令，再停止控制台管理的 ROS 节点。
- 以 `1920×1080` 车载屏为主界面，同时支持窄屏远程维护。
- `--simulate` 演示模式，无需连接 Docker 和 ROS。

## 系统架构

```text
浏览器（React + TypeScript + noVNC）
          │ HTTP / JSON / WebSocket
          ▼
Jetson 宿主机 Python 服务 :8765
          │ 白名单动作 + docker exec
          ▼
autoware_ai_orin 容器
          │
ROS 1 Melodic / Autoware.AI 1.14
          │
NDT → Waypoints → Pure Pursuit → Twist Filter
          │ /ctrl_cmd
          ▼
YHS 底盘节点 → CAN0 → 车辆
```

详细数据流和状态判定见 [系统架构](docs/ARCHITECTURE.md)。

## 当前车辆参数

| 项目 | 当前值 |
| --- | ---: |
| 循迹速度上限 | `0.2 m/s` (`0.72 km/h`) |
| Pure Pursuit 最小前视距离 | `2 m` |
| 激光雷达避障（`velocity_set` 点云停车） | **已关闭**（`detection_range 0.0` / `points_threshold 2000000000`） |
| Autoware `velocity_set` 障碍停车距离 | `0.05 m`（仅在避障开启时生效） |
| A* 绕障（`astar_avoid`） | 已关闭（`enable_avoidance=false`） |
| 底盘防撞净阈值 | `50 mm` |
| 激光雷达 | Hesai PandarXT-16 |
| 容器 | `autoware_ai_orin` |
| 车载屏分辨率 | `1920×1080` |

底盘程序会在 50 mm 净阈值上叠加传感器安装偏置，实际触发距离会大于 50 mm。详见 [YHS 底盘修改说明](vehicle/yhs_can_control_qt/README.md)。

> [!WARNING]
> 激光雷达避障当前被刻意关闭，用于排除点云误判导致的停车。关闭后车辆不会因为前方点云障碍自动停车，只有底盘自身的 50 mm 防撞网和物理急停仍然有效。在开放场地或有人走动的环境恢复行驶前，请先用 `bigcar/diagnostics/enable_lidar_avoidance.sh` 打开避障。

避障开关的位置、持久化位置和回滚方式（`bigcar/diagnostics/` 下的 disable/enable 脚本）见 [Autoware.AI 可调参数与功能边界](docs/AUTOWARE_TUNING.md)。

更多定位、规划、感知和车辆控制调参项见 [Autoware.AI 可调参数与功能边界](docs/AUTOWARE_TUNING.md)。

## 环境要求

### 开发机

- Node.js 20+
- npm
- Python 3.8+

### 车端

- NVIDIA Jetson AGX Orin
- Ubuntu + X11 桌面
- Docker
- ROS 1 Melodic
- Autoware.AI 1.14
- FFmpeg
- 已配置的 `autoware_ai_orin` 容器
- 宿主机 CAN0 和容器 ROS 网络

## 本机开发

多车管理平台已提供：本机运行 `npm run build` 后执行 `npm run fleet`，打开 `http://127.0.0.1:8870/fleet`。支持按 IP 添加车辆、批量六步流程、人工定位队列、任务记录与系统设置。详细操作与部署见 [本机管理平台](docs/FLEET.md)。

```bash
git clone https://github.com/SanWuCN/Intelligent-Driving-Console.git
cd Intelligent-Driving-Console
npm ci
npm run build
python3 backend/app.py --simulate --host 127.0.0.1 --port 8765
```

将 `runtime/config.example.json` 复制为 `runtime/config.json`，并把 `data_dir` 指向包含自己 PCD/CSV 的目录。然后打开 `http://127.0.0.1:8765`。

## 测试

```bash
npm test
npm run build
```

单元测试覆盖路径解析、路径穿越防护、流程状态、急停、重启、人工定位、`/ctrl_cmd` 成功判据、速度限制和障碍停车距离。

## 车端部署

完整步骤见 [车端部署指南](docs/DEPLOYMENT.md)。默认目录为：

```text
/home/nvidia/Desktop/bigcar-console
```

构建并同步代码后执行：

```bash
sudo bash /home/nvidia/Desktop/bigcar-console/deploy/install.sh
```

安装脚本会将控制令牌设为 `801801801`、安装 `bigcar-console.service`、创建车载桌面快捷方式，并在 `0.0.0.0:8765` 提供网页服务。实车实验结束后建议换成随机强令牌。

## 目录结构

```text
backend/                 Python HTTP 服务、ROS 编排和定位辅助
deploy/                  systemd、桌面快捷方式和安装脚本
docs/                    架构、部署和设计文档
public/brand/            学校品牌素材
runtime/                 本机配置、PID 和日志（实际数据不入库）
src/                     React/TypeScript 前端源码
tests/                   Python 单元测试
vehicle/                 YHS 底盘防撞修改文件和说明
```

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/api/health` | 服务健康与版本 |
| GET | `/api/state` | 聚合系统、ROS、CAN 和流程状态 |
| GET | `/api/route?file=...` | 读取白名单 CSV 路径 |
| POST | `/api/screen-ticket` | 签发一次性车载屏幕会话 |
| GET | `/api/screen?ticket=...` | WebSocket 到 x11vnc 的可交互屏幕代理 |
| GET | `/api/rviz.mjpeg` | 只读桌面画面（降级诊断用） |
| POST | `/api/action` | 执行白名单控制动作 |

除急停外，写操作必须在 `X-Control-Token` 请求头中提供控制令牌。

## 当前限制

- 页面中的“路径预览”是真实 CSV 航迹的二维绘制，不是 PCD 点云地图渲染；PCD 地图和实时雷达由屏幕监看中的 RViz 显示。
- 自动循环通过生成 200 圈连续航点实现；这对实训演示等价于长时间自动循环，但不是无限航程。
- 当前未启动“行人分类”链路；`velocity_set` 只把点云视为障碍物并减速/停车。
- 激光雷达避障当前已关闭（`LIDAR_OBSTACLE_AVOIDANCE_ENABLED = False`），`velocity_set` 只做航点速度整形，不再因点云停车。`astar_avoid` 节点的 `enable_avoidance` 同样为 `false`，因此当前**没有任何自动避障**，只有底盘 50 mm 防撞网。
- 当前 `/ctrl_cmd` 只含速度、加速度和转角，未实现转向灯 CAN 命令。
- 仓库不包含完整 Autoware 源码，`vehicle/` 只保留 YHS 底盘的受控修改文件。
- 雷达外参、车辆轴距、转向极性、CSV 速度单位和 CAN 报文必须按实车复核。
- 当前参数是封闭实训室低速测试配置，不适用于公开道路。

## 安全与许可

部署前请阅读 [SECURITY.md](SECURITY.md)。仓库不保存车端 VNC/SSH 密码、PID 或运行日志；`801801801` 是实训环境的约定默认控制令牌，不应用于不可信网络。

本仓库当前未附带开源许可证。除非权利人另行授权，否则保留所有权利。学校名称、校徽和其他品牌素材不因代码公开而授予使用权。
