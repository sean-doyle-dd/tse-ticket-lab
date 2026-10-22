#!/bin/sh
set -e
# Apply database migrations, then hand off to the server (or whatever command was given).
alembic upgrade head
# Every gunicorn worker must sign sessions with the same key. Without SECRET_KEY (dev mode),
# generate one here so all workers inherit it. Sessions then reset on container restart.
if [ -z "${SECRET_KEY:-}" ]; then
  SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
  export SECRET_KEY
  echo "SECRET_KEY not set: generated an ephemeral one (sign-ins reset on restart)."
fi
if [ "$#" -eq 0 ]; then
  if [ "${DD_TRACE_ENABLED:-false}" = "true" ]; then
    exec ddtrace-run gunicorn -c gunicorn.conf.py wsgi:app
  fi
  exec gunicorn -c gunicorn.conf.py wsgi:app
fi
exec "$@"
