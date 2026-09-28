#!/usr/bin/env bash
# Keep the vehicle's NetworkManager-managed Wi-Fi link available without
# depending on public Internet access.  A link is healthy when NetworkManager
# reports it connected and it owns an IPv4 address; no external host is pinged.
set -uo pipefail

export LC_ALL=C

WIFI_INTERFACE="${WIFI_INTERFACE:-wlan0}"
CHECK_INTERVAL_SECONDS="${CHECK_INTERVAL_SECONDS:-15}"
RADIO_RESET_AFTER_FAILURES="${RADIO_RESET_AFTER_FAILURES:-4}"
RUN_ONCE=false

if [[ "${1:-}" == "--once" ]]; then
  RUN_ONCE=true
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--once]" >&2
  exit 2
fi

log() {
  printf '%s %s\n' "$(date --iso-8601=seconds)" "$*"
}

disable_runtime_power_save() {
  if command -v iw >/dev/null 2>&1; then
    if iw dev "$WIFI_INTERFACE" set power_save off >/dev/null 2>&1; then
      return 0
    fi
  fi
  return 1
}

link_is_healthy() {
  local state
  state="$(nmcli -g GENERAL.STATE device show "$WIFI_INTERFACE" 2>/dev/null || true)"
  [[ "$state" == 100* ]] || return 1
  ip -4 -o address show dev "$WIFI_INTERFACE" scope global 2>/dev/null | grep -q .
}

reconnect() {
  local uuid type autoconnect

  log "Wi-Fi link is down; asking NetworkManager to reconnect $WIFI_INTERFACE"
  nmcli radio wifi on >/dev/null 2>&1 || true
  nmcli device set "$WIFI_INTERFACE" managed yes >/dev/null 2>&1 || true
  nmcli device wifi rescan ifname "$WIFI_INTERFACE" >/dev/null 2>&1 || true

  # First let NetworkManager select the strongest eligible saved profile.
  if nmcli --wait 20 device connect "$WIFI_INTERFACE" >/dev/null 2>&1; then
    disable_runtime_power_save || true
    return 0
  fi

  # Some older NetworkManager builds do not auto-select on "device connect".
  # Fall back to every saved, autoconnect-enabled Wi-Fi profile by UUID.
  while IFS=: read -r uuid type autoconnect; do
    [[ "$type" == "802-11-wireless" && "$autoconnect" == "yes" ]] || continue
    if nmcli --wait 20 connection up uuid "$uuid" ifname "$WIFI_INTERFACE" >/dev/null 2>&1; then
      disable_runtime_power_save || true
      return 0
    fi
  done < <(nmcli -t -f UUID,TYPE,AUTOCONNECT connection show 2>/dev/null || true)

  return 1
}

if ! command -v nmcli >/dev/null 2>&1; then
  log "nmcli is missing; cannot manage Wi-Fi"
  exit 1
fi

disable_runtime_power_save || log "could not apply runtime power-save setting yet"
failures=0

while true; do
  if link_is_healthy; then
    if (( failures > 0 )); then
      log "Wi-Fi link recovered on $WIFI_INTERFACE"
    fi
    failures=0
    disable_runtime_power_save || true
    $RUN_ONCE && exit 0
  else
    ((failures += 1))
    if (( failures >= RADIO_RESET_AFTER_FAILURES )); then
      log "Wi-Fi stayed down for $failures checks; resetting the Wi-Fi radio"
      nmcli radio wifi off >/dev/null 2>&1 || true
      sleep 2
      nmcli radio wifi on >/dev/null 2>&1 || true
      failures=0
    fi

    if reconnect && link_is_healthy; then
      log "Wi-Fi reconnect succeeded on $WIFI_INTERFACE"
      failures=0
      $RUN_ONCE && exit 0
    elif $RUN_ONCE; then
      log "Wi-Fi reconnect did not find an available saved network"
      exit 1
    fi
  fi

  sleep "$CHECK_INTERVAL_SECONDS"
done
