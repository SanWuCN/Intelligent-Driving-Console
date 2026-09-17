#!/usr/bin/env bash
# 同步车端控制台（后端 + 前端）到一辆或多辆车，并重启服务。
#
#   ./deploy-to-car.sh 192.168.31.134 192.168.31.232
#
# 约定：
#   * 车端 SSH 密码固定为 nvidia（脚本内部使用，不会打印）。
#   * dist/ 必须和后端一起同步：只更新 backend/ 会出现「后端新版、页面旧包」的错位。
#   * 每次部署前自动备份被覆盖的文件到 runtime/../backups/deploy-<时间戳>/。
set -euo pipefail

[[ $# -ge 1 ]] || { echo "用法: $0 <车端IP> [更多IP...]" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd)
DEST=/home/nvidia/Desktop/bigcar-console
PW=nvidia

remote() {  # remote <ip> <命令>
  ssh -o ConnectTimeout=10 -o StrictHostKeyChecking=no -o LogLevel=ERROR "nvidia@$1" 2>/dev/null \
    "echo $PW > /tmp/.dshpw; chmod 600 /tmp/.dshpw; sudo -S -p '' bash -c $(printf '%q' "$2") < /tmp/.dshpw 2>/dev/null; rm -f /tmp/.dshpw"
}

for IP in "$@"; do
  echo "=== $IP ==="
  if ! nc -z -G 3 "$IP" 22 2>/dev/null; then
    echo "  跳过：SSH 不可达（车辆离线？）"; continue
  fi
  STAMP=$(date +%Y%m%d-%H%M%S)
  remote "$IP" "mkdir -p $DEST/backups/deploy-$STAMP && cp -a $DEST/backend/app.py $DEST/backend/controller.py $DEST/backend/ros_probe.py $DEST/backups/deploy-$STAMP/ 2>/dev/null; cp -a $DEST/dist $DEST/backups/deploy-$STAMP/dist 2>/dev/null; true"
  echo "  已备份到 backups/deploy-$STAMP"

  scp -q -o ConnectTimeout=10 \
    "$HERE/backend/app.py" "$HERE/backend/controller.py" \
    "$HERE/backend/ros_bridge.py" "$HERE/backend/ros_probe.py" \
    "nvidia@$IP:$DEST/backend/"
  rsync -a --delete --timeout=60 "$HERE/dist/" "nvidia@$IP:$DEST/dist/"

  remote "$IP" "chmod 644 $DEST/backend/ros_bridge.py; systemctl restart bigcar-console; sleep 3; systemctl is-active bigcar-console"
  remote "$IP" "grep -o 'index-[A-Za-z0-9_-]*\.js' $DEST/dist/index.html"
done
