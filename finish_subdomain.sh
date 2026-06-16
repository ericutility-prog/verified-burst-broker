#!/usr/bin/env bash
# Waits for burst.solcleus.com DNS to point here, then issues TLS + registers the
# branded resource on x402scan. Idempotent + safe to re-run. Logs to finish_subdomain.log.
set -u
HOST="burst.solcleus.com"
IP="2.24.86.189"
EMAIL="ericutility@gmail.com"
LOG="/root/inference-burst/finish_subdomain.log"
MAX_TRIES=180          # ~3h at 60s
SLEEP=60

say(){ echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }

say "watcher started; waiting for $HOST -> $IP"
ok=0
for i in $(seq 1 $MAX_TRIES); do
  got="$(getent hosts "$HOST" | awk '{print $1}' | head -1)"
  if [ "$got" = "$IP" ]; then ok=1; say "DNS resolved on try $i ($HOST -> $got)"; break; fi
  sleep $SLEEP
done
if [ "$ok" != "1" ]; then say "TIMEOUT: DNS never resolved to $IP. Re-run this script after adding the A record."; exit 2; fi

# Let HTTP settle, then issue cert (reuses existing prod ACME account).
sleep 3
if [ -f "/etc/letsencrypt/live/$HOST/fullchain.pem" ]; then
  say "cert already present for $HOST, skipping issuance"
else
  say "running certbot --nginx for $HOST"
  certbot --nginx -d "$HOST" -n --agree-tos -m "$EMAIL" --redirect >>"$LOG" 2>&1
  rc=$?
  if [ $rc -ne 0 ]; then say "certbot FAILED (rc=$rc) — see log"; exit 3; fi
  say "certbot succeeded"
fi
systemctl reload nginx >>"$LOG" 2>&1

# Verify HTTPS endpoint over the public name.
code="$(curl -s -m 10 -o /dev/null -w '%{http_code}' "https://$HOST/v1/burst")"
say "GET https://$HOST/v1/burst -> $code (expect 402)"
if [ "$code" != "402" ]; then say "endpoint not returning 402 over TLS yet; aborting registration"; exit 4; fi

# Register the branded resource on x402scan (public, no-auth tRPC path).
say "registering https://$HOST/v1/burst on x402scan"
resp="$(curl -s -m 60 -X POST 'https://www.x402scan.com/api/trpc/public.resources.register?batch=1' \
  -H 'Content-Type: application/json' -H 'x-trpc-source: nextjs-react' \
  -H 'User-Agent: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36' \
  -d "{\"0\":{\"json\":{\"url\":\"https://$HOST/v1/burst\"}}}")"
echo "$resp" >>"$LOG"
if echo "$resp" | grep -q '"success":true'; then
  say "REGISTERED ✓  $(echo "$resp" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=d[0]["result"]["data"]["json"]["resource"]["resource"]; print("resourceId",r["id"],"originId",r["originId"])' 2>/dev/null)"
else
  say "registration response (not success): $(echo "$resp" | head -c 300)"
fi
say "DONE"
