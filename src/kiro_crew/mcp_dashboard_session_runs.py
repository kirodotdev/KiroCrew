"""What each session-control tool of the ``kirocrew-dashboard`` server does when called.

One ``_run_session_*`` body per row of ``_session_tools``
(:mod:`kiro_crew.mcp_dashboard_session_tools`): each takes ``(args, ctx)``, sends the
request its row declares under the verified caller key, and renders the answer. The folder
filing ``session_create``, ``session_fork`` and ``session_revive`` share, and the renderers
of a session summary and of a gone row's close time, are defined here too.
:mod:`kiro_crew.mcp_dashboard` binds every name defined here, so each one reads, imports
and patches as ``kiro_crew.mcp_dashboard.<name>``.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any, NamedTuple
from urllib.parse import quote

from kiro_crew.conductor_patrol import reply_line
from kiro_crew.mcp_dashboard_caller_scope import (
    CREW_MODE_FOLDER_NOTE,
    _caller_app_scope,
    _caller_in_crew_tree,
    _refuse_tree_shaping_if_unverifiable,
)
from kiro_crew.mcp_dashboard_folder_tree import _chat_folder_paths
from kiro_crew.mcp_dashboard_references import (
    _ensure_chat_folder_path,
    _get_rows,
    _resolve_chat_folder_id,
    _resolve_chat_folder_ref,
)
from kiro_crew.mcp_tools.dashboard_client import DashboardClient, DashboardError
from kiro_crew.mcp_tools.table import ToolContext
from kiro_crew.platform import redact_via_context as redact
from kiro_crew.validation import (
    BROADCAST_RESPONSE_MARGIN_SECS,
    BROADCAST_TARGET_ALLOWANCE_SECS,
    MAX_BROADCAST_TARGETS,
)


def _resolve_folder_for_new_session(
    ctx: ToolContext,
    folder_ref: str,
    verb: str,
    preflight: Callable[[str], str | None] | None = None,
    crew_project: bool = False,
) -> tuple[str, str, str, str | None]:
    """``(folder_id, folder_label, made_note, error)`` for filing a NEW session.

    Shared by ``session_create``, ``session_fork`` and ``session_revive``, which file a session the same
    way: the reference is resolved with ``chat_folder_create``'s `parent`
    semantics -- missing path segments are CREATED -- and creating folders is
    tree shaping, so the same gate applies rather than a second authorization
    path: a caller that could not reshape the tree by creating a folder must not
    reach the same write by naming the path here. The gate's verified key is what
    the segment creation writes under, per its own contract.

    An empty reference resolves to ``("", "", "", None)``: nothing to file.
    ``made_note`` names any path segments the mkdir -p walk created, and it is
    reported on BOTH outcomes -- those segments persist even when the create
    itself is then refused, the same partial-report posture chat_folder_create
    takes, since folder deletion is deliberately not a capability this server
    has.

    ``preflight`` closes that gap for the common case. The walk calls it just
    before its first create, with the deepest folder that already exists, and
    it returns an error string when the session create itself would be
    refused. That refusal ends the walk with nothing made, so a create that
    cannot succeed leaves no empty folder behind. Only a refusal that appears
    between the preflight and the create (a race, or a folder-create rate limit
    hit part-way down the path) can still strand segments.
    """
    if not folder_ref:
        return "", "", "", None
    gate_key, _gate_app, gate = _refuse_tree_shaping_if_unverifiable(ctx, verb)
    if gate:
        return "", "", "", gate
    # An app-scoped caller may create folders, but it can NEVER complete a
    # session create or fork (the endpoints refuse `app_scoped_caller`), so
    # resolving the folder for it would only leave created path segments behind
    # for a call that cannot succeed. This is a side-effect guard, not a second
    # authorization home: the endpoint's refusal stays authoritative for the
    # create itself.
    scope_rows, scope_err = _get_rows(ctx.client, "/api/chat/slots")
    if scope_err:
        return "", "", "", redact(f"Error: {scope_err}")
    if _caller_in_crew_tree(gate_key, scope_rows):
        # A chat that turned Crew Mode on keeps its sessions out of the person's
        # folders: no path segment is created and the reply says so. With
        # *crew_project* (``session_create`` only, whose server route files nothing
        # for a Crew Mode caller) an EXISTING folder is still named, which gives the
        # child that folder's project directory.
        if not crew_project:
            return "", "", CREW_MODE_FOLDER_NOTE, None
        crew_folders, crew_err = _get_rows(ctx.client, "/api/chat/folders")
        if crew_err:
            return "", "", "", redact(f"Error: {crew_err}")
        # The shared resolver, creating nothing. A folder that does not exist (one
        # the conductor's instructions would have created) names no project
        # directory and is ignored like the folder itself: the dispatch goes ahead
        # unfiled and the note says so. An AMBIGUOUS reference is still refused: it
        # would pick a project directory nobody chose.
        crew_id, _created, crew_ref_err = _resolve_chat_folder_ref(
            folder_ref, crew_folders, create_missing=False
        )
        if crew_ref_err:
            if crew_ref_err.startswith("folder not found"):
                return "", "", CREW_MODE_FOLDER_NOTE, None
            return "", "", CREW_MODE_FOLDER_NOTE, redact(f"Error: {crew_ref_err}")
        # The id only: the session is not filed there, so no label says it is.
        return crew_id, "", CREW_MODE_FOLDER_NOTE, None
    if _caller_app_scope(gate_key, scope_rows):
        return (
            "",
            "",
            "",
            (
                "Error: an app-scoped session cannot create sessions, so there is "
                "nothing to file — folder resolution is refused before it would "
                "create path segments for a create that cannot succeed."
            ),
        )
    chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
    if folders_err:
        return "", "", "", redact(f"Error: {folders_err}")
    fld_id, created_segments, fld_err = _ensure_chat_folder_path(
        folder_ref, chat_folders, client=ctx.client, session_key=gate_key, before_create=preflight
    )
    made_note = ""
    if created_segments:
        made_note = f" (created folder path: {'/'.join(created_segments)})"
    if fld_err:
        # Refuse the whole create: the caller asked for a session filed in this
        # folder, and "created but unfiled" would silently honor half of that.
        # No SESSION exists yet; path segments the mkdir -p walk already created
        # DO persist and are reported in `made_note`.
        return "", "", made_note, redact(f"Error: {fld_err}{made_note}")
    return fld_id, _chat_folder_paths(chat_folders).get(fld_id, fld_id), made_note, None


def _summary_time(value: object) -> str:
    """The sidecar's ``generated_at`` (a ``time.time()`` float) as UTC ISO-8601."""
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return "at an unknown time"


def _closed_at_display(value: object) -> str:
    """A gone row's ``closed_at`` (crew-log epoch MILLISECONDS) as UTC ISO-8601.

    The close edge stamps ``entry.time`` from ``now_ms()``, so the value is in
    milliseconds; render it human-readable rather than as a raw epoch number. Falls
    back to the value's own string form when it is not a finite millisecond count,
    so a malformed stamp still shows something rather than raising in the renderer.
    """
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(value) / 1000.0))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError, OSError):
        return str(value)


def _render_session_summary(resp: dict[str, Any]) -> str:
    """Render a ``/api/session-control/summary`` body as compact text.

    The route has already bounded and redacted every field; this only formats
    it and says where the route left something out.
    """
    state_line = "still working" if resp.get("running") else "idle"
    head = f"\U0001f9ed `{resp.get('target', '')}` — {resp.get('title', '')} ({state_line})"
    if not resp.get("enabled"):
        return (
            f"{head}\nSession summaries are switched off on this gateway "
            "(session_summary.enabled), so there is no digest to read. Use "
            "session_read_message instead."
        )
    intents = [i for i in (resp.get("intents") or []) if isinstance(i, dict)]
    if not intents:
        return (
            f"{head}\nNo summary has been written for this session yet (one is written "
            "at turn end once the session has enough turns). Use session_read_message "
            "instead."
        )
    when = _summary_time(resp.get("generated_at"))
    stale = " — STALE: newer turns exist." if resp.get("stale") else "."
    lines = [head, f"Summary written {when}{stale}"]
    for intent in intents:
        lines.append(f"- [{intent.get('state') or '?'}] {intent.get('title', '')}")
        lines.extend(f"    progress: {p}" for p in intent.get("progress") or [])
        if intent.get("progress_omitted"):
            lines.append(f"    ({intent['progress_omitted']} earlier progress item(s) not shown)")
        lines.extend(f"    next: {st}" for st in intent.get("next_steps") or [])
        if intent.get("next_steps_omitted"):
            lines.append(f"    ({intent['next_steps_omitted']} more next step(s) not shown)")
    if resp.get("intents_omitted"):
        lines.append(f"({resp['intents_omitted']} older intent(s) not shown.)")
    notes = resp.get("constraints") or []
    if notes:
        lines.append("Project notes:")
        lines.extend(f"- {n}" for n in notes)
    if resp.get("constraints_omitted"):
        lines.append(f"({resp['constraints_omitted']} more project note(s) not shown.)")
    return "\n".join(lines)


#: How the ``session_create`` reply names the fields the caller's crewmate profile
#: supplied. They are copied into the child's own slot when it is created, as a
#: per-session pick, so a later profile edit does not reach a child already open;
#: the note says "copied" so the reply does not read as a live link to the profile.
_PROFILE_COPY_NOTE = "copied from your crewmate profile at creation"


class _Created(NamedTuple):
    """What one ``session_create`` folder walk plus create call came back with.

    ``resp`` is the create route's JSON reply, or ``None`` when no session was
    made. Then ``error`` is the text ``session_create`` replies with, and either
    ``refused`` is the create call's own refusal, or the folder walk stopped first
    and ``walk_refused`` is the last refusal a request of the walk raised (the
    create's dry run or a folder write; ``None`` when none raised one). A walk that
    stopped never sent the create, so no session can exist then, but a folder
    write whose reply was lost may have made a folder.
    """

    resp: dict[str, Any] | None
    folder_label: str
    made_note: str
    error: str
    refused: DashboardError | None
    walk_refused: DashboardError | None = None


class _RefusalCodes:
    """A dashboard client that remembers every refusal it raised."""

    def __init__(self, client: DashboardClient) -> None:
        self._client = client
        self.refusals: list[DashboardError] = []

    def _note(self, refused: DashboardError) -> DashboardError:
        self.refusals.append(refused)
        return refused

    def get(
        self, path: str, *, session_key: str | None = None, timeout: float | None = None
    ) -> Any:
        try:
            return self._client.get(path, session_key=session_key, timeout=timeout)
        except DashboardError as refused:
            raise self._note(refused)

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        session_key: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        try:
            return self._client.post(path, body, session_key=session_key, timeout=timeout)
        except DashboardError as refused:
            raise self._note(refused)

    def patch(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        return self._client.patch(path, body, session_key=session_key)

    def put(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        return self._client.put(path, body, session_key=session_key)

    def delete(
        self, path: str, body: dict[str, Any] | None = None, *, session_key: str | None = None
    ) -> Any:
        return self._client.delete(path, body, session_key=session_key)


def _create_session(args: dict[str, Any], ctx: ToolContext, *, keep_walk: bool = False) -> _Created:
    """Run ``session_create``'s folder walk and create call, and return the reply.

    Shared by ``session_create``, which renders the reply as text, and
    ``work_dispatch``, which needs the key the route returned. ``keep_walk`` sends
    the walk through :class:`_RefusalCodes` so a refusal that stopped it comes
    back as ``walk_refused``; ``session_create`` has no use for it.
    """
    payload: dict[str, Any] = {"title": args.get("title", ""), "agent": args.get("agent", "")}
    if args.get("model"):
        payload["model"] = args["model"]
    if args.get("reasoning_effort"):
        payload["reasoning_effort"] = args["reasoning_effort"]
    if args.get("agent_kind"):
        payload["agent_kind"] = args["agent_kind"]
    if args.get("autocompact_pct") is not None:
        # Set before the folder preflight below, so its dry-run probe carries the
        # value and a refused threshold creates no folder.
        payload["autocompact_pct"] = args["autocompact_pct"]

    def _refused(refused: DashboardError, suffix: str) -> str:
        return redact(
            f"could not create a session: {refused.error}{suffix}"
            f"{_create_refusal_remedy(refused, payload)}"
        )

    walk = _RefusalCodes(ctx.client)
    walk_ctx = (
        ToolContext(client=walk, caller=ctx.caller, caller_key=ctx.caller_key) if keep_walk else ctx
    )

    def _preflight_create(deepest_id: str) -> str | None:
        # The same create, as a dry run, against the folder the new path
        # segments would hang from. Its refusal is the real create's, rendered
        # WITHOUT the `Error:` prefix: `_resolve_folder_for_new_session` adds
        # one to whatever the walk returns, and two of them read as a bug.
        probe = {**payload, "dry_run": True}
        if deepest_id:
            probe["folder_id"] = deepest_id
        try:
            walk_ctx.client.post("/api/session-control/create", probe, session_key=ctx.caller_key)
        except DashboardError as refused:
            return _refused(refused, " (no folder was created)")
        return None

    fld_id, folder_label, made_note, fld_err = _resolve_folder_for_new_session(
        walk_ctx,
        str(args.get("folder") or ""),
        "filing a new session at creation",
        preflight=_preflight_create,
        crew_project=True,
    )
    if fld_err:
        return _Created(None, "", made_note, fld_err, None, next(reversed(walk.refusals), None))
    if fld_id:
        payload["folder_id"] = fld_id
    try:
        resp = ctx.client.post(
            "/api/session-control/create",
            payload,
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return _Created(None, "", made_note, f"Error: {_refused(refused, made_note)}", refused)
    return _Created(resp, folder_label, made_note, "", None)


def _run_session_create(args: dict[str, Any], ctx: ToolContext) -> str:
    created = _create_session(args, ctx)
    if created.resp is None:
        return created.error
    resp, folder_label, made_note = created.resp, created.folder_label, created.made_note
    crew_mode = bool(resp.get("crew_mode") or resp.get("folder_ignored"))
    filed = f" filed in `{folder_label}`" if folder_label and not crew_mode else ""
    on_model = f" on model `{resp['model']}`" if resp.get("model") else ""
    if resp.get("reasoning_effort"):
        on_model += f" at reasoning effort `{resp['reasoning_effort']}`"
    # Name what the caller did not choose, so a conductor reading its own reply
    # learns the child runs on its profile's pick rather than the default.
    inherited = resp.get("inherited") or []
    if inherited:
        on_model += f" ({_PROFILE_COPY_NOTE}: {', '.join(inherited)})"
    if resp.get("inherited_model_dropped"):
        on_model += (
            " (your crewmate profile's worker model is not usable here, so the "
            "session starts on the default model; fix the profile or pass `model`)"
        )
    if resp.get("autocompact_pct") is not None:
        on_model += f", auto-compacting at {resp['autocompact_pct']:g}%"
    # A Crew Mode chat's worker is in the sidebar but filed in no folder.
    where = (
        "the user's sidebar, unfiled (this chat is in Crew Mode)"
        if crew_mode
        else "the user's sidebar"
    )
    return redact(
        f"\U0001f195 Opened `{resp.get('target')}` ({resp.get('title')}){filed}{on_model}.{made_note} "
        f"It is empty and waiting in {where}; watch it with "
        "session_read_message." + reply_line(resp) + _worker_capabilities_line(resp)
    )


def _worker_capabilities_line(resp: dict[str, Any]) -> str:
    """The gateway's line on what a crewmate's own worker can use, when it sent one."""
    note = resp.get("worker_capabilities")
    return f"\n{note}" if isinstance(note, str) and note else ""


def _create_refusal_remedy(refused: DashboardError, payload: dict[str, Any]) -> str:
    """The one refusal a caller can act on by itself, with the two real remedies.

    ``memory_delegation_denied`` is deliberately terse on the wire -- it names
    neither the store nor the member, so a guessed agent name is never confirmed --
    and that terseness is what left callers inventing remedies that do not exist
    (a memory-store setting under Settings > Crew: a crew member's memory binding is
    immutable, and the dashboard offers no such control). The remedy is appended
    HERE, in the tool reply, because it holds for the whole refusal class and
    discloses nothing the refusal withholds: dispatch the TEMPLATE of that name on
    the caller's own memory (what a one-item worker needs), or let the owner's own
    tab make the create. A request that already asked for the template gets the
    second remedy alone, since the first is what it just tried.
    """
    if refused.code != "memory_delegation_denied":
        return ""
    if payload.get("agent_kind") == "template":
        return (
            " The template dispatch binds this session's OWN memory, and that binding is "
            "what could not be verified. A member's memory binding cannot be changed in "
            "Settings; the owner can re-select this session's agent (which re-binds its "
            "memory) or run the dispatch from their own chat tab."
        )
    return (
        " The agent you named is a crew member whose private memory this session may "
        "not bind; a member's memory binding cannot be changed in Settings. Re-send "
        'with `agent_kind: "template"` to run that agent\'s shared template on your own '
        "memory (a kirocrew-worker needs no memory of its own), or ask the owner to run "
        "this dispatch from their own chat tab."
    )


def _run_session_fork(args: dict[str, Any], ctx: ToolContext) -> str:
    payload = {"source": args.get("source", ""), "title": args.get("title", "")}
    if args.get("at_message_index") is not None:
        payload["at_message_index"] = args["at_message_index"]
    fld_id, folder_label, made_note, fld_err = _resolve_folder_for_new_session(
        ctx, str(args.get("folder") or ""), "filing a forked session at creation"
    )
    if fld_err:
        return fld_err
    if fld_id:
        payload["folder_id"] = fld_id
    try:
        resp = ctx.client.post(
            "/api/session-control/fork",
            payload,
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return redact(f"Error: could not fork the session: {refused.error}{made_note}")
    filed = f" filed in `{folder_label}`" if folder_label else ""
    return redact(
        f"\U0001f500 Forked `{resp.get('source')}` into `{resp.get('target')}` "
        f"({resp.get('title')}) carrying {resp.get('messages')} message(s){filed}."
        f"{made_note} It is idle and waiting in the user's sidebar; seed it with "
        "session_send and watch it with session_read_message."
    )


def _run_session_stop(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/stop",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not stop that session: {refused.error}"
    target = resp.get("target", args["target"])
    info = resp.get("info")
    if info:
        # Two different facts share this reply and must not read alike. A
        # target that was never running has nothing to stop; one whose
        # cooperative cancel is still in flight IS stopping, and a re-sent
        # stop lands there routinely — telling that caller "nothing to
        # stop" would report the opposite of what happened and invite it to
        # act as though the target were still free-running.
        if resp.get("already_stopping"):
            return f"\u2139\ufe0f `{target}`: {info} — the earlier stop still stands."
        return f"\u2139\ufe0f `{target}`: {info} — nothing to stop."
    return f"\U0001f6d1 Stop sent to `{target}`. Its transcript now shows the stop card."


def _run_session_end_wait(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/end-wait",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not end that session's wait: {refused.error}"
    target = resp.get("target", args["target"])
    if not resp.get("ended"):
        info = resp.get("info") or "not sleeping in the wait tool"
        return f"\u2139\ufe0f `{target}`: {info}. Nothing to end."
    return (
        f"\u23f0 End-wait sent to `{target}`. Its wait returns on the next "
        "keepalive ping (within about 5s) and the turn continues."
    )


def _run_session_retry(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/retry",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not retry that session's turn: {refused.error}"
    target = resp.get("target", args["target"])
    return redact(
        f"\U0001f501 Retry started in `{target}`. Its transcript shows the resume row; "
        "read it with session_read_message."
    )


def _run_session_continue(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/continue",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not continue that session: {refused.error}"
    target = resp.get("target", args["target"])
    body = resp.get("body", "continued")
    if body == "resumed":
        how = "its last turn was interrupted, so it was told to pick that turn up"
    else:
        how = "its last turn had ended, so it was told to carry on"
    return redact(
        f"\u25b6\ufe0f Continue sent to `{target}` ({body}): {how}. "
        "Read the result with session_read_message."
    )


def _run_session_set_model(args: dict[str, Any], ctx: ToolContext) -> str:
    payload = {"target": args["target"]}
    for key in ("model", "reasoning_effort"):
        if key in args:
            payload[key] = args[key]
    try:
        resp = ctx.client.post(
            "/api/session-control/set-model",
            payload,
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not change that session's model: {refused.error}"
    target = resp.get("target", args["target"])
    effort = resp.get("reasoning_effort")
    effort_text = f"reasoning effort `{effort}`"
    if "model" in resp:
        change = f"`{resp.get('model') or 'auto'}`"
        if effort is not None:
            change += f" at {effort_text}"
    else:
        change = effort_text
    return redact(f"\U0001f501 `{target}` will switch to {change} when its next turn starts.")


def _run_session_rename(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/rename",
            {"target": args["target"], "title": args["title"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        resp = refused.body
    if resp.get("error"):
        return redact(f"Error: could not rename that session: {resp['error']}")
    target = resp.get("target", args["target"])
    return redact(f"Renamed `{target}` to {resp.get('title', args['title'])!r}.")


#: The generate-title route waits for one titling-model call, so the client
#: allows longer than the 30-second default. A transport timeout does not undo
#: the call: the title can still land, and the target's one-per-window budget
#: (``create_rate_limit.WINDOW_SECS``, longer than this wait) refuses a retry.
GENERATE_TITLE_TIMEOUT_SECS = 120


def _run_session_generate_title(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/generate-title",
            {"target": args["target"]},
            timeout=GENERATE_TITLE_TIMEOUT_SECS,
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        resp = refused.body
    if resp.get("error"):
        return redact(f"Error: could not generate a title for that session: {resp['error']}")
    target = resp.get("target", args["target"])
    title = resp.get("title", "")
    if resp.get("changed"):
        return redact(f"Retitled `{target}` to {title!r}.")
    return redact(f"`{target}` keeps its title {title!r}; the model offered nothing new.")


def _run_session_reload(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/reload",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        resp = refused.body
    # Read off the code before any error: the process WAS torn down and only the
    # notice was skipped, so "could not reload" would tell the agent nothing
    # happened.
    if resp.get("code") == "target_changed_during_reload":
        return (
            "Warning: the target's agent process was reset and starts again on "
            f"its next message, but no reload notice was added: {resp.get('error', '')}"
        )
    if resp.get("error"):
        return f"Error: could not reload that session: {resp['error']}"
    target = resp.get("target", args["target"])
    if resp.get("warning"):
        return redact(
            f"\U0001f504 `{target}` is relaunching its agent process with the conversation "
            f"kept, but the old process's teardown reported an error ({resp['warning']})."
        )
    return redact(
        f"\U0001f504 `{target}` is relaunching its agent process with the conversation "
        "kept. Its transcript shows the reload notice."
    )


def _run_session_set_color(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/set-color",
            {"target": args["target"], "color": args["color"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not set that session's color: {refused.error}"
    target = resp.get("target", args["target"])
    if resp.get("color_index") is None:
        return redact(f"Cleared the color of `{target}`.")
    return redact(f"Set the color of `{target}` to palette swatch {resp['color_index']}.")


def _run_session_close(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/close",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not close that session: {refused.error}"
    target = resp.get("target", args["target"])
    return (
        f"\U0001f5d1\ufe0f Closed `{target}` — the tab is dismissed and the "
        "conversation archived to history (it can be reopened later)."
    )


def _run_session_revive(args: dict[str, Any], ctx: ToolContext) -> str:
    payload_r: dict[str, Any] = {"target": args["target"]}
    fld_id, folder_label, made_note, fld_error = _resolve_folder_for_new_session(
        ctx, str(args.get("folder") or ""), "filing a revived session"
    )
    if fld_error:
        return fld_error
    if fld_id:
        payload_r["folder_id"] = fld_id
    try:
        resp = ctx.client.post("/api/session-control/revive", payload_r, session_key=ctx.caller_key)
    except DashboardError as refused:
        return redact(f"Error: could not revive that session: {refused.error}{made_note}")
    target = resp.get("target", args["target"])
    filed = f" and filed in `{folder_label}`" if resp.get("filed") and folder_label else ""
    unfiled_note = (
        " (the folder could not be applied; the session keeps its previous placement)"
        if fld_id and not resp.get("filed") and resp.get("folder_id") != fld_id
        else ""
    )
    return redact(
        f"\u267b\ufe0f Revived `{target}` ({resp.get('title')}) with "
        f"{resp.get('messages', 0)} messages{filed}.{unfiled_note}{made_note} It is open and idle "
        "in the user's sidebar; session_send starts its next turn."
    )


def _run_session_history_list(args: dict[str, Any], ctx: ToolContext) -> str:
    limit = int(args.get("limit") or 20)
    query = f"limit={limit}"
    folder_label = ""
    folder_ref = str(args.get("folder") or "").strip()
    if folder_ref:
        # Read-only resolution: a filter must never create the folder it
        # names, and an unknown one is refused rather than widened to "all".
        chat_folders, folders_err = _get_rows(ctx.client, "/api/chat/folders")
        if folders_err:
            return f"Error: {folders_err}"
        fid, folder_err = _resolve_chat_folder_id(folder_ref, chat_folders)
        if folder_err:
            return redact(f"Error: {folder_err}")
        if fid:  # "root" resolves to the top level, which filters nothing
            query += f"&folder_id={quote(fid)}"
            folder_label = _chat_folder_paths(chat_folders).get(fid, fid)
    try:
        resp = ctx.client.get(f"/api/session-control/history?{query}", session_key=ctx.caller_key)
    except DashboardError as refused:
        return redact(f"Error: could not list archived sessions: {refused.error}")
    hist_rows = resp.get("sessions") or []
    where = f" in `{folder_label}`" if folder_label else ""
    notes: list[str] = []
    if resp.get("lineage_unknown"):
        notes.append(
            "Some sessions you created may be missing: the crew log's session-tree "
            "lineage could not vouch for them (log off, or not seeded yet), and "
            "session_revive would refuse them for the same reason."
        )
    more = " (more exist; raise `limit` or narrow by `folder`)" if resp.get("more") else ""
    if not hist_rows:
        # Rows kept by the scan can be revived before the answer is built
        # while older rows remain unread, so an empty page is not an empty
        # archive whenever ``more`` is set.
        if more:
            empty = f"\U0001f5c4\ufe0f No archived sessions on this page{where}{more}."
        else:
            empty = f"\U0001f5c4\ufe0f No archived sessions you can revive{where}."
        return redact(" ".join([empty, *notes]))
    hist_lines = [
        f"\U0001f5c4\ufe0f {len(hist_rows)} archived session(s){where}, newest first{more}:"
    ]
    for row in hist_rows:
        bits = [str(row.get("last_active") or "")]
        if row.get("folder_id") and not folder_label:
            bits.append(f"folder {row['folder_id']}")
        meta = f" — {', '.join(b for b in bits if b)}" if any(bits) else ""
        hist_lines.append(f"  `{row.get('target', '')}` ({row.get('title', '')}){meta}")
    hist_lines.append("Pass a key to session_revive to reopen it.")
    hist_lines.extend(notes)
    return redact("\n".join(hist_lines))


def _run_session_send(args: dict[str, Any], ctx: ToolContext) -> str:
    steer = bool(args.get("steer"))
    try:
        resp = ctx.client.post(
            "/api/session-control/send",
            {"target": args["target"], "message": args["message"], "steer": steer},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not send to that session: {refused.error}"
    target = resp.get("target", args["target"])
    caps = redact(_worker_capabilities_line(resp))
    if resp.get("steered"):
        return (
            f"\U0001f4e8 Steered `{target}` — your message went into the turn it "
            "is running, so it reads it mid-work. Watch what it does with it "
            "with session_read_message." + caps
        )
    if resp.get("started"):
        return (
            f"\U0001f4e8 Delivered to `{target}` — it started a turn on your message. "
            "Watch the result with session_read_message." + caps
        )
    # A steer that could not be injected lands here, on the queue: say so, or
    # the caller reads "queued" as "the target was busy" and never learns its
    # steer did not cut anything.
    queued_note = (
        " Your steer could not go into the running turn, so it was queued instead." if steer else ""
    )
    return (
        f"\U0001f4e8 Queued for `{target}` — it is mid-turn, so your message runs "
        f"when the current turn ends.{queued_note} Poll with session_read_message." + caps
    )


def _run_session_broadcast(args: dict[str, Any], ctx: ToolContext) -> str:
    payload = {"message": args["message"], "mode": args["mode"]}
    # A call that names no audience is refused HERE, before any request. Kiro CLI
    # drops an empty `targets: []` from the call, so "no audience" reaches this
    # function as a missing key, and a bare body is the one thing that must never be
    # sent: a gateway older than this tool reads a body without `targets` as every
    # session the caller created. The backend refuses the same call, so a new
    # gateway gives the same answer; this check only covers the older one.
    if args.get("targets") is None and args.get("all") is not True:
        return (
            "Error: could not broadcast: no audience selected: name the sessions in "
            "targets, or pass all: true to reach every session you created "
            "(target_required). Nothing was sent."
        )
    # The VALUE decides, not the key: the validator keeps an explicit JSON
    # `null` as the field's `None` default, so a presence test would send
    # `None` into `list()`. A list is forwarded as given, an empty one included:
    # a gateway older than this tool refuses an explicit empty list but reads a
    # bare body as "every session you created", so dropping `[]` here would turn
    # an empty audience into a fleet-wide send during a mixed-version rollout.
    if args.get("targets") is not None:
        payload["targets"] = list(args["targets"])
    if args.get("all") is True:
        payload["all"] = True
    try:
        resp = ctx.client.post(
            "/api/session-control/broadcast",
            payload,
            # The backend delivers SEQUENTIALLY, so one request covers up to
            # `MAX_BROADCAST_TARGETS` deliveries and the 30-second default would
            # expire mid-fleet: the client then reports a failure for a broadcast
            # the server went on to deliver, discarding the per-target report that
            # is this verb's whole contract, and the caller's natural retry
            # delivers the whole message twice.
            #
            # The same per-target allowance the backend ENFORCES per delivery, read
            # from one name so the two cannot drift. Multiplied by the cap because
            # the deliveries are sequential, then given the shared response margin:
            # the client budget must EXCEED the backend's worst-case delivery time,
            # never merely equal it, or the per-target report can still be lost.
            timeout=(
                MAX_BROADCAST_TARGETS * BROADCAST_TARGET_ALLOWANCE_SECS
                + BROADCAST_RESPONSE_MARGIN_SECS
            ),
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not broadcast: {refused.error}"
    rows = resp.get("results") or []
    requested = int(resp.get("requested", len(rows)) or 0)
    delivered = int(resp.get("delivered", 0) or 0)
    mode = str(resp.get("mode", args["mode"]))
    if resp.get("audience_empty"):
        # Not an error, and said plainly: a conductor before its first dispatch
        # is in this state, and "delivered to 0 of 0" reads like a failure.
        return (
            "\U0001f4e3 Nothing to broadcast to — you have not created any "
            "session that is still open. Name `targets` to reach a session you "
            "did not create, or open one with session_create."
        )
    verb = "Steered" if mode == "steer" else "Queued for"
    lines = [f"\U0001f4e3 {verb} {delivered}/{requested} session(s):"]
    for row in rows:
        target = str(row.get("target", ""))
        if not row.get("ok"):
            lines.append(
                f"  \u274c `{target}` — {row.get('error', 'refused')} "
                f"({row.get('code', 'unknown')})"
            )
        elif row.get("steered"):
            lines.append(f"  \u2705 `{target}` — cut into its running turn")
        elif row.get("started"):
            lines.append(f"  \u2705 `{target}` — started a turn on it")
        else:
            # A steer that could not be injected lands here, for the reason
            # session_send spells out: reporting a plain queue would leave the
            # caller believing that target was interrupted.
            fell_back = " (steer fell back to the queue)" if mode == "steer" else ""
            lines.append(f"  \u2705 `{target}` — queued until its turn ends{fell_back}")
    if delivered < requested:
        lines.append(
            "Some targets were not reached — the rows above say which and why. "
            "Nothing retries them for you."
        )
    return redact("\n".join(lines))


#: Seconds the client gives ONE ``session_status`` read. Its GET does real work: the
#: route scans the transcript catalog and folds each ended child's crew log on the
#: gateway's shared default executor, where a busy gateway makes every hop wait its
#: turn. ``mcp_core._get``'s 10 s default is sized for a telemetry read and its
#: docstring asks a route like this one to raise it. The value is the write path's:
#: ``session_send`` reaches the same gateway through ``mcp_core._post``, whose default
#: is 30 s, so a patrol's roster read is not the call that fails first while its sends
#: still land.
SESSION_STATUS_READ_TIMEOUT_SECS = 30.0


def _run_session_status(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.get(
            "/api/session-control/status",
            session_key=ctx.caller_key,
            timeout=SESSION_STATUS_READ_TIMEOUT_SECS,
        )
    except DashboardError as refused:
        return f"Error: could not read your session roster: {refused.error}"
    rows = resp.get("sessions") or []
    tree = str(resp.get("tree", "unreadable"))
    history = str(resp.get("history", "readable"))
    quality_notes: list[str] = []
    if tree == "incomplete":
        quality_notes.append(
            "The crew-log roster read was INCOMPLETE, so this count is a floor: "
            "a session you created may be missing from it."
        )
    elif tree == "unreadable":
        quality_notes.append(
            "The crew-log roster was unreadable (crew log off, or not seeded "
            "yet), so this lists only sessions that are still open — a worker "
            "that was lost would not appear."
        )
    if history == "incomplete":
        quality_notes.append(
            "The transcript-metadata roster read was INCOMPLETE, so a session "
            "created before its first crew-log edge may be missing."
        )
    elif history == "unreadable":
        quality_notes.append(
            "The transcript-metadata roster was unreadable, so archived birth "
            "records could not complete this answer."
        )
    # The union cut, caveated under its OWN name. Three states, not two: the
    # field ABSENT (an older backend that does not compute it) says nothing and
    # must not claim a cut, zero says the union was retained whole, and a
    # published value that will not read as a number is a cut whose size is
    # unknown -- which is not the same as no cut and gets its own sentence.
    omitted_raw = resp.get("roster_omitted")
    if omitted_raw is not None:
        try:
            roster_cut = int(omitted_raw)
        except (TypeError, ValueError):
            roster_cut = -1
        if roster_cut > 0:
            quality_notes.append(
                f"{roster_cut} more session(s) you created are NOT listed: the "
                "three rosters' union exceeded this reply's row bound. Name a "
                "session directly to read it."
            )
        elif roster_cut < 0:
            quality_notes.append(
                "Sessions you created may be missing: this reply reported a "
                "roster overflow without a readable count."
            )
    if not rows:
        empty = "\U0001f4cb You have no sessions open or on record."
        if quality_notes:
            empty += " " + " ".join(quality_notes)
        return empty
    status_lines = [f"\U0001f4cb {len(rows)} session(s) you stood up:"]
    for row in rows:
        target = str(row.get("target", ""))
        status = str(row.get("status", ""))
        if status == "closed":
            closed_at = row.get("closed_at")
            when = f" at {_closed_at_display(closed_at)}" if closed_at else ""
            status_lines.append(
                f"  \u2705 `{target}` — closed (finished and closed its tab{when}; "
                "nothing to re-dispatch)"
            )
            continue
        if status == "lost":
            status_lines.append(
                f"  \U0001faa6 `{target}` — lost (the crew log has it, the "
                "dashboard does not, and it did not close cleanly — a turn was "
                "open, a recycle or destroy tore it down, or the lifecycle could "
                "not be read: re-dispatch it)"
            )
            continue
        title = str(row.get("title", ""))
        depth = int(row.get("queue_depth", 0) or 0)
        queued = f", {depth} queued" if depth else ""
        if status == "unknown":
            # Its OWN mark, not the glyph map's default. Falling through to that
            # default draws idle's sleep glyph -- "open and doing nothing", the
            # one status that means no decision is needed -- for a row whose
            # actual meaning is the opposite: nothing here knows whether this
            # session finished or was lost.
            status_lines.append(
                f"  \u2753 `{target}` ({title}) — unknown (you created it; "
                "neither a live session nor the crew log accounts for it)"
            )
            continue
        mark = {"working": "\U0001f503", "queued": "\u23f8\ufe0f"}.get(status, "\U0001f4a4")
        if row.get("pending_approval"):
            status_lines.append(
                f"  \u270b `{target}` ({title}) — {status}, "
                f"{_pending_approval_phrase(row)}{queued}"
            )
            continue
        status_lines.append(f"  {mark} `{target}` ({title}) — {status}{queued}")
    status_lines.extend(quality_notes)
    return redact("\n".join(status_lines))


def _run_session_handoff(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/handoff",
            {"target": args["target"], "to": args["to"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not hand that session over: {refused.error}"
    target = resp.get("target", args["target"])
    manager = resp.get("manager", args["to"])
    return (
        f"\U0001f91d Handed `{target}` to `{manager}`. That session manages it now: it "
        "can send to, read, stop and close it. If you are a fenced session you lose "
        "that reach, unless agent.session_manager_chain is on and you still manage "
        "that session. It cannot hand the session back to you. Who created it is "
        "unchanged."
    )


def _run_session_adopt(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/adopt",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not adopt that session: {refused.error}"
    target = resp.get("target", args["target"])
    previous = resp.get("previous_parent") or ""
    took_over = (
        f" It was under `{previous}` before, and that is recorded."
        if previous
        else " It was a root before."
    )
    return (
        f"\U0001f91d Adopted `{target}` — the sidebar now nests it under this "
        f"session, along with anything it opened.{took_over}"
    )


def _run_session_release(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.post(
            "/api/session-control/release",
            {"target": args["target"]},
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not release that session: {refused.error}"
    target = resp.get("target", args["target"])
    previous = resp.get("previous_parent") or ""
    return (
        f"\U0001f513 Released `{target}` from `{previous}` — it stands on its own "
        "in the sidebar again, keeping whatever it opened under itself."
    )


def _pending_approval_phrase(resp: dict[str, Any]) -> str:
    """The state words for a session parked on a tool approval."""
    tool = str(resp.get("pending_approval_tool") or "")
    return "waiting on a tool approval" + (f": {redact(tool)}" if tool else "")


def _run_session_read_message(args: dict[str, Any], ctx: ToolContext) -> str:
    query = f"target={quote(str(args['target']))}&limit={args.get('limit', 20)}"
    if args.get("since") is not None:
        query += f"&since={int(args['since'])}"
    try:
        resp = ctx.client.get(f"/api/session-control/read?{query}", session_key=ctx.caller_key)
    except DashboardError as refused:
        return f"Error: could not read that session: {refused.error}"
    msg_rows = resp.get("messages") or []
    state_line = "still working" if resp.get("running") else "idle"
    if resp.get("pending_approval"):
        state_line = _pending_approval_phrase(resp)
    queued = resp.get("queue_depth", 0)
    if queued:
        state_line += f", {queued} message(s) queued"
    if resp.get("model"):
        state_line += f", model {redact(str(resp['model']))}"
    if resp.get("pending_model"):
        state_line += f", pending model {redact(str(resp['pending_model']))} for its next turn"
    if resp.get("reasoning_effort"):
        state_line += f", reasoning effort {redact(str(resp['reasoning_effort']))}"
    if resp.get("pending_reasoning_effort"):
        pending_effort = redact(str(resp["pending_reasoning_effort"]))
        state_line += f", pending reasoning effort {pending_effort} for its next turn"
    head_line = (
        f"\U0001f4d6 `{resp.get('target', '')}` — {resp.get('title', '')} "
        f"({state_line}; total={resp.get('total', 0)})"
    )
    if not msg_rows:
        # The cursor rides along even with nothing to show. A poll loop's most
        # common answer is an empty window, and a caller left without a
        # position either re-reads without `since` -- taking the tail, which
        # silently skips everything older than the last `limit` rows once the
        # target answers in a burst -- or keeps reusing a cursor from before,
        # re-reading rows it has already seen. The server now returns
        # `next_since` on trimmed sessions too (positions are based on a
        # durable-only prefix count), so its absence is only a defensive
        # possibility here, not a live server state.
        empty_lines = [head_line, "No messages in that window yet."]
        if "next_since" in resp:
            empty_lines.append(
                f"Pass since={resp['next_since']} on your next read to resume from here."
            )
        return "\n".join(empty_lines)
    read_lines = [head_line]
    for row in msg_rows:
        text_body = str(row.get("content", ""))
        if row.get("truncated"):
            text_body += " …[truncated]"
        read_lines.append(f"[{row.get('index')}] {row.get('role')}: {text_body}")
    read_lines.append(
        (
            f"Pass since={resp['next_since']} on your next read to see only what "
            f"is new. (total={resp.get('total', 0)} is the backlog depth — when it "
            f"exceeds next_since there are older rows this window did not reach, so "
            f"read again immediately rather than waiting.)"
        )
        if "next_since" in resp
        else (
            "No cursor came back with this read. "
            "Read again without `since` to get the latest messages."
        )
    )
    return "\n".join(read_lines)


def _run_session_summary(args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        resp = ctx.client.get(
            f"/api/session-control/summary?target={quote(str(args['target']))}",
            session_key=ctx.caller_key,
        )
    except DashboardError as refused:
        return f"Error: could not read that session's summary: {refused.error}"
    return _render_session_summary(resp)


#: The ``kirocrew-work`` route ``work_dispatch`` writes the ledger through, as the caller.
_WORK_RECORD_ROUTE = "/api/work-ledger/record"


def _dispatch_outcome(refused: DashboardError) -> tuple[str, str]:
    """``(outcome, code)`` of a failed step: a lost reply is never a refusal."""
    if refused.body.get("transport_error"):
        return "outcome_unknown", ""
    return "refused", refused.code


def _dispatch_cleanup(ctx: ToolContext, step: str, item_id: str, target: str) -> list[str]:
    """Close the session, then the item, best effort; one line per action.

    A refused cleanup is reported with the id that survived, never retried.
    """
    lines: list[str] = []
    actions: list[tuple[str, str, dict[str, Any]]] = []
    if target:
        actions.append(
            (f"close session `{target}`", "/api/session-control/close", {"target": target})
        )
    if item_id:
        decision = f"work_dispatch failed at step {step}; nothing was sent."
        body = {"action": "close", "item_id": item_id, "state": "abandoned", "decision": decision}
        actions.append((f"close item `{item_id}` as abandoned", _WORK_RECORD_ROUTE, body))
    for label, route, body in actions:
        try:
            ctx.client.post(route, body, session_key=ctx.caller_key)
        except DashboardError as refused:
            outcome, code = _dispatch_outcome(refused)
            lines.append(f"cleanup: {label}: {outcome} (code={code or 'none'}): {refused.error}")
            continue
        lines.append(f"cleanup: {label}: done")
    return lines


def _dispatch_failed(
    step: str,
    outcome: str,
    code: str,
    error: object,
    ids: tuple[str, str],
    tail: list[str],
) -> str:
    """The reply for a dispatch that stopped at *step*."""
    item_id, target = ids
    head = (
        f"Error: work_dispatch stopped at step={step} outcome={outcome} "
        f"code={code or 'none'}: {error}"
    )
    held = f"item_id={item_id or '(none)'} worker_session_key={target or '(none)'}"
    return redact("\n".join([head, held, *tail]))


def _run_work_dispatch(args: dict[str, Any], ctx: ToolContext) -> str:
    """Create, open, bind and seed one work item, in that order (rfc-work-dispatch.md).

    Each step is the route the four-call path uses, called as the caller. The seed
    goes only to the key step 2 returned, and only after ``bind`` succeeded.
    """
    not_sent = "seed: not sent"
    look = (
        "seed: not sent. Read your board (work_ledger_read) and sidebar (session_status) "
        "for anything this step may have made before dispatching again."
    )

    # Step 1: create the item.
    # Marked, so the record route refuses ``no_ledger`` instead of opening one: the
    # route, not this tool, decides who may dispatch (rfc-work-dispatch.md,
    # record-route amendment).
    create = {
        "action": "create",
        "title": args["title"],
        "acceptance": args["acceptance"],
        "dispatch": True,
    }
    try:
        item = ctx.client.post(_WORK_RECORD_ROUTE, create, session_key=ctx.caller_key)
    except DashboardError as refused:
        outcome, code = _dispatch_outcome(refused)
        tail = [look if outcome == "outcome_unknown" else not_sent]
        return _dispatch_failed("create", outcome, code, refused.error, ("", ""), tail)
    item_id = str((item.get("item") or {}).get("item_id") or "")
    if not item_id:
        error = "the reply named no item_id"
        return _dispatch_failed("create", "outcome_unknown", "", error, ("", ""), [look])

    # Step 2: open the session, filed in its folder.
    opened = _create_session(
        {
            "title": args.get("session_title") or args["title"],
            **{
                k: args[k]
                for k in ("agent", "agent_kind", "folder", "model", "reasoning_effort")
                if args.get(k)
            },
        },
        ctx,
        keep_walk=True,
    )
    target = str((opened.resp or {}).get("target") or "")
    if not target:
        outcome, code = ("outcome_unknown", "") if opened.resp is not None else ("refused", "")
        stopped = opened.refused or opened.walk_refused
        if stopped is not None:
            outcome, code = _dispatch_outcome(stopped)
        error = opened.error.removeprefix("Error: ") or "the reply named no session key"
        cleanup = _dispatch_cleanup(ctx, "session_create", item_id, "")
        tail = [look if outcome == "outcome_unknown" else not_sent, *cleanup]
        return _dispatch_failed("session_create", outcome, code, error, (item_id, ""), tail)

    # Step 3: bind the item to the key step 2 returned.
    bind = {"action": "bind", "item_id": item_id, "worker_session_key": target}
    try:
        bound = ctx.client.post(_WORK_RECORD_ROUTE, bind, session_key=ctx.caller_key)
    except DashboardError as refused:
        outcome, code = _dispatch_outcome(refused)
        cleanup = _dispatch_cleanup(ctx, "bind", item_id, target)
        tail = [not_sent, *cleanup]
        return _dispatch_failed("bind", outcome, code, refused.error, (item_id, target), tail)

    # Step 4: seed. Only reached once the bind succeeded.
    seed = {"target": target, "message": args["seed"], "steer": False}
    try:
        sent = ctx.client.post("/api/session-control/send", seed, session_key=ctx.caller_key)
    except DashboardError as refused:
        outcome, code = _dispatch_outcome(refused)
        tail = ["The item is bound and the session exists; nothing was cleaned up."]
        if outcome == "outcome_unknown":
            tail.append(
                "Check the session with session_read_message before resending: a blind "
                "resend can seed it twice."
            )
        else:
            tail.append("Seed it with session_send once the refusal is resolved.")
        return _dispatch_failed("seed", outcome, code, refused.error, (item_id, target), tail)
    delivery = "started a turn" if sent.get("started") else "queued"
    created = opened.resp or {}
    crew_mode = bool(created.get("crew_mode") or created.get("folder_ignored"))
    filed = f" filed in `{opened.folder_label}`" if opened.folder_label and not crew_mode else ""
    return redact(
        f"\U0001f4cc Dispatched item `{item_id}` to `{target}`{filed}: created, opened, "
        f"bound, and seeded ({delivery}).{opened.made_note}\n"
        f"item_id={item_id} worker_session_key={target}"
        + reply_line(bound)
        + _worker_capabilities_line(opened.resp or {})
        + _worker_capabilities_line(sent)
    )
