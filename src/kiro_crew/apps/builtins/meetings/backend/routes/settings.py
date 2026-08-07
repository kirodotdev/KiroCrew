"""Settings routes — app config and the speech-correction dictionary.

``GET  …/config``            the app config (agents, providers, presets)
``PUT  …/config``            replace the app config (validated, narrow schema)
``GET  …/dictionary``        the speech-correction terms
``POST …/dictionary``        add a term
``POST …/dictionary/remove`` remove a term
``POST …/dictionary/reload`` re-read the file from disk

The config writer is a **narrow allow-list**, not a merge of whatever the client
sends: ``config.json`` drives which agent runs and where tasks are filed, so an
unvalidated write would let a request name an arbitrary agent or provider.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain import session as sess
from kiro_crew.apps.builtins.meetings.backend.providers import calendar as cal
from kiro_crew.apps.builtins.meetings.backend.providers import tasks as taskprov
from kiro_crew.apps.builtins.meetings.backend.routes._common import (
    BadRequest,
    data_root,
    field_int,
    field_str,
    field_str_list,
    json_body,
)

logger = logging.getLogger("kirocrew.app.meetings")

_MAX_AGENTS = 12
_MAX_PRESETS = 30
_MAX_PROMPT = 8000
# An agent's ``agent`` field names an installed agent spec by its DECLARED name
# (``meetings-note-taker``) — that is the dispatchable identifier, and what
# kiro-cli enumerates. The namespaced form (``meetings/meetings-note-taker``) is a
# display/tracking id and is NOT resolvable; asking for it yields
# ``Mode '…' not found``.
#
# The slash is still accepted by the charset below, deliberately: a config written
# by an older build may carry the namespaced value, and refusing it here would turn
# a stale setting into a validation error on an unrelated save. Such a value is not
# left to fail at dispatch — that failure reaches only the Gateway log, never the
# UI — so ``store.read_config`` strips the namespace from builtin rows on read.
# Path traversal and separators stay refused.
_AGENT_REF_MAX = 128


def _clean_agent_ref(value: object) -> str:
    """Validate an agent-spec reference. Anything suspicious becomes ""."""
    if not isinstance(value, str):
        return ""
    ref = value.strip()
    if not ref or len(ref) > _AGENT_REF_MAX or ".." in ref or ref.startswith(("/", ".")):
        return ""
    if not all(ch.isalnum() or ch in "-_/" for ch in ref):
        return ""
    return ref


def _clean_agent_def(raw: Any) -> dict[str, Any] | None:
    """Coerce one agent definition into the app's schema, or drop it."""
    if not isinstance(raw, dict):
        return None
    try:
        agent_id = store.safe_agent_id(raw.get("id"))
    except store.MeetingsPathError:
        return None
    widget = raw.get("widget_type")
    return {
        "id": agent_id,
        "name": str(raw.get("name") or agent_id).strip()[:120],
        "agent": _clean_agent_ref(raw.get("agent")),
        "widget_type": widget if widget in k.WIDGET_EXT_MAP else k.DEFAULT_WIDGET_TYPE,
        "prompt": str(raw.get("prompt") or "").strip()[:_MAX_PROMPT],
        "enabled_by_default": bool(raw.get("enabled_by_default", True)),
        "listening_by_default": bool(raw.get("listening_by_default", True)),
        "builtin": bool(raw.get("builtin", False)),
    }


def _clean_preset(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    agents = raw.get("enabled_agents")
    if not isinstance(agents, list):
        agents = []
    cleaned: list[str] = []
    for item in agents[:_MAX_AGENTS]:
        try:
            cleaned.append(store.safe_agent_id(item))
        except store.MeetingsPathError:
            continue
    return {"enabled_agents": cleaned}


def _known_calendar_ids() -> set[str]:
    return {row["id"] for row in cal.available_calendar_providers()}


def _known_task_ids() -> set[str]:
    return {row["id"] for row in taskprov.available_task_providers()}


async def handle_get_config(request: web.Request) -> web.Response:
    config = await asyncio.to_thread(store.read_config, data_root(request))
    return web.json_response(
        {
            "config": config,
            "task_providers": taskprov.available_task_providers(),
            "calendar_providers": cal.available_calendar_providers(),
            "stt_providers": [{"id": k.STT_PROVIDER_KIROCREW, "label": "Kiro Crew speech-to-text"}],
        }
    )


async def handle_put_config(request: web.Request) -> web.Response:
    """Replace the app config from a validated, allow-listed body."""
    body = await json_body(request)
    root = data_root(request)
    incoming = body.get("config")
    if not isinstance(incoming, dict):
        raise BadRequest("config must be a JSON object")

    agents_raw = incoming.get("meeting_agents")
    agents: list[dict[str, Any]] = []
    if isinstance(agents_raw, list):
        seen: set[str] = set()
        for raw in agents_raw[:_MAX_AGENTS]:
            cleaned = _clean_agent_def(raw)
            if cleaned is not None and cleaned["id"] not in seen:
                seen.add(cleaned["id"])
                agents.append(cleaned)
    if not agents:
        agents = list(store.DEFAULT_MEETING_AGENTS)

    presets_raw = incoming.get("presets")
    presets: dict[str, Any] = {}
    if isinstance(presets_raw, dict):
        for name, raw in list(presets_raw.items())[:_MAX_PRESETS]:
            if not isinstance(name, str) or not name.strip():
                continue
            cleaned_preset = _clean_preset(raw)
            if cleaned_preset is not None:
                presets[name.strip()[:120]] = cleaned_preset

    calendar_raw = incoming.get("calendar")
    calendar_raw = calendar_raw if isinstance(calendar_raw, dict) else {}
    calendar_provider = field_str(
        calendar_raw, "provider", default=k.DEFAULT_CALENDAR_PROVIDER, max_len=64
    )
    if calendar_provider not in _known_calendar_ids():
        calendar_provider = k.DEFAULT_CALENDAR_PROVIDER

    task_provider = field_str(
        incoming, "task_provider", default=k.DEFAULT_TASK_PROVIDER, max_len=64
    )
    if task_provider not in _known_task_ids():
        task_provider = k.DEFAULT_TASK_PROVIDER

    default_preset = field_str(incoming, "default_preset", max_len=120)
    if default_preset and default_preset not in presets:
        default_preset = ""

    config = {
        "meeting_agents": agents,
        "stt_provider": k.STT_PROVIDER_KIROCREW,  # the only provider; not client-settable
        "task_provider": task_provider,
        "calendar": {
            "provider": calendar_provider,
            # A calendar source is validated at FETCH time (scheme allow-list +
            # private-address refusal in providers/calendar.py), not here: the
            # check needs DNS and must not run on the loop during a settings save.
            "source": field_str(calendar_raw, "source", max_len=2000),
        },
        "presets": presets,
        "default_preset": default_preset,
        "poll_interval_active": field_int(
            incoming, "poll_interval_active", default=5000, low=1000, high=120_000
        ),
        "poll_interval_idle": field_int(
            incoming, "poll_interval_idle", default=30_000, low=5000, high=600_000
        ),
    }
    # A single write, wrapped inline: the replacement config is built entirely from
    # the validated request body, so there is nothing read from disk to keep it
    # atomic with.
    await asyncio.to_thread(store.write_config, config, root)
    return web.json_response({"ok": True, "config": config})


# ── dictionary ──────────────────────────────────────────────────────────────


def _reload_terms(root: Any) -> list[dict[str, Any]]:
    """Re-read ``dictionary.toml`` and return its terms. BLOCKING.

    Runs on a worker thread, never the event loop: a TOML file read plus one
    compiled regex per alias (up to ``MAX_DICTIONARY_TERMS`` terms' worth).

    The list is built here rather than by the caller so the returned terms are the
    ones this load produced, even if another request reloads the shared dictionary
    while the response is being serialized.
    """
    return sess.reload_dictionary(root).as_list()


def _add_term(root: Any, correct: str, aliases: list[str]) -> list[dict[str, Any]]:
    """Reload, add a term, and save the dictionary. BLOCKING.

    Runs on a worker thread, never the event loop: a TOML read, a recompile, and an
    atomic TOML write.

    Grouped into ONE hop because this is a read-modify-write of a single file
    through process-wide shared state (``domain.session._dictionary``): splitting it
    would let a concurrent add reload the dictionary between this one's load and its
    save, and the later save would drop the earlier term. Raises ``ValueError`` for
    an invalid term exactly as ``add_term`` does inline; the caller maps it to 400.
    """
    with sess.dictionary_transaction():
        dictionary = sess.reload_dictionary(root)
        dictionary.add_term(correct, aliases)
        dictionary.save(store.dictionary_path(root))
        return dictionary.as_list()


def _remove_term(root: Any, correct: str) -> list[dict[str, Any]] | None:
    """Reload, remove a term, and save. None when the term did not exist. BLOCKING.

    Runs on a worker thread, never the event loop, and grouped into ONE hop for the
    same read-modify-write reason as :func:`_add_term`.
    """
    with sess.dictionary_transaction():
        dictionary = sess.reload_dictionary(root)
        if not dictionary.remove_term(correct):
            return None
        dictionary.save(store.dictionary_path(root))
        return dictionary.as_list()


async def handle_get_dictionary(request: web.Request) -> web.Response:
    terms = await asyncio.to_thread(_reload_terms, data_root(request))
    return web.json_response({"terms": terms})


async def handle_reload_dictionary(request: web.Request) -> web.Response:
    terms = await asyncio.to_thread(_reload_terms, data_root(request))
    return web.json_response({"ok": True, "count": len(terms)})


async def handle_add_dictionary_term(request: web.Request) -> web.Response:
    body = await json_body(request)
    root = data_root(request)
    correct = field_str(body, "correct", required=True, max_len=120)
    aliases = field_str_list(body, "aliases", max_items=25, max_len=120) or []

    try:
        terms = await asyncio.to_thread(_add_term, root, correct, aliases)
    except ValueError as exc:
        raise BadRequest(str(exc)) from None
    return web.json_response({"ok": True, "terms": terms})


async def handle_remove_dictionary_term(request: web.Request) -> web.Response:
    body = await json_body(request)
    root = data_root(request)
    correct = field_str(body, "correct", required=True, max_len=120)

    terms = await asyncio.to_thread(_remove_term, root, correct)
    if terms is None:
        return web.json_response({"error": "term not found", "code": "term_not_found"}, status=404)
    return web.json_response({"ok": True, "terms": terms})
