#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/home/nvidia/Desktop/bigcar-console"
SERVICE_FILE="/etc/systemd/system/bigcar-console.service"

if [[ ! -f "$APP_DIR/dist/index.html" ]]; then
  echo "Missing $APP_DIR/dist/index.html" >&2
  exit 1
fi

install -d -m 0755 "$APP_DIR/runtime/logs"
if [[ ! -f "$APP_DIR/runtime/config.json" ]]; then
  CONTROL_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  python3 - "$APP_DIR/runtime/config.json" "$CONTROL_TOKEN" <<'PY'
import json, sys
path, token = sys.argv[1:]
config = {
    "container": "autoware_ai_orin",
    "data_dir": "/home/nvidia/Desktop",
    "control_token": token,
    "listen": "0.0.0.0",
    "port": 8765,
    "display": ":0",
    "xauthority": "/run/user/1000/gdm/Xauthority",
    "screen_size": "1920x1080"
}
with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
  chmod 0600 "$APP_DIR/runtime/config.json"
fi

install -m 0644 "$APP_DIR/deploy/bigcar-console.service" "$SERVICE_FILE"
systemctl daemon-reload
systemctl enable --now bigcar-console.service
systemctl restart bigcar-console.service

install -m 0755 "$APP_DIR/deploy/bigcar-console.desktop" "/home/nvidia/Desktop/智能驾驶控制台.desktop"
chown nvidia:nvidia "/home/nvidia/Desktop/智能驾驶控制台.desktop"
rm -f "/home/nvidia/Desktop/智能驾驶大车控制台.desktop"

echo "bigcar-console installed"
echo "URL: http://$(hostname -I | awk '{print $1}'):8765"
echo -n "Control token: "
python3 - "$APP_DIR/runtime/config.json" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle)["control_token"])
PY
