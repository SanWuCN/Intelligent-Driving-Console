# 实时地图与雷达（RViz 式画面）

车端控制台中间那块「实时地图与雷达」不再是静态的 CSV 预览图，而是和 RViz 一样的
低延迟俯视图：点云地图 + 路线 + 实时位姿 + 激光雷达点云 + 行驶轨迹。

## 数据链路

```text
容器内 ROS 话题                    ros_bridge.py（python2, 容器内）
  /bms_flag_Infor_fb  /bms_Infor_fb  ─┐
  /current_pose                       ├─ 订阅 + 降采样 + 坐标变换
  /final_waypoints                    │
  /points_raw (PointCloud2)          ─┘
        │
        │ 二进制帧：'BC' + type(u8) + len(u32 LE) + payload
        ▼
bigcar-console（python3, 宿主机）── /api/live（WebSocket）── 浏览器 Canvas 2D
```

- 桥接进程由 `backend/controller.py` 的 `LiveBridge` 懒启动：**只有浏览器在看时才起**，
  全部断开后发送 `{"cmd":"stop"}`，车端不会有人没人都在算点云。
- 桥接命令：`docker exec -i <容器> bash -lc "<source ROS>; exec python2 ros_bridge.py"`。
  容器里默认的 `python` 是 Python 3 且没 source ROS，必须显式 `bash -lc` + `python2`。
- 点云解析优先走 numpy（Jetson 上有 numpy 1.13），实测单帧 **1326 ms → 32 ms**；
  没有 numpy 时自动退回纯 Python 实现。

## 协议

桥接 stdout（宿主 `LiveBridge._read_frames` 解析）：

| type | 名称 | 载荷 |
| --- | --- | --- |
| 1 | hello | JSON：桥接自述 |
| 2 | status | JSON：`ros` / `streaming` / `pose_hz` / `cloud` |
| 3 | battery | JSON：BMS 电量 |
| 4 | pose | JSON：`x y yaw z frame_id speed steering` |
| 5 | waypoints | JSON：`/final_waypoints` 降采样后的坐标数组 |
| 6 | trace | JSON：最近 90 秒行驶轨迹 |
| 7 | cloud | `u32 头长度 | JSON 头 | float32 xyz` |
| 8 | map | 同 cloud，一次性 |
| 9 | error | JSON：`scope` + `message` |

浏览器 `/api/live`（WebSocket，二进制帧 `[type u8][payload]`）：

| type | 名称 | 载荷 |
| --- | --- | --- |
| 1 | json | welcome / route / pose / battery / waypoints / trace / bridge / bridge_error / pong |
| 2 | map | `u32 头长度 | JSON 头 | float32 xyz` |
| 3 | cloud | 同上 |

浏览器可下发：`{"cmd":"map","map":"map.pcd"}`、`{"cmd":"cloud","enabled":false}`、`{"cmd":"ping"}`。

## 帧格式的坑

`struct.Struct("<BI")` 会按**原生对齐**补成 7 字节，桥接写的是 5 字节 —— 两边必须都用
`"<B I"`（带空格表示标准尺寸）。帧头之后才是 payload，读的时候别忘了跳过 2 字节 magic。

## 订阅与鉴权

- `/api/live-ticket`（POST）签发一次性票据，**不要求控制令牌**：地图、雷达、位姿、电量是
  只读遥测，打开页面就该看得到；危险动作仍然由 `/api/action` 的令牌把关。
- `/api/live?ticket=...` 校验票据后升级为 WebSocket，票据 60 秒过期且只能用一次。
- 每次新页面连接都会带 `fresh=True` 重新要一次地图：地图是静态的，但服务端只缓存最新一帧，
  新连接需要它。先 `set_wants` 再 `request_map`，否则地图帧会比订阅名单先到而被丢掉。

## 前端

`src/useLiveScene.ts` 负责协议与重连；`src/components/LiveMap.tsx` 负责画：

- 地图只在拿到时投影一次，栅格化成一张 1800 px 的位图，之后只做整体平移缩放，
  避免每帧重算 7.7 万个点。
- 雷达按「离车距离」上色（近蓝远红），没定位时按高度上色。
- 鼠标滚轮缩放、拖动平移；「跟随」让视角跟着车走，「全图」缩放到整张地图。
- 读数区显示剩余电量、当前速度、地图点数、雷达点数、坐标系；位姿超过 3 秒没更新会提示
  去 RViz 做初始位姿标定。

## 本机联调（没有 ROS 也能跑）

`ros_bridge.py --mock` 会造一个合成场景（环形路线 + 16 线雷达 + 电量），
宿主用环境变量指向它即可：

```bash
BIGCAR_BRIDGE_COMMAND="python3 backend/ros_bridge.py --mock" \
BIGCAR_MAP_DIR=/tmp/bigcar-demo-data \
python3 backend/app.py --port 8799
```

`tests/live-console-browser.mjs` 就是用这个模式做的浏览器集成测试
（校验 1366×768 不溢出、速度档位、电量上屏、地图与雷达真的画到画布上）。
