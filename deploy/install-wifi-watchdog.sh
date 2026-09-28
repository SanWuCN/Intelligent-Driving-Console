#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/home/nvidia/Desktop/bigcar-console}"
WIFI_INTERFACE="${WIFI_INTERFACE:-wlan0}"
WIFI_SSID="${WIFI_SSID:-tx801}"
WIFI_BSSID="${WIFI_BSSID:-64:64:4A:A3:0F:B3}"

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

# Select the currently working tx801 profile.  Cars often accumulate old
# profiles (including duplicate tx801 entries without a usable PSK); allowing
# all of them to autoconnect makes recovery non-deterministic.
target_uuid="$(nmcli -g GENERAL.CON-UUID device show "$WIFI_INTERFACE" 2>/dev/null || true)"
if [[ -n "$target_uuid" ]]; then
  active_ssid="$(nmcli -g 802-11-wireless.ssid connection show uuid "$target_uuid" 2>/dev/null || true)"
  [[ "$active_ssid" == "$WIFI_SSID" ]] || target_uuid=""
fi
if [[ -z "$target_uuid" ]]; then
  while IFS=: read -r uuid type; do
    [[ "$type" == "802-11-wireless" ]] || continue
    ssid="$(nmcli -g 802-11-wireless.ssid connection show uuid "$uuid" 2>/dev/null || true)"
    if [[ "$ssid" == "$WIFI_SSID" ]]; then
      target_uuid="$uuid"
      break
    fi
  done < <(nmcli -t -f UUID,TYPE connection show)
fi
[[ -n "$target_uuid" ]] || { echo "No saved $WIFI_SSID profile found" >&2; exit 1; }

# Pin the control profile to the known-good AP and make it the only Wi-Fi
# profile eligible for automatic recovery.  Profiles are preserved for manual
# use; only their autoconnect flag is disabled.
while IFS=: read -r uuid type autoconnect; do
  [[ "$type" == "802-11-wireless" ]] || continue
  nmcli connection modify uuid "$uuid" 802-11-wireless.powersave 2
  if [[ "$uuid" == "$target_uuid" ]]; then
    nmcli connection modify uuid "$uuid" \
      connection.autoconnect yes \
      connection.autoconnect-priority 100 \
      connection.autoconnect-retries 0 \
      802-11-wireless.bssid "$WIFI_BSSID"
  else
    nmcli connection modify uuid "$uuid" connection.autoconnect no
  fi
done < <(nmcli -t -f UUID,TYPE,AUTOCONNECT connection show)

nmcli general reload >/dev/null 2>&1 || true
if command -v iw >/dev/null 2>&1; then
  iw dev "$WIFI_INTERFACE" set power_save off >/dev/null 2>&1 || true
fi

systemctl daemon-reload
systemctl enable --now bigcar-wifi-watchdog.service
systemctl restart bigcar-wifi-watchdog.service

echo "bigcar Wi-Fi power saving disabled; reconnect watchdog installed"
