"""Thin wrapper over the Anthropic SDK: one place for the provider switch, structured output,
streaming, prompt caching, refusal handling and token accounting.

- Provider: LLM_PROVIDER=anthropic (API key) | bedrock (AWS creds) | vertex (GCP ADC) |
  claude_cli (a Claude Code login via `claude -p`; see claude_cli.py).
- Structured output: `output_config.format` with a strict JSON schema generated from the
  pydantic model, then validated with pydantic. Forced tool_choice is rejected by
  current models, so there are no tools-as-schema tricks here.
- Caching: the system prompt carries a cache breakpoint. Callers keep it byte-stable for
  the life of a conversation, so every turn after the first reads it from cache.
- Refusals: on the Claude API we opt into server-side fallbacks (`fallbacks: "default"`)
  so a safety-classifier decline (a debug log can look "cyber"-ish) is retried on a
  fallback model inside the same call. We still check `stop_reason` afterwards.
"""
import json
import logging
import time
from dataclasses import dataclass, field

import anthropic
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

# USD per million tokens: (input, output, cache read). Haiku 5.5 is the <=100K-prompt tier.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}

FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Server-side fallbacks ("default" routing) exist for these; Haiku has none.
FALLBACK_MODELS = ("claude-opus-5-5", "claude-sonnet-5-5", "claude-opus-5", "claude-fable-5-1")


class LLMError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    duration_ms: int = 0
    cost_usd: float = 0.0

    @classmethod
    def from_message(cls, model, msg, started):
        u = getattr(msg, "usage", None)
        return cls(
            model=model,
            input_tokens=getattr(u, "input_tokens", 0) or 0,
            output_tokens=getattr(u, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            duration_ms=int((time.monotonic() - started) * 1000),
        ).priced()

    def priced(self):
        """Fill cost_usd from list prices when the provider didn't report one."""
        p = PRICES.get(self.model.removeprefix("anthropic."))
        if p and not self.cost_usd:
            inp, out, cache_read = p
            self.cost_usd = (self.input_tokens * inp + self.output_tokens * out
                             + self.cache_read_tokens * cache_read
                             + self.cache_write_tokens * inp * 1.25) / 1e6
        return self


@dataclass
class StreamResult:
    text: str
    blocks: list = field(default_factory=list)
    usage: Usage = None


# ------------------------------------------------------------------- schema conversion

_DROP_KEYS = ("title", "default", "examples")


def strict_schema(model):
    """Pydantic JSON schema -> strict structured-output schema: every object closes with
    additionalProperties:false and lists all its properties as required; titles and
    defaults are dropped; $ref nodes carry no siblings."""
    raw = model.model_json_schema()

    def clean(node):
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return {"$ref": node["$ref"]}
        out = {k: v for k, v in node.items() if k not in _DROP_KEYS}
        if "properties" in out:
            out["properties"] = {name: clean(sub) for name, sub in out["properties"].items()}
            out["required"] = list(out["properties"].keys())
            out["additionalProperties"] = False
        if "items" in out:
            out["items"] = clean(out["items"])
        for key in ("anyOf", "allOf", "oneOf"):
            if key in out:
                out[key] = [clean(s) for s in out[key]]
        if "$defs" in out:
            out["$defs"] = {name: clean(sub) for name, sub in out["$defs"].items()}
        return out

    return clean(raw)


def replayable_blocks(blocks):
    """Assistant content to send back on later turns. Blocks are replayed unchanged
    (thinking included; stripping them can 400). The one exception is a mid-output
    fallback: anything model-internal before the last `fallback` marker is dropped."""
    idx = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=None)
    if idx is None:
        return blocks
    keep = [b for b in blocks[:idx] if b.get("type") == "text"]
    return keep + blocks[idx + 1:]


# ----------------------------------------------------------------------------- client

class LLM:
    def __init__(self, cfg):
        self.cfg = cfg
        self.provider = cfg["LLM_PROVIDER"]
        self._client = None
        self._cli = None
        if self.provider == "claude_cli":
            from .claude_cli import ClaudeCLI
            self._cli = ClaudeCLI(cfg)

    def client(self):
        if self._client is None:
            if self.provider == "anthropic":
                if not self.cfg["ANTHROPIC_API_KEY"]:
                    raise LLMError("ANTHROPIC_API_KEY is not set. Add it to .env and restart.", 503)
                self._client = anthropic.Anthropic(api_key=self.cfg["ANTHROPIC_API_KEY"], max_retries=3)
            elif self.provider == "bedrock":
                self._client = anthropic.AnthropicBedrockMantle(aws_region=self.cfg["AWS_REGION"], max_retries=3)
            elif self.provider == "vertex":
                self._client = anthropic.AnthropicVertex(
                    project_id=self.cfg["VERTEX_PROJECT_ID"], region=self.cfg["VERTEX_REGION"], max_retries=3)
            else:
                raise LLMError(f"Unknown LLM_PROVIDER {self.provider!r}", 500)
        return self._client

    def model_id(self, model):
        return f"anthropic.{model}" if self.provider == "bedrock" else model

    @property
    def supports_mid_conversation_system(self):
        # Mid-conversation system messages: confirmed on the Claude API; elsewhere we fold
        # the directive into the user turn instead.
        return self.provider == "anthropic"

    def _request(self, model, system, messages, effort, max_tokens, output_format=None):
        output_config = {"effort": effort}
        if output_format is not None:
            output_config["format"] = output_format
        params = {
            "model": self.model_id(model),
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "output_config": output_config,
        }
        if self.provider == "anthropic" and self.cfg.get("LLM_FALLBACKS") and model in FALLBACK_MODELS:
            params["extra_headers"] = {"anthropic-beta": FALLBACK_BETA}
            params["extra_body"] = {"fallbacks": "default"}
        return params

    @staticmethod
    def _check_stop(msg):
        if msg.stop_reason == "refusal":
            raise LLMError("Claude declined this request (safety classifier). Try generating again.", 502)
        if msg.stop_reason == "max_tokens":
            raise LLMError("Claude's response was cut off (max_tokens). Try again.", 502)

    @staticmethod
    def _translate(exc):
        if isinstance(exc, anthropic.AuthenticationError):
            return LLMError("Claude rejected the credentials. Check ANTHROPIC_API_KEY / cloud credentials.", 503)
        if isinstance(exc, anthropic.PermissionDeniedError):
            return LLMError("These credentials aren't allowed to use the configured model.", 503)
        if isinstance(exc, anthropic.NotFoundError):
            return LLMError("Model not found. Check the MODEL_* settings for this provider.", 503)
        if isinstance(exc, anthropic.RateLimitError):
            return LLMError("Claude is rate-limiting us right now. Wait a minute and retry.", 429)
        if isinstance(exc, anthropic.BadRequestError):
            return LLMError(f"Claude rejected the request: {exc.message}", 502)
        if isinstance(exc, anthropic.APIStatusError):
            return LLMError(f"Claude API error ({exc.status_code}). Retry shortly.", 502)
        if isinstance(exc, anthropic.APIConnectionError):
            return LLMError("Couldn't reach the Claude API. Check network/proxy settings.", 502)
        return None

    # ------------------------------------------------------------------- public calls

    def structured(self, model, system, messages, schema_model, effort="high", max_tokens=48000):
        """Return (instance_of_schema_model, Usage). Streams under the hood so big outputs
        never hit HTTP timeouts; retries once if the JSON fails validation."""
        if self._cli:
            return self._cli.structured(model, system, messages, schema_model, effort)
        fmt = {"type": "json_schema", "schema": strict_schema(schema_model)}
        last_err = None
        for attempt in range(2):
            started = time.monotonic()
            try:
                with self.client().messages.stream(
                        **self._request(model, system, messages, effort, max_tokens, fmt)) as stream:
                    msg = stream.get_final_message()
            except anthropic.AnthropicError as exc:
                translated = self._translate(exc)
                if translated:
                    raise translated from exc
                raise
            usage = Usage.from_message(model, msg, started)
            self._check_stop(msg)
            text = "".join(b.text for b in msg.content if b.type == "text")
            try:
                return schema_model.model_validate_json(text), usage
            except ValidationError as exc:
                last_err = exc
                log.warning("structured output failed validation (attempt %s): %s", attempt + 1, exc)
        raise LLMError(f"Claude returned output that didn't match the schema: {last_err}", 502)

    def stream_text(self, model, system, messages, effort="medium", max_tokens=16000):
        """Generator: yields text chunks as they arrive, then returns a StreamResult
        (via StopIteration.value; use `yield from` or the helper `drain`)."""
        if self._cli:
            return (yield from self._cli.stream_text(model, system, messages, effort))
        started = time.monotonic()
        try:
            with self.client().messages.stream(
                    **self._request(model, system, messages, effort, max_tokens)) as stream:
                for chunk in stream.text_stream:
                    yield chunk
                msg = stream.get_final_message()
        except anthropic.AnthropicError as exc:
            translated = self._translate(exc)
            if translated:
                raise translated from exc
            raise
        usage = Usage.from_message(model, msg, started)
        self._check_stop(msg)
        blocks = [b.model_dump(mode="json", exclude_none=True) for b in msg.content]
        text = "".join(b.text for b in msg.content if b.type == "text")
        return StreamResult(text=text, blocks=replayable_blocks(blocks), usage=usage)


def drain(gen, on_chunk=None):
    """Run a stream_text generator to completion, returning its StreamResult."""
    while True:
        try:
            chunk = next(gen)
        except StopIteration as stop:
            return stop.value
        if on_chunk:
            on_chunk(chunk)


def to_json(obj):
    return json.loads(obj.model_dump_json()) if isinstance(obj, BaseModel) else obj
