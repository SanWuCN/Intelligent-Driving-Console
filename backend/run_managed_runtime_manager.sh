#!/bin/bash
set -u
export PYTHONUNBUFFERED=1

if ! pgrep -f '[p]roc_manager.py' >/dev/null; then
  python2 /root/autoware_1.14.0/install/runtime_manager/lib/runtime_manager/proc_manager.py &
fi

exec python2 /from_host/bigcar-console/backend/managed_runtime_manager.py
