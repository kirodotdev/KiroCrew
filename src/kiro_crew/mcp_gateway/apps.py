"""MCP Apps (SEP-1865) disk-spool + result-interception helpers.

The gateway is the actual MCP Apps *host*: when a backend's ``tools/call``
result carries a ``ui://`` resource reference, the gateway fetches that
resource out-of-band (a gateway-originated ``resources/read``), writes the
rendered HTML + render policy to a spool file on disk, and injects an opaque
marker string into the tool result's text content. The chat runner
pattern-matches ``[kirocrew-mcp-app:<id>]`` on the wire, reads the spool file
by its opaque id, and renders the app inline. **The LLM never reads the spool
file** — only the deterministic chat_runner does, keyed by the opaque id.

Security posture:

* The marker carries only an opaque ``uuid4().hex`` — never a filesystem path —
  so a compromised/adversarial tool result cannot smuggle a path-traversal
  reference through the marker.
* Spool files are written ``0600`` inside a ``0700`` directory so co-tenant
  users on a shared host cannot read another session's rendered app payload.
* :func:`sweep_spool` reaps stale files (default 24h) so the spool does not
  grow without bound.

Everything here is invoked only behind the ``KIROCREW_MCP_APPS`` feature flag
that lives in :mod:`kiro_crew.mcp_gateway.backend`; this module has no flag of
its own and is a pure, side-effect-light helper library.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional

from kiro_crew import platform_compat
from kiro_crew.mcp_apps_render import MAX_SPOOL_BYTES, SPOOL_SCHEMA_VERSION

logger = logging.getLogger(__name__)

# Env override for the spool directory (tests point this at a tmp dir so they
# never touch a developer's real ~/.kiro/crew). Read per-call, never cached.
SPOOL_ENV = "KIROCREW_MCP_APPS_SPOOL"

# Marker injected into the first text content item of an intercepted tool
# result. The chat_runner matches ``MARKER_PREFIX<opaque-id>MARKER_SUFFIX``.
MARKER_PREFIX = "[kirocrew-mcp-app:"
MARKER_SUFFIX = "]"

# Spool record schema version — bump on any breaking shape change so a stale
# reader can reject a record it does not understand. Single source of truth
# lives beside the reader (mcp_apps_render enforces it in load_spool); the
# writer aliases it so the two sides can never drift.
SCHEMA_VERSION = SPOOL_SCHEMA_VERSION

# The ``ui`` extension writes the resource reference under ``_meta.ui`` per
# SEP-1865; a flat ``_meta["ui/resourceUri"]`` key is also read for
# backwards compatibility with an earlier draft.
_DEPRECATED_FLAT_URI_KEY = "ui/resourceUri"
_UI_SCHEME = "ui://"


def spool_dir() -> Path:
    """Return the spool directory (``KIROCREW_MCP_APPS_SPOOL`` override, else
    ``$KIROCREW_HOME/mcp-apps``). Not created here — :func:`write_spool` does the
    ``mkdir``/``chmod`` so callers that only read (sweep) stay side-effect-free
    when the dir is absent.

    MUST resolve identically to ``mcp_apps_render._spool_dir`` (the dashboard
    reader) — both go through :func:`kiro_crew.config.paths.config_dir` so a
    ``KIROCREW_HOME`` override (pods, tests) moves writer and reader together.
    A ``Path.home()`` hardcode here once split them: the pod's gateway wrote
    into the LIVE plane's home and the pod's reader found nothing.
    """
    override = os.environ.get(SPOOL_ENV)
    if override:
        # ``.expanduser()`` MUST match ``mcp_apps_render._spool_dir`` exactly —
        # otherwise a ``~/…`` override makes the writer (here) and the reader
        # resolve different directories, so records are written where the
        # reader never looks.
        return Path(override).expanduser()
    # circular import: config.paths pulls config modules; keep lazy.
    from kiro_crew.config.paths import config_dir

    return config_dir() / "mcp-apps"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_spool(payload: dict) -> str:
    """Write one spool record and return its opaque ``uuid4().hex`` id.

    The record is written ``0600`` inside a ``0700`` directory. Only the keys
    named in the schema are persisted (the input ``payload`` may carry extras;
    they are dropped so the on-disk shape is stable and auditable).

    Each write also opportunistically sweeps expired records (best-effort) so
    a flag-on host that renders apps but never restarts its gateway still
    stays bounded — the startup sweep in gatewayd covers the restart path.
    """
    try:
        sweep_spool()
    except Exception:  # pragma: no cover — hygiene must never block a render
        logger.debug("opportunistic spool sweep failed", exc_info=True)
    directory = spool_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # mkdir honours umask, so force the private mode explicitly.
    try:
        # 0o700 is deliberately STRICTER than the rule's 0o644 suggestion: spool
        # records carry per-session app payloads, so the dir must be owner-only.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(directory, 0o700)
    except OSError:  # pragma: no cover — e.g. dir owned by another uid
        logger.debug("could not chmod spool dir %s to 0700", directory, exc_info=True)
    if not platform_compat.IS_POSIX:  # pragma: no cover — exercised on Windows CI
        # Windows ignores POSIX mode bits: without an explicit owner-only
        # DACL another local account could read the directory listing, and
        # spool FILENAMES are live capability tokens (see app_call.py).
        # Fail loud — a record must never exist without owner-only
        # protection; the interception caller's failure-safe path delivers
        # the original tool result with no app render.
        platform_compat.restrict_to_owner(directory)

    spool_id = uuid.uuid4().hex
    record = {
        "schema": SCHEMA_VERSION,
        "server": payload.get("server", ""),
        "tool": payload.get("tool", ""),
        "session_key": payload.get("session_key", ""),
        # Callback capability secret: split from the render id so the
        # model-visible marker (which carries only ``spool_id``) authorizes
        # NOTHING. This high-entropy secret is delivered ONLY over the
        # owner-only WS render frame and is REQUIRED to authorize any
        # app→gateway callback (see app_call.handle_app_call). A model that
        # reads the marker from its own context therefore cannot invoke
        # app-visible tools against the uid socket.
        "callback_secret": secrets.token_urlsafe(32),
        # Exact PoolKey digest of the PRODUCING backend — the app-call path
        # resolves its backend exclusively through this (see app_call.py).
        "pool_digest": payload.get("pool_digest", ""),
        "html": payload.get("html", ""),
        "csp": payload.get("csp"),
        "permissions": payload.get("permissions"),
        "structured_content": payload.get("structured_content"),
        # Additive v1 fields (optional — readers .get() them): the originating
        # tools/call arguments and full result content array, forwarded to the
        # app so it initializes from real state (SEP-1865 tool-input/result).
        "tool_input": payload.get("tool_input"),
        "result_content": payload.get("result_content"),
        "created_at": payload.get("created_at") or _now_iso(),
    }
    data = json.dumps(record, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_SPOOL_BYTES:
        # Reject over the SAME cap the reader enforces (load_spool). Writing a
        # record the reader will refuse produces an unusable file and lets a
        # hostile/huge payload grow the spool; fail the interception instead so
        # the caller's failure-safe path delivers the original tool result.
        raise ValueError(
            f"mcp-apps spool record {len(data)} bytes exceeds cap {MAX_SPOOL_BYTES}"
        )
    path = directory / f"{spool_id}.json"
    # O_CREAT|O_EXCL would be ideal, but uuid4 collision is negligible and
    # O_TRUNC keeps this idempotent on the (impossible) reuse. Mode honours
    # umask so chmod after to guarantee 0600.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover
        logger.debug("could not chmod spool file %s to 0600", path, exc_info=True)
    if not platform_compat.IS_POSIX:  # pragma: no cover — exercised on Windows CI
        # POSIX mode bits above are meaningless on Windows — apply the
        # owner-only DACL (repo rule for secret-bearing files). Fail closed:
        # if the lockdown cannot be established, remove the record rather
        # than leave a readable capability token behind, then propagate so
        # the interception failure-safe path delivers the original result.
        try:
            platform_compat.restrict_to_owner(path)
        except OSError:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
    return spool_id


def extract_ui_resource_uri(result: dict) -> Optional[str]:
    """Return the ``ui://`` resource reference from a ``tools/call`` result's
    ``_meta``, or ``None`` when absent/ineligible.

    Reads ``result._meta.ui.resourceUri`` first, falling back to the deprecated
    flat ``result._meta["ui/resourceUri"]``. Only a string beginning ``ui://``
    is accepted — any other scheme (or a non-string) is rejected so a tool
    cannot point the gateway at an arbitrary ``file://`` / ``http://`` URI.
    """
    if not isinstance(result, dict):
        return None
    meta = result.get("_meta")
    if not isinstance(meta, dict):
        return None
    uri: Any = None
    ui = meta.get("ui")
    if isinstance(ui, dict):
        uri = ui.get("resourceUri")
    if uri is None:
        uri = meta.get(_DEPRECATED_FLAT_URI_KEY)
    if isinstance(uri, str) and uri.startswith(_UI_SCHEME):
        return uri
    return None


class WithheldTools(NamedTuple):
    """Tool names withheld from the agent's listing, split by WHY.

    The split exists so the caller can log the two cases at different levels.
    ``declared`` is the server doing exactly what SEP-1865 provides for and
    needs no operator attention; ``unreadable`` means this host could not parse
    a ``visibility`` the server did set, so a tool disappeared on a judgement
    call and somebody should see it.
    """

    declared: list[str]
    unreadable: list[str]

    @property
    def names(self) -> list[str]:
        return [*self.declared, *self.unreadable]

    def __bool__(self) -> bool:
        return bool(self.declared or self.unreadable)


#: The audiences SEP-1865 defines for ``_meta.ui.visibility``.
AUDIENCE_MODEL = "model"
AUDIENCE_APP = "app"


class VisibilityVerdict(NamedTuple):
    """How :func:`visibility_allows` read a tool's ``_meta.ui.visibility``."""

    #: True when this audience may see/call the tool.
    allowed: bool
    #: True when the server DID set ``visibility`` but this host could not parse
    #: it. Distinguishes "the server said no" from "we could not tell", which
    #: the caller logs differently.
    unreadable: bool


def visibility_allows(tool: Any, audience: str) -> VisibilityVerdict:
    """Decide whether ``audience`` may reach ``tool`` per its declared visibility.

    ONE parser for BOTH directions — the agent's ``tools/list`` (audience
    ``"model"``) and an app's ``tools/call`` (audience ``"app"``). They were
    separate implementations with *opposite* defaults, and the app-side one
    denied on absence while claiming the spec required it. The spec says the
    opposite, so the two are now the same function and cannot drift again.

    How each shape of ``visibility`` is read:

    ==========================  ==============================================
    ``visibility``              Verdict
    ==========================  ==============================================
    absent                      ALLOW — spec default is ``["model", "app"]``
    ``["model", "app"]``        ALLOW for both
    ``["app"]``                 ALLOW app, DENY model
    ``["model"]``               ALLOW model, DENY app
    ``[]``                      DENY both — an explicit empty audience list
    ``"app"`` (bare string)     read as ``["app"]``
    present, uninterpretable    DENY both, flagged ``unreadable``
    ``_meta``/``ui`` = ``null``  ALLOW — a null container is an unset optional
    ``_meta``/``ui`` not a dict  DENY both, flagged ``unreadable``
    ==========================  ==============================================

    Only ABSENCE gets the permissive default, and absence is distinguished from
    malformation at EVERY level, not just the leaf:

    * ``_meta`` missing, or present-and-a-dict with no ``ui`` key, or ``ui``
      present-and-a-dict with no ``visibility`` key → genuine absence → allow.
    * ``_meta`` or ``ui`` explicitly ``null`` → also absence. A JSON ``null`` is
      how serializers spell an unset optional object, and it cannot conceal a
      declaration, so the reason non-dict containers deny does not apply.
    * ``_meta`` or ``ui`` present as some OTHER non-dict → the container that
      would hold the declaration is unreadable, so a visibility may well be in
      there and we cannot see it → deny, flagged ``unreadable``.
    * ``visibility`` present but unparseable → deny, flagged ``unreadable``.

    Absence is tested by key presence rather than by value, so an explicit
    ``"visibility": null`` is a declaration this host cannot read rather than an
    omission — ``.get()`` cannot tell those apart.

    KNOWN DIVERGENCE, pre-existing and deliberately unchanged here: the C#
    reference SDK documents ``null`` and ``[]`` on ``visibility`` ITSELF as
    "visible to both the model and the app by default". This host denies both
    for those two shapes, as it did before this function existed. The SEP text
    grants the default to an OMITTED field, and an explicitly empty audience
    list reads as a deliberate exclusion, so the conservative reading is kept
    rather than widened as a side effect of an unrelated fix. Worth settling
    separately — it is a conformance question, not an oversight.

    Bare strings are coerced rather than lumped in with the unreadable values,
    because the realistic typo for this field is a scalar instead of a
    one-element list; blanket-denying every malformed value would hide a tool
    whose author wrote ``"model"`` meaning to expose it.

    Anything present-but-unreadable denies both audiences: it is an attempt to
    restrict the tool that this host cannot honor, and the errors are not
    symmetric. Over-denying is loud (the name is logged, the tool is simply
    absent) while under-denying silently lets a tool run without readable
    authorization metadata.
    """
    if not isinstance(tool, dict):
        return VisibilityVerdict(False, False)
    if "_meta" not in tool:
        # SEP-1865: ``visibility`` defaults to ``["model", "app"]`` when omitted.
        return VisibilityVerdict(True, False)
    meta = tool["_meta"]
    # An explicit ``null`` CONTAINER is absence, not malformation. JSON
    # serializers routinely emit ``"_meta": null`` for an unset optional
    # object, and the reason non-dict containers deny — a declaration could be
    # hiding in there — does not apply to ``null``, which cannot hold one.
    # Denying it would drop every tool from such a server.
    if meta is None:
        return VisibilityVerdict(True, False)
    if not isinstance(meta, dict):
        return VisibilityVerdict(False, True)
    if "ui" not in meta:
        return VisibilityVerdict(True, False)
    ui = meta["ui"]
    if ui is None:
        return VisibilityVerdict(True, False)
    if not isinstance(ui, dict):
        return VisibilityVerdict(False, True)
    if "visibility" not in ui:
        return VisibilityVerdict(True, False)
    raw = ui["visibility"]
    vis = [raw] if isinstance(raw, str) else raw
    if not isinstance(vis, list):
        return VisibilityVerdict(False, True)
    return VisibilityVerdict(audience in vis, False)


def strip_model_hidden_tools(result: dict) -> WithheldTools:
    """Remove tools the agent may not see from a ``tools/list`` result IN PLACE.

    SEP-1865: the host MUST NOT include a tool in the agent's tool list when its
    ``_meta.ui.visibility`` does not include ``"model"``. Returns the withheld
    names split by cause, so the caller can log an unreadable declaration more
    loudly than a well-formed one.

    Every shape of ``visibility`` is read by :func:`visibility_allows`, which
    the app-call direction shares — see that docstring for the full table.
    """
    tools = result.get("tools")
    if not isinstance(tools, list):
        return WithheldTools([], [])
    kept: list[Any] = []
    declared: list[str] = []
    unreadable: list[str] = []
    for tool in tools:
        if not isinstance(tool, dict):
            kept.append(tool)
            continue
        verdict = visibility_allows(tool, AUDIENCE_MODEL)
        if verdict.allowed:
            kept.append(tool)
            continue
        name = tool.get("name")
        label = name if isinstance(name, str) else "<unnamed>"
        (unreadable if verdict.unreadable else declared).append(label)
    withheld = WithheldTools(declared, unreadable)
    if withheld:
        result["tools"] = kept
    return withheld


def extract_declared_ui_uris(result: dict) -> dict[str, str]:
    """Return ``{tool_name: ui_uri}`` for every tool in a ``tools/list``
    result that DECLARES a ``ui://`` resource on its tool definition.

    SEP-1865's primary association form puts ``_meta.ui.resourceUri`` on the
    **tool declaration** (both the real pdf-server and Excalidraw do this);
    the per-result ``_meta`` is optional and some servers omit it there. The
    gateway harvests these declarations as tools/list responses flow through,
    so :func:`extract_ui_resource_uri`'s result-side check can fall back to
    the declared uri for the called tool. Same scheme gate as the result-side
    extractor: only ``ui://`` strings are accepted.
    """
    out: dict[str, str] = {}
    if not isinstance(result, dict):
        return out
    tools = result.get("tools")
    if not isinstance(tools, list):
        return out
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        meta = tool.get("_meta")
        if not isinstance(meta, dict):
            continue
        uri: Any = None
        ui = meta.get("ui")
        if isinstance(ui, dict):
            uri = ui.get("resourceUri")
        if uri is None:
            uri = meta.get(_DEPRECATED_FLAT_URI_KEY)
        if isinstance(uri, str) and uri.startswith(_UI_SCHEME):
            out[name] = uri
    return out


def append_marker(result: dict, spool_id: str) -> dict:
    """Return a copy of a ``tools/call`` result with the spool marker appended
    to its FIRST text content item (or a new text item when none exists).

    Copy discipline mirrors ``backend._strip_caller_meta``: the input ``result``
    and every nested container on the mutated path are copied, never mutated in
    place, so the routing layer's original response object is left pristine.
    """
    marker = f"{MARKER_PREFIX}{spool_id}{MARKER_SUFFIX}"
    out = dict(result)
    content_raw = out.get("content")
    content = list(content_raw) if isinstance(content_raw, list) else []
    text_idx = next(
        (
            i
            for i, item in enumerate(content)
            if isinstance(item, dict)
            and item.get("type") == "text"
            and isinstance(item.get("text"), str)
        ),
        None,
    )
    if text_idx is None:
        content.append({"type": "text", "text": marker})
    else:
        item = dict(content[text_idx])
        item["text"] = f"{item['text']} {marker}"
        content[text_idx] = item
    out["content"] = content
    return out


def sweep_spool(max_age_hours: float = 24.0) -> int:
    """Delete spool records older than ``max_age_hours`` (by mtime). Returns
    the count of records removed. Missing directory is a no-op. Best-effort: a
    file that vanishes or cannot be stat'd/unlinked mid-sweep is skipped,
    never raised.

    Expiry is a DELIBERATE capability-lifetime decision, not just disk
    hygiene: the spool id doubles as the app's callback capability token
    (``app_call.py``), so sweeping a record also expires the embedded app's
    ability to call tools. 24h comfortably outlives any dashboard session
    while bounding both disk growth (a real PDF-viewer record is 4.4MB) and
    the token's validity window.

    Each record's ``<id>.rendered`` single-consume sidecar (written by the
    dashboard's render claim) is reaped with it; orphaned sidecars past the
    cutoff are also removed.
    """
    directory = spool_dir()
    if not directory.is_dir():
        return 0
    cutoff = time.time() - max_age_hours * 3600.0
    removed = 0
    for path in directory.glob("*.json"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
                sidecar = path.with_suffix(".rendered")
                if sidecar.is_file():
                    sidecar.unlink()
        except OSError:
            continue
    # Orphaned sidecars (record already gone) past the cutoff.
    for sidecar in directory.glob("*.rendered"):
        try:
            if not sidecar.with_suffix(".json").exists() and sidecar.stat().st_mtime < cutoff:
                sidecar.unlink()
        except OSError:
            continue
    return removed
