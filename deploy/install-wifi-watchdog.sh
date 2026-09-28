#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/nvidia/Desktop/bigcar-console}"

if [[ ! -x /usr/bin/nmcli && ! -x /bin/nmcli ]]; then
  echo "NetworkManager/nmcli is required" >&2
  exit 1
fi

install -m 0755 "$APP_DIR/deploy/wifi-watchdog.sh" /usr/local/sbin/bigcar-wifi-watchdog
rm -f /etc/NetworkManager/conf.d/90-bigcar-wifi-powersave.conf
install -m 0644 "$APP_DIR/deploy/zz-bigcar-wifi-powersave.conf" \
  /etc/NetworkManager/conf.d/zz-bigcar-wifi-powersave.conf
install -m 0644 "$APP_DIR/deploy/bigcar-wifi-watchdog.service" \
  /etc/systemd/system/bigcar-wifi-watchdog.service

# Make the setting explicit on every saved Wi-Fi profile.  This takes
# precedence over distribution defaults and survives reconnects/reboots.
while IFS=: read -r uuid type autoconnect; do
  [[ "$type" == "802-11-wireless" ]] || continue
  nmcli connection modify uuid "$uuid" 802-11-wireless.powersave 2
  if [[ "$autoconnect" == "yes" ]]; then
    nmcli connection modify uuid "$uuid" connection.autoconnect-retries 0
  fi
done < <(nmcli -t -f UUID,TYPE,AUTOCONNECT connection show)

nmcli general reload >/dev/null 2>&1 || true
if command -v iw >/dev/null 2>&1; then
  iw dev wlan0 set power_save off >/dev/null 2>&1 || true
fi

systemctl daemon-reload
systemctl enable --now bigcar-wifi-watchdog.service
systemctl restart bigcar-wifi-watchdog.service

echo "bigcar Wi-Fi power saving disabled; reconnect watchdog installed"
