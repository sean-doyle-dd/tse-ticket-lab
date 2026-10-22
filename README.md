# TSE Ticket Lab

Practise on realistic support tickets. Pick a **spec** (Agent, APM, Synthetics or Web Platform) and a **difficulty** (Low, Medium or High). Claude writes a fresh customer ticket based on anonymized patterns from recent tickets in that spec. Where the issue can be reproduced locally, you also get a **docker-compose sandbox** that really has the fault.

Work it like a real ticket. A simulated customer answers your questions and only reveals what you actually ask for. It attaches logs, flares and config when you request them, pushes back the way real customers do, and tells you whether your proposed fix worked. When you close the ticket, a grader scores your **diagnosis, information gathering, communication and resolution**, then reveals the answer key.

Setup guide on Confluence: https://datadoghq.atlassian.net/wiki/x/BoSJtgE

```
pick spec + difficulty ─▶ Claude (Opus) writes ticket + hidden truth + sandbox choice
                                 │
          ┌──────────────────────┼─────────────────────────┐
          ▼                      ▼                         ▼
  ticket thread           sandbox.zip (only allow-     answer key (hidden)
  + customer chat         listed template params)      facts, root cause,
  (Sonnet, streamed)      verify.sh / reset.sh         red herrings, escalation
          │
          ▼
  Close ─▶ grader (Opus) scores 4 criteria, reveals answer, model first reply
```

## Get your own Claude token (required)

The app calls Claude through the Claude Code CLI using **your own** Claude login. No token ships with this repo; each person makes their own.

1. Install Claude Code if you don't have it: `npm install -g @anthropic-ai/claude-code` (or see the [Claude Code docs](https://docs.claude.com/en/docs/claude-code)).
2. Run `claude setup-token` in a terminal. It opens a browser to sign in with your Claude account and then prints a long-lived OAuth token (starts with `sk-ant-oat01-`).
3. Copy the example env file: `cp .env.example .env`
4. Open `.env` and paste the token after `CLAUDE_CODE_OAUTH_TOKEN=` (one line, no quotes):
   ```
   LLM_PROVIDER=claude_cli
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
   ```

`.env` is gitignored and excluded from the Docker build context, so the token never gets committed or baked into the image. It's passed to the container at runtime through `env_file`. Treat it like a password: don't paste it into Slack, tickets or commits, and if it leaks, revoke it from your Claude account settings and run `claude setup-token` again. Usage counts against the account that owns the token.

Prefer an API key? Set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY=...` in `.env` instead (see **Claude access** below).

## Quick start

```bash
claude setup-token                # one-time: prints a long-lived token for your Claude login
cp .env.example .env              # paste it as CLAUDE_CODE_OAUTH_TOKEN
docker compose up -d --build
open http://localhost:8095        # dev sign-in: any name + @datadoghq.com email
```

`docker compose up` is the only setup step. Postgres starts, migrations run, and the app serves on port `8095` (change it with `WEB_PORT`). Health check: `curl localhost:8095/healthz`.

## Shared team deployment

Set these in `.env`:

| Setting | Why |
|---|---|
| `AUTH_MODE=google`, `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Google SSO. The redirect URI is `https://<host>/auth/callback`. |
| `ALLOWED_EMAIL_DOMAIN=datadoghq.com` | Checked server-side against the verified email and the `hd` claim. |
| `SECRET_KEY` (32+ chars) | Required in google mode. The app refuses to start without it. |
| `SESSION_COOKIE_SECURE=true` | When served over HTTPS (put it behind your TLS proxy or ingress). |
| `ADMIN_EMAILS` | Comma-separated list of people who can open the **Team** page (everyone's scores and Claude token usage). |
| `POSTGRES_PASSWORD` | Change it from the default. |
| `GENERATIONS_PER_HOUR`, `MESSAGES_PER_HOUR` | Per-user cost guards (defaults 10 and 120). |

`AUTH_MODE=dev` (the default) is a name and email form with no password, meant for laptops only. Every page shows a banner while it's on.

## Claude access

The default is `LLM_PROVIDER=claude_cli`, which needs no API key. The image includes the Claude Code CLI, and every call runs as `claude -p` under your Claude subscription login. To authenticate it, run `claude setup-token` on your laptop and put the token in `.env` as `CLAUDE_CODE_OAUTH_TOKEN`. Each call runs with no tools, no MCP servers, no settings or CLAUDE.md, and no saved session (`app/claude_cli.py`). Usage counts against the subscription that owns the token, so for a shared deployment use a team or service login rather than a personal one. `CLAUDE_CLI_CONCURRENCY` (default 4) caps how many calls run at once.

`anthropic` (with `ANTHROPIC_API_KEY`), `bedrock` (with `AWS_REGION` and AWS credentials) and `vertex` (with `VERTEX_PROJECT_ID` and GCP ADC) call the API directly through the SDK. That adds thinking replay across chat turns and server-side refusal fallbacks, which the CLI path doesn't have.

| Job | Model (env override) | Notes |
|---|---|---|
| Generate ticket | `claude-opus-5-5` (`MODEL_GENERATOR`) | Structured output, strict JSON schema, effort `high`, about a minute, runs in the background |
| Customer replies | `claude-sonnet-5-5` (`MODEL_CUSTOMER`) | Streamed over SSE. The system prompt is byte-stable per ticket, so it's read from the prompt cache every turn |
| Disclosure check | `claude-haiku-5-5` (`MODEL_CLASSIFIER`) | Records which hidden facts each reply revealed (drives the progress meter and grading) |
| Grade | `claude-opus-5-5` (`MODEL_GRADER`) | Rubric scores, strengths and improvements, missed facts, a model first reply |

On the Claude API (`anthropic` provider) the app opts into server-side refusal fallbacks (`LLM_FALLBACKS=true`), and every call checks `stop_reason`. Token usage per call is stored and shown on the Team page.

## Where the tickets come from (and the compliance boundary)

Every practice ticket is rebuilt from **one real solved ticket**. The container never talks to Zendesk and never sees raw customer data. Its only view of past tickets is `corpus/<spec>/*.json`: **anonymized seeds** with no names, companies, hosts or IDs. Each seed holds:
- the real symptom, root cause and resolution path
- `evidence`: error messages, log lines, config keys and commands copied verbatim from the thread
- the docs links the TSE actually sent
- `tse_approach`: how the real TSE worked it (steps, key questions, what made their replies land, and a paraphrase of the customer's CSAT comment)
- provenance: CSAT rating, the ticket's real `TICKET_COMPLEXITY` (this sets its difficulty) and the month it was solved

Every seed passes a PII lint (`app/pii.py`) at load time and in CI.

**Building seeds from real tickets** is a host-only tool, because it uses your local `claude` CLI and its Snowflake and Zendesk MCP logins, the same way ticket-memory does:

```bash
python3 tools/refresh_corpus.py --spec apm --days 60 --limit 15     # good-CSAT tickets only (default)
# review corpus/_staging/apm/*.json for accuracy and anonymity, then:
python3 tools/refresh_corpus.py --promote corpus/_staging/apm/<file>.json
```

- **Selection (Snowflake):**
  - Picks solved tickets with `PRIMARY_PRODUCT_COMPONENT` of `agent_ppc`, `apm_ppc`, `synthetics_ppc` or `web_platform_ppc`, a CSAT of **good** (`--csat any` widens this) and a `TICKET_COMPLEXITY`.
  - Excludes HIPAA orgs, orgs restricted from AI training, orgs that disabled Anthropic, FedRAMP regions, and tickets tagged `hipaa` or `opted_out`.
- **Distillation:** reads the thread through the Zendesk MCP (which enforces its own restrictions), scrubs identifiers, then has Claude write the seed using only what the thread says.
- **Grounding:**
  - Evidence strings and doc links must appear *verbatim* in the thread, or they're dropped.
  - A second Claude pass lists anything else the thread doesn't support. Flagged list items are removed, and a flagged root cause, symptom or resolution rejects the seed.
- **Review:** results go to a gitignored staging folder until a person promotes them. Ticket IDs are kept only in a local, gitignored ledger (`.corpus-state/`), outside `corpus/`, because the app container mounts `corpus/` read-only. After promoting, run `docker compose restart web`; no rebuild is needed.

The 39 hand-written **bootstrap** seeds from the first version are not from real tickets, so they're **off by default**. Set `CORPUS_ALLOW_BOOTSTRAP=true` to use them, for example for a demo before you've built any real seeds.

### How hallucination is kept out

| Stage | Guard |
|---|---|
| Seed | Evidence and doc links must be verbatim substrings of the scrubbed thread. A fact-check pass drops unsupported items and rejects seeds whose root cause or fix isn't supported. A person reviews every seed before promoting it. |
| Ticket generation | Built from one seed only, never a blend. The prompt allows technical content only from the seed (and, for sandbox behaviour, the template catalog); only story details like people, company and timeline may be invented. Doc links not in the seed are removed in code. A separate model (`MODEL_VERIFIER`) lists unsupported claims; the generator retries once with that list, and if any remain the ticket is discarded (`GROUNDING_CHECK`). |
| Answer key | The real root cause, resolution path and TSE approach are stored with the ticket as written in the seed, not rewritten by the generator, and shown at close under **How the real TSE solved it**. |
| Customer chat | The persona may only use facts, the ticket and the seed's verbatim evidence, and says "I'd have to check" rather than invent. Each reply is checked for unsupported details. Any found are listed under **Simulation caveats** when the ticket closes, and the grader is told not to penalize the TSE for them. |
| Grading | Graded against the real TSE's approach on a good-CSAT ticket, not the model's own idea of best practice. The model first reply may only use technical content from the answer key. |

Every ticket header shows where it came from, for example "Based on a real solved ticket · 2026-09 · customer rated it good · grounding check passed".

These guards make hallucination much less likely and flag it when it slips through, but no LLM-generated simulation can guarantee zero. The customer's replies are generated live, so they're flagged rather than blocked.

## Sandboxes

Claude never writes sandbox code. Each template under `sandbox_templates/<spec>/<name>/` declares its parameters and the allowed value for each one in `manifest.yaml`. The generator can only pick a template and choose values from those lists. Anything else is dropped (see `SandboxLibrary.normalize`). Rendering is deterministic Jinja over `files/`.

| Template | Faults it can inject |
|---|---|
| `agent/dogstatsd-app` | client → localhost or wrong port, non-local traffic off, DogStatsD off, `key=value` tags, missing p95, wrong site |
| `agent/proxy-openmetrics` | proxy → localhost, missing `no_proxy` (Squid 403), wrong endpoint or port, wrong basic-auth password, untyped metric not mapped, logs off, DogStatsD non-local off |
| `agent/nginx-check` | stub_status missing, ACL 403, Autodiscovery check-name typo, status URL localhost or wrong port, log `source` not `nginx` |
| `apm/multi-service` | Python → Node → Postgres. Agent host localhost, wrong trace port, APM non-local off or disabled, Node tracer not loaded, `ddtrace-run` missing, propagation `none`, sample rate 0, env mismatch, logs injection off |
| `synthetics/private-location` | blocked reserved IP ranges, public-only DNS, worker on an isolated network, 65s endpoint latency. You supply the PL config from your sandbox org |

Web Platform tickets (UI, RBAC, monitors, sharing) usually can't be reproduced locally, so they come with a **repro guide** for your Datadog sandbox org.

Every zip contains a README with only the customer's view (never the answer), `.env.example` (use a **sandbox org** key), `verify.sh` (healthy-state checks), and `reset.sh` (restores every file you were expected to edit). Host ports are ephemeral, so several sandboxes can run at once.

**Adding a template:** create `sandbox_templates/<spec>/<name>/` with `manifest.yaml`, `files/` and `verify.sh`, then run the tests. They render every variant and fail if a broken value renders the same as the healthy one, or if any YAML or JSON is invalid. **Adding a spec:** add it to `app/catalog.py`, then add seeds and (optionally) templates.

## Development

```bash
docker build --target test -t tg-test . && docker run --rm tg-test     # unit + route tests (fake LLM)
```

| Path | Contents |
|---|---|
| `app/engine.py` | generate, customer, disclosure and grade jobs (pure functions over an `LLM`) |
| `app/services.py` | persistence, background generation, quotas, token accounting |
| `app/llm.py` | provider switch, strict-schema structured output, streaming, caching, refusal handling |
| `app/prompts/` | generator, customer, grader and classifier prompts, plus the difficulty rubric |
| `app/sandbox.py`, `app/corpus.py`, `app/pii.py` | templates, seeds, identifier lint |
| `app/routes/`, `app/templates/`, `app/static/` | Flask UI and JSON/SSE API, vanilla JS, light/dark CSS |
| `migrations/` | Alembic (runs on container start) |

Self-monitoring: `DD_TRACE_ENABLED=true` and `DD_API_KEY=…`, then `docker compose --profile datadog up -d`.
