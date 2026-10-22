import logging
import os

import requests
from flask import Flask, jsonify

# Log format includes the trace/span IDs ddtrace injects when DD_LOGS_INJECTION=true.
FORMAT = ("%(asctime)s %(levelname)s [%(name)s] "
          "[dd.service=%(dd.service)s dd.env=%(dd.env)s dd.version=%(dd.version)s "
          "dd.trace_id=%(dd.trace_id)s dd.span_id=%(dd.span_id)s] %(message)s")


class _DefaultDDFields(logging.Filter):
    """Without log injection the dd.* fields don't exist; show '-' instead of crashing."""
    def filter(self, record):
        for f in ("dd.service", "dd.env", "dd.version", "dd.trace_id", "dd.span_id"):
            if not hasattr(record, f):
                setattr(record, f, "-")
        return True


logging.basicConfig(format=FORMAT, level=logging.INFO)
for h in logging.getLogger().handlers:
    h.addFilter(_DefaultDDFields())
log = logging.getLogger("storefront")

app = Flask(__name__)
BACKEND = os.environ.get("BACKEND_URL", "http://inventory:3000")


@app.route("/")
def index():
    return "storefront ok\n"


@app.route("/products")
def products():
    log.info("listing products")
    r = requests.get(f"{BACKEND}/items", timeout=5)
    return jsonify(r.json())


@app.route("/checkout")
def checkout():
    log.info("checkout started")
    r = requests.get(f"{BACKEND}/items/1", timeout=5)
    return jsonify({"ordered": r.json()})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
