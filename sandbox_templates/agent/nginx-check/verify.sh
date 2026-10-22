#!/usr/bin/env bash
# Checks the healthy end state: the nginx check is scheduled through Autodiscovery and
# reports OK, and the web container's logs use the nginx source.
cd "$(dirname "${BASH_SOURCE[0]}")"
pass=0; fail=0
ok()  { echo "  PASS  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL  $1"; fail=$((fail+1)); }
status=$(docker compose exec -T datadog-agent agent status 2>/dev/null)
[ -z "$status" ] && { echo "Agent not running. Start with: docker compose up -d --build"; exit 2; }

docker compose exec -T datadog-agent agent configcheck 2>/dev/null | grep -q "=== nginx check ===" \
  && ok "nginx check scheduled by Autodiscovery" || bad "no nginx check scheduled (agent configcheck)"
echo "$status" | grep -A8 "nginx (" | grep -q "\[OK\]" && ok "nginx check reports OK" || bad "nginx check not OK (agent status → Collector)"
echo "$status" | grep -A12 "^Logs Agent" >/dev/null && echo "$status" | grep -q "Source: nginx" \
  && ok "web logs collected with source nginx" || bad "web logs not tailed with source nginx"
echo; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
