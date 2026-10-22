"""Sandbox templates -> downloadable repro zips.

Claude never writes sandbox code. Each template under sandbox_templates/<spec>/<name>/ ships
a manifest.yaml that declares its tunable parameters and the allowed values for each one;
the generator may only pick a template and choose values from those allowlists. The
renderer is deterministic: `*.j2` files are rendered with the chosen values, and
everything else is copied as-is.

Template layout:
    manifest.yaml        id, title, description, params{name: {description, default, choices}}, editable[]
    files/               the docker-compose project (any *.j2 is rendered with `p` = params)
    verify.sh            optional health check, copied to the zip root
"""
import io
import logging
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

from .schemas import SandboxSpec

log = logging.getLogger(__name__)


@dataclass
class Param:
    name: str
    description: str
    default: str
    choices: dict  # value -> note (the note for the default value says it is healthy)


@dataclass
class Template:
    id: str
    spec: str
    title: str
    description: str
    params: dict
    editable: list
    path: Path
    services: list = field(default_factory=list)

    def defaults(self):
        return {name: p.default for name, p in self.params.items()}


class SandboxLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self.templates = {}
        self.errors = []
        self.reload()

    def reload(self):
        templates, errors = {}, []
        for manifest in sorted(self.root.glob("*/*/manifest.yaml")):
            try:
                t = self._load(manifest)
                templates[t.id] = t
            except Exception as exc:  # a broken template must not take the app down
                errors.append(f"{manifest.parent.relative_to(self.root)}: {exc}")
        self.templates, self.errors = templates, errors
        for e in errors:
            log.warning("sandbox: %s", e)

    @staticmethod
    def _load(manifest):
        data = yaml.safe_load(manifest.read_text())
        spec, name = manifest.parent.parent.name, manifest.parent.name
        tid = f"{spec}/{name}"
        if data.get("id") and data["id"] != tid:
            raise ValueError(f"manifest id {data['id']!r} doesn't match its folder {tid!r}")
        params = {}
        for pname, p in (data.get("params") or {}).items():
            choices = {str(k): str(v) for k, v in (p.get("choices") or {}).items()}
            default = str(p["default"])
            if default not in choices:
                raise ValueError(f"param {pname}: default {default!r} isn't one of its choices")
            params[pname] = Param(pname, p.get("description", ""), default, choices)
        if not (manifest.parent / "files").is_dir():
            raise ValueError("missing files/ directory")
        return Template(
            id=tid, spec=spec, title=data["title"], description=data.get("description", "").strip(),
            params=params, editable=list(data.get("editable") or []), path=manifest.parent,
            services=list(data.get("services") or []),
        )

    # ------------------------------------------------------------------ for the generator

    def for_spec(self, spec):
        return [t for t in self.templates.values() if t.spec == spec]

    def catalog_text(self, spec):
        """Plain-text catalog for the generator prompt (stable ordering, which keeps caching happy)."""
        ts = self.for_spec(spec)
        if not ts:
            return "(No sandbox templates exist for this spec. Set sandbox.needed=false and write repro_steps.)"
        out = []
        for t in ts:
            out.append(f"### template_id: {t.id}\n{t.title}. {t.description}")
            for p in t.params.values():
                out.append(f"- param `{p.name}`: {p.description}")
                for value, note in p.choices.items():
                    marker = " (default/healthy)" if value == p.default else ""
                    out.append(f"    - `{value}`{marker}: {note}")
            out.append("")
        return "\n".join(out)

    def normalize(self, spec, sb: SandboxSpec):
        """Clamp the generator's choice to the allowlists. Returns (SandboxSpec, warnings).
        An unknown template becomes needed=false so the TSE still gets repro_steps."""
        warnings = []
        if not sb.needed:
            return sb.model_copy(update={"template_id": "", "params": []}), warnings
        t = self.templates.get(sb.template_id)
        if t is None or t.spec != spec:
            warnings.append(f"unknown template {sb.template_id!r}; falling back to repro steps")
            return sb.model_copy(update={"needed": False, "template_id": "", "params": []}), warnings
        clean = []
        for p in sb.params:
            spec_p = t.params.get(p.name)
            if spec_p is None:
                warnings.append(f"dropped unknown param {p.name!r}")
            elif p.value not in spec_p.choices:
                warnings.append(f"dropped {p.name}={p.value!r}: not an allowed value")
            else:
                clean.append(p)
        return sb.model_copy(update={"params": clean}), warnings

    # ----------------------------------------------------------------------- rendering

    def resolve_params(self, template_id, params):
        t = self.templates[template_id]
        values = t.defaults()
        for p in params:
            if p["name"] in t.params and p["value"] in t.params[p["name"]].choices:
                values[p["name"]] = p["value"]
        return values

    def render(self, template_id, params, meta):
        """Return {relative_path: (bytes, executable)} for the project."""
        t = self.templates[template_id]
        values = self.resolve_params(template_id, params)
        env = Environment(undefined=StrictUndefined, keep_trailing_newline=True, autoescape=False)
        files = {}
        base = t.path / "files"
        for src in sorted(base.rglob("*")):
            if src.is_dir():
                continue
            rel = src.relative_to(base).as_posix()
            executable = bool(src.stat().st_mode & stat.S_IXUSR) or rel.endswith(".sh")
            if rel.endswith(".j2"):
                rel = rel[:-3]
                data = env.from_string(src.read_text()).render(p=values, meta=meta).encode()
            else:
                data = src.read_bytes()
            files[rel] = (data, executable)
        verify = t.path / "verify.sh"
        if verify.exists():
            files["verify.sh"] = (env.from_string(verify.read_text()).render(p=values, meta=meta).encode(), True)
        # Pristine copies of the files the TSE is expected to edit, so reset.sh can restore them.
        for rel in t.editable:
            if rel in files:
                files[f".tg/original/{rel}"] = files[rel]
        files["reset.sh"] = (_reset_script(t.editable).encode(), True)
        return files

    def build_zip(self, template_id, params, meta, readme):
        folder = f"tg-sandbox-{meta['short_id']}"
        files = self.render(template_id, params, meta)
        files["README.md"] = (readme.encode(), False)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for rel, (data, executable) in sorted(files.items()):
                info = zipfile.ZipInfo(f"{folder}/{rel}")
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = 0o755 if executable else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                zf.writestr(info, data)
        return buf.getvalue(), f"{folder}.zip"


def _reset_script(editable):
    restores = "\n".join(
        f'cp ".tg/original/{rel}" "{rel}" && echo "  restored {rel}"' for rel in editable
    ) or 'echo "  (no editable files in this template)"'
    return f"""#!/usr/bin/env bash
# Put the sandbox back to the state you downloaded it in: tear down containers and
# restore every file you were expected to edit.
set -euo pipefail
cd "$(dirname "${{BASH_SOURCE[0]}}")"
echo "→ Stopping containers..."
docker compose down --remove-orphans || true
echo "→ Restoring original files..."
{restores}
echo "✓ Reset complete. Start again with: docker compose up -d --build"
"""
