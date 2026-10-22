#!/usr/bin/env bash
# Checks the healthy end state: forwarder reaches Datadog through the proxy, the
# OpenMetrics check runs cleanly and collects cart_total_value, logs and DogStatsD flow.
cd "$(dirname "${BASH_SOURCE[0]}")"
pass=0; fail=0
ok()  { echo "  PASS  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL  $1"; fail=$((fail+1)); }
status=$(docker compose exec -T datadog-agent agent status 2>/dev/null)
[ -z "$status" ] && { echo "Agent not running. Start with: docker compose up -d --build"; exit 2; }

echo "$status" | grep -qi "API Key valid" && ok "forwarder reaches Datadog (API key validated)" || bad "forwarder can't validate the API key (check proxy settings)"
echo "$status" | grep -A6 "openmetrics (" | grep -q "\[OK\]" && ok "openmetrics check is OK" || bad "openmetrics check is not OK (agent status → Collector)"
check=$(docker compose exec -T datadog-agent agent check openmetrics 2>/dev/null)
echo "$check" | grep -q "shop.cart_total_value" && ok "shop.cart_total_value collected" || bad "shop.cart_total_value not collected"
if echo "$status" | grep -q "Logs Agent" && ! echo "$status" | grep -qi "Logs Agent is not running"; then ok "logs agent running"; else bad "logs agent not running"; fi
echo "$status" | grep -A12 "^DogStatsD$" | grep -E "Metric Packets: [1-9]" >/dev/null && ok "DogStatsD receiving packets" || bad "DogStatsD not receiving packets from shop-app"
echo; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
