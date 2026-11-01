"""Registering and managing sources: list, add, rename, delete, pause, confirm, resume.

Also the native folder picker, a folder source's file list with its retry and skip
routes, and ``GET /api/knowledge/config``.

Served through :mod:`kiro_crew.dashboard.handlers.knowledge`, which callers and tests import
and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from functools import partial
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, NamedTuple, cast

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.handlers.files import _PathProbeBusy, _probe_busy_response, _run_path_probe
from kiro_crew.dashboard.handlers.knowledge_access import (
    _audited_write,
    _require_knowledge_owner,
    _sel_log,
    _store,
    _task_registry,
    _track_scan_task,
)
from kiro_crew.dashboard.handlers.knowledge_sync import (
    DocIngestHostsUnavailable,
    _add_source_unique,
    _doc_ingest_hosts,
    _hosts_unavailable_response,
    _ingest_local_file_task,
    _is_syncable,
    _source_row,
    _url_refusal_response,
)
from kiro_crew.dashboard.origin import is_direct_local_request
from kiro_crew.knowledge.folder_watcher import (
    estimate_scan_cost,
    folder_chunk_budget,
    max_files_prop,
    walk_filters,
)
from kiro_crew.knowledge.ingestion import _redact
from kiro_crew.knowledge.readers import FileReader
from kiro_crew.knowledge.spend import source_spend
from kiro_crew.knowledge.store import AUTO_REGISTRATION_RETIRED_PROP
from kiro_crew.knowledge.url_fetch import url_fetch_refusal
from kiro_crew.security import is_sensitive_path

if TYPE_CHECKING:
    # Type-only: importing the module at runtime would load boto3 at gateway
    # boot, which the lazy connector proxy in knowledge_lifecycle exists to avoid.
    from kiro_crew.knowledge.connectors.bedrock_kb import BedrockKBConnector

logger = logging.getLogger("kiro_crew.dashboard.handlers.knowledge")


# Max length for a user-editable source display name (rename endpoint).
_MAX_SOURCE_NAME_LEN = 200


def _create_source_audited(store, source_type: str, **kwargs) -> tuple[str, bool]:
    """:func:`_add_source_unique`, auditing only a row this call created."""
    sid, created = _add_source_unique(store, source_type=source_type, **kwargs)
    if created:
        _sel_log("source.add", source_id=sid, source_type=source_type)
    return sid, created


def _reclaim_rowless_attestations(store, consent_mod, recorded) -> int:
    """Revoke each Bedrock attestation whose ``sources`` row is gone; the count.

    Called by the add route when the attested population is full. The delete
    route answers 404 for a missing row, so an attestation whose row was
    removed outside the dashboard routes has no other way out and would hold
    one of the registration slots for good. A row minted under that id later
    is unattested, as after a delete through the route. Sync (sqlite and the
    consent store's file lock); the caller holds ``target_change_lock`` on a
    worker thread.
    """
    reclaimed = 0
    for sid in recorded:
        if _source_row(store, sid) is not None:
            continue
        if consent_mod.revoke_source_attestation(sid):
            _sel_log("source.attestation_reclaim", source_id=sid, reason="row_missing")
            reclaimed += 1
    return reclaimed


def _source_exists_response(source_type: str, source_id: str) -> web.Response:
    """The 409 for a uri that already has a row: same code and id for every type.

    A ``bedrock_kb`` row can exist and still be dark: a grant confirmed for
    another account leaves its attestation outside ``attested_sources``, and
    re-adding the same knowledge base lands here, where only a row the add
    CREATES is attested. So that type's text names the recovery that does
    work -- the owner-only delete revokes the attestation with the row, and a
    fresh add validates, inserts and attests -- rather than a re-add that
    can never re-attest. Every other type keeps the plain text.
    """
    error = "source already exists"
    if source_type == "bedrock_kb":
        error = (
            "A source for these knowledge bases is already registered. If it "
            "stopped returning results after the AWS account changed, remove "
            "it and add it again."
        )
    return web.json_response({"error": error, "id": source_id, "code": "source_exists"}, status=409)


def _set_file_state(store, source_id: str, file_path: str, status: str) -> None:
    """Reset one scanned file to *status* and clear its error, in one off-loop take.

    One take per logical write, for the reason ``_set_sync_status`` documents.
    """
    store.db.execute(
        "UPDATE folder_file_state SET status = ?, error_message = NULL "
        "WHERE source_id = ? AND file_path = ?",
        (status, source_id, file_path),
    )
    store.db.commit()


def _source_rows(store, uri_filter: str | None) -> list:
    """The sources listing with per-source item counts, in one off-loop take.

    Sync on purpose: the dashboard polls the sources list while a source is
    syncing -- exactly the window in which the knowledge DB is contended --
    and the LEFT JOIN aggregates over ``items``, which grows without bound.
    The caller dispatches this to a worker thread; ``store.db`` is
    thread-local, so the thread gets its own connection.
    """
    if uri_filter:
        resolved_filter = (
            str(Path(uri_filter).resolve()) if uri_filter.startswith("/") else uri_filter
        )
        return store.db.execute(
            "SELECT s.*, COALESCE(c.cnt, 0) AS item_count "
            "FROM sources s LEFT JOIN (SELECT source_id, COUNT(*) AS cnt FROM items GROUP BY source_id) c "
            "ON s.id = c.source_id WHERE s.uri = ? ORDER BY s.updated_at DESC",
            (resolved_filter,),
        ).fetchall()
    return store.db.execute(
        "SELECT s.*, COALESCE(c.cnt, 0) AS item_count "
        "FROM sources s LEFT JOIN (SELECT source_id, COUNT(*) AS cnt FROM items GROUP BY source_id) c "
        "ON s.id = c.source_id ORDER BY s.updated_at DESC"
    ).fetchall()


async def list_sources(request: web.Request) -> web.Response:
    """GET /api/knowledge/sources.

    Each source carries a ``spend`` block: how far its indexing has got and how
    many Kiro requests it still owes -- one model call is one billed request, so
    the figure is directly comparable to a bill. Indexing draws those requests
    sweep after sweep at idle, so without them here the only place the ongoing
    cost surfaces is a credit balance after the fact.
    """
    owner_denied = await _require_knowledge_owner(request, "knowledge.sources.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    rows = await asyncio.to_thread(_source_rows, store, request.query.get("uri"))
    sources = [dict(r) for r in rows]
    # Aggregate scans plus a size stat per outstanding file, and the dashboard polls
    # this list while a source is syncing -- offloaded so a large folder cannot stall
    # chat and heartbeat processing on the event loop.
    spend = await asyncio.to_thread(source_spend, store, sources)
    for source in sources:
        source["spend"] = spend.get(source["id"], {})
        # Whether POST /sources/{id}/sync can do anything for this row. The
        # Sources list offers Sync from this flag, so the button and the 409
        # source_not_syncable refusal in _sync_source_body share one rule.
        source["syncable"] = _is_syncable(source)
    return web.json_response(sources)


# Max wall-clock the native folder dialog may stay open before we give up.
_FOLDER_DIALOG_TIMEOUT = 180  # seconds


def _folder_picker_available(request: web.Request) -> bool:
    """The native folder picker is offered only on macOS (via osascript) and
    only when the dashboard is local -- a dialog on a remote gateway would open
    on the wrong screen.

    It also requires osascript to resolve out of the fixed system directories.
    A host where it does not hides the button rather than offering one whose
    only possible answer is a refusal."""
    if sys.platform != "darwin" or not bool(request.app.get("local_only", False)):
        return False
    if platform_compat.trusted_system_bin("osascript") is None:
        return False
    # `local_only` is necessary but not sufficient: it describes how the GATEWAY
    # was started, not where THIS request came from. The gateway binds loopback
    # and remote access is delivered by a same-host tunnel or reverse proxy, so a
    # remote user's request arrives from a loopback peer with `local_only` still
    # True. A host-side native dialog must never open for such a request -- it
    # would appear on the gateway operator's screen, not the requester's, and
    # block there for up to `_FOLDER_DIALOG_TIMEOUT`. Require a DIRECT local
    # request too, so a proxied one fails the gate and falls back to the
    # typed-path route. The sibling project picker in `file_api/desktop.py`
    # gates on the same helper for the same reason.
    return is_direct_local_request(request)


def _run_folder_dialog() -> str | None:
    """Open the macOS native folder chooser (blocking) and return the selected
    absolute path, or None if the user cancelled or it failed to launch. Meant
    to run off the event loop via an executor.

    The binary is resolved from the fixed system directories rather than PATH: a
    gateway's PATH can lead with an agent-writable directory, so a bare argv name
    lets a planted shim run with the gateway's environment and outside the
    sandbox. An unresolvable osascript reads as "failed to launch", which is what
    it is -- the spawn does not happen at all."""
    osascript = platform_compat.trusted_system_bin("osascript")
    if osascript is None:
        return None
    cmd = [
        osascript,
        "-e",
        "POSIX path of (choose folder with prompt "
        '"Select a folder to add to your knowledge base")',
    ]
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_FOLDER_DIALOG_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    # osascript exits non-zero (and prints nothing) when the user cancels.
    path = proc.stdout.strip()
    return path if proc.returncode == 0 and path else None


async def pick_folder(request: web.Request) -> web.Response:
    """POST /api/knowledge/pick-folder -- open the macOS native folder chooser on
    the gateway host and return the selected absolute path.

    Offered only on a local macOS dashboard (see _folder_picker_available). The
    returned path is not trusted -- it is fed back into the folder path field and
    re-validated by add_source like any typed path."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.pick_folder")
    if owner_denied is not None:
        return owner_denied
    if not _folder_picker_available(request):
        return web.json_response(
            {"error": "Folder picker is not available on this system"},
            status=403,
        )
    loop = asyncio.get_running_loop()
    path = await loop.run_in_executor(None, _run_folder_dialog)
    if path:
        _sel_log("source.pick_folder", outcome="completed")
    return web.json_response({"path": path})


#: URI prefixes add_source treats as remote: no filesystem probe runs for them.
_REMOTE_URI_PREFIXES = ("https://", "http://", "upload://", "code://", "bedrock-kb://")


class _LocalUriProbe(NamedTuple):
    """What one off-loop look at a local source path found.

    ``resolved`` is the canonical path. ``is_file`` / ``is_dir`` are only
    probed when the path is not sensitive, so a restricted path is never
    stat'ed.
    """

    resolved: str
    sensitive: bool
    is_file: bool
    is_dir: bool


def _probe_local_uri(uri: str) -> _LocalUriProbe:
    """Resolve *uri* once, then check and stat that same canonical path.

    Blocking, and the path is the caller's choice, so it runs on the
    request-path probe pool via ``_run_path_probe``. One resolve feeds the
    sensitive-path check and the type check alike, so no symlink swap between
    two resolves can split what was checked from what is stored.
    """
    resolved = Path(uri).resolve()
    canonical = str(resolved)
    if is_sensitive_path(canonical):
        return _LocalUriProbe(canonical, True, False, False)
    return _LocalUriProbe(canonical, False, resolved.is_file(), resolved.is_dir())


def _add_source_path_checks(  # type: ignore[no-untyped-def]
    connector, config: dict, uri: str
) -> tuple[str | None, _LocalUriProbe | None]:
    """add_source's filesystem work, in the order the handler answers it.

    Returns ``(connector_error, probe)``. A connector's ``validate_config`` can
    stat the path (``LocalFolderConnector`` calls ``exists`` and ``is_dir``),
    so it runs here, in the same hop as the resolve: one request pays one
    trip to the probe pool. ``probe`` is ``None`` for an empty or remote uri.
    """
    if connector:
        valid, err = connector.validate_config(config)
        if not valid:
            return err, None
    if not uri or uri.startswith(_REMOTE_URI_PREFIXES):
        return None, None
    return None, _probe_local_uri(uri)


def _file_path_restricted(file_path: str) -> bool:
    """retry_file / skip_file's sensitive-path check: the raw and resolved forms.

    Blocking (``Path.resolve``) on a request-supplied path, so the handlers run
    it through ``_run_path_probe``.
    """
    return is_sensitive_path(file_path) or is_sensitive_path(str(Path(file_path).resolve()))


# Every top-level field ``add_source`` reads as text. Absent or ``null`` is
# fine (each has a default); any other non-string is refused.
_ADD_SOURCE_TEXT_FIELDS = ("name", "source_type", "uri", "namespace")

# What a ``bedrock_kb`` row retains in ``properties`` is the connector's
# business: ``bedrock_kb.retained_properties`` returns the validated triple
# (``kb_ids``, ``region``, ``profile``) the insert below stores verbatim.


def _add_source_shape_error(body: dict) -> str | None:
    """First shape violation in a POST /api/knowledge/sources body, else None.

    The ONE gate for the body's types, run before any field is read: a
    wrong-typed field stops here as a 400, so no line downstream
    dereferences one. Per-field checks at each first use leave one gap per
    field: a connector's ``validate_config`` may ignore ``url``, so a JSON
    object in ``uri`` would reach ``str.startswith`` and surface as a 500.
    """
    for key in _ADD_SOURCE_TEXT_FIELDS:
        value = body.get(key)
        if value is not None and not isinstance(value, str):
            return f"{key} must be a string"
    properties = body.get("properties")
    if properties is not None and not isinstance(properties, dict):
        return "properties must be an object"
    if isinstance(properties, dict):
        nested = properties.get("namespace")
        if nested is not None and not isinstance(nested, str):
            return "namespace must be a string"
    return None


def _bedrock_registration_identity(properties: dict) -> tuple[dict, str, int]:
    """``(retained, uri, name_cap)`` for a ``bedrock_kb`` add; ValueError if invalid.

    Sync, and the only place add_source imports the connector: the first
    import loads boto3, so the caller runs this on a worker thread.
    """
    from kiro_crew.knowledge.connectors.bedrock_kb import (
        _MAX_RESULT_SOURCE_NAME_CHARS,
        canonical_source_uri,
        retained_properties,
    )

    retained = retained_properties(properties)
    return retained, canonical_source_uri(retained), _MAX_RESULT_SOURCE_NAME_CHARS


async def _insert_bedrock_source(  # type: ignore[no-untyped-def]
    store, source_type: str, stored_name: str, uri: str, retained: dict, validated_account: str
) -> tuple[str, bool] | web.Response:
    """add_source's ``bedrock_kb`` insert: ``(sid, created)`` or the refusal.

    Recheck-and-insert under the connector's target-change lock -- the same
    lock the consent POST holds across its check + grant write -- so a grant
    confirmed between the handler's earlier check and this insert cannot
    interleave. The slow validation stayed outside the lock; this section is
    sqlite-only. add_source has already loaded ``bedrock_kb`` on a worker
    thread (``_bedrock_registration_identity``), so this import is a
    ``sys.modules`` lookup, not a boto3 load on the loop.
    """
    from kiro_crew.knowledge.connectors.bedrock_kb import (
        MAX_BEDROCK_KB_SOURCES,
        TargetLookupError,
        registered_target_mismatch,
        target_change_lock,
    )

    def _locked_insert():
        from kiro_crew import aws_consent as consent_mod

        # ``retained`` and ``uri`` were derived from the validated config;
        # the checks under the lock compare these same values, so the row can
        # only ever hold what the grant equality matched.
        new_profile = retained["profile"]
        new_region = retained["region"]
        with target_change_lock:
            # Recheck the UNIQUE uri inside the lock before the count gate.
            # The earlier fast-path check can race another add; an existing
            # registration remains a duplicate even when the population is
            # full, and no consent-store read or write is needed to say so.
            try:
                existing = store.get_source_by_uri(uri)
            except Exception as e:
                raise TargetLookupError(f"bedrock_kb source duplicate lookup failed: {e}") from e
            if existing is not None:
                return (existing["id"], False), None, None

            # Count every sealed registration, including one recorded under
            # an older account. Those dark rows still occupy a registration
            # and must be removed before their slot can be reused. A full
            # population first reclaims attestations whose row is gone,
            # which no route can remove. Read strictly, before either the
            # database row or attestation is written, so an unreadable
            # consent store fails closed.
            try:
                registered = consent_mod.recorded_source_attestations()
                held_slots = len(registered)
                if held_slots >= MAX_BEDROCK_KB_SOURCES:
                    held_slots -= _reclaim_rowless_attestations(store, consent_mod, registered)
            except Exception as e:
                raise TargetLookupError(f"bedrock_kb source attestation lookup failed: {e}") from e
            if held_slots >= MAX_BEDROCK_KB_SOURCES:
                return None, "source_limit", None

            held = registered_target_mismatch(store, new_profile, new_region)
            if held is not None:
                return None, "conflict", held
            # The grant this source depends on must match ITS target AT
            # INSERT TIME, under the same lock the consent write holds:
            # validation ran seconds ago against the then-current grant,
            # and a target-B confirmation landing in between would leave
            # this source registered but grantless -- dark while its row
            # says Live. Same-target re-confirmation (fresh grant_id, same
            # target) passes: retrieval demands target equality, not grant
            # identity.
            granted, _reason = consent_mod.is_granted(
                consent_mod.SERVICE_BEDROCK_KB, profile=new_profile, region=new_region
            )
            if not granted:
                return None, "grant_changed", None
            # Same target is not the same account: a profile can be
            # re-pointed and re-confirmed for the same (profile, region)
            # while validation was probing the KB in the account before.
            # The attestation pins the account the grant confirms now, so
            # it must be the one validation ran under, else the row would
            # authorize paid retrieval of a KB never validated there. A
            # same-target, same-account re-confirmation (a fresh grant_id)
            # still passes.
            live = consent_mod.read_grant(consent_mod.SERVICE_BEDROCK_KB)
            if live is None or live.account != validated_account:
                return None, "grant_changed", None
            sid, created = _create_source_audited(
                store, source_type, name=stored_name, uri=uri, properties=retained
            )
            if created:
                # The row is proof of nothing on its own: knowledge.db is
                # agent-writable in-sandbox. The sealed consent store records
                # that THIS gateway insert, owner-gated and consent-checked
                # above, registered exactly this row, and the paid enumeration
                # searches only rows so attested. The two writes are not one
                # transaction, so a failed attestation undoes the insert: a
                # row nothing attests is dark for good, and an add that
                # reports success must have produced a row the enumeration
                # will search.
                try:
                    consent_mod.record_source_attestation(
                        sid, uri, retained, expected_account=validated_account
                    )
                except Exception:
                    store.delete_source_cascade(sid)
                    _sel_log(
                        "source.delete",
                        source_id=sid,
                        source_type=source_type,
                        reason="attestation_failed",
                    )
                    raise
            return (sid, created), None, None

    try:
        inserted, refusal, held = await asyncio.to_thread(_locked_insert)
    except TargetLookupError as e:
        # Fail CLOSED: an unreadable sources table must refuse the add, not
        # wave it through as "no conflict". The exception text goes to the
        # log, never to the form: it can carry a filesystem path.
        logger.warning("bedrock_kb one-account check failed: %s", e)
        return web.json_response(
            {
                "error": (
                    "could not check the existing Bedrock Knowledge Base "
                    "source, so nothing was added; try again"
                ),
                "code": "bedrock_kb_conflict_check_failed",
            },
            status=503,
        )
    except Exception:
        # The insert was undone above (or never happened). Same log/form
        # split as the conflict check: the text can carry a path.
        logger.exception("bedrock_kb source registration failed")
        return web.json_response(
            {
                "error": (
                    "the source could not be registered with the account "
                    "confirmation, so nothing was added; try again"
                ),
                "code": "bedrock_kb_attestation_failed",
            },
            status=500,
        )
    if refusal == "source_limit":
        return web.json_response(
            {
                "error": (
                    "Bedrock Knowledge Base sources are limited to "
                    f"{MAX_BEDROCK_KB_SOURCES}; remove a source before adding another."
                ),
                "code": "bedrock_kb_source_limit",
            },
            status=409,
        )
    if refusal == "conflict":
        # 409 like the consent POST's refusal under the same code: the
        # request is well formed and the conflict is with registered state,
        # which the owner can change by removing that source.
        return web.json_response(
            {
                "error": (
                    f"{held} already uses a different AWS profile or "
                    "region. Only one is supported for Bedrock Knowledge "
                    "Base sources right now; remove that source first."
                ),
                "code": "bedrock_kb_target_conflict",
            },
            status=409,
        )
    if refusal == "grant_changed":
        return web.json_response(
            {
                "error": (
                    "the AWS account confirmation changed while this source was "
                    "being validated — confirm the account for this exact "
                    "profile and region, then add again"
                ),
                "code": "bedrock_kb_grant_changed",
            },
            status=409,
        )
    return inserted


async def add_source(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources -- add a remote source."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.add")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    shape_error = _add_source_shape_error(body)
    if shape_error is not None:
        return web.json_response({"error": shape_error}, status=400)
    name = body.get("name") or ""
    source_type = body.get("source_type") or ""
    uri = body.get("uri") or ""
    properties = body.get("properties") or {}
    namespace = body.get("namespace") or properties.get("namespace") or ""
    if namespace:
        namespace = namespace.strip()[:64]

    if not source_type:
        return web.json_response({"error": "source_type required"}, status=400)

    if source_type == "bedrock_kb":
        # Owner action, like the consent grant it rides: validation below
        # probes the KB with the owner's AWS credentials and the insert
        # registers a KB the gateway then queries on the owner's account.
        # ``_require_knowledge_owner`` above gates dashboard users only; an
        # app token and an internal-secret caller pass it, which is fine for
        # a local folder but would let them expose an unapproved KB against
        # the owner's grant. This gate refuses every caller but the owner.
        owner_denied = await require_owner_dashboard_request(
            request, "knowledge.add_source.bedrock_kb"
        )
        if owner_denied is not None:
            return owner_denied
        # The account validation below proves the KB access in. The locked
        # insert requires the grant's account to still be this one, because the
        # attestation pins the account the grant confirms AT INSERT, and a grant
        # confirmed for another account in between would attest a KB never
        # validated there. It comes from validation itself, never from a grant
        # read before it: the grant can move to another account and back while
        # the probes run, and then a read taken first matches the final grant
        # while the KB was proven in the other account. Empty until a
        # validation succeeds, which the insert refuses.
        validated_account = ""

    # Refuse UNC ("\\host\share") and Win32 extended-length ("\\?\") prefixes
    # BEFORE anything filesystem-adjacent runs — connector.validate_config()
    # can do Path.exists() on the value, and resolving a UNC path on Windows
    # fires an outbound SMB/DNS lookup to `host` before any sensitive-path
    # check would reject it. This gate also has to precede the Path.resolve()
    # below, because resolve() leaves "\\?\" un-normalized so is_sensitive_path
    # misses a `\\?\C:\Users\me\.ssh\id_rsa` bypass of the credential floor.
    # Windows accepts either slash flavour AND their mixture as a device-path
    # prefix — Path("\\/?\\C:\\...") normalizes to the same extended path as
    # \\?\ — so match on "first two chars are any slash", not literal "\\" /
    # "//" alone.
    if len(uri) >= 2 and uri[0] in ("\\", "/") and uri[1] in ("\\", "/"):
        _sel_log("source.add_denied", reason="unsupported_prefix", uri=uri)
        return web.json_response(
            {
                "error": "UNC and extended-length paths are not supported",
                "code": "uri_unsupported_prefix",
            },
            status=400,
        )

    # Validate via connector if available. The connector check and the local
    # path's resolve both touch the filesystem on a caller-chosen path, so they
    # run off the loop, on the bounded probe pool, in one hop.
    sync_scheduler = request.app.get("knowledge_sync")
    connector = sync_scheduler.get_connector(source_type) if sync_scheduler else None
    retained: dict[str, str] | None = None
    bedrock_name_cap = 0
    if source_type == "bedrock_kb":
        # Bedrock validation is a live network probe (boto session + one
        # Retrieve per KB, ~seconds worst case), not a filesystem look, so it
        # runs on a worker thread rather than the bounded path-probe pool.
        if connector:
            bedrock = cast("BedrockKBConnector", connector)
            valid, err, validated_account = await asyncio.to_thread(
                bedrock.validate_registration, {**properties, "url": uri}
            )
            if not valid:
                return web.json_response({"error": err}, status=400)
        # The row's identity and config come from what validation judged,
        # never from the caller's spelling: ``uri`` is the UNIQUE dedup handle,
        # so a caller-chosen one could mint a row per request for the same
        # knowledge base. Derived once here; the locked insert below stores
        # exactly these values and runs its checks on them. The connector's
        # own bounds on every retained field apply here too, so a value no
        # grant could name is refused even when no sync scheduler validated.
        # Off the loop: the first import of ``bedrock_kb`` loads boto3, which
        # reads hundreds of files and would stall every gateway task.
        try:
            retained, uri, bedrock_name_cap = await asyncio.to_thread(
                _bedrock_registration_identity, properties
            )
        except ValueError as e:
            return web.json_response(
                {"error": str(e), "code": "bedrock_kb_config_invalid"}, status=400
            )
        probe = None
    else:
        try:
            connector_err, probe = await _run_path_probe(
                _add_source_path_checks, connector, {**properties, "url": uri}, uri
            )
        except _PathProbeBusy:
            return _probe_busy_response(resource=str(uri), operation="knowledge.source.add")
        if connector_err is not None:
            return web.json_response({"error": connector_err}, status=400)

    if not uri:
        return web.json_response({"error": "uri required"}, status=400)

    # Sandbox guard: reject sensitive paths for any local source
    if probe is not None and probe.sensitive:
        _sel_log("source.add_denied", reason="sensitive_path", uri=uri)
        return web.json_response({"error": "Path is restricted for security reasons"}, status=403)

    # Validate URI format for sources without a dedicated connector
    if source_type == "local_file":
        # local_file sources use absolute file paths as URIs. Use is_absolute()
        # rather than a leading-"/" test: a Windows absolute path starts with a
        # drive letter (C:\... or C:/...), never "/", so the string test
        # rejected every valid Windows input and made single-file ingest 100%
        # unusable there.
        #
        # UNC / extended-length prefixes are already refused by the pre-gate
        # above (before any Path.resolve() call), so we do not re-check them
        # here — that check has to precede the probe's own resolve().
        # is_absolute() is flavour-bound to the RUNNING host, so a Windows drive
        # path is NOT "absolute" to a POSIX gateway (the documented Windows
        # browser -> Linux gateway topology). Accept either flavour explicitly so
        # the answer does not depend on which OS the gateway happens to run.
        if not (PurePosixPath(uri).is_absolute() or PureWindowsPath(uri).is_absolute()):
            return web.json_response(
                {"error": "local_file URI must be an absolute path", "code": "uri_not_absolute"},
                status=400,
            )
        # The probe resolved symlinks and ".." once and refused a sensitive
        # result above; that same canonical path is what is typed here and
        # then stored and ingested. A uri with a remote prefix was never
        # probed and names no local file.
        if probe is None or not probe.is_file:
            return web.json_response({"error": "file not found"}, status=404)
        uri = probe.resolved
    elif not (sync_scheduler and sync_scheduler.get_connector(source_type)):
        # A source with no connector is synced by the URL-fetch pool, so it is
        # held to the same rule as the fetch: https, and a listed host.
        try:
            allowed_hosts = await asyncio.to_thread(_doc_ingest_hosts)
        except DocIngestHostsUnavailable:
            return _hosts_unavailable_response()
        refusal = url_fetch_refusal(uri, allowed_hosts)
        if refusal is not None:
            _sel_log("source.add_denied", reason=refusal[0], uri=uri, outcome="denied")
            return _url_refusal_response(*refusal)

    # Check for existing source with same URI
    existing = await asyncio.to_thread(store.get_source_by_uri, uri)
    if existing:
        return _source_exists_response(source_type, existing["id"])

    # The name every insert below stores, resolved once the uri is final; a
    # blank name falls back to the uri. Only a bedrock_kb name is cut, and it
    # is cut rather than refused: the search reads those rows through
    # bedrock_kb._BOUNDED_ROW_SELECT, which returns a name longer than
    # _MAX_RESULT_SOURCE_NAME_CHARS as NULL, and the result would then name the
    # source "Bedrock KB <kb id>" instead. Cutting it here keeps the stored name
    # the one every result names. Every other type stores the name as sent:
    # its readers (the Markdown panel's file basename, the Knowledge tab's
    # typed name or folder path, md-notebook's vault name) can send longer
    # names, and a cut or a refusal would change what they get back.
    stored_name = name or uri
    if source_type == "bedrock_kb":
        stored_name = stored_name[:bedrock_name_cap]

    # Folder sources: discovery walk + pending_confirmation (no auto-scan)
    if source_type in ("local_folder", "obsidian_vault"):
        # The probe above already refused a sensitive path. A folder uri with a
        # remote prefix was never probed, and is not a directory either.
        if probe is None or not probe.is_dir:
            return web.json_response({"error": f"Directory not found: {uri}"}, status=400)
        folder_path = probe.resolved

        # Run discovery walk to count files (no ingestion)
        watcher = request.app.get("knowledge_watcher")
        file_count = 0
        # Scale of the ingestion this source is about to start, so the user sees
        # the cost before it is spent rather than in a credit balance afterwards.
        # Zeroed when no watcher is wired: reporting 0 files is honest there,
        # inventing an estimate is not.
        cost = {"files": 0, "capped": 0, "chunks": 0, "llm_calls": 0}
        budget = folder_chunk_budget(properties)
        if watcher:
            # The same filters the sweep applies, or the count describes a
            # different file set from the one that gets ingested.
            discovered = await asyncio.to_thread(
                watcher._folder_watcher._walk,
                str(folder_path),
                **walk_filters(properties, source_type),
            )
            file_count = len(discovered)
            cost = await asyncio.to_thread(
                estimate_scan_cost, discovered, max_files=max_files_prop(properties)
            )

        # Store with pending_confirmation status
        if isinstance(properties, dict):
            properties["sync_status"] = "pending_confirmation"
            # Fold top-level namespace into properties for folder watchers
            if namespace and "namespace" not in properties:
                properties["namespace"] = namespace
        sid, created = await asyncio.to_thread(
            _create_source_audited,
            store,
            source_type,
            name=stored_name,
            uri=uri,
            properties=properties,
        )
        if not created:
            return _source_exists_response(source_type, sid)
        return web.json_response(
            {
                "id": sid,
                "status": "pending_confirmation",
                "file_count": file_count,
                # Files beyond the source's max_files cap, which are discovered
                # but never ingested.
                "capped_file_count": cost["capped"],
                "estimated_chunks": cost["chunks"],
                # One extraction call per chunk plus one summary call per file.
                "estimated_llm_calls": cost["llm_calls"],
                # 0 means unbounded: everything lands in the first sweep.
                "chunk_budget_per_sweep": budget or 0,
            },
            status=201,
        )

    if source_type == "bedrock_kb":
        assert retained is not None  # set on the bedrock_kb path above
        inserted_or_error = await _insert_bedrock_source(
            store, source_type, stored_name, uri, retained, validated_account
        )
        if isinstance(inserted_or_error, web.Response):
            return inserted_or_error
        sid, created = inserted_or_error
    else:
        sid, created = await asyncio.to_thread(
            _create_source_audited,
            store,
            source_type,
            name=stored_name,
            uri=uri,
            properties=properties,
        )
    if not created:
        return _source_exists_response(source_type, sid)

    # Trigger immediate ingestion for local_file sources. The task claims
    # 'syncing' itself, so nothing is written here that a disconnect could
    # commit before the work is scheduled.
    if source_type == "local_file":
        pipeline = request.app.get("knowledge_pipeline")
        if pipeline:
            task = asyncio.create_task(_ingest_local_file_task(pipeline, store, uri, sid))
            app_tasks = _task_registry(request.app, "_bg_tasks")
            app_tasks.add(task)
            task.add_done_callback(app_tasks.discard)

    return web.json_response({"id": sid, "status": "created"}, status=201)


async def delete_source(request: web.Request) -> web.Response:
    """DELETE /api/knowledge/sources/{id} -- remove a source and its items."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.delete")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    source_id = request.match_info["id"]
    row = await asyncio.to_thread(_source_row, store, source_id)
    if not row:
        return web.json_response({"error": "not found"}, status=404)
    # The registration is the sealed attestation under this id, not the row's
    # ``source_type``: that column lives in the agent-writable knowledge.db,
    # so an agent can re-label a registered row, and a delete that trusted
    # the label would skip the owner gate and the revoke below, leaving the
    # attestation for a row re-minted under the same id and values to
    # inherit. Read strictly (an unreadable store refuses every delete, a
    # re-labelled row being indistinguishable from a local one without it);
    # sync file I/O under the consent lock, so off the loop.
    from kiro_crew import aws_consent as consent_mod

    try:
        registered = await asyncio.to_thread(consent_mod.has_source_attestation, source_id)
    except Exception:
        logger.exception("bedrock_kb attestation read failed at delete: source_id=%s", source_id)
        return web.json_response(
            {
                "error": "Couldn't remove this source. Nothing was deleted. Try again.",
                "code": "bedrock_kb_attestation_revoke_failed",
            },
            status=500,
        )
    if registered or row["source_type"] == "bedrock_kb":
        # Owner action, the mirror of the owner-gated insert: the knowledge
        # owner gate above admits an app token and an internal-secret caller,
        # and removing a registration the owner made (and the approval
        # recorded with it) is the owner's call, like making it was.
        owner_denied = await require_owner_dashboard_request(
            request, "knowledge.delete_source.bedrock_kb"
        )
        if owner_denied is not None:
            return owner_denied
        # The attestation goes FIRST. The two writes are not one transaction,
        # and the safe direction is a row nothing attests (skipped, audited)
        # rather than an attestation with no row, which a later row minted
        # under the same id and values would inherit. A failed revoke leaves
        # both in place and reports; a failed row delete after it leaves a
        # dark row the retry removes. Sync file I/O under the consent lock,
        # so off the loop like the row delete below.
        try:
            await asyncio.to_thread(consent_mod.revoke_source_attestation, source_id)
        except Exception:
            logger.exception("bedrock_kb attestation revoke failed: source_id=%s", source_id)
            return web.json_response(
                {
                    "error": "Couldn't remove this source. Nothing was deleted. Try again.",
                    "code": "bedrock_kb_attestation_revoke_failed",
                },
                status=500,
            )
    try:
        # BEGIN IMMEDIATE takes the write lock eagerly and the connection's
        # busy_timeout is 10s, so a concurrent ingestion writer could park this
        # call for that long -- never on the event loop.
        await _audited_write(
            partial(store.delete_source_cascade, source_id),
            event="source.delete",
            fields={"source_id": source_id},
        )
    except Exception:
        logger.exception("delete_source failed: source_id=%s", source_id)
        return web.json_response({"error": "internal server error"}, status=500)
    return web.json_response({"status": "deleted"})


async def rename_source(request: web.Request) -> web.Response:
    """PATCH /api/knowledge/sources/{id} -- rename a source (name only).

    Only ``name`` is editable; ``uri`` (the source identity) stays immutable.
    """
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.rename")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    source_id = request.match_info["id"]
    if not await asyncio.to_thread(_source_row, store, source_id):
        return web.json_response({"error": "not found"}, status=404)
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    name = body.get("name")
    if not isinstance(name, str):
        return web.json_response({"error": "name must be a string"}, status=400)
    name = name.strip()
    if not name:
        return web.json_response({"error": "name cannot be empty"}, status=400)
    if len(name) > _MAX_SOURCE_NAME_LEN:
        return web.json_response(
            {"error": f"name must be {_MAX_SOURCE_NAME_LEN} characters or fewer"}, status=400
        )
    await _audited_write(
        partial(store.update_source, source_id, name=name),
        event="source.rename",
        fields={"source_id": source_id},
    )
    return web.json_response({"ok": True, "name": name})


def _adopt_source(store, source_id: str, *, event: str):
    """Activate a source and stamp it adopted, in ONE off-loop take.

    Returns ``(outcome, row, props)``: ``"missing"`` when the row is gone,
    ``"denied"`` when its path is restricted, ``"ok"`` when it was activated.

    The ``properties`` mutation goes through ``store.merge_source_properties``,
    which reads and writes the blob under one ``BEGIN IMMEDIATE``. That is what
    closes the lost-update window, not the fact that this helper runs in one
    take: ``properties`` is a whole-column rewrite, so before this the read and
    the write were two statements on an autocommit connection and a concurrent
    pause could be erased by a resume that had read the older blob. On the loop
    that could not happen, because nothing else ran between the two statements;
    off the loop the database has to be the serialization point.

    One take still earns its keep for the rest: it puts the ``resolve()`` and
    sensitive-path check off the loop, which is filesystem I/O the coroutine was
    doing inline, and it keeps the check in front of the write, so a denied
    source is never activated.

    Confirming (or resuming) IS the user adopting this source, so stamp it as
    adopted in the same write that activates it. Without this, a row Kiro Crew
    registered itself would be refused by the scan funnel's gate immediately
    after the user satisfied that very gate, and bounce back to
    pending_confirmation.
    """
    row = _source_row(store, source_id)
    if not row:
        return "missing", None, {}
    # TOCTOU: re-resolve path in case the symlink was swapped since add-time
    if is_sensitive_path(str(Path(row["uri"]).resolve())):
        _sel_log(f"{event}_denied", source_id=source_id, reason="sensitive_path")
        return "denied", row, {}
    props = store.merge_source_properties(
        source_id,
        set_keys={AUTO_REGISTRATION_RETIRED_PROP: True},
        remove_keys=("scan_paused",),
        sync_status="active",
    )
    if props is None:
        # Deleted between the read above and the write. Same answer as a row
        # that was never there: the caller 404s.
        return "missing", None, {}
    _sel_log(event, source_id=source_id)
    return "ok", row, props


def _pause_source_row(store, source_id: str) -> bool:
    """Mark a source paused, in ONE off-loop take. False when the row is gone.

    The blob write goes through ``store.merge_source_properties`` for the reason
    ``_adopt_source`` documents: ``properties`` is a whole-column rewrite, so the
    read and the write happen under one ``BEGIN IMMEDIATE`` and a concurrent
    resume cannot erase this pause.

    The watcher's pre-scan skip reads the sync_status COLUMN, so this write is
    what stops the sweep from walking and delete-reconciling the whole folder;
    the deeper scan_paused gate in folder_watcher stops the ingestion itself.
    """
    if (
        store.merge_source_properties(
            source_id, set_keys={"scan_paused": True}, sync_status="paused"
        )
        is None
    ):
        return False
    _sel_log("source.pause", source_id=source_id)
    return True


async def confirm_source(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/confirm -- confirm and start scanning."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.confirm")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    source_id = request.match_info["id"]
    outcome, row, props = await asyncio.to_thread(
        _adopt_source, store, source_id, event="source.confirm"
    )
    if outcome == "missing":
        return web.json_response({"error": "not found"}, status=404)
    if outcome == "denied":
        return web.json_response({"error": "Path is restricted for security reasons"}, status=403)
    # Trigger scan
    watcher = request.app.get("knowledge_watcher")
    if watcher:
        source = {
            "id": source_id,
            "uri": row["uri"],
            "source_type": row["source_type"],
            "properties": json.dumps(props),
        }
        # Paced like the watcher's own sweeps. This is the burst that costs the
        # most -- nothing is ingested yet, so every discovered file is new -- so
        # skipping the budget here would spend the whole folder before the first
        # sweep ever ran.
        task = asyncio.create_task(
            watcher._folder_watcher.scan_source(source, chunk_budget=folder_chunk_budget(props))
        )
        _track_scan_task(request.app, task)
    return web.json_response({"status": "scanning"})


async def pause_source(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/pause -- pause active scan."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.pause")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    source_id = request.match_info["id"]
    if not await asyncio.to_thread(_pause_source_row, store, source_id):
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"status": "paused"})


async def resume_source(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/resume -- resume paused scan."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.resume")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    source_id = request.match_info["id"]
    outcome, row, props = await asyncio.to_thread(
        _adopt_source, store, source_id, event="source.resume"
    )
    if outcome == "missing":
        return web.json_response({"error": "not found"}, status=404)
    if outcome == "denied":
        return web.json_response({"error": "Path is restricted for security reasons"}, status=403)
    # Trigger scan to pick up remaining files
    watcher = request.app.get("knowledge_watcher")
    if watcher:
        source = {
            "id": source_id,
            "uri": row["uri"],
            "source_type": row["source_type"],
            "properties": json.dumps(props),
        }
        task = asyncio.create_task(
            watcher._folder_watcher.scan_source(source, chunk_budget=folder_chunk_budget(props))
        )
        _track_scan_task(request.app, task)
    return web.json_response({"status": "scanning"})


def _folder_file_rows(store, source_id: str) -> list:
    """A source's per-file scan state, in one off-loop take.

    Sync on purpose: the dashboard polls this every few seconds during a scan
    -- exactly the window in which the knowledge DB is contended. The caller
    dispatches this to a worker thread; ``store.db`` is thread-local, so the
    thread gets its own connection.
    """
    return store.db.execute(
        "SELECT file_path, status, error_message, mtime, content_hash, item_ids, last_seen "
        "FROM folder_file_state WHERE source_id = ? ORDER BY last_seen DESC",
        (source_id,),
    ).fetchall()


async def list_source_files(request: web.Request) -> web.Response:
    """GET /api/knowledge/sources/{id}/files -- list files with scan status."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.files.read")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    source_id = request.match_info["id"]
    rows = await asyncio.to_thread(_folder_file_rows, store, source_id)
    files = [
        {
            "file_path": r["file_path"],
            "status": r["status"] or "pending",
            "error_message": _redact(r["error_message"]) if r["error_message"] else None,
            "mtime": r["mtime"],
            "item_count": len(json.loads(r["item_ids"] or "[]")),
        }
        for r in rows
    ]
    # Also count totals
    total = len(files)
    done = sum(1 for f in files if f["status"] == "done")
    failed = sum(1 for f in files if f["status"] == "failed")
    skipped = sum(1 for f in files if f["status"] == "skipped")
    return web.json_response(
        {"files": files, "total": total, "done": done, "failed": failed, "skipped": skipped}
    )


async def retry_file(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/files/retry -- reset file to pending."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.file_retry")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    source_id = request.match_info["id"]
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    file_path = body.get("file_path", "")
    if not file_path:
        return web.json_response({"error": "file_path required"}, status=400)
    try:
        restricted = await _run_path_probe(_file_path_restricted, file_path)
    except _PathProbeBusy:
        return _probe_busy_response(
            resource=str(file_path), operation="knowledge.source.file_retry"
        )
    if restricted:
        _sel_log("source.file.retry_denied", source_id=source_id, reason="sensitive_path")
        return web.json_response({"error": "path is restricted"}, status=403)
    await _audited_write(
        partial(_set_file_state, store, source_id, file_path, "pending"),
        event="source.file.retry",
        fields={"source_id": source_id},
    )
    return web.json_response({"status": "pending"})


async def skip_file(request: web.Request) -> web.Response:
    """POST /api/knowledge/sources/{id}/files/skip -- mark file as skipped."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.source.file_skip")
    if owner_denied is not None:
        return owner_denied
    store = _store(request)
    source_id = request.match_info["id"]
    body, body_err = await read_bounded_json(request, max_bytes=None)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    file_path = body.get("file_path", "")
    if not file_path:
        return web.json_response({"error": "file_path required"}, status=400)
    try:
        restricted = await _run_path_probe(_file_path_restricted, file_path)
    except _PathProbeBusy:
        return _probe_busy_response(resource=str(file_path), operation="knowledge.source.file_skip")
    if restricted:
        _sel_log("source.file.skip_denied", source_id=source_id, reason="sensitive_path")
        return web.json_response({"error": "path is restricted"}, status=403)
    await _audited_write(
        partial(_set_file_state, store, source_id, file_path, "skipped"),
        event="source.file.skip",
        fields={"source_id": source_id},
    )
    return web.json_response({"status": "skipped"})


# ---------- Config ----------


async def _url_allowed_hosts_or_none() -> list[str] | None:
    """The host list for ``GET /config``, or ``None`` when it cannot be read."""
    try:
        return await asyncio.to_thread(_doc_ingest_hosts)
    except DocIngestHostsUnavailable:
        return None


async def get_config(request: web.Request) -> web.Response:
    """GET /api/knowledge/config -- returns supported formats and status."""
    owner_denied = await _require_knowledge_owner(request, "knowledge.config.read")
    if owner_denied is not None:
        return owner_denied
    pipeline = request.app.get("knowledge_pipeline")
    # ``FileReader.SUPPORTED`` contains '' (the empty suffix) to mark that
    # extensionless files (e.g. ``README``, ``Makefile``) are ingestable as
    # plain text. An empty string is not a valid HTML ``accept`` token, so we
    # keep ``supported_formats`` as the clean extension list and surface the
    # no-extension capability via an explicit boolean instead of stripping the
    # information away entirely.
    return web.json_response(
        {
            "enabled": pipeline is not None,
            "supported_formats": sorted(FileReader.SUPPORTED - {""}),
            "accepts_no_extension": "" in FileReader.SUPPORTED,
            "folder_picker": _folder_picker_available(request),
            # The hosts a web-page source may name; empty on a default config,
            # where the add form says that nothing can be fetched yet. None when
            # the list cannot be read, which the form shows as an error.
            "url_allowed_hosts": await _url_allowed_hosts_or_none(),
        }
    )
