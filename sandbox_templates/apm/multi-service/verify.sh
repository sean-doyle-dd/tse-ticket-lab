#!/usr/bin/env bash
# Checks the healthy end state: both tracers load and reach the Agent, traces are kept,
# envs match, and storefront logs carry trace IDs.
cd "$(dirname "${BASH_SOURCE[0]}")"
pass=0; fail=0
ok()  { echo "  PASS  $1"; pass=$((pass+1)); }
bad() { echo "  FAIL  $1"; fail=$((fail+1)); }
status=$(docker compose exec -T datadog-agent agent status 2>/dev/null)
[ -z "$status" ] && { echo "Agent not running. Start with: docker compose up -d --build"; exit 2; }
echo "(traffic takes ~1-2 minutes to show up in 'Receiver (previous minute)')"

recv=$(echo "$status" | sed -n '/Receiver (previous minute)/,/Writer (previous minute)/p')
[ -n "$recv" ] && ok "trace-agent running" || bad "trace-agent not running (APM disabled?)"
echo "$recv" | grep -qi "python" && ok "storefront (python) traces reaching the Agent" || bad "no traces from the python tracer"
echo "$recv" | grep -qi "nodejs" && ok "inventory (nodejs) traces reaching the Agent" || bad "no traces from the nodejs tracer"
se=$(docker compose exec -T storefront printenv DD_ENV 2>/dev/null); ie=$(docker compose exec -T inventory printenv DD_ENV 2>/dev/null)
[ -n "$se" ] && [ "$se" = "$ie" ] && ok "storefront and inventory share env:$se" || bad "env mismatch (storefront=$se inventory=$ie)"
[ "$(docker compose exec -T storefront printenv DD_TRACE_SAMPLE_RATE 2>/dev/null | tr -d '\r')" != "0" ] && ok "storefront traces are sampled for ingestion" || bad "storefront DD_TRACE_SAMPLE_RATE=0 drops every trace from ingestion"
[ -z "$(docker compose exec -T storefront printenv DD_TRACE_PROPAGATION_STYLE_INJECT 2>/dev/null | grep -i none)" ] && ok "storefront injects trace headers" || bad "storefront doesn't inject trace headers (propagation=none)"
docker compose logs --since 3m storefront 2>/dev/null | grep -qE "dd.trace_id=[1-9]" && ok "storefront logs carry trace IDs" || bad "storefront logs have no dd.trace_id"
echo; echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
