import os
import random
import time

from datadog import initialize, statsd

initialize(statsd_host=os.environ.get("STATSD_HOST", "datadog-agent"), statsd_port=8125)
print("shop-app: sending web.request.count to datadog-agent:8125", flush=True)
while True:
    statsd.increment("web.request.count", tags=["service:shop-app", "env:sandbox"])
    print("shop-app: handled request", flush=True)
    time.sleep(random.uniform(1, 6))
