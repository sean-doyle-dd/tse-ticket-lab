"""Loads the anonymized scenario seeds in corpus/<spec>/*.json and picks which ones
inspire a new ticket.

Seeds are built offline (tools/refresh_corpus.py) and reviewed before they're committed.
The running app never reads Zendesk; this directory is its only view of past tickets.
"""
import json
import logging
import random
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from .pii import find_pii
from .schemas import Seed

log = logging.getLogger(__name__)


class Corpus:
    def __init__(self, root, allow_bootstrap=False):
        self.root = Path(root)
        self.allow_bootstrap = allow_bootstrap
        self.skipped_bootstrap = 0
        self._by_spec = {}
        self.errors = []
        self.reload()

    def reload(self):
        by_spec, errors, skipped = {}, [], 0
        for path in sorted(self.root.glob("*/*.json")):
            if path.parent.name.startswith(("_", ".")) or path.name.startswith("."):
                continue  # _staging/ and the local ledger are never served
            try:
                seed = Seed.model_validate(json.loads(path.read_text()))
            except (json.JSONDecodeError, ValidationError) as exc:
                errors.append(f"{path.relative_to(self.root)}: {exc}")
                continue
            hits = find_pii(path.read_text())
            if hits:
                errors.append(f"{path.relative_to(self.root)}: skipped, possible identifiers {hits[:3]}")
                continue
            if seed.source == "bootstrap" and not self.allow_bootstrap:
                skipped += 1
                continue
            by_spec.setdefault(seed.spec, []).append(seed)
        self._by_spec, self.errors, self.skipped_bootstrap = by_spec, errors, skipped
        for e in errors:
            log.warning("corpus: %s", e)

    def seeds(self, spec):
        return list(self._by_spec.get(spec, []))

    def get(self, seed_id):
        for seeds in self._by_spec.values():
            for s in seeds:
                if s.id == seed_id:
                    return s
        return None

    def counts(self):
        return {spec: len(seeds) for spec, seeds in self._by_spec.items()}

    def difficulty_counts(self, difficulties=("low", "medium", "high")):
        """{spec: {difficulty: n}}: how many seeds pick() would accept per difficulty."""
        out = {}
        for spec, seeds in self._by_spec.items():
            unrated = sum(1 for s in seeds if not s.provenance.complexity)
            out[spec] = {d: sum(1 for s in seeds if s.provenance.complexity == d) or unrated
                         for d in difficulties}
        return out

    @staticmethod
    def _weight(seed, difficulty, avoid, today):
        try:
            age = (today - date.fromisoformat(seed.recency)).days
        except ValueError:
            age = 365
        w = 3.0 if age <= 90 else 2.0 if age <= 180 else 1.0  # favour recent ticket patterns
        if difficulty in seed.difficulty_range:
            w *= 2.5
        if seed.source == "distilled":
            w *= 4.0  # a real ticket always beats a hand-written starter
        if seed.id in avoid:
            w *= 0.1  # rarely repeat what this TSE has already practised
        return w

    def pick(self, spec, difficulty, avoid_ids=(), rng=None, today=None):
        """Return (primary, extras). The primary seed is the one real ticket the scenario is
        built from. Extras are always empty: mixing tickets would let details from one
        leak into another's answer key. (The tuple shape is kept for callers.)

        When seeds carry the ticket's real complexity, only those matching the requested
        difficulty are eligible; with none, it refuses rather than stretch a ticket, which
        would mean inventing detail."""
        rng = rng or random.Random()
        today = today or date.today()
        pool = self.seeds(spec)
        if not pool:
            raise LookupError(f"No corpus seeds for spec {spec!r}. Add some under corpus/{spec}/.")
        rated = [s for s in pool if s.provenance.complexity]
        if rated:
            pool = ([s for s in rated if s.provenance.complexity == difficulty]
                    or [s for s in pool if not s.provenance.complexity])
            if not pool:
                have = sorted({s.provenance.complexity for s in rated})
                raise LookupError(f"No real {spec} tickets rated {difficulty!r} are loaded yet "
                                  f"(loaded: {', '.join(have)}). Pick another difficulty or add more.")
        avoid = set(avoid_ids)
        weights = [self._weight(s, difficulty, avoid, today) for s in pool]
        primary = rng.choices(pool, weights=weights, k=1)[0]
        return primary, []
