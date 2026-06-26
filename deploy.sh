#!/usr/bin/env bash
# Deploy gate: NEVER restart the money path on a red suite. Runs the full security
# audit first; only restarts verified-burst.service if every deterministic invariant
# holds, then confirms health. Use this instead of `systemctl restart` by hand.
set -euo pipefail
cd /root/inference-burst

echo "[deploy] running security audit gate…"
if ! .venv/bin/python audit.py; then
  echo "[deploy] AUDIT FAILED — refusing to restart the broker. Fix and retry." >&2
  exit 1
fi

echo "[deploy] audit GREEN — restarting verified-burst.service"
systemctl restart verified-burst.service
sleep 2
state=$(systemctl is-active verified-burst.service)
echo "[deploy] service: $state"
if [ "$state" != "active" ]; then
  echo "[deploy] service did not come up active!" >&2
  exit 1
fi
code=$(curl -sS -o /dev/null -w '%{http_code}' http://127.0.0.1:8402/healthz || true)
echo "[deploy] healthz: HTTP $code"
[ "$code" = "200" ] && echo "[deploy] OK — deployed behind a green audit." || { echo "[deploy] health check failed" >&2; exit 1; }
