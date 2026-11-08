"""What a Shared Crew guest may not do: the owner-only surface, pinned in one place.

A guest is a collaborator a membership boundary admitted (``collaborators``).
Inside the Crew a guest has the owner's abilities: ``is_owner_dashboard_request``
answers true for a guest, so every owner-gated in-Crew route (chat, mates,
approvals, artifacts, skills, ...) admits one. The routes below are the
exception. They manage the share itself or the host's ceiling, so a guest is
refused on them. Most refused a guest before this list existed. The writes that
were open to one and are refused now each act with the host owner's credentials or
reach the host or the installation: messages, uploads, pins and reactions sent with
a channel's token, linking a session to Slack, deploys, session-storage cleanup, the
native file picker and screenshot, the ffmpeg download, the gateway's update and log
level, the voice, STT, notification-channel and project settings, theme pack
installs, the usage refresh, the mobile sign-in link, the portability preview and
installed apps' own routes. Matching is by request path, which keeps the list reviewable as
data and lets one test pin it. A request whose path, as sent, percent-encodes a
separator is owner-only whatever it names (:func:`_owner_only_request`).

Anything that is not listed is open to a guest. Add a route here when it
manages who may reach the Crew, the network path, ``config.json``, security
policy, a credential, the gateway process, or code and tools the host runs
outside an agent's approvals.
"""

from __future__ import annotations

import os
import posixpath
import re
import stat
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.image_refs import _PATH_RE, mask_user_literal_text

__all__ = [
    "HOST_PATH_FIELDS",
    "OWNER_ONLY_METHOD_PATHS",
    "OWNER_ONLY_PATH_PATTERNS",
    "OWNER_ONLY_PATH_PREFIXES",
    "OWNER_ONLY_WRITE_PATTERNS",
    "OWNER_ONLY_RAW_PREFIXES",
    "guest_acts_as_owner",
    "attachment_read",
    "guest_attachment_read",
    "guest_created_by",
    "guest_creator_name",
    "guest_host_path_denial",
    "guest_write_denial",
    "guest_job_tag",
    "guest_posture_refusal",
    "slack_recipient",
    "guest_recently_revoked",
    "is_guest_session",
    "record_guest_revocation",
    "is_owner_only_path",
]

#: Matched on a path-segment boundary: ``/api/mcp`` covers ``/api/mcp/custom`` and
#: not ``/api/mcp-apps/call``.
OWNER_ONLY_PATH_PREFIXES: tuple[str, ...] = (
    # Who may reach the Crew, and over which network path.
    "/api/shared-crew/access",
    "/api/shared-crew/preview",
    "/api/shared-crew/tailscale",
    "/api/shared-crew/ssm",
    "/api/tailnet",
    "/api/tunnel",
    "/api/mobile-connect",
    "/api/instances",
    # config.json and security policy.
    "/api/config",
    "/api/dashboard/config",
    "/api/agent/config",
    "/api/security",
    "/api/governance",
    "/api/redaction",
    "/api/admin",
    "/api/sel",
    "/api/decisions/consent",
    "/api/decisions/provider",
    "/api/decisions/local-models",
    "/api/aws",
    "/api/computer-use",
    "/api/telemetry/collection",
    "/api/debug",
    "/api/logs",
    "/api/diagnostics",
    "/api/system",
    # Credentials, connection secrets and the channels that hold them.
    "/api/secrets",
    "/api/connections",
    "/api/kas-login",
    "/api/kas-bundle",
    "/api/sso-login",
    "/api/sso-ttl",
    "/api/token",
    "/api/relay-viewer",
    "/api/auth",
    "/api/webhooks",
    "/api/channels",
    "/api/channel",
    "/api/channel-folders",
    "/api/session-environments",
    "/api/wakatime",
    "/api/onboarding",
    "/api/stt/ffmpeg",
    "/api/slack",
    "/api/telegram",
    "/api/discord",
    "/api/feishu",
    "/api/imessage",
    "/api/teams",
    "/api/messaging",
    "/api/webex",
    "/api/wecom",
    "/api/weixin",
    "/api/whatsapp",
    "/api/voice/config",
    "/api/notifications/push",
    "/api/cloud",
    "/api/usage",
    "/api/sessions/usage",
    "/api/deploy",
    "/api/publish-providers",
    "/api/remote-artifacts",
    "/api/send-message",
    "/api/delete-message",
    "/api/slack-profile",
    "/api/mediated-secret-request",
    "/api/mediated-secret-capability",
    "/api/connection-proxy",
    "/api/slack-history",
    "/api/app-store",
    "/api/mcp-gateway",
    "/api/skills/-/trust",
    "/api/mcp/oauth",
    # Monitors probe a provider with the owner's credentials and report what they find.
    "/api/monitors",
    "/api/autonudge",
    # The gateway process and code the host runs outside an agent's approvals.
    "/api/restart",
    "/api/shutdown",
    "/api/update",
    "/api/terminal",
    "/api/ws/terminal",
    "/api/mcp",
    "/api/capability",
    "/api/app-tools",
    "/api/mcp-apps",
    "/api/hooks",
    "/api/kiro-hooks",
    "/api/apps",
    "/api/browser",
    "/apps",
    "/api/themes/install",
    "/api/themes/install-zip",
    "/api/portability",
    "/api/worktree",
    "/api/project-scaffold",
    # The host filesystem outside the Crew's own stores. ``/api/file-raw`` is
    # reached by a guest only for chat attachments (``guest_attachment_read``).
    "/api/directory-upload",
    "/api/browse-dirs",
    "/api/browse-files",
    "/api/path-complete",
    "/api/reveal",
    "/api/project/git",
    "/api/project/tree",
    "/api/project/session-files",
    "/api/project/session-file-diff",
    "/api/knowledge/pick-folder",
    "/api/knowledge/remote-search",
    "/api/knowledge/import",
    "/api/knowledge/ingest",
    "/api/knowledge/sources",
    "/api/memory/stores",
    "/api/memory/backup",
    "/api/memory/backups",
    "/api/memory/restore",
    "/api/memory/import",
    "/api/memory/migrate",
    "/api/memory/enable-embeddings",
    "/api/memory/disable-embeddings",
    "/api/memory/embedding-model",
    # The host owner's own dashboard state, and the remote-dispatch budget and mailbox
    # that act for the owner's hub account.
    "/api/ui-prefs",
    "/api/chat/headroom",
)

#: Matched as a plain string prefix (the file routes share a ``file-`` stem).
OWNER_ONLY_RAW_PREFIXES: tuple[str, ...] = ("/api/file-",)

#: Matched against the whole path.
OWNER_ONLY_PATH_PATTERNS: tuple[str, ...] = (
    r"/api/chat/slots/[^/]+/work-item",
    # A schedule's script source and secret grants.
    r"/api/crons/[^/]+/(?:script|secrets)",
    # Publishing or syncing an artifact spends the owner's account and posts it publicly.
    r"/api/artifacts/[^/]+/(?:publish|sharing|pull-latest|upstream-status|overwrite-remote)(?:/.*)?",
    # A session's remote environment: browsing and writing its files.
    r"/api/chat/slots/[^/]+/environment/(?:browse|file)(?:/.*)?",
)

#: Matched against the whole path for every method but a read. A guest reads these
#: (the agent picker, the workspace list, a session's environment) and does not
#: change them: an agent spec writes machine-wide tool approvals and MCP
#: declarations into ``config.json``, a workspace adopts a host directory, and an
#: environment repoints where the session runs.
OWNER_ONLY_WRITE_PATTERNS: tuple[str, ...] = (
    r"/api/agents(?:/.*)?",
    r"/api/workspaces(?:/.*)?",
    r"/api/projects(?:/.*)?",
    r"/api/chat/slots/[^/]+/environment",
    r"/api/agent-spec-pins(?:/.*)?",
    # A workflow's agents run with every tool call approved, so starting, saving or
    # promoting one is the owner's. A guest still reads definitions and runs.
    r"/api/workflows/(?:run|run_intent)",
    r"/api/workflows/definitions(?:/.*)?",
    r"/api/workflows/runs/[^/]+/(?:rerun|promote|update-definition)",
    # Installation-wide settings a guest reads and does not change: history retention
    # (a heartbeat sweep then deletes everyone's older history), notification channels,
    # a mate's Goals check-in schedules (they create unattended jobs no revoke can find),
    # and linking a session to the owner's Slack. A plan step or the plan itself, which an
    # owner's auto-approved run would execute under its standing grant.
    r"/api/memory/settings",
    r"/api/notifications/channels/settings",
    r"/api/members/[^/]+/goals/schedules",
    r"/api/chat/slots/[^/]+/(?:slack|mirror)-(?:link|pause|unlink)",
    r"/api/taskrunner/[^/]+/(?:tasks/[^/]+|plan)",
    # Rebinding an artifact to another host file.
    r"/api/artifacts/[^/]+/relocate",
)

#: ``(method, path)`` pairs refused whatever the rest of the path's methods allow:
#: deleting every stored session cannot be undone.
OWNER_ONLY_METHOD_PATHS: tuple[tuple[str, str], ...] = (
    ("DELETE", "/api/sessions"),
    # The native file picker and the interactive screenshot start host desktop processes.
    ("POST", "/api/upload"),
    ("POST", "/api/screenshot"),
)

_PATTERNS = tuple(re.compile(p) for p in OWNER_ONLY_PATH_PATTERNS)
_DOT_SEGMENT = re.compile(r"(?:^|/)\.\.?(?:/|$)")
#: ``/`` or ``\`` percent-encoded, in either case, through any depth of ``%25``.
_ENCODED_SEPARATOR = re.compile(r"%(?:25)*(?:2f|5c)", re.IGNORECASE)
_WRITE_PATTERNS = tuple(re.compile(p) for p in OWNER_ONLY_WRITE_PATTERNS)
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_owner_only_path(path: str, method: str = "") -> bool:
    """Whether *path* (with *method*, when given) is a route a guest is refused on."""
    if _DOT_SEGMENT.search(path):
        # The router matches the path as sent, so a dot segment can stand in a
        # path variable where normalization would erase it.
        return True
    path = posixpath.normpath("/" + path.lstrip("/")) if path else "/"
    if (method.upper(), path) in OWNER_ONLY_METHOD_PATHS:
        return True
    if any(path.startswith(p) for p in OWNER_ONLY_RAW_PREFIXES):
        return True
    for prefix in OWNER_ONLY_PATH_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    if any(p.fullmatch(path) for p in _PATTERNS):
        return True
    return (
        bool(method)
        and method.upper() not in _READ_METHODS
        and any(p.fullmatch(path) for p in _WRITE_PATTERNS)
    )


def _raw_path(request: Any) -> str:
    """The request's path as sent (still percent-encoded, no query), else its ``path``."""
    raw = getattr(getattr(request, "rel_url", None), "raw_path", None)
    return raw if isinstance(raw, str) else str(getattr(request, "path", "") or "")


def _owner_only_request(request: Any, method: str) -> bool:
    """Whether *request* is on a route a guest is refused on (:func:`is_owner_only_path`).

    The router matches the path as sent, where ``x%2Fy`` is ONE segment its handler
    reads as ``x/y``, while ``request.path`` is decoded, where it reads as two: a
    ``[^/]+`` pattern then misses ``/api/taskrunner/x%2Fy/tasks/0``. No dashboard
    client sends an encoded separator in a path, so such a request is owner-only,
    whatever route it reaches. :func:`guest_write_denial` and
    :func:`guest_acts_as_owner` both judge a request here.
    """
    if _ENCODED_SEPARATOR.search(_raw_path(request)):
        return True
    return is_owner_only_path(str(getattr(request, "path", "") or ""), method)


#: Owner-only paths a guest's session still writes to: signing out and refreshing.
#: Compared with the path as sent, so an encoded spelling of one is not exempt.
_GUEST_WRITE_EXEMPT_PATHS = frozenset({"/api/auth/logout", "/api/auth/refresh", "/api/logout"})


def guest_write_denial(request: Any) -> Any:
    """A 403 when a guest writes to an owner-only route, else ``None``.

    Run before the handler, so a listed route is closed even when its handler
    never asks the owner gate. Reads are left to their handlers: the dashboard
    polls several listed routes and meets their own refusals.
    """
    method = str(getattr(request, "method", "") or "").upper()
    if method in _READ_METHODS or not is_guest_session(request):
        return None
    if "guest_admitted_at" not in request:
        # When this write was admitted, before its body is read: a revoke that lands
        # later fences it however slowly the body arrives (``guest_recently_revoked``).
        import time

        request["guest_admitted_at"] = time.monotonic()
    if _raw_path(request).rstrip("/") in _GUEST_WRITE_EXEMPT_PATHS or not _owner_only_request(
        request, method
    ):
        return None
    from aiohttp import web

    return web.json_response(
        {"error": "owner authorization required", "code": "owner_only"}, status=403
    )


#: Request fields that name a location on the host's disk. A guest never supplies
#: one: the gateway would read, write or run in a directory the guest picked (a
#: spec file read into a transcript, a plan's progress file written beside it, a
#: workspace on a UNC share, an artifact bound to a host file). ``guest_host_path_denial``
#: refuses a guest write carrying any of these, in the body (top level) or the query,
#: so a new route that reads one is closed without a per-handler guard. Task text and
#: attachments stay open: they are content, not locations.
#: ``test_every_guest_open_write_handler_field_is_classified_as_host_path_or_reviewed``
#: fails when a guest-open handler reads a path-like field that is neither here nor
#: reviewed in the test.
HOST_PATH_FIELDS: frozenset[str] = frozenset(
    {
        "path",
        "paths",
        "file",
        "file_path",
        "source_path",
        "spec",
        "spec_path",
        "yaml_path",
        "plan_path",
        "workspace_dir",
        "project",
        "project_dir",
        "project_path",
        "steering_dirs",
        "cwd",
        "workdir",
        "work_dir",
        "working_dir",
        "dir",
        "directory",
        "root",
        "env_file",
        "local_path",
        "host_path",
        "folder_path",
        "output_path",
        "target_path",
        "dest_path",
        "old_path",
        "new_path",
    }
)

#: ``spec`` also carries an inline spec (the task text itself, written into the run's
#: own directory), which a guest may send; any other value is a file path.
_INLINE_SPEC_PREFIX = "__inline__:"

#: A form part named like these carries an uploaded file, not a location.
_UPLOAD_PART_NAMES = frozenset({"file", "files"})

#: Routes whose handler streams the body (``request.multipart()`` or ``request.content``)
#: past the app's ``client_max_size`` on purpose, up to its own byte limit: a video up to
#: 512 MiB, a session archive of any size. ``request.read()`` here would answer 413 above
#: 60 MiB, so the body of these is not read at the checkpoint (the query still is). Each
#: handler consumes only its ``file``/``audio`` parts or the raw archive and reads no form
#: field, so there is no host-path field in them for it to see; the sweep test pins that
#: every guest-open streaming handler is listed here and that none reads a listed field.
_STREAMED_UPLOAD_PATHS = frozenset(
    {"/api/upload/file", "/api/stt/transcribe", "/api/chat/slots/import"}
)


def _carries_host_path(key: object, value: object) -> bool:
    """Whether *key* is a host-path field holding a value (empty and null are not)."""
    if not isinstance(key, str) or key.lower() not in HOST_PATH_FIELDS:
        return False
    if isinstance(value, str):
        if key.lower() == "spec" and value.startswith(_INLINE_SPEC_PREFIX):
            return False
        return bool(value.strip())
    return value not in (None, False, 0, [], {})


def _host_path_fields_in(mapping: Any) -> list[str]:
    return [k for k, v in mapping.items() if _carries_host_path(k, v)]


def _replay_body(request: Any, raw: bytes) -> None:
    """Give *request* a payload stream that yields *raw* again from the start."""
    import asyncio

    from aiohttp.streams import StreamReader

    # A limit above the body's size keeps feed_data from pausing the real connection.
    stream = StreamReader(
        request.protocol, limit=max(2**16, len(raw) + 1), loop=asyncio.get_running_loop()
    )
    if raw:
        stream.feed_data(raw)
    stream.feed_eof()
    request._payload = stream  # noqa: SLF001 - aiohttp keeps no public setter


async def _body_names_host_path(request: Any) -> bool:
    """Whether the body carries a host-path field, read the way a handler reads it.

    The rule: **the checkpoint reads the body with the handler's own parser, so they
    cannot disagree.** There is no second parser here (no byte sniffing, no encoding or
    boundary handling, no label list): ``request.json()`` and ``request.post()`` are the
    calls handlers make, on this same request, and they decode the charset, the BOM and
    the multipart boundary exactly as the handler's call will. A body neither call
    parses has no field a handler could read, and passes. Both are tried, because a
    body can parse as each (a JSON object that is also a urlencoded string).

    A location can also ride inside text, for a reader that runs later with no guest mark:
    a scheduled run builds its prompt as the owner, and so does the owner regenerating a
    guest's row. Every string the body carries, in any field or nesting (a chat message, a
    note, a queued prompt), is checked by :func:`_local_image_outside_stores`, so no route
    that stores a guest's text needs its own guard. A session import is streamed past this
    checkpoint; a host image its rows name is copied with the host-origin name, which no
    guest reader serves (``chat_attachments.read_with_origin``), and a guest's regenerate
    of its rows reads their paths as the guest's (``guest_prompt_origin``).
    """
    import asyncio

    import aiohttp
    from aiohttp import web

    # The text itself is judged off the event loop (``_values_name_outside_image``):
    # normalizing and path-scanning a large body takes time a guest must not be able
    # to spend on the loop every other request shares.
    values: list[Any] = []
    try:
        fields = await request.json()
    except (LookupError, RecursionError, ValueError):
        fields = None
    if isinstance(fields, dict) and _host_path_fields_in(fields):
        return True
    values.append(fields)
    # ``post()`` reads a multipart upload's file parts into temporary files, never
    # into memory, and drains the payload the caller replayed for it (and replays again).
    try:
        form = await request.post()
    except (
        ValueError,
        KeyError,
        AssertionError,
        aiohttp.ClientError,
        aiohttp.http_exceptions.HttpProcessingError,
    ):
        form = {}
    for name, value in form.items():
        if isinstance(value, web.FileField):
            if name.lower() in HOST_PATH_FIELDS and name.lower() not in _UPLOAD_PART_NAMES:
                return True
            continue
        if isinstance(value, (bytes, bytearray)):
            value = bytes(value).decode("utf-8", "replace")
        if _carries_host_path(name, value):
            return True
        values.append(value)
    return await asyncio.to_thread(_values_name_outside_image, values)


#: How many characters of a guest's write may need the full image-path scan (about three
#: seconds of a worker thread at the scan's worst). Text that cannot spell an image suffix
#: is skipped in linear time and does not count; past this, the write is refused rather
#: than scanned.
_IMAGE_SCAN_BUDGET_CHARS = 1 << 18


class _OverScanBudget(Exception):
    """A guest's write carries more possibly image-naming text than one check may scan."""


def _values_name_outside_image(values: list[Any]) -> bool:
    """Whether the body *values* name a local image outside the stores. Blocking."""
    texts: list[str] = []
    budget = [_IMAGE_SCAN_BUDGET_CHARS]
    try:
        for value in values:
            _strings_in(value, texts, budget=budget)
    except _OverScanBudget:
        return True
    return bool(texts) and _local_image_outside_stores(texts)


#: Every character a prefilter view drops: what is left is word characters and the
#: three a reference needs (``.`` before a suffix, ``![`` before markdown).
_NOT_REFERENCE_CHARS = re.compile(r"[^\w.!\[]+")


def _may_name_an_image(value: str) -> bool:
    """Whether some view of *value* could hold an image suffix or ``![`` markup.

    Linear, C-speed, and never narrower than :func:`_reader_views`: those views only
    drop hidden characters, compose (NFC, NFKC) and fold punctuation
    (``_MULTIBYTE_TABLE``), so with every character that is not a word character,
    ``.``, ``!`` or ``[`` removed from both folds below, any suffix or ``![`` a view
    spells is still spelled here.
    """
    import unicodedata

    from kiro_crew.context_assembly.markers import _MULTIBYTE_TABLE
    from kiro_crew.image_refs import _ANY_IMAGE_SUFFIX_RE

    for form in (value, unicodedata.normalize("NFKC", value)):
        kept = _NOT_REFERENCE_CHARS.sub("", form.translate(_MULTIBYTE_TABLE))
        if "![" in kept or _ANY_IMAGE_SUFFIX_RE.search(kept):
            return True
    return False


async def guest_host_path_denial(request: Any) -> Any:
    """A 403 ``owner_only`` when a guest's write names a location on the host's disk.

    The one checkpoint for the whole class (see :data:`HOST_PATH_FIELDS`). The query and
    the body are read before the handler, and the body is replayed so the handler reads
    it unchanged (:func:`_body_names_host_path` says how it is read). Reads are left to
    their handlers. A body over the app's ``client_max_size`` is answered 413 by
    ``request.read()``.
    """
    method = str(getattr(request, "method", "") or "").upper()
    if method in _READ_METHODS or not is_guest_session(request):
        return None
    path = str(getattr(request, "path", "") or "")
    if path.rstrip("/") in _GUEST_WRITE_EXEMPT_PATHS:
        return None
    from aiohttp import web

    refusal = web.json_response(
        {"error": "owner authorization required", "code": "owner_only"}, status=403
    )
    if _host_path_fields_in(request.query):
        return refusal
    if not request.can_read_body or path.rstrip("/") in _STREAMED_UPLOAD_PATHS:
        return None
    # ``read()`` caches the bytes, which is what ``request.json()`` and urlencoded
    # ``post()`` then parse; a handler that streams ``request.content`` gets a replay.
    raw = await request.read()
    _replay_body(request, raw)
    named = bool(raw) and await _body_names_host_path(request)
    _replay_body(request, raw)
    return refusal if named else None


#: Postures and actions that auto-approve later tool calls without the owner.
_UNATTENDED_POSTURES = frozenset(
    {
        "yolo",
        "trust",
        "trust_reads",
        "trust_command",
        "trust_base",
        # The approve action that turns trust-reads on (``permission_prompt``), posted
        # without the ``trust_reads`` conversion; and a spawned run's ``auto`` mode.
        "approved_trust_reads",
        "auto",
    }
)


def guest_posture_refusal(request: Any, posture: object) -> Any:
    """A 403 when a guest asks for an approval posture that outlasts their click.

    A guest approves or rejects each tool call. Turning on trust, trust-reads or
    YOLO would auto-approve later calls (YOLO for every session) and keep doing so
    after the guest is revoked, so those stay the host owner's.
    """
    if not isinstance(posture, str) or posture not in _UNATTENDED_POSTURES:
        return None
    if not is_guest_session(request):
        return None
    from aiohttp import web

    return web.json_response(
        {"error": "this approval mode is owner-only", "code": "owner_only"}, status=403
    )


_GUEST_TAG_PREFIX = "guest:"

#: Revoked people held at once; the oldest marks go first.
_REVOKED_MAX = 1024
_REVOKED_AT: dict[str, float] = {}


def record_guest_revocation(principal_ids: Any) -> None:
    """Note that these people were just revoked, before their schedules are swept.

    A create or enable the guest sent before the revoke can commit after the sweep
    read the store; it finds this mark when it commits and undoes itself
    (``guest_recently_revoked``). Recorded first, so a commit the sweep missed is
    always one that sees the mark. The mark is not time-limited: a request is judged
    by when it was admitted, so one that dribbles its body in for minutes is still
    fenced, and a person invited again later is admitted after the mark and is not.
    """
    import time

    now = time.monotonic()
    for pid in principal_ids:
        if pid:
            _REVOKED_AT.pop(pid, None)
            _REVOKED_AT[pid] = now
    while len(_REVOKED_AT) > _REVOKED_MAX:
        del _REVOKED_AT[next(iter(_REVOKED_AT))]


def guest_recently_revoked(request: Any) -> bool:
    """Whether *request*'s guest was revoked after this request was admitted.

    The write checkpoint stamps every guest write with its admission time; a request
    without one is fenced whenever its guest carries a revocation mark.
    """
    from kiro_crew.shared_crew.identity import viewer_principal

    at = _REVOKED_AT.get(viewer_principal(request).id)
    if at is None:
        return False
    admitted = request.get("guest_admitted_at")
    return admitted is None or bool(admitted <= at)


def guest_job_tag(principal_id: str) -> str:
    """The ``created_by`` a schedule a guest created carries, so a revoke can find it."""
    return f"{_GUEST_TAG_PREFIX}{principal_id}"


def slack_recipient(created_by: object, owner_id: object) -> str:
    """Who a job's Slack result or alert goes to when it names no channel.

    The job's creator when that is a Slack user id, else the owner. A guest's stamp
    (``guest:<principal id>``) is not a Slack user id and Slack would refuse it, so a
    guest's schedule falls back to the owner like an ownerless one.
    """
    creator = str(created_by or "")
    if creator and not creator.startswith(_GUEST_TAG_PREFIX):
        return creator
    return str(owner_id or "")


def guest_creator_name(created_by: str) -> str | None:
    """The display name of the guest a schedule's ``created_by`` stamp names.

    ``None`` when the stamp is not a guest's. ``""`` for a guest nobody has a
    name for, which the dashboard words as "Guest". The stamp itself is never
    shown: it holds the principal id.
    """
    if not isinstance(created_by, str) or not created_by.startswith(_GUEST_TAG_PREFIX):
        return None
    from kiro_crew.shared_crew.people import registry

    principal_id = created_by[len(_GUEST_TAG_PREFIX) :]
    for seen in registry().snapshot():
        if seen.principal.id == principal_id:
            shown = seen.principal.display
            return (shown.name or shown.login or "").strip()
    return ""


def guest_created_by(request: Any) -> str:
    """The ``created_by`` stamp for a schedule *request* creates: a guest's, else ``""``."""
    if not is_guest_session(request):
        return ""
    from kiro_crew.shared_crew.identity import viewer_principal

    return guest_job_tag(viewer_principal(request).id)


def is_guest_session(request: Any) -> bool:
    """Whether *request* is a guest's own dashboard session, on any route.

    A guest is a person a membership boundary admitted: an empty (present) app
    claim, the dashboard-user claim, no internal secret, and a verified
    collaborator actor. Never with the Shared Crew preview off.
    """
    if request.get("internal_auth") is True or request.get("app", None) != "":
        return False
    if request.get("is_dashboard_user") is not True or not request.get("user"):
        return False
    if _is_host_owner(request):
        # The owner on the direct tailnet listener carries the listener's provenance,
        # which a collaborator also carries; the subject tells them apart.
        return False
    from kiro_crew.shared_crew.collaborators import is_collaborator_actor
    from kiro_crew.shared_crew.identity import actor_of

    return is_collaborator_actor(actor_of(request))


def _is_host_owner(request: Any) -> bool:
    """Whether *request* is the host owner; ``False`` when that cannot be told."""
    try:
        from kiro_crew.dashboard.handlers.source_providers import (
            is_host_owner_dashboard_request,
        )

        return is_host_owner_dashboard_request(request)
    except Exception:
        return False


def guest_acts_as_owner(request: Any) -> bool:
    """Whether *request* is a guest on a route a guest may use as the owner would.

    A call that is not a route (no path: a background task, a socket frame) is not
    judged by the list, so it fails closed.
    """
    if not str(getattr(request, "path", "") or ""):
        return False
    return is_guest_session(request) and not _owner_only_request(
        request, str(getattr(request, "method", "") or "")
    )


def _is_plain_dir(path: Path) -> bool:
    """Whether *path* is a real directory, not a symlink or a Windows junction.

    ``lstat`` reports a junction as a directory, so its name-surrogate reparse tag is
    checked too (:func:`platform_compat.lstat_is_name_surrogate`). An absent path raises
    ``OSError``, as ``lstat`` does; every caller already treats that as a refusal.
    """
    info = os.lstat(path)
    return stat.S_ISDIR(info.st_mode) and not platform_compat.lstat_is_name_surrogate(info)


def _store_root(root: Any) -> Path | None:
    """*root* anchored without following it: its parent resolved, the leaf itself not.

    ``None`` when the leaf is a symlink or a Windows junction, is not a directory, or does
    not exist (the upload directory is made by the first upload): that store is then
    skipped, so nothing in it is served. A store directory swapped for a link would
    otherwise resolve to wherever the link points, and the containment test would compare
    the file against that target.
    """
    leaf = Path(os.path.expanduser(str(root)))
    try:
        anchored = Path(os.path.realpath(leaf.parent)) / leaf.name
        return anchored if _is_plain_dir(anchored) else None
    except OSError:
        return None


def _local_image_outside_stores(texts: list[str]) -> bool:
    """Whether any of *texts* names a local image outside the attachment stores.

    A guest's text reaches the model's image reader (``build_prompt_blocks`` reads any
    readable image path in it) and the session copier (a row's ``![alt](/abs/path)``),
    possibly much later and with no guest mark: a scheduled run builds its prompt as the
    owner, and so does the owner regenerating a guest's row. A copy of a host file is
    named ``-`` and no guest reads it, but the owner-side reader would still send the
    host's picture to the model. So a guest's text may name only a file in a store,
    judged here before it is accepted. Both readers'
    grammars are used: the prompt builder's own (``_PATH_RE`` over the same view of the
    text, literal code spans masked) and the copier's (``iter_local_refs``, with its
    destination normalization). Each path is judged as text by :func:`_store_relative`
    and never resolved, and a network path is refused first. Blocking only for the
    server's own data-home lookup.
    """
    from kiro_crew.messaging.outbound_files import iter_local_refs, local_destination

    for view in (view for text in texts for view in _reader_views(text)):
        named = [
            match.group(1).strip() for match in _PATH_RE.finditer(mask_user_literal_text(view))
        ]
        for ref in iter_local_refs(view):
            source = local_destination(ref.dest)
            if source is not None:
                named.append(str(source))
        if not all(_is_plain_store_file(raw) for raw in named):
            return True
    return False


def _reader_views(text: str) -> tuple[str, ...]:
    """*text* as each later reader of a guest's text may see it, the text as sent first.

    The handlers store a field through ``sanitize_string`` (hidden characters dropped,
    NFC), and the turn builder folds multibyte punctuation (``_MULTIBYTE_TABLE``) before
    the image reader scans it; the NFKC member view (``_member_normalized_view``) is the
    widest fold the context applies. A path those steps assemble, such as
    ``/srv/x.p<U+200B>ng`` or ``/srv/a<U+2014>b.png``, is judged in the view that
    spells it. The readers' own normalizers are used, so the views follow them.
    """
    from kiro_crew.context_assembly.markers import _MULTIBYTE_TABLE, _member_normalized_view
    from kiro_crew.validation_parts.text_sanitizer import sanitize_string

    stored = sanitize_string(text)
    return (text, stored, stored.translate(_MULTIBYTE_TABLE), _member_normalized_view(text))


def _strings_in(
    value: Any, out: list[str], depth: int = 0, *, budget: list[int] | None = None
) -> None:
    """Collect every string value that may name a local image in *value*, 16 levels deep.

    The readers with no guest mark read a stored string: a schedule's message, a row's
    text. The one field a reader stringifies whole, an acknowledgement's ``summary``, a
    guest may send only as a string the store keeps unchanged (``cron_runs.api_cron_ack``),
    so object keys and deeper nesting reach no prompt.
    """
    if isinstance(value, str):
        if not _may_name_an_image(value):
            return
        if budget is not None:
            budget[0] -= len(value)
            if budget[0] < 0:
                raise _OverScanBudget
        if any("![" in view or _PATH_RE.search(view) for view in _reader_views(value)):
            out.append(value)
    elif depth < 16 and isinstance(value, dict):
        for item in value.values():
            _strings_in(item, out, depth + 1, budget=budget)
    elif depth < 16 and isinstance(value, list):
        for item in value:
            _strings_in(item, out, depth + 1, budget=budget)


_SEPARATORS = re.compile(r"[\\/]")


def _store_relative(raw: str) -> tuple[Path, tuple[str, ...]] | None:
    """The server-known store root *raw* sits under and the name parts below it, or ``None``.

    Purely lexical: the guest's string is split, never resolved. It must be the literal
    upload directory plus one file name, or the literal ``sessions/`` directory plus one
    ``<stem>.attachments`` directory plus one file name. Every part is a plain name: no
    empty segment, ``.`` or ``..``, control character, or ``:`` (a drive may only open the
    path), and a leading double separator (a network path) is refused first of all.

    A session file must also carry the Crew-origin name, ``<16 lowercase hex>_<name>``
    (``chat_attachments.is_crew_origin_name``): the copier names a copy so only when its
    bytes came from a Crew-owned root, and a ``-`` copy (a host image an assistant reply
    named, or any copy an earlier build made) stays the owner's. Every guest reader
    comes through here: :func:`attachment_read` (the ``/api/file-raw`` guest arm and the
    guest prompt builder) and :func:`_is_plain_store_file` (the write checkpoint).
    """
    if not raw or (len(raw) >= 2 and raw[0] in "/\\" and raw[1] in "/\\"):
        return None
    segments = _SEPARATORS.split(raw)
    if any(
        seg in (".", "..") or any(ord(c) < 32 for c in seg) or (i and (not seg or ":" in seg))
        for i, seg in enumerate(segments)
    ):
        return None
    from kiro_crew.chat_attachments import ATTACHMENTS_DIR_SUFFIX, is_crew_origin_name
    from kiro_crew.config.paths import config_dir
    from kiro_crew.dashboard.handlers.files import _upload_dir

    folded = [os.path.normcase(seg) for seg in segments]
    for root, depth in ((_upload_dir(), 1), (config_dir() / "sessions", 2)):
        root_segments = [os.path.normcase(seg) for seg in _SEPARATORS.split(str(root))]
        while root_segments and not root_segments[-1]:
            root_segments.pop()
        if folded[: len(root_segments)] != root_segments:
            continue
        parts = tuple(segments[len(root_segments) :])
        if len(parts) != depth or not all(parts):
            return None
        if depth == 2 and not (
            parts[0].endswith(ATTACHMENTS_DIR_SUFFIX)
            and parts[0] != ATTACHMENTS_DIR_SUFFIX
            and is_crew_origin_name(parts[1])
        ):
            return None
        return root, parts
    return None


def _is_plain_store_file(raw: str) -> bool:
    """Whether *raw* is a literal attachment-store name for a regular file, with no link on the way.

    :func:`_store_relative` takes the name apart as text (a session file must carry the
    Crew-origin name, so a guest's text naming a host-origin copy is refused, a guest's
    schedule prompt included: a scheduled run reads its paths as the owner's); this then
    ``lstat``-s the store root, the ``<stem>.attachments`` directory under ``sessions/``
    and the file, all built from the server's own root and the plain names, so a
    directory or file replaced by a link is refused instead of followed. Blocking
    (``lstat``).
    """
    found = _store_relative(raw)
    if found is None:
        return False
    root, parts = found
    store = _store_root(root)
    if store is None:
        return False
    try:
        parent = store
        if len(parts) == 2:
            parent = store / parts[0]
            if not _is_plain_dir(parent):
                return False
        return stat.S_ISREG(os.lstat(parent / parts[-1]).st_mode)
    except (OSError, ValueError):
        return False


def attachment_read(raw: str, max_bytes: int, *, head: bool = False) -> tuple[str, bytes] | None:
    """The bytes of a chat attachment named by *raw*, or ``None``.

    ``None`` too when the file is over *max_bytes*; with *head* the first *max_bytes*
    are returned instead, so a caller can tell a picture too large to send from a name
    that is no attachment at all.

    A chat attachment is a file directly inside the upload directory, or a Crew-origin
    copy (see :func:`_store_relative`) directly inside a session's ``<stem>.attachments``
    directory under the data home's ``sessions/``. *raw* is
    only a name to look up: :func:`_store_relative` takes it apart without resolving it,
    and the path that is opened is built from the server's own store root, so the guest's
    string itself never reaches ``realpath``, a link or a network path. The store root, and the
    ``<stem>.attachments`` directory under ``sessions/``, must be real directories, not
    links or Windows junctions; the file must be a regular file, not a link (``lstat``, as
    :func:`_is_plain_store_file` checks it), and is read through the house chokepoint
    (:func:`kiro_crew.hooks.safe_read_file_bytes_nolink`) pinned to that directory, which
    judges the descriptor it opened, so a link or a second hard link swapped in for the
    file after that check, or a link swapped in for its directory, is refused.
    Blocking (``lstat``, ``open``).
    """
    from kiro_crew.hooks import FileTooLargeError, safe_read_file_bytes_nolink

    try:
        found = _store_relative(raw)
        if found is None:
            return None
        root, parts = found
        store = _store_root(root)
        if store is None:
            return None
        parent = store
        if len(parts) == 2:
            parent = store / parts[0]
            if not _is_plain_dir(parent):
                return None
        target = parent / parts[-1]
        if not stat.S_ISREG(os.lstat(target).st_mode):
            return None
        data = safe_read_file_bytes_nolink(
            str(target),
            within_root=str(parent),
            within_root_is_canonical=True,
            max_bytes=max_bytes,
            allow_truncate=head,
        )
    except (FileTooLargeError, OSError, ValueError):
        return None
    return None if data is None else (str(target), data)


async def guest_attachment_read(
    request: Any, raw: str, max_bytes: int, *, run: Any = None
) -> tuple[str, bytes] | None:
    """What a guest may read through ``/api/file-raw``: a chat attachment only, else ``None``.

    The read resolves a path the guest chose, which can block on a stalled mount.
    *run* is the executor to do that on (the caller's isolated path-probe pool, so a
    stall cannot starve the gateway's shared one); it defaults to ``asyncio.to_thread``.
    """
    import asyncio
    import functools

    if not is_guest_session(request):
        return None
    return await (run or asyncio.to_thread)(functools.partial(attachment_read, raw, max_bytes))
