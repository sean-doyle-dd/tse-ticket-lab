# gthread workers: each SSE stream (a customer reply being typed) holds one thread for
# its duration, so give each worker plenty of threads. Generation runs in background
# threads and doesn't hold a request.
import os

bind = f"0.0.0.0:{os.environ.get('PORT', '8000')}"
workers = int(os.environ.get("WEB_WORKERS", "2"))
worker_class = "gthread"
threads = int(os.environ.get("WEB_THREADS", "16"))
timeout = 180
graceful_timeout = 30
keepalive = 5
accesslog = "-"
errorlog = "-"
forwarded_allow_ips = os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1")
