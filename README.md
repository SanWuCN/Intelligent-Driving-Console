# 智能驾驶控制台

面向 Jetson AGX Orin、ROS 1 Melodic 和 Autoware.AI 1.14 的车载网页控制台。它将原本需要在多个终端和 Runtime Manager 中手工完成的启动、定位、路径加载和循迹操作，整合为带真实状态校验的六步流程。

> [!WARNING]
> 这是实车控制软件。网页急停不能替代物理急停或遥控接管。首次运行必须在封闭场地低速测试。

## 主要功能

- 六步引导：环境检查、底盘与雷达、Autoware、地图与标定、路径配置、循迹运行。
- 根据真实 ROS 节点、话题和话题消息推进流程，不把“按过按钮”当成“启动成功”。
- 在车载桌面打开 Runtime Manager、控制终端和 RViz。
- 网页内嵌 Jetson 桌面/RViz MJPEG 实时画面。
- 人工 `2D Pose Estimate` 完成后才启动 NDT。
- 支持选择 PCD 地图和 CSV 轨迹，并绘制路径预览。
- 监控 Docker、CAN、Hesai 激光雷达、TF、NDT、路径和控制话题。
- 控制令牌、启动前安全确认、常驻软件急停和结构化日志。
- 安全重启全部流程：先发布零速指令，再停止控制台管理的 ROS 节点。
- 以 `1920×1080` 车载屏为主界面，同时支持窄屏远程维护。
- `--simulate` 演示模式，无需连接 Docker 和 ROS。

## 系统架构

```text
浏览器（React + TypeScript）
          │ HTTP / JSON / MJPEG
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
| Autoware `velocity_set` 障碍停车距离 | `0.05 m` |
| 底盘防撞净阈值 | `50 mm` |
| 激光雷达 | Hesai PandarXT-16 |
| 容器 | `autoware_ai_orin` |
| 车载屏分辨率 | `1920×1080` |

底盘程序会在 50 mm 净阈值上叠加传感器安装偏置，实际触发距离会大于 50 mm。详见 [YHS 底盘修改说明](vehicle/yhs_can_control_qt/README.md)。

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

安装脚本会生成独立控制令牌、安装 `bigcar-console.service`、创建车载桌面快捷方式，并在 `0.0.0.0:8765` 提供网页服务。

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
| GET | `/api/rviz.mjpeg` | 车载桌面/RViz 实时画面 |
| POST | `/api/action` | 执行白名单控制动作 |

除急停外，写操作必须在 `X-Control-Token` 请求头中提供控制令牌。

## 当前限制

- CSV 轨迹当前为单次执行；到达终点后会停车，未实现自动循环。
- 仓库不包含完整 Autoware 源码，`vehicle/` 只保留 YHS 底盘的受控修改文件。
- 雷达外参、车辆轴距、转向极性、CSV 速度单位和 CAN 报文必须按实车复核。
- 当前参数是封闭实训室低速测试配置，不适用于公开道路。

## 安全与许可

部署前请阅读 [SECURITY.md](SECURITY.md)。仓库不保存控制令牌、密码、PID 或运行日志。

本仓库当前未附带开源许可证。除非权利人另行授权，否则保留所有权利。学校名称、校徽和其他品牌素材不因代码公开而授予使用权。
