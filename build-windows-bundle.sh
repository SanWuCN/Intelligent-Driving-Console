#!/usr/bin/env bash
# 打一个 Windows 上「双击即用」的本机管理端压缩包。
#
#   ./build-windows-bundle.sh [输出目录]
#
# 产物：<输出目录>/bigcar-console-win-<日期>.zip
# 包内是 fleet 本机管理平台（不是车端页面），预置实训车01 / 实训车03。
# 目标机器只需要装 Python 3（Windows 安装时勾选 Add python.exe to PATH）。
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
OUT_DIR=${1:-$HERE/dist-bundle}
STAMP=$(date +%Y%m%d)
NAME=bigcar-console-win-$STAMP
STAGE=$(mktemp -d)
PKG="$STAGE/$NAME"

echo "==> 构建前端"
(cd "$HERE" && npm run build >/dev/null)

echo "==> 收集文件"
mkdir -p "$PKG/backend" "$PKG/runtime" "$PKG/docs" "$PKG/public" "$PKG/deploy"

# 本机管理平台：只需要这几个后端文件
cp "$HERE/backend/fleet.py" "$HERE/backend/app.py" "$HERE/backend/controller.py" \
   "$HERE/backend/ros_probe.py" "$HERE/backend/ros_bridge.py" "$PKG/backend/"
cp -R "$HERE/dist" "$PKG/dist"
cp -R "$HERE/public/." "$PKG/public/" 2>/dev/null || true
cp "$HERE/docs/FLEET.md" "$HERE/docs/LIVE_VIEW.md" "$PKG/docs/" 2>/dev/null || true

echo "==> 预置车辆（实训车01 / 实训车03）"
# 控制令牌与车端 runtime/config.json 的 control_token 一致，客户不用再输
python3 - "$PKG/runtime/fleet.json" <<'PY'
import json, sys, time, uuid
path = sys.argv[1]
vehicles = {
    "实训车01": "192.168.31.134",
    "实训车03": "192.168.31.232",
}
data = {
    "settings": {"vehicle_port": 8765, "poll_seconds": 3, "step_timeout": 120, "localization_timeout": 1800},
    "vehicles": {
        uuid.uuid4().hex: {
            "id": uuid.uuid4().hex, "name": name, "ip": ip, "port": 8765, "token": "801801801",
        } for name, ip in vehicles.items()
    },
    "jobs": [],
}
# 车辆 id 必须和字典键一致
for key, vehicle in data["vehicles"].items():
    vehicle["id"] = key
with open(path, "w", encoding="utf-8") as handle:
    json.dump(data, handle, ensure_ascii=False, indent=2)
print("  已写入", ", ".join("%s(%s)" % (v["name"], v["ip"]) for v in data["vehicles"].values()))
PY

cat > "$PKG/start.bat" <<'BAT'
@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo   [错误] 没有找到 python 命令。
  echo   请先安装 Python 3：https://www.python.org/downloads/windows/
  echo   安装时务必勾选 "Add python.exe to PATH"，装完重新双击本文件。
  echo.
  pause
  exit /b 1
)

echo.
echo   正在启动智能驾驶管理平台 ...
echo   浏览器会自动打开 http://127.0.0.1:8870/fleet
echo   关闭本窗口即停止服务。
echo.

start "" http://127.0.0.1:8870/fleet
python backend\fleet.py --port 8870
echo.
echo   服务已停止。
pause
BAT

cat > "$PKG/README-Windows.txt" <<'TXT'
智能驾驶管理平台 · Windows 本机版
=================================

一、运行条件
  1. 安装 Python 3（3.8 以上都行）：https://www.python.org/downloads/windows/
     安装时务必勾选 “Add python.exe to PATH”。
  2. 电脑要和实训车在同一个局域网（能 ping 通 192.168.31.134 / 192.168.31.232）。

二、怎么用
  双击「start.bat」→ 浏览器会自动打开 http://127.0.0.1:8870/fleet
  关闭那个黑窗口 = 停止服务。

三、已经预置好的车辆
  实训车01   192.168.31.134
  实训车03   192.168.31.232
  控制令牌已填好（801801801），不需要再输入。

  如果车的 IP 变了：车辆管理 → 编辑该车 → 改 IP → 保存。
  要加新车：车辆管理 → 添加车辆 → 填名称、IP、控制令牌。

四、能做什么
  · 车辆管理：在线状态、步骤进度、电量/CPU/温度、打开该车控制台、屏幕监看、急停。
  · 批量任务：勾选车辆 → 选地图与路径 → 一键启动批量流程。
  · 任务记录：步骤账本、事件、导出 CSV / JSON。
  · 实时地图与雷达：在车辆卡片上点「打开控制台」，进入车端页面的「实时地图与雷达」，
    可以看到点云地图、路线、实时位姿、激光雷达点云和行驶轨迹。

五、连不上怎么办
  · 管理平台左下角显示「管理服务正常」但车辆显示离线：
      在 Windows 上开一个终端执行  ping 192.168.31.134
      不通 = 电脑和车不在同一网络，或车没开机/没连上 Wi-Fi。
  · 车辆显示「控制令牌无效」：车辆管理 → 编辑 → 重新填令牌。
  · 打不开页面：看黑窗口里的报错；端口被占用时，可以编辑 bat 把 8870 换成 8871。

六、重要提醒
  这是实车控制软件。网页急停不能替代物理急停或遥控接管；
  首次运行必须在封闭场地、低速、有人能随时接管的前提下测试。
  本机版只负责调度，车辆动作仍由车端控制台执行。
TXT

echo "==> 打包"
mkdir -p "$OUT_DIR"
(cd "$STAGE" && zip -qr "$OUT_DIR/$NAME.zip" "$NAME")
rm -rf "$STAGE"
echo "==> 完成: $OUT_DIR/$NAME.zip"
unzip -l "$OUT_DIR/$NAME.zip" | tail -12
