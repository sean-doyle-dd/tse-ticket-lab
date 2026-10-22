"""internal-api: an internal-only service the customer wants to monitor from a private location."""
import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DELAY = float(os.environ.get("ORDERS_DELAY_SECONDS", "0"))


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200, {"status": "ok"})
        if self.path.startswith("/api/orders"):
            time.sleep(DELAY)
            return self._send(200, {"orders": [{"id": 1001, "status": "shipped"}, {"id": 1002, "status": "pending"}]})
        return self._send(404, {"error": "not found"})


if __name__ == "__main__":
    print(f"internal-api listening on :8080 (orders delay {DELAY}s)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
