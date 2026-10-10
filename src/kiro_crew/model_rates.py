"""Notice a change in a model's credit multiplier between two catalog reads.

kiro-cli's ``chat --list-models`` rows carry ``rate_multiplier`` (the credit cost
relative to Auto), and the picker already shows it as a badge. What nothing kept
was the PREVIOUS value, so a repricing (``gpt-5.6-sol`` going from 2.4x to 4.4x)
was visible only to someone who reopened the picker and remembered the old
number. This module keeps the last multiplier seen per model in one
data-home sidecar and reports the models whose multiplier differs from it.

The sidecar holds ``{model id: multiplier}`` and nothing else. It is never served
as a catalog, so it cannot carry a downgraded account's stale rows past a restart
(the reason the catalog itself stays in memory only, see
``dashboard/handlers/agents.py:_CatalogCache``).

Rules:

- The first sighting of a model records a baseline and reports nothing: there is
  no earlier rate to compare against, and a fresh install must not announce every
  model in the catalog.
- A model missing from one read keeps its baseline. Catalogs are entitlement- and
  deprecation-filtered, so a row can drop out and return; returning at a new rate
  is still a change.
- Only a priced multiplier (finite and > 0, not a bool) counts. A row without one
  says nothing about price, so it neither records nor reports.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import math
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.model_registry import ADVERTISED_MODEL_ID_MAX_CHARS, ADVERTISED_MODELS_MAX_IDS

logger = logging.getLogger(__name__)

SIDECAR_NAME = "model_rates.json"

#: The bell-feed channel a repricing note is posted on (notifications/bus.py).
CHANNEL = "system.models"

#: The model registry's bounds on backend-authored ids it retains. The catalog
#: handed in is already admitted under them (``_bounded_catalog``); the sidecar is
#: read back under the same bounds because it is a file on disk.
MAX_MODELS = ADVERTISED_MODELS_MAX_IDS
MAX_MODEL_ID_CHARS = ADVERTISED_MODEL_ID_MAX_CHARS


@dataclasses.dataclass(frozen=True)
class RateChange:
    model: str
    old: float
    new: float


def _priced(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _model_id(row: Mapping[str, Any]) -> str:
    for key in ("model_id", "model_name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def catalog_rates(rows: Iterable[Any]) -> dict[str, float]:
    """``{model id: multiplier}`` for every catalog row with a priced multiplier."""
    rates: dict[str, float] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        model = _model_id(row)
        rate = _priced(row.get("rate_multiplier"))
        if not model or len(model) > MAX_MODEL_ID_CHARS or rate is None:
            continue
        rates[model] = rate
        if len(rates) >= MAX_MODELS:
            break
    return rates


def sidecar_path() -> Path:
    from kiro_crew.config.paths import peek_data_home

    return peek_data_home() / SIDECAR_NAME


def _load(path: Path) -> dict[str, float]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        # An unreadable baseline is treated as none: the next read re-records it
        # and reports nothing, which is the safe direction for a notice.
        logger.debug("model rates: unreadable sidecar %s", path, exc_info=True)
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        k: v
        for k, v in ((k, _priced(v)) for k, v in raw.items() if isinstance(k, str))
        if v is not None and k and len(k) <= MAX_MODEL_ID_CHARS
    }


def _same(a: float, b: float) -> bool:
    # kiro publishes multipliers to two decimals; anything finer is float noise.
    return round(a, 4) == round(b, 4)


def observe_catalog_rates(rows: Iterable[Any], path: Path | None = None) -> list[RateChange]:
    """Compare a catalog read with the recorded baseline, then record it.

    Blocking file I/O: async callers run this off the event loop. Returns the
    changed models sorted by id; an empty list when nothing changed or when this
    read is the first sighting of every priced model.
    """
    current = catalog_rates(rows)
    if not current:
        return []
    target = path if path is not None else sidecar_path()
    previous = _load(target)
    changes = [
        RateChange(model, previous[model], rate)
        for model, rate in sorted(current.items())
        if model in previous and not _same(previous[model], rate)
    ]
    # Current rows win; baselines for models absent from this read are kept,
    # oldest-first trimmed only if the union outgrows the bound.
    merged = {k: v for k, v in previous.items() if k not in current}
    merged.update(current)
    if len(merged) > MAX_MODELS:
        merged = dict(list(merged.items())[-MAX_MODELS:])
    if merged != previous:
        try:
            atomic_write(target, json.dumps(merged, sort_keys=True))
        except OSError:
            # Report nothing the baseline does not hold: a change announced
            # without being recorded would be announced again on every read.
            logger.debug("model rates: could not persist %s", target, exc_info=True)
            return []
    return changes


def format_multiplier(value: float) -> str:
    """The picker's spelling: at least one decimal, at most two, ASCII ``x``."""
    text = f"{round(value, 2):.2f}".rstrip("0")
    if text.endswith("."):
        text += "0"
    return f"{text}x"


def change_note(changes: list[RateChange]) -> tuple[str, str]:
    """Title and body of the bell note for one catalog read's rate changes."""
    lines = [
        f"{c.model} now bills at {format_multiplier(c.new)} credits "
        f"(was {format_multiplier(c.old)})."
        for c in changes
    ]
    title = "Model credit rate changed" if len(changes) == 1 else "Model credit rates changed"
    return title, "\n".join(lines)


async def notify_rate_changes(rows: list[dict], bus: Any) -> None:
    """Record this catalog read's rates and post one bell note if any changed.

    The baseline is recorded even with no bus, so a later read with one still
    compares against the real previous rate. Best-effort: a failure here is
    logged and never reaches the catalog fetch that called it.
    """
    import asyncio

    from kiro_crew.executors import maintenance_executor
    from kiro_crew.notifications.bus import NotificationPayload

    try:
        changes = await asyncio.get_running_loop().run_in_executor(
            maintenance_executor(), observe_catalog_rates, rows
        )
        if not changes or bus is None:
            return
        title, body = change_note(changes)
        bus.push(
            NotificationPayload(
                source="system",
                channel=CHANNEL,
                title=title,
                body=body,
                group_key="model-rate-change",
            )
        )
    except Exception:  # the catalog must not depend on the feed
        logger.warning("model rates: could not report a rate change", exc_info=True)
