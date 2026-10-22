#!/usr/bin/env bash
# Checks the healthy end state from the worker's point of view: effective config, DNS and
# reachability of internal-api, and response time of /api/orders.
cd "$(dirname "${BASH_SOURCE[0]}")"
pass=0; fail=0
ok()  { echo "  PASS  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL  $1"; fail=$((fail+1)); }
[ -f config/worker-config.json ] || { echo "Missing config/worker-config.json (see config/README.txt)"; exit 2; }
docker compose ps --status running --services 2>/dev/null | grep -q pl-worker || { echo "Worker not running. Start with: docker compose up -d --build"; exit 2; }

cfg=$(docker compose exec -T pl-worker cat /etc/datadog/synthetics-check-runner.json 2>/dev/null)
echo "$cfg" | grep -q '"enableDefaultBlockedIpRanges": true' && bad "enableDefaultBlockedIpRanges is true (private IPs blocked)" || ok "private IP ranges not blocked"
echo "$cfg" | grep -q '"dnsUseHost": false' && bad "dnsUseHost is false (internal names won't resolve)" || ok "worker uses the host resolver"
{% raw %}net=$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$(docker compose ps -q pl-worker)" 2>/dev/null | awk '{print $1}'){% endraw %}
if docker run --rm --network "$net" curlimages/curl:8.10.1 -s -m 5 -o /dev/null -w "%{http_code}" http://internal-api:8080/health 2>/dev/null | grep -q 200; then
  ok "internal-api reachable from the worker's network ($net)"
else
  bad "internal-api not reachable from the worker's network ($net)"
fi
t=$(docker compose exec -T internal-api python -c "import time,urllib.request;s=time.time();urllib.request.urlopen('http://127.0.0.1:8080/api/orders',timeout=90);print(int(time.time()-s))" 2>/dev/null)
[ "${t:-99}" -lt 30 ] && ok "/api/orders responds in ${t}s" || bad "/api/orders took ${t:-?}s (test timeout risk)"
docker compose logs --since 5m pl-worker 2>/dev/null | grep -qiE "error|unauthorized|forbidden" && echo "  NOTE  worker logged errors recently: docker compose logs pl-worker"
echo; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
