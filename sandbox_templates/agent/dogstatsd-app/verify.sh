#!/usr/bin/env bash
# Checks the healthy end state: the Agent receives DogStatsD packets from checkout,
# with key:value tags, the expected percentiles, and a valid API key.
cd "$(dirname "${BASH_SOURCE[0]}")"
pass=0; fail=0
ok()  { echo "  PASS  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL  $1"; fail=$((fail+1)); }
status=$(docker compose exec -T datadog-agent agent status 2>/dev/null)
[ -z "$status" ] && { echo "Agent not running. Start with: docker compose up -d --build"; exit 2; }

echo "$status" | grep -qi "API Key valid" && ok "API key accepted by the configured site" || bad "API key not valid for the configured site (check forwarder section of agent status)"
packets=$(echo "$status" | grep -A12 "^DogStatsD$" | grep -m1 "Metric Packets:" | grep -oE "[0-9][0-9,]*" | tr -d , | head -1)
[ "${packets:-0}" -gt 0 ] && ok "DogStatsD received $packets metric packets" || bad "DogStatsD received no metric packets"
stats=$(docker compose exec -T datadog-agent agent dogstatsd-stats 2>/dev/null)
echo "$stats" | grep -q "env:sandbox" && ok "metrics carry key:value tags (env:sandbox)" || bad "no metric seen with tag env:sandbox (agent dogstatsd-stats)"
docker compose exec -T datadog-agent agent config 2>/dev/null | grep -A3 "histogram_percentiles" | grep -q "0.95" \
  && ok "histograms produce the 95th percentile" || bad "histogram_percentiles doesn't include 0.95"
echo; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
