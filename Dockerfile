# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN useradd --create-home --uid 10001 app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY app ./app
COPY migrations ./migrations
COPY corpus ./corpus
COPY sandbox_templates ./sandbox_templates
COPY alembic.ini gunicorn.conf.py wsgi.py entrypoint.sh ./

# Test image: `docker build --target test -t tg-test . && docker run --rm tg-test`
FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY tests ./tests
COPY pytest.ini ./
CMD ["pytest", "-q"]

FROM base AS prod
ARG CLAUDE_CODE_VERSION=2.1.304
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /app/instance && chown -R app:app /app/instance
USER app
# Claude Code CLI, used when LLM_PROVIDER=claude_cli (auth: CLAUDE_CODE_OAUTH_TOKEN).
RUN curl -fsSL https://claude.ai/install.sh | bash -s ${CLAUDE_CODE_VERSION} \
 && /home/app/.local/bin/claude --version
ENV PATH="/home/app/.local/bin:${PATH}" DISABLE_AUTOUPDATER=1
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
ENTRYPOINT ["./entrypoint.sh"]
