"""
variation_engine.py — Phase 2 template variation engine.

Rotates the subject line, opening hook, proof point, and CTA of each touch *within
the proven structure* of its source template, so no two leads in the same batch get
identical copy. This is rule-based (no LLM at runtime — CLAUDE.md decision #3): each
touch has a curated pool of faithful paraphrases per slot, and the engine assigns a
unique slot-combination to every lead in the batch.

Variant pools live in: active/outreach/templates/variants/{prefix}-{touch}.json
Each file declares:
  source          source template name (for performance tagging)
  formula         the copy structure the variants preserve (e.g. PAS, breakup)
  subject[]       optional — present only when this touch owns its subject (Touch 1,
                  breakup). Follow-ups omit it; the engine threads "Re: <Touch-1 subject>".
  opening[] / proof[] / cta[]   slot pools (any subset may be present)
  body_structure  scaffolding with {opening}/{proof}/{cta} tokens and the engine's
                  own {{name}}/{{company}}/{{calendly_url}}/{{sender_name}} tokens,
                  which are filled later by outreach_engine._render_template.

If no variant file exists for a (prefix, touch), build_plan returns {} and the caller
falls back to the flat .txt template — variation is a safe, optional enhancement.

Variant log (one JSON line per varied send) is written alongside the send log at
logs/variant_log.jsonl so copy performance can be traced per lead/touch.
"""

import itertools
import json
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path

from config import TEMPLATES_DIR

logger = logging.getLogger(__name__)

_VARIANTS_DIR = os.path.join(TEMPLATES_DIR, "variants")
_VARIANT_LOG = Path(__file__).resolve().parents[2] / "logs" / "variant_log.jsonl"
_SLOT_ORDER = ("subject", "opening", "proof", "cta")

# Per-process cache of loaded variant sets, keyed by "prefix-touch".
_cache: dict[str, dict | None] = {}


def load_variant_set(prefix: str, touch_number: int) -> dict | None:
    """Load and cache the variant pool for a touch. Returns None if no file (caller
    falls back to the flat template) or if the file is malformed."""
    key = f"{prefix}-{touch_number}"
    if key in _cache:
        return _cache[key]
    path = os.path.join(_VARIANTS_DIR, f"{key}.json")
    if not os.path.exists(path):
        _cache[key] = None
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            vs = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[VARIATION] Could not load {path}: {e}. Falling back to flat template.")
        _cache[key] = None
        return None
    vs.setdefault("source", key)
    vs.setdefault("formula", "unspecified")
    _cache[key] = vs
    return vs


def _active_slots(vs: dict) -> list[str]:
    """Slot names present in this variant set with a non-empty pool, in canonical order."""
    return [s for s in _SLOT_ORDER if isinstance(vs.get(s), list) and len(vs[s]) > 0]


def _assemble(vs: dict, active: list[str], combo: tuple) -> dict:
    """Build one variant (subject may be None for threaded follow-ups) from a slot-index
    combination. Body keeps {{...}} tokens intact for later placeholder rendering."""
    idx = dict(zip(active, combo))
    subject = vs["subject"][idx["subject"]] if "subject" in idx else None
    body = vs["body_structure"]
    for slot in ("opening", "proof", "cta"):
        if slot in idx:
            body = body.replace("{" + slot + "}", vs[slot][idx[slot]])
    variant_id = vs["source"] + "#" + "".join(f"{s[0]}{idx[s]}" for s in active)
    return {
        "subject": subject,
        "body": body,
        "indices": idx,
        "variant_id": variant_id,
        "formula": vs["formula"],
        "source": vs["source"],
    }


def build_plan(prefix: str, touch_number: int, emails: list[str]) -> dict[str, dict]:
    """Assign a UNIQUE slot-combination to every email in the batch for this touch.

    Combinations are deterministically shuffled (seeded by prefix+touch) so the spread
    is stable across reruns but not in a predictable file order. If the batch is larger
    than the available combination space, combos cycle (a warning is logged); otherwise
    every lead's copy is distinct. Returns {email_lower: variant_dict}; empty dict means
    no variant pool for this touch (caller uses the flat template)."""
    vs = load_variant_set(prefix, touch_number)
    if not vs:
        return {}
    active = _active_slots(vs)
    if not active:
        return {}

    combos = list(itertools.product(*[range(len(vs[s])) for s in active]))
    random.Random(f"{prefix}-{touch_number}").shuffle(combos)

    uniq = list(dict.fromkeys(e.strip().lower() for e in emails if e and e.strip()))
    if len(uniq) > len(combos):
        logger.warning(
            f"[VARIATION] batch of {len(uniq)} > {len(combos)} combinations for "
            f"{prefix}-{touch_number}; some leads will share copy."
        )

    plan: dict[str, dict] = {}
    for i, email in enumerate(sorted(uniq)):
        plan[email] = _assemble(vs, active, combos[i % len(combos)])
    logger.info(
        f"[VARIATION] {prefix}-{touch_number}: planned {len(plan)} unique variants "
        f"from {len(combos)} combinations (formula: {vs['formula']})."
    )
    return plan


def log_variant(email: str, touch_number: int, variant: dict) -> None:
    """Append one variant record to logs/variant_log.jsonl (alongside the send log).
    Best-effort: a logging failure must never block a send."""
    try:
        _VARIANT_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "email": email,
            "touch": touch_number,
            "source_template": variant.get("source"),
            "formula": variant.get("formula"),
            "variant_id": variant.get("variant_id"),
            "indices": variant.get("indices"),
            "subject": variant.get("subject"),
        }
        with open(_VARIANT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:  # pragma: no cover - logging must not raise
        logger.warning(f"[VARIATION] Could not write variant log for {email}: {e}")
