"""The Issue Radar crew tools' plumbing: the crew identity the read route echoes, the
bounded resume view of its ledger, and the scrubbing of what a crew writes: every
string of a forwarded JSON body, and the public text that may reach the forge.

:mod:`kiro_crew.mcp_core` stays the import path and the patch surface; see
:mod:`kiro_crew.mcp_core_parts`.
"""

from __future__ import annotations

import contextlib
import socket
import tempfile
from pathlib import Path
from typing import Any

from kiro_crew.config.loader import config_dir
from kiro_crew.platform import redact_via_context as redact


def _redact_json_strings(value: Any) -> Any:
    """Recursively credential-redact every string (keys included) in decoded JSON.

    ``sanitize_json_values`` strips hidden characters; this pass scrubs
    credential material itself. Bodies forwarded to persisting routes (ledger
    entries sync to a configured remote) need redaction on the way IN — the
    response-path ``redact`` cannot un-persist what ``_post`` already wrote.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {_redact_json_strings(k): _redact_json_strings(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_json_strings(item) for item in value]
    return value


#: Progress lines returned by a read. The log is repo-wide and append-only, so an
#: unbounded slice grows without limit and would eventually be the largest thing
#: in a crew's context — the opposite of what a resume needs. Newest first.
_CREW_MAX_EVENTS = 20


def _crew_machine_markers() -> list[tuple[str, str]]:
    """Strings that identify THIS machine, longest first.

    Longest-first matters: the Kiro Crew home normally sits inside the user's
    home, so scrubbing the home first would leave ``<home>/.kiro/crew/...`` —
    still a directory layout — instead of collapsing the whole prefix.
    """
    markers: list[tuple[str, str]] = []
    for value, placeholder in (
        (str(config_dir()), "<kirocrew-home>"),
        (str(Path.home()), "<home>"),
        (tempfile.gettempdir(), "<tmp>"),
    ):
        if value and value not in ("/", "\\"):
            markers.append((value, placeholder))
    host = ""
    with contextlib.suppress(Exception):
        host = socket.gethostname()
    # Only a distinctive hostname is scrubbed. A short one ("dev", "mac") is a
    # real English word often enough that substring-replacing it would corrupt
    # ordinary prose, and a corrupted progress line is a worse outcome than a
    # short hostname the brief already forbids writing.
    if len(host) >= 8:
        markers.append((host, "<host>"))
    markers.sort(key=lambda pair: len(pair[0]), reverse=True)
    return markers


def _crew_public_text(text: str) -> str:
    """Sanitize a crew string that becomes PUBLIC, on the way IN.

    Two passes, for two different reasons:

    1. ``redact`` — the module's ``platform.redact_via_context`` shim, the same
       canonical egress helper ``issue_radar_record_investigation`` uses. That
       tool redacts because LLM prose about an untrusted issue body is
       re-rendered on a card; here the same prose is ALSO rendered into a
       comment on the forge, so a credential or exfil URL quoted out of an issue
       would be published, not merely stored.
    2. ``_crew_machine_markers`` — redaction covers credentials and exfil URLs,
       NOT an absolute path or a host name, and those are exactly what must not
       leave this machine in a public comment. This pass is a backstop, not the
       control: it can only remove identifiers this process can name, so the
       crew brief's prohibition remains the primary rule. It is deliberately NOT
       applied to ``worktree`` / ``branch`` / ``base_sha`` — those are the one
       place an absolute path legitimately belongs, they stay local, and
       scrubbing them would break the resume they exist for.
    """
    out = redact(text)
    for value, placeholder in _crew_machine_markers():
        out = out.replace(value, placeholder)
        if "\\" in value:
            # Windows: the same path is written both ways in practice.
            out = out.replace(value.replace("\\", "/"), placeholder)
    return out


def _crew_identity(payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """Pull ``(owner, repo, crew_id)`` out of a crew-read response.

    The identity is echoed back by the READ route, which resolves it from the
    calling session's ``X-Session-Key`` — it is never taken from tool arguments.
    That is what makes a cross-crew write impossible: a crew cannot name a repo, so
    it cannot overwrite a same-numbered issue in another repo, and it cannot reach
    another crew's item at all (which would also defeat the store's per-crew
    "one editing item" invariant).

    Tolerant of where the route puts it — top level, on the crew record, or on a
    work item — because all three carry it and a single hard-coded location would
    turn a harmless shape difference into a dead write path. The top level is the
    one that is always present: a crew with no work items yet has no other source.
    """
    _raw_crew = payload.get("crew")
    crew: dict[str, Any] = _raw_crew if isinstance(_raw_crew, dict) else {}
    _raw_items = payload.get("items")
    items: list[Any] = _raw_items if isinstance(_raw_items, list) else []
    first = next((it for it in items if isinstance(it, dict)), {})
    owner = repo = ""
    for source in (payload, crew, first):
        owner = str(source.get("owner") or "").strip()
        repo = str(source.get("repo") or "").strip()
        if owner and repo:
            break
    if not (owner and repo):
        return None
    crew_id = str(crew.get("id") or crew.get("crew_id") or first.get("crew_id") or "").strip()
    if not crew_id:
        return None
    return owner, repo, crew_id


def _crew_ledger_view(payload: dict[str, Any]) -> dict[str, Any]:
    """Project a crew-read response into what a resuming turn actually needs.

    Everything the route returns about the crew and its unfinished items is
    passed through — those fields ARE the resume state — while the repo-wide
    event log is bounded to the newest ``_CREW_MAX_EVENTS`` lines.

    The two skip fields are passed through as the route bounded them and are NOT
    re-trimmed here. ``skipped_numbers`` in particular must stay complete: a crew
    tests membership against it before spending a turn investigating, and a list
    trimmed at this layer would answer "not skipped" for an issue that is, which
    reintroduces the duplicated investigation the index removes.
    """
    _raw_events = payload.get("events")
    events: list[Any] = _raw_events if isinstance(_raw_events, list) else []
    view: dict[str, Any] = {
        "crew": payload.get("crew") or {},
        "settings": payload.get("settings") or {},
        "open_items": payload.get("items") or [],
        "counts": payload.get("counts") or {},
        "skipped_numbers": payload.get("skipped_numbers") or [],
        "recent_skips": payload.get("recent_skips") or [],
        # ``read_events`` returns NEWEST FIRST, so the newest N is ``events[:N]``,
        # not ``events[-N:]`` — the latter took the N OLDEST while the note below
        # told the crew they were the newest, so any crew past its first N events
        # was handed ancient history labelled as current. The ``reversed`` is
        # deliberate and stays: the crew reads this as a transcript of what
        # happened, which wants chronological order.
        "recent_events": list(reversed(events[:_CREW_MAX_EVENTS])),
    }
    if len(events) > _CREW_MAX_EVENTS:
        view["recent_events_note"] = (
            f"newest {_CREW_MAX_EVENTS} of {len(events)} — the full log is on the crew page"
        )
    return view
