"""The KEYSTONE primary enable for computer use.

State lives in ``<config_dir>/computer_use.json`` — **not** in ``config.json``.
That is a security decision with a precedent in this repo: the denied-command
opt-out is deliberately kept off ``config.json`` BECAUSE it is a security
ceiling, and ``security.py`` records the reasoning. A primary enable for full
desktop observation plus input synthesis is the same class of control, so it
gets the same treatment.

The mechanics that make it un-flippable by the agent:

* the leaf is on ``security._CREW_SECRET_LEAVES``, so ``is_sensitive_path``
  blocks agent reads AND writes on the tool path, and
  ``is_sensitive_bash_command`` blocks the shell forms (``cat``, ``>``,
  ``tar -x`` into the trust root);
* the only writer is the dashboard PUT handler, which does not route through the
  agent tool gate;
* every read fails soft to ``{}`` → **DISABLED**. A missing, unreadable,
  truncated or hand-mangled file must never mean "enabled" — fail-safe for a
  gate whose open position hands out the operator's whole desktop.

Modeled on ``hooks.load_denied_commands_state()``, which has the identical
contract and the identical failure posture.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.computer_use.types import (
    STATE_FILE_NAME,
    STATE_KEY_ENABLED,
    PolicyConfig,
)
from kiro_crew.config import loader as config_loader

logger = logging.getLogger(__name__)

# Owner-only: the file records a security decision, and its ``allowed_apps`` list
# names applications the operator automates — mildly sensitive on its own.
_STATE_FILE_MODE = 0o600


def computer_use_state_path() -> Path:
    """Path to the keystone ``computer_use.json``.

    Delegates to ``config.loader.computer_use_state_path()`` when that helper is
    present so there is a single canonical definition (and so the suite's usual
    ``patch("kiro_crew.config.loader.…")`` redirection keeps working). The
    fallback computes the same path from ``config_dir()`` — resolved through the
    loader module attribute, not a direct import, so a test that patches
    ``config.loader.config_dir`` still redirects us.
    """
    dedicated = getattr(config_loader, "computer_use_state_path", None)
    if callable(dedicated):
        path = dedicated()
        return path if isinstance(path, Path) else Path(path)
    return config_loader.config_dir() / STATE_FILE_NAME


def load_state() -> dict:
    """Read the keystone state (fail-soft to ``{}``).

    Returns ``{}`` — which :func:`is_enabled` reads as DISABLED — when the file
    is absent, unreadable, not valid JSON, or not a JSON object. A corrupt
    ceiling file must not be interpreted generously.
    """
    try:
        raw = json.loads(computer_use_state_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("computer_use.json load failed; treating as disabled", exc_info=True)
        return {}


def is_enabled(state: "dict | None" = None) -> bool:
    """True only when the keystone explicitly says ``enabled: true``.

    Strict identity against ``True`` rather than a truthiness test: a
    hand-edited ``"enabled": "false"`` (a truthy non-empty string) or
    ``"enabled": 1`` must not enable desktop control. The only spelling that
    enables the feature is a real JSON ``true``, which is what the dashboard
    writes.

    *state* lets a caller that already loaded the file avoid a second read
    inside one dispatch.
    """
    data = load_state() if state is None else state
    return data.get(STATE_KEY_ENABLED) is True


def load_policy_config(state: "dict | None" = None) -> PolicyConfig:
    """Operator target policy (allow-list / extra denials) from the keystone.

    Raises :class:`PolicyStateError` for ONE case: a present-but-malformed
    ``allowed_apps``. Everything else is coerced, because an empty
    ``extra_denied_apps`` only means "no extra denials" (fail-closed on its own),
    while an empty ``allowed_apps`` means "every app that is not denied" — so
    silently coercing a malformed allow-list would convert an operator's
    restriction into no restriction at all. The dispatcher catches this and refuses
    the action naming the file; refusing is the safe direction for a value that was
    clearly INTENDED to narrow something.

    Note that an ABSENT ``allowed_apps`` is still the documented "no allow-list"
    case and is not an error.
    """
    return PolicyConfig.from_state(load_state() if state is None else state)


def save_state(state: dict) -> None:
    """Write the keystone state atomically, owner-only.

    Callers must pass the COMPLETE object (read-modify-write): this replaces the
    file wholesale, and the dashboard handler is responsible for refusing to
    mutate a corrupt file rather than clobbering it — resetting a populated but
    unparseable ceiling to defaults would be a silent security downgrade.

    Raises on failure (``OSError``) so the HTTP handler can report a real error;
    the read path is the only fail-soft direction here.
    """
    if not isinstance(state, dict):
        raise ValueError("computer_use.json state must be a dict")
    path = computer_use_state_path()
    payload: dict[str, Any] = dict(state)
    atomic_write(path, json.dumps(payload, indent=2) + "\n", mode=_STATE_FILE_MODE)
