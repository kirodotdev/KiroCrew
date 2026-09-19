"""The KEYSTONE consent for the decision seam.

Whether Jev may be asked at all lives in ``<config_dir>/decisions_consent.json``,
**not** in ``config.json``. Enabling the seam sends message text and skill
descriptions to an external, paid provider, and ``config.json`` is writable by an
auto-approved agent shell: a prompt-injected agent could set
``decisions.enabled: true`` and the live config watcher would start the egress
without a restart. The precedent is ``aws_service_consent.json`` (consent to spend
the operator's money) and ``computer_use.json`` (consent to drive the desktop):
an authorization goes where the agent cannot write it.

What makes it un-flippable by the agent:

* the leaf is on ``security._CREW_SECRET_LEAVES``, so ``is_sensitive_path`` blocks
  agent reads AND writes on the file-TOOL path; and it is a ``READONLY`` leaf in
  ``sandbox._CREW_READONLY_LEAVES``, so the OS sandbox denies every WRITE from the
  agent's shell in every mode. A sandboxed shell can still READ it -- that is the
  documented keystone posture (masking a ceiling would remove it, not protect it),
  and the file holds only a flag and an endpoint, nothing secret;
* the only writer is the browser-only dashboard PUT handler, which does not route
  through the agent tool gate and refuses app tokens;
* every read fails soft to ``{}`` -> **NOT CONSENTED**. A missing, unreadable,
  truncated or hand-mangled file must never mean "send".

The ``decisions`` section of ``config.json`` keeps the knobs that grant nothing on
their own -- the sampling share and the provider -- so there is exactly one place
the seam can be switched on.

Consent is bound to a DESTINATION. ``provider.endpoint`` lives in ``config.json``
too, so a switch that only said "yes" would let the same prompt-injected shell
redirect consented messages to an endpoint it controls. The keystone therefore
records the endpoint the owner consented to, and the gate sends only while the
configured endpoint still equals it; a changed endpoint is a refusal until the
owner consents again through the dashboard.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config import loader as config_loader

logger = logging.getLogger(__name__)

STATE_KEY_ENABLED = "enabled"
STATE_KEY_ENDPOINT = "endpoint"

# Owner-only: the file records a security decision.
_STATE_FILE_MODE = 0o600


class ConsentCorruptError(RuntimeError):
    """The keystone exists but cannot be parsed; a writer must not clobber it."""


def consent_path() -> Path:
    """Path to the keystone, resolved through the loader so tests can redirect it."""
    return config_loader.decisions_consent_path()


def load_state() -> dict:
    """Read the keystone (fail-soft to ``{}``, which :func:`is_enabled` reads as off)."""
    try:
        raw = json.loads(consent_path().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.debug("decisions_consent.json load failed; treating as not consented", exc_info=True)
        return {}


def is_enabled(state: "dict | None" = None) -> bool:
    """True only when the keystone explicitly says ``enabled: true``.

    Strict identity against ``True``: a hand-edited ``"enabled": "false"`` or
    ``"enabled": 1`` is not consent. The only spelling that enables the seam is a
    real JSON ``true``, which is what the dashboard writes.
    """
    data = load_state() if state is None else state
    return data.get(STATE_KEY_ENABLED) is True


def normalize_endpoint(value: object) -> str:
    """The comparable spelling of an endpoint: a stripped string, ``""`` otherwise."""
    return value.strip() if isinstance(value, str) else ""


def consented_endpoint(state: "dict | None" = None) -> str:
    """The endpoint the owner consented to, or ``""`` when none was recorded."""
    data = load_state() if state is None else state
    return normalize_endpoint(data.get(STATE_KEY_ENDPOINT))


def permits(endpoint: object, state: "dict | None" = None) -> bool:
    """Whether the keystone consents to sending to *endpoint*, exactly.

    Both halves must hold: ``enabled`` is a literal ``true`` AND the recorded
    endpoint equals the one asked about. An empty recorded endpoint permits
    nothing -- a keystone with the flag but no destination never came from the
    dashboard writer.
    """
    data = load_state() if state is None else state
    if not is_enabled(data):
        return False
    wanted = normalize_endpoint(endpoint)
    recorded = consented_endpoint(data)
    return bool(recorded) and recorded == wanted


def read_state_strict() -> dict:
    """Read the keystone for a MUTATION: raise on corrupt, ``{}`` when absent.

    A populated-but-unparseable ceiling must be reported, not overwritten: resetting
    it to defaults would be a silent change to a security decision.
    """
    path = consent_path()
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConsentCorruptError(str(exc)) from exc
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConsentCorruptError(f"decisions_consent.json is not valid JSON: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConsentCorruptError("decisions_consent.json top level is not a JSON object")
    return loaded


def save_enabled(enabled: bool, *, endpoint: str) -> dict:
    """Record *enabled* for *endpoint* atomically, owner-only; return the state written.

    Enabling records the endpoint the owner is consenting to -- the caller passes
    the one currently configured, so the keystone says where the messages will go
    at the moment consent is given. Disabling clears it, so a later re-enable
    cannot inherit a stale destination. Read-modify-write so an unknown key an
    operator added by hand survives. Raises :class:`ConsentCorruptError` rather
    than clobbering a corrupt file, and ``OSError`` on a write failure, so the
    HTTP handler can report a real error.
    """
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a bool")
    target = normalize_endpoint(endpoint)
    if enabled and not target:
        raise ValueError("consent needs the endpoint it is given for")
    state: dict[str, Any] = dict(read_state_strict())
    state[STATE_KEY_ENABLED] = enabled
    state[STATE_KEY_ENDPOINT] = target if enabled else ""
    atomic_write(consent_path(), json.dumps(state, indent=2) + "\n", mode=_STATE_FILE_MODE)
    return state
