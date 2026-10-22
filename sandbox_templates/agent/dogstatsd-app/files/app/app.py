"""checkout: emits business metrics over DogStatsD every few seconds."""
import logging
import os
import random
import time

from datadog import initialize, statsd

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s checkout: %(message)s")
log = logging.getLogger()

HOST = os.environ.get("STATSD_HOST", "datadog-agent")
PORT = int(os.environ.get("STATSD_PORT", "8125"))
TAGS = [t for t in os.environ.get("METRIC_TAGS", "").split(",") if t]
INTERVAL = float(os.environ.get("EMIT_INTERVAL_SECONDS", "5"))

initialize(statsd_host=HOST, statsd_port=PORT)
log.info("sending DogStatsD to %s:%s with tags %s", HOST, PORT, TAGS)

while True:
    orders = random.randint(0, 8)
    statsd.increment("checkout.orders", orders, tags=TAGS)
    statsd.gauge("checkout.queue_depth", random.randint(0, 40), tags=TAGS)
    for _ in range(max(orders, 1)):
        statsd.histogram("checkout.latency", random.gauss(220, 60), tags=TAGS)
        statsd.distribution("checkout.payment.duration", random.gauss(450, 120), tags=TAGS)
    log.info("emitted batch: orders=%s", orders)
    time.sleep(INTERVAL)
