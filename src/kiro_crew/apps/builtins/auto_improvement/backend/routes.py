"""In-process HTTP surface for the auto-improvement app.

The ported app ran its own aiohttp process on a gateway-assigned port and
authenticated every request with a proxy HMAC header. A Kiro Crew builtin
does not: ``register_routes(app)`` mounts handlers on the gateway's OWN aiohttp
application at startup, so requests are same-origin and already authenticated by
the gateway's middleware. That deletes the app's ``proxy_auth.py`` +
``middleware.py`` + ``bin/`` launcher entirely.

Because routes are registered once at startup while the app is opt-in
(``defaultEnabled: false``), every handler is wrapped in an explicit
``is_app_enabled`` gate — deny-by-default, matching ``issue_radar``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.security import redact

from ..profiles.github_repo.pr_recipe import GitHubPRRecipe
from ..spine.push_policy import normalize_branch
from . import (
    clone_setup,
)
from . import commit as commit_mod
from . import (
    deps,
    ledger_admin,
    pr_checks,
    pr_watchers,
    profile_normalize,
    progress,
    runner,
    sse,
    store,
)

logger = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

#: Caps on the text a single finding-detail response may carry. A diff is
#: attacker-influenced in size (an agent can write a large file), and this
#: response is rendered in the browser — so it is bounded here rather than
#: trusting the producer. Truncation is reported, never silent.
_MAX_DIFF_CHARS = 200_000
_MAX_BODY_CHARS = 40_000

#: Config keys a client may set. An allowlist, not a merge: the original learned
#: this the hard way — ``clone``/``target_url`` must not be settable through the
#: generic config PUT, because they decide which repository the agent is turned
#: loose on. Those move only through the dedicated setup endpoint.
_CONFIG_WRITABLE = frozenset(
    {
        "branch",
        "profile",
        "scopeDiffBase",
        "directCommit",
        "measureReps",
        "calibrationReps",
        "bandCapMs",
        "forceBugSeeds",
        "autoDraftPr",
        # Opt-in: mark a fully-green DRAFT pull request ready-for-review. Safe to
        # expose because it can only ever flip a draft to ready — it never merges,
        # never enables auto-merge, and the gate (pr_watchers.auto_publish_gate) is
        # fail-closed and not configurable. Default OFF.
        "autoPublish",
        # Opt-in: let a polled GET /watchers PROMOTE watchers for filed pull requests whose
        # checks went red later. Default OFF, and deliberately so — a watcher's prompt is
        # built from pull-request comment text an outsider can write, it runs with an
        # auto-approved shell, and the strict sandbox hides credential stores but does NOT
        # isolate the network. Auto-starting that from a READ meant the operator never had a
        # consent moment. With this off, reading the list is read-only. Raised by the GPT
        # review; the auto-start path was found while re-deriving D-105's own claim.
        "watcherAutoStart",
        # Opt-in: acknowledge the watcher's residual NETWORK-EGRESS risk before any watcher
        # agent is allowed to run. Default OFF, fail-closed. The watcher agent is UNATTENDED,
        # its prompt embeds outsider-writable PR-comment text, and it needs `gh` (host auth +
        # network) to read PR state — so it CANNOT be run under a strict credential+network
        # sandbox without deleting the feature (D-84). The provider-runner path confirms the
        # sandbox hides credential DIRECTORIES but does NOT isolate the network (D-105), so an
        # injected instruction could read an exposed credential and send it out. Rather than
        # weaken the sandbox or silently accept that, `_make_runner` REFUSES to build a runner
        # unless this flag is set — the operator states, once and explicitly, that they point
        # watchers only at repositories whose PR comments they would be willing to execute.
        # Same opt-in shape as `watcherAutoStart`. Raised by the GPT review.
        "watcherAcceptEgressRisk",
        # Opt-in: acknowledge that the LOOP's authoring agent runs without this app's own
        # strict credential masking. Default OFF, fail-closed. The subprocess path spawns
        # through `sandboxed_spawn_argv(mode="strict")` + `strip_credential_env`, which hides
        # `~/.aws`/`~/.gnupg`/`gh` stores; the PROVIDER path drives a Kiro Crew session
        # instead, so isolation is whatever the gateway's `sandbox` setting gives — and only
        # 'cc'/'strict' profiles hide credential directories from the agent. On a gateway
        # with default 'auto'/'standard' (which exposes .aws/.ssh for workflow use), a
        # repository instruction reaching the agent's auto-approved Bash could read those
        # stores and exfiltrate. `runner._build_runner` therefore runs OFFLINE unless the
        # sandbox is 'cc'/'strict' or this flag is set. Same one-time-consent shape as
        # `watcherAcceptEgressRisk`. Raised by the GPT review.
        "acceptUnsandboxedAgentRisk",
        # Run budget. Safe to expose: these only ever SHRINK or grow how much work
        # one run does; none of them can retarget the repository or relax a gate.
        # A slow suite makes the defaults expensive, so an operator needs to be
        # able to bound a run without editing config.json by hand.
        "maxCycles",
        "quiesceAfter",
        "maxHours",
        "maxCostUsd",
        "proposerWide",
        "proposerDeep",
        "reproduceReps",
        "canaryAdvisory",
        # Confine edits to specific globs (blast-radius control). Safe to expose:
        # it can only NARROW what the agent may touch, never widen past the
        # off-limits fence, and never retarget the repository.
        "editAllowlist",
    }
)


def _require_enabled(handler: Handler) -> Handler:
    """Deny when the app is disabled. ``is_app_enabled`` reads installed.json
    synchronously, so it runs off the event loop."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.StreamResponse:
        if not await asyncio.to_thread(is_app_enabled, store.APP_NAME):
            return web.json_response(
                {"code": "app_disabled", "error": f"{store.APP_NAME} is disabled"}, status=403
            )
        return await handler(request)

    return _wrapped


#: Supervisor states in which the loop owns the clone and the workspace key.
_ACTIVE_RUN_STATUSES = frozenset(
    {runner.STATUS_RUNNING, runner.STATUS_CALIBRATING, runner.STATUS_STOPPING}
)


#: Sentinel returned by the setup path's in-lock recheck, so the caller can map it to a 409
#: `run_in_progress` rather than the 400 `invalid_repo_url` every other error string means.
_RUN_STARTED_WHILE_WAITING = "__run_started_while_waiting__"


def _run_is_active() -> bool:
    """Whether a run/calibration is in flight. SYNCHRONOUS, for use inside a locked section.

    The async :func:`_refuse_while_running` cannot be awaited from inside a worker thread that
    holds ``clone_lock``, and that is exactly where the check is needed: a pre-lock check and a
    post-lock mutation are not atomic with each other, so a request that passes the check and
    then blocks on the lock can have a run start while it waits. Mutual exclusion decides who
    goes first; it does not re-validate the precondition after waiting. Raised by the GPT
    review.
    """
    status = runner.get_supervisor().status().get("status")
    return status in _ACTIVE_RUN_STATUSES


async def _refuse_while_running(what: str) -> web.Response | None:
    """A 409 while a run is live, or ``None`` when it is safe to proceed.

    ONE implementation for every handler that must not act mid-run. This existed inline in
    two places and was then needed in two more; a fourth hand-rolled copy is how the set of
    guarded statuses drifts. ``what`` completes the sentence "a run is <status> — <what>".
    """
    current = await asyncio.to_thread(lambda: runner.get_supervisor().status())
    status = current.get("status")
    if status not in _ACTIVE_RUN_STATUSES:
        return None
    return web.json_response(
        {"code": "run_in_progress", "error": f"a run is {status} — {what} Stop the run first."},
        status=409,
    )


def _validated_fp(request: web.Request) -> tuple[str, web.Response | None]:
    """The finding fingerprint from the URL, VALIDATED at the boundary, or a 400.

    Every ``{fp}`` handler interpolates the value into a filesystem path
    (``pr_queue/<fp>.diff``, per-repo ledger subtrees, watcher clone dirs), so an
    unvalidated ``fp`` is a path-traversal vector — ``../../etc`` or an absolute path
    would escape the data dir. ``ledger_admin.validate_fingerprint`` is the shared
    allowlist authority (``^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`` — no ``.``, ``/`` or
    ``..``), and it REJECTS rather than sanitizes so a mangled fingerprint cannot
    silently point at another finding's files.

    Applied at the HTTP boundary, not left to each downstream sink: some sinks validate
    (``ledger_admin.forget``/``purge``) and some do not (``commit_finding`` via
    ``pr_queue_dir``, the watcher clone path), and "close to the point of origin" is the
    allowlist-at-the-edge discipline. Fail-closed: a bad value never reaches a path.

    Returns ``(fp, None)`` on success or ``("", response)`` with a 400 to return as-is.
    Raised by the GPT review of this branch.
    """
    raw = (request.match_info.get("fp") or "").strip()
    if not raw:
        return "", web.json_response(
            {"code": "fingerprint_required", "error": "fingerprint is required"}, status=400
        )
    try:
        return ledger_admin.validate_fingerprint(raw), None
    except ValueError:
        # Terse and input-free: the message reaches an HTTP client.
        return "", web.json_response(
            {"code": "invalid_fingerprint", "error": "fingerprint is not a valid identifier"},
            status=400,
        )


def _redact_for_display(text: str) -> str:
    """Redact credentials/exfil URLs from agent-authored text before serving it.

    A candidate diff is written by the model into a scratch file and then read back
    here and rendered in the operator's browser, which makes this an egress boundary
    even though the file itself never leaves the host. CodeQL flagged the write side
    (`py/clear-text-storage-sensitive-data`); this is the read side, which is where a
    redaction pass can actually be applied without corrupting the diff the gate must
    still be able to apply.

    FAIL-CLOSED, unlike the watcher log's fail-open ``_redact``: there the risk is
    silencing the operator's only view of a live watcher, so raw text is the safer
    default. Here the text is already on disk and re-readable, so if the redactor
    cannot run, serving nothing beats serving something unscanned.
    """
    try:
        return redact(text)
    except Exception:  # noqa: BLE001 - fail closed: never serve unscanned agent text
        logger.warning("diff redaction failed; withholding the diff", exc_info=True)
        return "[diff withheld: redaction unavailable]"


def _redact_tree(value: Any) -> Any:
    """Redact every string inside a nested structure, leaving non-strings untouched.

    The gate detail is a dict of flags plus free text (a failing test's assertion output),
    so redacting only the top level would miss the field that carries the text — the same
    mistake the activity feed made before ``runner._redact_activity`` went recursive.
    Booleans and numbers must survive as themselves: the UI renders gate flags as
    tri-state icons, and a stringified ``True`` would break that.
    """
    if isinstance(value, str):
        return _redact_for_display(value)
    if isinstance(value, dict):
        return {k: _redact_tree(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_tree(v) for v in value]
    return value


async def _json_body(request: web.Request) -> dict[str, Any]:
    """Parse a JSON object body, tolerating an empty one."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed body is a client error, not a crash
        return {}
    return body if isinstance(body, dict) else {}


# ── config ───────────────────────────────────────────────────────────────────


async def _handle_get_config(_request: web.Request) -> web.StreamResponse:
    config = await asyncio.to_thread(store.read_json, store.config_path(), {})
    return web.json_response(config or {})


async def _handle_put_config(request: web.Request) -> web.StreamResponse:
    patch = await _json_body(request)
    rejected = sorted(set(patch) - _CONFIG_WRITABLE)

    # Refuse while a run is live: `branch` is WRITABLE and `store.workspace_key()` reads config
    # FRESH, keying on `target_url` + `branch` — so a mid-run edit moves the whole artifact set
    # (ruler, results, PR queue, profiles) to a different key while the loop is still writing to
    # it. Measured: flipping `branch` from `origin/main` to `origin/feature` moved the key from
    # `..._repoa__main` to `..._repoa__feature`. The calibrate path was already pinned to its
    # LAUNCHED config for this reason; this is the write side of the same hazard.
    # Raised by the GPT review of this branch.
    if busy := await _refuse_while_running("changing config would move the run's workspace."):
        return busy

    # Under the clone lock with an in-lock RE-CHECK, exactly like `_handle_setup_clone`. The
    # guard above only proves no run was live when the request arrived; a run that starts while
    # this write is in flight would have its workspace moved underneath it, because
    # `store.workspace_key()` reads config FRESH on every path lookup — so the ruler, results,
    # ledger and PR queue can end up in DIFFERENT workspaces within one run. Fourth handler to
    # need this pattern, so it reuses the same helper rather than hand-rolling a copy.
    # Raised by the GPT review.
    def _apply() -> dict[str, Any] | None:
        with commit_mod.clone_lock():
            if _run_is_active():
                return None
            current = store.read_json(store.config_path(), {}) or {}
            current.update({k: v for k, v in patch.items() if k in _CONFIG_WRITABLE})
            store.write_json_atomic(store.config_path(), current)
            return current

    config = await asyncio.to_thread(_apply)
    if config is None:
        return web.json_response(
            {
                "code": "run_in_progress",
                "error": "a run started while this config change was waiting — "
                "changing config would move the run's workspace. Stop the run first.",
            },
            status=409,
        )
    return web.json_response({"config": config, "rejected": rejected})


# ── repository setup (choose the repo a run works on) ────────────────────────


async def _handle_setup_clone(request: web.Request) -> web.StreamResponse:
    """Validate a GitHub URL, clone it push-disabled, and record it as the target.

    This is the ONLY path that may set ``clone``/``target_url`` in config —
    ``PUT /config`` deliberately cannot, because these decide which repository
    the agent is turned loose on. The clone itself is a blocking git subprocess,
    so it runs off the event loop.
    """
    body = await _json_body(request)
    url = str(body.get("url") or "").strip()
    if not url:
        return web.json_response({"code": "url_required", "error": "url is required"}, status=400)

    # Refuse while a run is live — the stronger form of the `PUT /config` hazard above. This
    # handler sets `clone` AND `target_url`, so it repoints the tree the loop is mid-edit on and
    # moves the workspace key at the same time.
    if busy := await _refuse_while_running("retargeting the repository now would strand it."):
        return busy

    def _clone() -> tuple[dict, str]:
        return clone_setup.setup_safe_clone(url, store.scratch_dir())

    # `result` is a PARAMETER, not a closure read. It used to be a free variable of this
    # handler, which broke the moment clone+persist moved inside `_clone_and_persist` to take
    # the lock: that inner function binds its own local `result`, so the outer cell stayed
    # empty and every successful setup raised `NameError` (a 500, with the clone on disk and
    # config.json never written — the app could not be set up at all). Passing it explicitly
    # makes the dependency visible instead of scope-dependent. Raised by the Opus 5 review.
    def _persist(result: dict) -> dict[str, Any]:
        current = store.read_json(store.config_path(), {}) or {}
        retargeted = str(current.get("target_url") or "") != url
        current["clone"] = result["clone"]
        current["target_url"] = url
        # The real remote lives HERE, not in the clone's git config: the clone has both
        # urls neutralized so agent-run Bash inside it cannot discover a push target.
        # Only the trusted publishers read this key. NOT in `_CONFIG_WRITABLE` — like
        # `clone`/`target_url` it decides where a push can land, so it moves only through
        # this setup path.
        current["origin_url"] = str(result.get("origin_url") or "")
        current["target_display"] = result["display"]
        if retargeted:
            # A branch belongs to the repo it came from. Carrying it across a retarget
            # leaves config naming a branch that does not exist in the NEW clone — the
            # picker then shows a value with no matching option, and a run would try to
            # check out a missing ref. Clearing it makes the UI fall back to "default"
            # and forces an explicit choice. Same for a diff scope, which is a ref pair.
            for stale in ("branch", "scopeDiffBase"):
                current.pop(stale, None)
        store.write_json_atomic(store.config_path(), current)
        return current

    # ONE locked section covering clone → persist, because the hazard is the window BETWEEN
    # them: the busy check above only asks whether a run is already live, so a Start click
    # arriving during the (slow: network + git) clone read the OLD config and launched
    # against the repository being replaced, while the dashboard — reading config after the
    # persist — showed the new one. The run's artifacts then hang off a different
    # `workspace_key` than the UI displays. Run startup takes the same lock across its own
    # config-read → driver build, which is what makes the pair mutually exclusive; a lock
    # held by one side alone serializes nothing. Raised by the GPT review.
    def _clone_and_persist() -> tuple[dict, str, dict[str, Any] | None]:
        with commit_mod.clone_lock():
            # Re-check INSIDE the lock: the pre-lock guard above only proves no run was live
            # when the request arrived, and this section may have waited on the lock for the
            # length of a driver build. A run that started meanwhile would otherwise be
            # retargeted underneath itself, stranding its artifacts in the old workspace.
            if _run_is_active():
                return {}, _RUN_STARTED_WHILE_WAITING, None
            result, err = _clone()
            if err or not result.get("push_disabled"):
                return result, err, None
            return result, "", _persist(result)

    result, err, config = await asyncio.to_thread(_clone_and_persist)
    if err == _RUN_STARTED_WHILE_WAITING:
        # Same shape as the pre-lock guard: a run conflict is 409 `run_in_progress`, not a
        # 400 about the URL — the operator's url was fine, the timing was not.
        return web.json_response(
            {
                "code": "run_in_progress",
                "ok": False,
                "error": "a run started while this setup was waiting — stop the run first.",
            },
            status=409,
        )
    if err:
        return web.json_response(
            {"code": "invalid_repo_url", "ok": False, "error": err}, status=400
        )
    if config is None:
        # Never record a clone we could not confirm is push-safe.
        return web.json_response(
            {
                "code": "push_not_disabled",
                "ok": False,
                "error": "clone push could not be disabled — refusing",
            },
            status=400,
        )
    return web.json_response({"ok": True, "clone": result, "config": config})


async def _handle_branches(request: web.Request) -> web.StreamResponse:
    """List the configured clone's branches for the base-branch picker."""
    config = await asyncio.to_thread(store.read_json, store.config_path(), {})
    clone = str((config or {}).get("clone") or "").strip()
    if not clone:
        return web.json_response(
            {"code": "no_repo_configured", "error": "no repository configured yet"}, status=409
        )

    def _list() -> tuple[list[str], str]:

        return clone_setup.list_clone_branches(Path(clone))

    branches, err = await asyncio.to_thread(_list)
    if err:
        return web.json_response(
            {"code": "branch_list_failed", "error": err, "branches": []}, status=502
        )
    return web.json_response({"branches": branches})


# ── PR status (requirements 1 + 4) ───────────────────────────────────────────


async def _handle_pr_status(request: web.Request) -> web.StreamResponse:
    """Live status + CI checks + watcher verdict for one PR url."""
    url = (request.query.get("url") or "").strip()
    if not url:
        return web.json_response(
            {"code": "url_required", "error": "url query parameter is required"}, status=400
        )
    refresh = request.query.get("refresh") in {"1", "true", "yes"}
    status = await pr_checks.fetch_pr_status(url, refresh=refresh)
    if status.get("ok"):
        return web.json_response(status, status=200)
    return web.json_response(
        {"code": "pr_status_unavailable", "error": _redact_for_display(str(status.get("error") or ""))},
        status=502,
    )


# ── chat-session links (requirement 3) ───────────────────────────────────────


async def _handle_list_sessions(_request: web.Request) -> web.StreamResponse:
    # Redacted for the same reason as the watcher snapshots: `save_session` merges the
    # caller's patch, and the `title` the UI stores is built from a finding's `target` —
    # model-derived text. Not flagged by review; found by checking the sibling responses
    # after the watcher ones, since fixing only the reported sites is how the next one drifts.
    records = await asyncio.to_thread(store.list_sessions)
    return web.json_response(_redact_tree({"sessions": records}))


async def _handle_get_session(request: web.Request) -> web.StreamResponse:
    key = request.match_info.get("key", "")
    try:
        record = await asyncio.to_thread(store.load_session, key)
    except ValueError as exc:
        return web.json_response({"code": "invalid_config", "error": str(exc)}, status=400)
    return web.json_response(_redact_tree({"session": record}))


async def _handle_save_session(request: web.Request) -> web.StreamResponse:
    """Link (or update) the chat session for one subject.

    The frontend calls this after creating a slot so a repeat click RESUMES the
    same conversation instead of starting a duplicate one.
    """
    key = request.match_info.get("key", "")
    patch = await _json_body(request)
    allowed = {"slot_key", "folder_id", "status", "subject", "title", "url"}
    payload = {k: v for k, v in patch.items() if k in allowed}
    try:
        record = await asyncio.to_thread(store.save_session, key, payload)
    except ValueError as exc:
        return web.json_response({"code": "invalid_request", "error": str(exc)}, status=400)
    return web.json_response(_redact_tree({"session": record}))


async def _handle_delete_session(request: web.Request) -> web.StreamResponse:
    key = request.match_info.get("key", "")
    try:
        removed = await asyncio.to_thread(store.delete_session, key)
    except ValueError as exc:
        return web.json_response({"code": "invalid_request", "error": str(exc)}, status=400)
    return web.json_response({"removed": removed})


# ── run artifacts ────────────────────────────────────────────────────────────


async def _handle_ruler(_request: web.Request) -> web.StreamResponse:
    ruler = await asyncio.to_thread(
        store.read_json, store.ruler_dir() / "ruler.json", {"status": "uncalibrated"}
    )
    return web.json_response(ruler or {"status": "uncalibrated"})


async def _handle_findings(_request: web.Request) -> web.StreamResponse:
    """One row per finding, newest first — the findings list's data source.

    The ledger is append-only, so a finding accretes a row per status change
    (``seen`` -> ``failed_gate`` -> ``duplicate``). The list wants ONE entry per
    finding — its current state — not the whole history: emitting every row gave
    duplicate fingerprints, and a UI keyed on the fingerprint then toggled the
    wrong row's detail panel when two shared an id. The full history is still
    available per finding from ``/findings/{fp}``.
    """

    def _read() -> list[dict[str, Any]]:
        path = store.ledger_path()
        if not path.exists():
            return []
        rows: list[dict[str, Any]] = []
        # Read line-by-line: the ledger is append-only JSONL and one corrupt
        # tail line (a crash mid-write) must not hide every earlier finding.

        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        # Newest first, then keep only the first (latest) row per fingerprint so
        # each finding appears exactly once. A row without an ``fp`` cannot be
        # deduped, so it is kept as-is rather than silently dropped.
        seen: set[str] = set()
        collapsed: list[dict[str, Any]] = []
        for row in reversed(rows):
            fp = str(row.get("fp") or "")
            if fp and fp in seen:
                continue
            if fp:
                seen.add(fp)
            collapsed.append(row)
        return collapsed

    # Redact: a ledger row's `note` is agent-authored prose (a gate reason, a discard
    # explanation), so it is the same class of text the DETAIL endpoint already scans. Found
    # by sweeping every reader of run evidence after review caught the MCP surface — the
    # four earlier fixes each treated their own path as the last one.
    rows = await asyncio.to_thread(_read)
    return web.json_response({"findings": _redact_tree(rows)})


async def _handle_finding_detail(request: web.Request) -> web.StreamResponse:
    """Everything the run recorded about ONE finding, joined by fingerprint.

    The list endpoint returns bare ledger rows (kind / target / status), which is
    not enough to judge a finding — the evidence lives in three other places:
    the per-candidate artifact (signature, hypothesis, reproducing test, gate
    results), the archive row (cycle, measured delta, noise band), and the draft
    PR queue (title, body, diff). This gathers all of it so the UI can show WHY
    a finding was kept or rejected instead of only offering to chat about it.

    The join is by fingerprint where possible and by target otherwise: the
    ledger keys on ``fp`` while candidate artifacts are named after the
    ``cand_id`` (which embeds the target), so there is no single shared id.
    """
    fp, _bad = _validated_fp(request)
    if _bad is not None:
        return _bad

    def _gather() -> dict[str, Any]:

        # ── the ledger row(s) for this fingerprint, oldest first ──
        history: list[dict[str, Any]] = []
        path = store.ledger_path()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and str(row.get("fp") or "") == fp:
                    history.append(row)
        if not history:
            return {}

        latest = history[-1]
        target = str(latest.get("target") or "")

        detail: dict[str, Any] = {
            "fp": fp,
            "kind": latest.get("kind") or "",
            "target": target,
            "status": latest.get("status") or "",
            "note": latest.get("note") or "",
            "ts": latest.get("ts"),
            # The ledger's field is historically named ``cr``; expose it as ``pr``
            # so the UI speaks one vocabulary, and keep the raw key readable.
            "pr": latest.get("pr") or latest.get("cr") or "",
            "history": history,
        }

        # ── candidate artifacts: match on the target embedded in cand_id ──
        # ``src/search.py::negamax_root`` -> ``search_py_negamax_root``
        #
        # BASENAME, not the full path. ``proposer._short()`` builds a cand_id from the
        # file's BASENAME plus the symbol (``c6_wide_contracts_py_Proposal_2cbc5716``),
        # so slugging the whole target produced
        # ``kiro_crew_apps_builtins_auto_improvement_spine_contracts_py_proposal`` and the
        # substring test below could NEVER match for a nested file. Every deeply-nested
        # finding therefore rendered its diff with NO defect/hypothesis — the evidence was
        # on disk the whole time, just never joined (observed on the one committed finding).
        slug = progress._target_slug(target)
        cand_dir = store.results_dir() / "candidates"
        best: dict[str, Any] | None = None
        diff_text = ""
        if cand_dir.is_dir():
            for meta_path in sorted(cand_dir.glob("*.json")):
                if slug and slug not in meta_path.stem.lower():
                    continue
                data = store.read_json(meta_path)
                if not isinstance(data, dict):
                    continue
                # Prefer a kept candidate; otherwise keep the first match so a
                # rejected finding still shows its evidence.
                if best is None or data.get("status") == "kept":
                    best = data
                    sibling = meta_path.with_suffix(".diff")
                    if sibling.is_file():
                        try:
                            diff_text = _redact_for_display(sibling.read_text(encoding="utf-8"))
                        except OSError:
                            diff_text = ""
        if best:
            raw_proposal = best.get("proposal")
            proposal: dict[str, Any] = raw_proposal if isinstance(raw_proposal, dict) else {}
            raw_cand = proposal.get("candidate")
            cand: dict[str, Any] = raw_cand if isinstance(raw_cand, dict) else {}
            # EVERY agent-authored text field is redacted, not just the diff. These are
            # the model's own prose about the defect ("the fix uses KEY=…"), rendered in
            # the operator's browser by FindingDetail — the same egress boundary the diff
            # crosses, and the same class of text. Raised by review of this branch: the
            # first pass redacted the diff and the PR body and missed these.
            detail["candidate"] = {
                "cand_id": proposal.get("cand_id") or "",
                "signature": _redact_for_display(str(cand.get("signature") or "")),
                "hypothesis": _redact_for_display(str(cand.get("hypothesis") or "")),
                "evidence": _redact_for_display(str(cand.get("evidence") or "")),
                "severity_note": _redact_for_display(str(cand.get("severity_note") or "")),
                "blast_radius": _redact_for_display(str(cand.get("blast_radius") or "")),
                "reproducing_test": cand.get("reproducing_test") or {},
            }
            # The gate detail carries pytest output — assertion text from the repo under
            # test, which is equally agent-adjacent and equally rendered.
            detail["gate"] = _redact_tree(best.get("bug_gate") or best.get("gate") or {})
            detail["measurement"] = best.get("measurement") or {}
            detail["candidateStatus"] = best.get("status") or ""
        if diff_text:
            detail["diff"] = diff_text[:_MAX_DIFF_CHARS]
            detail["diffTruncated"] = len(diff_text) > _MAX_DIFF_CHARS

        # ── the archive row (cycle, measured delta, noise band) ──
        archive = store.results_dir() / "candidates.jsonl"
        cand_id = (detail.get("candidate") or {}).get("cand_id") or ""
        if archive.is_file() and cand_id:
            for line in archive.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict) and row.get("cand_id") == cand_id:
                    detail["archive"] = row
                    break

        # ── the drafted PR body, when one was queued for this fingerprint ──
        body_path = store.pr_queue_dir() / f"{fp}.pr.md"
        if body_path.is_file():
            try:
                detail["prBody"] = _redact_for_display(body_path.read_text(encoding="utf-8"))[
                    :_MAX_BODY_CHARS
                ]
            except OSError:
                pass
        queued_diff = store.pr_queue_dir() / f"{fp}.diff"
        if queued_diff.is_file() and not detail.get("diff"):
            try:
                text = _redact_for_display(queued_diff.read_text(encoding="utf-8"))
                detail["diff"] = text[:_MAX_DIFF_CHARS]
                detail["diffTruncated"] = len(text) > _MAX_DIFF_CHARS
            except OSError:
                pass

        # ── run provenance ──
        meta = store.read_json(store.results_dir() / "run.meta.json")
        if isinstance(meta, dict):
            detail["run"] = meta
        return detail

    detail = await asyncio.to_thread(_gather)
    if not detail:
        return web.json_response(
            {"code": "finding_not_found", "error": f"no finding with fingerprint {fp}"}, status=404
        )
    # Redact the WHOLE tree, not just the fields enumerated in `_gather`. The per-field
    # calls above are defense-in-depth, but the endpoint also returns blocks assembled
    # elsewhere — the `run` provenance meta, the gate tree — and a field added later would
    # leak the moment someone forgets one more `_redact_for_display`. The list endpoint
    # already wraps its whole payload; this makes the two consistent. Raised by the GPT
    # review of this branch.
    return web.json_response({"finding": _redact_tree(detail)})


async def _handle_draft_pr(request: web.Request) -> web.StreamResponse:
    """Draft (or re-draft) a pull request for a finding already in the queue.

    The loop drafts automatically after a candidate is verified and independently
    reproduced; this is the MANUAL affordance for the case where that drafting
    failed — no ``gh`` on PATH, no network, a push refused — and left the change
    sitting in ``pr_queue/`` with the evidence intact.

    Upstream's equivalent only ever returned ``{"status": "queued"}`` without
    drafting anything, so this is the behavior that endpoint always described.

    Draft-only, like every other path that opens a pull request here: the recipe
    passes ``--draft`` and never publishes, marks ready, merges, or enables
    auto-merge.
    """
    fp, _bad = _validated_fp(request)
    if _bad is not None:
        return _bad

    # Refuse while a run is live — the SAME gate `_handle_commit` uses, and for the same
    # reason. This route became clone-MUTATING when it started materializing its queued diff
    # (`checkout -B`, `apply --index`, and `reset --hard` on a failed apply) in
    # `config["clone"]`. That is the very tree the driver's worker thread is mid-cycle on:
    # `_stage_winner` / `_commit_winner_provisional` do checkout/apply/add -A/commit on that
    # branch, so interleaving discards the loop's staged-or-provisionally-committed winner and
    # then pushes whatever HEAD the interleaving left — reintroducing the metadata/content
    # mismatch the materialize step exists to prevent. Every other clone-mutating handler
    # (`/run`, `/calibrate`, `/findings/{fp}/commit`) already refuses; this one was the gap,
    # introduced by the materialize fix itself. Raised by the Opus 5 review of this branch.
    if busy := await _refuse_while_running(
        "drafting now would race the loop's own checkout/apply/push on this branch."
    ):
        return busy

    def _draft() -> dict[str, Any]:
        queue = store.pr_queue_dir()
        body_path = queue / f"{fp}.pr.md"
        diff_path = queue / f"{fp}.diff"
        if not body_path.is_file() or not diff_path.is_file():
            return {"ok": False, "error": f"no queued change for fingerprint {fp}"}

        config = store.read_json(store.config_path(), {}) or {}
        clone = str(config.get("clone") or "").strip()
        if not clone:
            return {"ok": False, "error": "no repository configured"}

        body = body_path.read_text(encoding="utf-8")
        # The queued body leads with the summary as an H1; the recipe wants the
        # title separately, so recover it from that heading.
        first = body.lstrip().splitlines()[0] if body.strip() else ""
        summary = first.lstrip("# ").strip() or f"auto-improvement: {fp}"

        # MATERIALIZE the queued diff on its own base before drafting. The recipe's draft
        # step only writes the queue copy, and `_push_fix_branch` pushes the clone's `HEAD` — so
        # drafting an OLDER finding published whatever a LATER cycle left at HEAD. Measured
        # against a real bare repo: finding A's diff adds `FINDING_A`, and the branch pushed
        # for A contained `FINDING_B`. The loop path is safe because `_stage_winner` applies
        # the winner first; this manual path had no such step. Raised by the GPT review of
        # this branch.
        diff_text = diff_path.read_text(encoding="utf-8")
        staged = commit_mod.materialize_queued_diff(
            clone=Path(clone),
            branch=normalize_branch(str(config.get("branch") or "origin/main")),
            config=config,
            diff_text=diff_text,
        )
        if not staged.get("ok"):
            return {
                "ok": False,
                "error": _redact_for_display(
                    str(staged.get("error") or "could not stage the diff")
                ),
            }

        # COMMIT what was staged. `git apply --index` does not move `HEAD`, and
        # `_push_fix_branch` pushes `HEAD:refs/heads/<branch>` — so staging alone published
        # the BASE and the queued fix was absent from the pull request. Measured on a real
        # bare repo: worktree `return 2`, pushed branch `return 1`. Raised by the GPT review.
        # Everything after the commit must ROLL BACK on failure. Committing left the change
        # on the configured branch, and `clone_setup.checkout_branch` prefers an existing
        # local branch — so a draft that fails (no `gh`, no network, a refused push) would
        # leave the next run starting from an unfiled commit and treating the queued change
        # as already-landed baseline. Measured on a real bare repo: local `work` sat 1 commit
        # ahead of a remote it had never been pushed to. `commit_finding` already resets at
        # each of its own failure points; this path had none. Raised by the GPT review.
        base = str(staged.get("base") or "")

        def _rollback() -> None:
            if base:
                commit_mod._git(Path(clone), "reset", "--hard", base)

        committed = commit_mod.commit_staged_for_draft(
            clone=Path(clone), body_path=body_path, fp=fp
        )
        if not committed.get("ok"):
            _rollback()
            return {
                "ok": False,
                "error": _redact_for_display(
                    str(committed.get("error") or "could not commit the staged diff")
                ),
            }

        recipe = GitHubPRRecipe(
            user=str(config.get("githubUser") or ""),
            clone_path=Path(clone),
            pr_queue_dir=queue,
            base_ref=str(config.get("branch") or "origin/main"),
            # From config, not the clone — see clone_setup._disable_push.
            fetch_url=clone_setup.resolve_origin_url(config) or None,
        )
        try:
            ref = recipe.draft(
                summary=summary,
                description=body,
                diff=diff_text,
                fingerprint=fp,
            )
        except Exception:
            # Even an unexpected raise must not strand the commit on the branch.
            _rollback()
            raise
        drafted = ref.startswith("http")
        # The reset runs on BOTH arms, so it lives in `finally` rather than being duplicated:
        # the ledger append is the only thing that differs, and it must not be able to SKIP the
        # reset by raising. D-79 deliberately ordered the row before the reset so a reset could
        # never run in the row's place — but that let a full disk strand the commit on the
        # branch, which is the very defect D-79 fixed, reached through a different door. The
        # pull request is already published either way, so the row is best-effort and loud.
        try:
            if drafted:
                # The reference lets the findings list and the progress chart link to the PR.
                # Best-effort: `draft()` has already published, and raising here would lose the
                # response AND the reset while the PR still exists on GitHub.
                try:
                    ledger_admin_record(fp, ref)
                except Exception:  # noqa: BLE001 — never unpublish a real PR by raising
                    logger.exception(
                        "%s: drafted %s but could not record the ledger row — the findings "
                        "list will not link to it",
                        store.APP_NAME,
                        ref,
                    )
        finally:
            # Unconditional. A successful draft otherwise leaves its commit checked out:
            # `draft()` publishes with `git push HEAD:refs/heads/<generated>`, which never moves
            # the LOCAL branch. Measured on a real bare repo — drafting finding-1 then
            # finding-2 put BOTH commits on finding-2's pushed branch. A later RUN is the path
            # that cannot recover, because `clone_setup.checkout_branch` returns early with
            # "already on <branch>" without resetting, so the run adopts the leftover commit as
            # its measurement baseline. A degraded "queued" draft published nothing, so its
            # commit must not stay behind either; the durable queue copy a retry works from is
            # untouched. Unlike the perf loop's deliberate non-reset (D-71), a manual draft is
            # one discrete publish action with no cumulative-measurement story.
            _rollback()
        return {
            "ok": drafted,
            "fp": fp,
            "pr": ref,
            "detail": (
                "draft pull request opened"
                if drafted
                else "still queued locally — see the returned reference"
            ),
        }

    # Hold the clone lock across the WHOLE sequence — materialize, commit, draft, rollback —
    # not around each step. The race is BETWEEN the steps: another operator-triggered mutation
    # doing `checkout -B` between our apply and our commit is what merges two findings into a
    # single commit (measured on a real bare repo). The run-status gate above only stops this
    # racing the loop, not a second click. Raised by the Opus 5 review of this branch.
    # RE-CHECK the run status INSIDE the lock, not just via the pre-lock `_refuse_while_running`
    # above. The two are not atomic: a request that passed the arrival check can then block on
    # `clone_lock` for the length of a driver build, and a run that starts in that window is now
    # mid-cycle on this very clone when `_draft` runs its checkout/apply/reset — the exact
    # mutation-races-the-loop bug the pre-lock guard is meant to stop, reached by waiting. This
    # is the same D-95/D-96 fix `PUT /config` and `POST /setup-clone` already carry; `_run_is_active`
    # exists precisely for this in-lock check (the async guard cannot be awaited here). A 409
    # `run_in_progress` sentinel, distinguished from a real draft failure. Raised by the GPT review.
    _RUN_STARTED = object()

    def _draft_serialized() -> dict[str, Any] | object:
        with commit_mod.clone_lock():
            if _run_is_active():
                return _RUN_STARTED
            return _draft()

    result = await asyncio.to_thread(_draft_serialized)
    if result is _RUN_STARTED:
        return web.json_response(
            {
                "code": "run_in_progress",
                "error": "a run started while this draft was waiting — drafting would race the "
                "loop's checkout/apply/push on this branch. Stop the run first.",
            },
            status=409,
        )
    assert isinstance(result, dict)
    if result.get("ok"):
        return web.json_response(result, status=200)
    return web.json_response(
        {"code": "draft_pr_failed", "error": str(result.get("detail") or "")},
        status=400,
    )


def ledger_admin_record(fp: str, pr_ref: str) -> None:
    """Append a ``filed`` ledger row carrying the pull-request reference.

    Append-only rather than a rewrite: the ledger is the run's audit trail, and
    the latest row for a fingerprint is what readers treat as current, so adding
    a row records the new fact without mutating history.

    The reference goes in ``cr``, and ``kind``/``target`` are always present, because
    ``spine.ledger.Ledger._load()`` does ``LedgerEntry(**row)`` inside a bare
    ``except: continue`` that exists to tolerate a torn final line. ``LedgerEntry`` is a
    fixed-field dataclass with ``cr`` and REQUIRED ``kind``/``target``, so a row spelled
    ``pr``, or one missing either field, raises ``TypeError`` and is silently discarded:
    this ``filed`` marker would never enter the dedup index, and after the retry cooldown
    the loop would re-discover the locus and draft a SECOND pull request for a change
    already filed. ``backend/ledger_admin.py`` documents the same hazard for its purge
    event, and ``_purged_event`` is the shape followed here. Readers accept both
    spellings (``progress.read_findings``, ``prUrlOf``), so the UI link still renders.
    """

    row: dict[str, Any] = {
        "fp": fp,
        "status": "filed",
        "cr": pr_ref,
        "note": "manually drafted from the queued change",
        "ts": time.time(),
    }
    path = store.ledger_path()
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
    # Preserve the target/kind from the finding's prior rows so the new row is
    # still identifiable in the list view.
    for line in reversed(existing.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            prior = json.loads(line)
        except ValueError:
            continue
        if isinstance(prior, dict) and str(prior.get("fp") or "") == fp:
            row.setdefault("kind", prior.get("kind") or "")
            row.setdefault("target", prior.get("target") or "")
            break
    # Unconditionally, even when no prior row was found: `kind` and `target` are
    # REQUIRED dataclass fields, so omitting them fails `LedgerEntry(**row)` exactly
    # like the wrong key spelling would, and the row would be dropped just as silently.
    row.setdefault("kind", "")
    row.setdefault("target", "")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")


# ── per-PR watcher sessions ──────────────────────────────────────────────────


async def _handle_watchers(_request: web.Request) -> web.StreamResponse:
    """Every watcher session and its current state, plus a reconcile sweep.

    Reconciling from this polled read is how upstream drove it too: a watcher exits when
    it runs out of nudges or the PR looks done, so a filed PR whose CI goes red LATER had
    nobody driving it. The sweep re-drives those, promotes anything the concurrency cap
    deferred, and reclaims clones orphaned by a crash. It is rate-limited internally
    (``RECONCILE_MIN_INTERVAL_S``) so a chatty UI cannot hammer the forge's API, and it is
    best-effort: a sweep failure must never fail the list the caller actually asked for.

    The PROMOTION half is opt-in (``watcherAutoStart``, default OFF). Starting a watcher runs
    an agent with an auto-approved shell against untrusted pull-request text, so doing it as a
    side effect of a READ gave the operator no consent moment. Orphan-clone reclamation still
    runs either way — that only deletes scratch directories.
    """

    registry = pr_watchers.get_registry()
    sessions = await asyncio.to_thread(registry.list_sessions)
    reconcile: dict[str, object] = {"skipped": "rate-limited"}
    # Read the opt-in BEFORE the rate gate, so a config-off install never even consumes the
    # reconcile window. `watcherAutoStart` defaults False: an absent key must mean OFF, or an
    # opt-in silently becomes the default.
    _cfg = await asyncio.to_thread(store.read_json, store.config_path(), {})
    _auto_start = runner._as_bool((_cfg or {}).get("watcherAutoStart"), False)
    if not _auto_start:
        reconcile = {"skipped": "watcherAutoStart is off"}
    try:
        if _auto_start and registry.should_reconcile():
            # ``fetch_pr_status`` is a COROUTINE, so statuses are fetched here on the
            # loop and handed to the (threaded, sync) sweep as a plain mapping. Doing it
            # the other way round — calling the async fetch from inside the worker
            # thread — would need a second event loop per sweep.
            findings = await asyncio.to_thread(progress.read_findings)
            urls = {
                str(f.get("pr") or f.get("cr") or "")
                for f in findings
                if str(f.get("status") or "") in pr_watchers.RECONCILABLE_STATUSES
            }
            urls = {u for u in urls if pr_watchers.is_watchable_pr(u)}
            fetched = await asyncio.gather(
                *(pr_checks.fetch_pr_status(u) for u in urls), return_exceptions=True
            )
            statuses = {
                u: (s if isinstance(s, dict) else {})
                for u, s in zip(urls, fetched)  # a failed fetch → {} → not actionable
            }
            reconcile = await asyncio.to_thread(
                registry.reconcile_failing_prs,
                findings=findings,
                status_for=lambda url: statuses.get(url, {}),
                force=True,  # the rate-limit gate was already taken above
            )
            reconcile["orphanClonesRemoved"] = await asyncio.to_thread(
                pr_watchers.sweep_orphan_clones
            )
        else:
            # Reclaim disk even when promotion is off. Sweeping only deletes scratch clone
            # directories no live watcher claims — it starts nothing and reads no untrusted
            # text — so gating it behind the opt-in would leak disk on every install that
            # leaves the flag at its default. (The docstring promised this; nesting it inside
            # the promotion branch would have made that promise false.)
            reconcile["orphanClonesRemoved"] = await asyncio.to_thread(
                pr_watchers.sweep_orphan_clones
            )
    except Exception:  # noqa: BLE001 — housekeeping, never the caller's problem
        logger.debug("%s: watcher reconcile failed", store.APP_NAME, exc_info=True)
        reconcile = {"skipped": "error"}
    # Redacted like the findings response. The watcher log ring is already scanned on WRITE
    # (`pr_watchers._log`), but the `as_dict()` snapshot beside it was served raw — and it
    # carries `target`/`title`/`lastNote`/`verdictReason`/`fixing`, all derived from model
    # output or from the pull request's own text, which the watcher ingests as untrusted input
    # by design. Measured: a `target` of `src/m.py::aws_secret_access_key=AKIA…` reached the
    # browser verbatim. Raised by the GPT review.
    return web.json_response(_redact_tree({"sessions": sessions, "reconcile": reconcile}))


async def _handle_watcher_start(request: web.Request) -> web.StreamResponse:
    """Start (or re-attach to) the watcher for one finding's pull request.

    The finding must already carry a real PR url: a queued-only change has
    nothing to watch, and the registry records that refusal as terminal state
    rather than raising.
    """
    fp, _bad = _validated_fp(request)
    if _bad is not None:
        return _bad

    rows = [f for f in await asyncio.to_thread(progress.read_findings) if f.get("fp") == fp]
    if not rows:
        return web.json_response(
            {"code": "finding_not_found", "error": f"no finding with fingerprint {fp}"}, status=404
        )
    finding = rows[0]
    pr_ref = str(finding.get("pr") or "")

    if not pr_watchers.is_watchable_pr(pr_ref):
        return web.json_response(
            {
                "code": "no_pr_to_watch",
                "error": f"finding {fp} has no pull request to watch (pr={pr_ref or 'none'})",
            },
            status=409,
        )

    config = await asyncio.to_thread(store.read_json, store.config_path(), {})
    config = config or {}
    registry = pr_watchers.get_registry()
    # A watcher thread bridges its one async call (the PR status read) back onto
    # the gateway loop. Bind it HERE, from the coroutine, because the registry's
    # own fallback uses ``get_running_loop()`` — and the body below runs in a
    # worker thread where there is none, which would refuse the start outright.
    registry.attach_loop(asyncio.get_running_loop())

    def _start() -> dict[str, Any]:
        state = registry.start(
            fp=fp,
            pr=pr_ref,
            kind=str(finding.get("kind") or ""),
            target=str(finding.get("target") or ""),
            base_ref=str(config.get("branch") or "origin/main"),
            clone=str(config.get("clone") or ""),
        )
        return registry.status(fp) or {"fp": fp, "status": getattr(state, "status", "")}

    # Same snapshot, same readers — see the reconcile listing above.
    return web.json_response(_redact_tree({"session": await asyncio.to_thread(_start)}))


async def _handle_watcher_stop(request: web.Request) -> web.StreamResponse:
    """Ask a watcher to stop after its current attempt."""
    fp, _bad = _validated_fp(request)
    if _bad is not None:
        return _bad

    registry = pr_watchers.get_registry()
    stopped = await asyncio.to_thread(registry.stop, fp)
    return web.json_response({"stopped": stopped, "fp": fp})


async def _handle_watcher_log(request: web.Request) -> web.StreamResponse:
    """What a watcher has done so far. ``since`` tails only newer entries."""
    fp, _bad = _validated_fp(request)
    if _bad is not None:
        return _bad
    try:
        since = int(request.query.get("since") or 0)
    except (TypeError, ValueError):
        since = 0

    registry = pr_watchers.get_registry()
    return web.json_response(await asyncio.to_thread(registry.get_log, fp, since))


# ── profiler frame trees + ledger maintenance ────────────────────────────────


async def _handle_profiles(_request: web.Request) -> web.StreamResponse:
    """Every captured profiler frame tree, newest first."""

    # Redacted like every other browser-facing payload here: a profiler frame carries
    # function/module/file names captured from the TARGET repo's code, and a credential-shaped
    # identifier there (an `AKIA…`-shaped symbol, a token-shaped path segment) would otherwise
    # reach the dashboard unscanned. Same redactor the findings/session/progress routes use; the
    # single-profile route below is wrapped for the same reason. Raised by the GPT review.
    profiles = await asyncio.to_thread(profile_normalize.list_profiles)
    return web.json_response(_redact_tree({"profiles": profiles}))


async def _handle_profile(request: web.Request) -> web.StreamResponse:
    """One finding's normalized frame tree, for the flame / sunburst view."""
    fp, _bad = _validated_fp(request)
    if _bad is not None:
        return _bad

    try:
        tree = await asyncio.to_thread(profile_normalize.read_profile, fp)
    except ValueError as exc:
        return web.json_response({"code": "invalid_request", "error": str(exc)}, status=400)
    if tree is None:
        return web.json_response(
            {"code": "profile_not_found", "error": f"no profile for fingerprint {fp}"}, status=404
        )
    # Scan the frame tree before it reaches the browser: its function/module/file names come
    # from the target repo's code, so a credential-shaped identifier would otherwise be served
    # raw — the same egress boundary the findings/session responses close. Raised by the GPT
    # review.
    return web.json_response(_redact_tree({"profile": tree}))


async def _handle_forget(request: web.Request) -> web.StreamResponse:
    """Mark a finding purged so the dedup layer will let it be retried.

    A hard-terminal ledger status (``failed_gate``) otherwise blocks a finding
    forever — even after the reason it failed has been fixed. This is the escape
    hatch for exactly that, and it keeps the artifacts.
    """
    fp, _bad = _validated_fp(request)
    if _bad is not None:
        return _bad

    try:
        result = await asyncio.to_thread(ledger_admin.forget, fp)
    except ValueError as exc:
        return web.json_response({"code": "invalid_request", "error": str(exc)}, status=400)
    if result.get("ok"):
        return web.json_response(result, status=200)
    return web.json_response(
        {"code": "finding_not_found", "error": _redact_for_display(str(result.get("error") or ""))},
        status=404,
    )


async def _handle_purge(request: web.Request) -> web.StreamResponse:
    """Forget a finding AND remove its artifacts."""
    fp, _bad = _validated_fp(request)
    if _bad is not None:
        return _bad

    try:
        result = await asyncio.to_thread(ledger_admin.purge, fp)
    except ValueError as exc:
        return web.json_response({"code": "invalid_request", "error": str(exc)}, status=400)
    if result.get("ok"):
        return web.json_response(result, status=200)
    return web.json_response(
        {"code": "finding_not_found", "error": _redact_for_display(str(result.get("error") or ""))},
        status=404,
    )


async def _handle_purge_dead(request: web.Request) -> web.StreamResponse:
    """Sweep findings that can never make progress.

    Artifact removal is opt-in (``?artifacts=1``): a sweep is a bulk operation and
    the evidence is usually the reason someone is looking at a dead record.
    """
    remove = request.query.get("artifacts") in {"1", "true", "yes"}

    result = await asyncio.to_thread(ledger_admin.purge_dead, remove_artifacts=remove)
    return web.json_response(result)


async def _handle_calibrate(_request: web.Request) -> web.StreamResponse:
    """Run Phase 1 — prove the ruler — before any improvement cycle.

    A run refuses to start on an uncalibrated ruler, so this is the gate for the
    perf track. Building the profile touches git and the test suite, so the whole
    call runs off the event loop and the work itself continues on a worker thread.
    """
    config = await asyncio.to_thread(store.read_json, store.config_path(), {})
    config = config or {}
    if not str(config.get("clone") or "").strip():
        return web.json_response(
            {"code": "no_repo_configured", "error": "no repository configured"}, status=409
        )

    def _start() -> dict[str, Any]:

        return runner.get_supervisor().calibrate(config)

    try:
        return web.json_response(await asyncio.to_thread(_start))
    except RuntimeError as exc:
        return web.json_response({"code": "watcher_conflict", "error": str(exc)}, status=409)


async def _handle_commit(request: web.Request) -> web.StreamResponse:
    """Commit a queued change straight to the configured branch (the one-click
    autocommit button). Denylist-gated to a non-protected branch, same as the
    loop's direct-commit mode."""
    fp, _bad = _validated_fp(request)
    if _bad is not None:
        return _bad

    # Refuse while a run is live. One-click commit checks out the target branch, applies
    # the queued diff and pushes — and the loop's own direct-commit mode does the same on
    # the same branch from the worker thread. Running both at once interleaves two
    # checkout/apply/commit sequences in one clone: the button can commit onto a tree the
    # loop is mid-edit on, or push a HEAD the loop just moved. Gate on the supervisor's
    # live status (RUNNING / CALIBRATING / STOPPING) so the operator's manual commit only
    # runs when the loop is not touching the clone. Raised by the GPT review of this branch.
    if busy := await _refuse_while_running(
        "committing now would race the loop's own checkout/apply/push on this branch."
    ):
        return busy

    # RE-CHECK inside the lock, same reason as `PUT /config`, `POST /setup-clone` and the draft
    # route: the pre-lock `_refuse_while_running` above only proves no run was live on arrival,
    # and a request that then blocks on `clone_lock` can have a run start while it waits — which
    # would put the loop mid checkout/apply/push on this clone when `commit_finding` runs the
    # same sequence. `clone_lock` is a re-entrant RLock, so holding it here and letting
    # `commit_finding` re-enter it is safe and keeps the whole recheck-then-mutate atomic.
    # Raised by the GPT review.
    _RUN_STARTED = object()

    def _commit() -> dict[str, Any] | object:
        with commit_mod.clone_lock():
            if _run_is_active():
                return _RUN_STARTED
            return commit_mod.commit_finding(fp)

    result = await asyncio.to_thread(_commit)
    if result is _RUN_STARTED:
        return web.json_response(
            {
                "code": "run_in_progress",
                "error": "a run started while this commit was waiting — committing would race the "
                "loop's checkout/apply/push on this branch. Stop the run first.",
            },
            status=409,
        )
    assert isinstance(result, dict)
    if result.get("ok"):
        # Supersede the `filed` row: the ledger is last-write-wins per fingerprint, and
        # `filed` is what drives the PR watchers and the UI's commit button — so without
        # this a change already on the branch keeps reading as an open, uncommitted PR and
        # the operator is invited to commit it again. Bookkeeping only; the push already
        # succeeded, so a failure here is logged inside the helper and does NOT turn a
        # landed commit into an error response.
        await asyncio.to_thread(
            ledger_admin.record_committed,
            str(result.get("fp") or fp),
            branch=str(result.get("branch") or ""),
            sha=str(result.get("sha") or ""),
        )
        return web.json_response(result, status=200)
    # Redacted, like every sibling error response here. `commit.py` builds its `error` from
    # `(proc.stderr or '')[:160]` — raw git stderr, which quotes the ref, the path, and
    # anything a repository's own hook printed. Latent while nothing rendered it; D-97 started
    # showing it at the finding row, which made it a live egress path to the browser.
    # Raised by the GPT review.
    return web.json_response(
        {"code": "request_failed", "error": _redact_for_display(str(result.get("error") or ""))},
        status=400,
    )


async def _handle_progress(_request: web.Request) -> web.StreamResponse:
    """The cumulative-best staircase the progress chart draws."""
    return web.json_response(_redact_tree(await asyncio.to_thread(progress.read_progress)))


async def _handle_deps(_request: web.Request) -> web.StreamResponse:
    """Which external tools a run needs, and whether they are present."""
    return web.json_response(await asyncio.to_thread(deps.check_deps))


async def _handle_deps_install(_request: web.Request) -> web.StreamResponse:
    """Install the optional dependencies that can be installed safely."""
    result = await asyncio.to_thread(deps.install_deps)
    if result.get("ok"):
        return web.json_response(result, status=200)
    return web.json_response(
        {"code": "operation_failed", "error": _redact_for_display(str(result.get("error") or ""))},
        status=500,
    )


async def _handle_events(request: web.Request) -> web.StreamResponse:
    """Subscribe to the run's live event stream (server-sent events)."""
    return await sse.stream(request)


async def _handle_health(_request: web.Request) -> web.StreamResponse:
    return web.json_response({"ok": True, "app": store.APP_NAME})


# ── the run engine (start / status / stop) ───────────────────────────────────


async def _handle_run_start(_request: web.Request) -> web.StreamResponse:
    """Start a run from the config ON DISK.

    The body is ignored on purpose: the run's target repository, base branch and
    budgets are the persisted config, so a Start click cannot smuggle in a different
    repo or a wider budget than what the config endpoints allow. Building the driver
    is blocking (git, provider probe), so the whole call runs off the event loop.
    """
    # Read the config INSIDE the lock, together with the start it feeds. Read outside, a
    # retarget landing between the read and the start means the run operates on the repo
    # that was just replaced while the UI shows the new one. `_build_driver` re-enters the
    # same RLock, which is why it is re-entrant.
    def _start() -> dict:
        with commit_mod.clone_lock():
            config = store.read_json(store.config_path(), {})
            return runner.get_supervisor().start(config or {})

    try:
        result = await asyncio.to_thread(_start)
    except (RuntimeError, ValueError, PermissionError) as exc:
        # Already running / no repository configured / push not disabled — all three are
        # "the request conflicts with current state", all three are user-fixable, and the
        # message is the actionable part, so it goes through verbatim.
        return web.json_response({"code": "session_conflict", "error": str(exc)}, status=409)
    return web.json_response(result)


async def _handle_run_status(_request: web.Request) -> web.StreamResponse:
    """Live run status. Reads in-memory state only, so it stays responsive while the
    worker thread is deep in a measurement."""

    return web.json_response(runner.get_supervisor().status())


async def _handle_run_stop(_request: web.Request) -> web.StreamResponse:
    """Request a clean stop. Blocking (it joins the worker thread, bounded), so it runs
    off the event loop."""

    def _stop() -> dict:

        return runner.get_supervisor().stop()

    return web.json_response(await asyncio.to_thread(_stop))


# ── registration ─────────────────────────────────────────────────────────────

_PREFIX = f"/api/apps/{store.APP_NAME}"


def register_routes(app: web.Application) -> None:
    """Mount the app's routes on the gateway application."""
    try:
        store.ensure_layout()
    except Exception:  # pragma: no cover - never break gateway startup
        logger.warning("%s: ensure_layout failed at startup", store.APP_NAME, exc_info=True)

    add = app.router.add_route
    add("GET", f"{_PREFIX}/health", _require_enabled(_handle_health))
    add("GET", f"{_PREFIX}/config", _require_enabled(_handle_get_config))
    add("PUT", f"{_PREFIX}/config", _require_enabled(_handle_put_config))
    add("POST", f"{_PREFIX}/setup-clone", _require_enabled(_handle_setup_clone))
    add("GET", f"{_PREFIX}/branches", _require_enabled(_handle_branches))
    add("GET", f"{_PREFIX}/pr-status", _require_enabled(_handle_pr_status))
    add("POST", f"{_PREFIX}/run", _require_enabled(_handle_run_start))
    add("GET", f"{_PREFIX}/run", _require_enabled(_handle_run_status))
    add("POST", f"{_PREFIX}/run/stop", _require_enabled(_handle_run_stop))
    add("GET", f"{_PREFIX}/ruler", _require_enabled(_handle_ruler))
    add("GET", f"{_PREFIX}/progress", _require_enabled(_handle_progress))
    add("POST", f"{_PREFIX}/calibrate", _require_enabled(_handle_calibrate))
    add("POST", f"{_PREFIX}/draft-pr/{{fp}}", _require_enabled(_handle_draft_pr))
    add("POST", f"{_PREFIX}/findings/{{fp}}/commit", _require_enabled(_handle_commit))
    add("GET", f"{_PREFIX}/watchers", _require_enabled(_handle_watchers))
    add("POST", f"{_PREFIX}/watchers/{{fp}}/start", _require_enabled(_handle_watcher_start))
    add("POST", f"{_PREFIX}/watchers/{{fp}}/stop", _require_enabled(_handle_watcher_stop))
    add("GET", f"{_PREFIX}/watchers/{{fp}}/log", _require_enabled(_handle_watcher_log))
    add("GET", f"{_PREFIX}/profiles", _require_enabled(_handle_profiles))
    add("GET", f"{_PREFIX}/profile/{{fp}}", _require_enabled(_handle_profile))
    add("POST", f"{_PREFIX}/findings/{{fp}}/forget", _require_enabled(_handle_forget))
    add("POST", f"{_PREFIX}/findings/{{fp}}/purge", _require_enabled(_handle_purge))
    add("POST", f"{_PREFIX}/findings/purge-dead", _require_enabled(_handle_purge_dead))
    add("GET", f"{_PREFIX}/deps", _require_enabled(_handle_deps))
    add("POST", f"{_PREFIX}/deps/install", _require_enabled(_handle_deps_install))
    add("GET", f"{_PREFIX}/events", _require_enabled(_handle_events))
    add("GET", f"{_PREFIX}/findings", _require_enabled(_handle_findings))
    add("GET", f"{_PREFIX}/findings/{{fp}}", _require_enabled(_handle_finding_detail))
    add("GET", f"{_PREFIX}/sessions", _require_enabled(_handle_list_sessions))
    add("GET", f"{_PREFIX}/sessions/{{key}}", _require_enabled(_handle_get_session))
    add("PUT", f"{_PREFIX}/sessions/{{key}}", _require_enabled(_handle_save_session))
    add("DELETE", f"{_PREFIX}/sessions/{{key}}", _require_enabled(_handle_delete_session))

    async def _bind_watcher_loop(_app: web.Application) -> None:
        """Give the watcher registry the gateway loop, once, at startup.

        The watcher threads bridge their one async call (reading live PR status)
        back onto this loop. Binding it here means a watcher started from any
        path — a route, a run, a restart — already has it.
        """
        try:

            pr_watchers.attach_loop(asyncio.get_running_loop())
        except Exception:  # pragma: no cover - never break gateway startup
            logger.warning("%s: could not bind the watcher loop", store.APP_NAME, exc_info=True)

    async def _stop_watchers(_app: web.Application) -> None:
        """Ask every watcher to stop so shutdown is not waiting on a nudge."""
        try:

            pr_watchers.get_registry().stop_all()
        except Exception:  # pragma: no cover - defensive
            logger.warning("%s: watcher shutdown failed", store.APP_NAME, exc_info=True)

    # Guarded: register_routes runs before the runner freezes its signal lists,
    # so appending is safe — but a failure here must never break gateway startup.
    try:
        app.on_startup.append(_bind_watcher_loop)
        app.on_cleanup.append(_stop_watchers)
    except Exception:  # pragma: no cover - defensive
        logger.warning("%s: could not register watcher lifecycle hooks", store.APP_NAME)

    logger.info("%s backend routes registered", store.APP_NAME)
