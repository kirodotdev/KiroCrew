"""The session-control rows of the ``kirocrew-dashboard`` tool table.

``_session_tools`` returns one ``Tool`` per session-control verb (the board's
``session_move_to_column`` row is in ``_board_tools``): the description the agent reads,
its argument schema, the identity it requires and the routes it reaches, each row pointing
at its body in :mod:`kiro_crew.mcp_dashboard_session_runs`. A new session verb is
one more row here, a body there, and its schema in ``MCP_DASHBOARD_SCHEMAS``;
:mod:`kiro_crew.mcp_dashboard` assembles these rows into ``TABLE`` after the folder, tag
and board rows, and binds ``_session_tools`` so it reads and patches as
``kiro_crew.mcp_dashboard._session_tools``.
"""

from __future__ import annotations

from kiro_crew.config.sections import AUTOCOMPACT_PCT_MAX, AUTOCOMPACT_PCT_MIN
from kiro_crew.mcp_dashboard_session_runs import (
    _run_session_adopt,
    _run_session_broadcast,
    _run_session_close,
    _run_session_continue,
    _run_session_create,
    _run_session_end_wait,
    _run_session_fork,
    _run_session_generate_title,
    _run_session_handoff,
    _run_session_history_list,
    _run_session_read_message,
    _run_session_release,
    _run_session_reload,
    _run_session_rename,
    _run_session_retry,
    _run_session_revive,
    _run_session_send,
    _run_session_set_color,
    _run_session_set_model,
    _run_session_status,
    _run_session_stop,
    _run_session_summary,
    _run_work_dispatch,
)
from kiro_crew.mcp_tools.table import Tool


def _session_tools() -> tuple[Tool, ...]:
    """Session control: open, fork, steer, stop and read other sessions."""
    return (
        Tool(
            name="session_create",
            description=(
                "Open a NEW chat session, pre-named and bound to the agent you pick, so a "
                "separate workstream has a home of its own. The new session appears in "
                "the user's sidebar like any other — they can read it, take it over and "
                "close it — so use it to stand up a workstream alongside this one (watch a "
                "build, grind a long refactor) rather than to hide work. It starts empty: "
                "the person is the one who types the first message into it. Returns its "
                "key; pass that as `target` to the other session tools. To open a session "
                "that already CARRIES a transcript (splitting your own investigation into "
                "several sessions), use session_fork instead."
            ),
            schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "Short name for the session, shown in the sidebar. Say what the "
                            "session is FOR so the user can tell your sessions apart."
                        ),
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "Agent to bind the session to. Omitting it inherits the "
                            "CALLER'S OWN agent, not a global default: create_session "
                            "falls back to the calling slot's agent so the child stays in "
                            "this workspace's memory boundary. A conductor that omits it "
                            "therefore gets a second conductor, which has no fs_write and "
                            "cannot do the work. Name the agent the child needs "
                            "explicitly \u2014 kirocrew-worker for a leaf work item. "
                            "In a crewmate's own chat this field is not used: the "
                            "session is always that crewmate's own worker."
                        ),
                    },
                    "agent_kind": {
                        "type": "string",
                        "enum": ["template"],
                        "description": (
                            "Pass 'template' to run the shared template `agent` names "
                            "on YOUR OWN memory, even when a crew member carries the "
                            "same name. Omit for the usual name-first resolution (a "
                            "member wins, with its own memory). Always pass 'template' "
                            "for kirocrew-worker: a worker needs no memory of its own, "
                            "and on installs where an older agent sync enrolled a crew "
                            "member of that name the bare name selects that member and "
                            "is refused `memory_delegation_denied`. Requires `agent`."
                        ),
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Sidebar folder to file the new session into, atomically with "
                            "creation — a folder id or a '/'-separated human path. Missing "
                            "path segments are created (mkdir -p), like chat_folder_create's "
                            "`parent`. Omit to leave the session at the top level."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Model the new session starts on, pinned exactly as if the "
                            "person had picked it in the session's model dropdown: an id "
                            "from that same list, or 'auto'. If omitted: when YOU are a "
                            "crewmate, the child gets a copy of your profile's worker model "
                            "(or your own model when none is set), made at creation, so a "
                            "later profile change does not reach it; otherwise it uses the "
                            "agent's or the global default. A well-formed id the account "
                            "cannot serve is accepted here but withheld at the session's "
                            "first turn: the turn succeeds on the account's default model, "
                            "and the only signal is a notice in that session's transcript. "
                            "The person can still change it later."
                        ),
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": (
                            "Reasoning effort the new session starts at, one of the five "
                            "standard levels: 'low', 'medium', 'high', 'xhigh', 'max'. "
                            "If omitted, it is filled the same way as `model`: a crewmate's "
                            "child gets a copy of the profile's worker effort (or the "
                            "crewmate's own), made at creation; anyone else's child keeps "
                            "the model's default."
                        ),
                    },
                    "autocompact_pct": {
                        "type": "number",
                        "minimum": AUTOCOMPACT_PCT_MIN,
                        "maximum": AUTOCOMPACT_PCT_MAX,
                        "description": (
                            "Context-usage percent at which the NEW session auto-compacts, "
                            "set as that session's own override exactly as if the person "
                            "had set 'Auto-compact at' in its context meter. Only the new "
                            "session gets it: the global session.autocompact_pct and every "
                            "other session, including yours, are unchanged. Omit to follow "
                            "the agent's own default, else the global value. The person can "
                            "still change or reset it later."
                        ),
                    },
                },
                "required": [],
            },
            run=_run_session_create,
            identity="strict",
            routes=(
                "POST /api/session-control/create",
                "GET /api/chat/slots",
                "GET /api/chat/folders",
                "POST /api/chat/folders",
            ),
        ),
        Tool(
            name="session_fork",
            description=(
                "Open a NEW chat session that CARRIES the transcript of an existing one — "
                "the same thing as the dashboard's Fork button. The child holds a copy of "
                "the source's messages up to and including the fork point (the whole "
                "transcript by default), so it starts with the context already built "
                "instead of empty; use it to split a long investigation into several "
                "sessions that each know what was found so far. By default the source is "
                "YOUR OWN session. The child inherits the source's agent, model, memory "
                "store and mode, project and folder exactly as a human fork does — there "
                "is deliberately no agent, model or mode override here (a fork must stay "
                "inside the memory boundary its transcript came from); to open a session "
                "bound to a different agent use session_create. It starts IDLE: the copied "
                "messages are history, and no turn runs until you session_send into it or "
                "the person types. It appears in the user's sidebar like any other session, "
                "for them to read, take over and close. Returns its key; pass that as "
                "`target` to the other session tools."
            ),
            schema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": (
                            "The session to copy from: a session key from list_sessions, a "
                            "slot key, or its exact unique title. Omit to fork YOUR OWN "
                            "session. Forking another session requires the same "
                            "authorization as reading it with session_read_message."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "Short name for the new session, shown in the sidebar. Omit to "
                            "start from `Fork of <source title>`, which is then renamed "
                            "automatically after the latest question the copy holds."
                        ),
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Sidebar folder to file the new session into — a folder id or a "
                            "'/'-separated human path, created if missing (mkdir -p), the same "
                            "as session_create's `folder`. Omit to leave it in the source's "
                            "folder."
                        ),
                    },
                    "at_message_index": {
                        "type": "integer",
                        "minimum": 0,
                        "description": (
                            "Fork point: the position of the LAST message to carry, counting "
                            "the source's user and assistant messages from 0 (system and "
                            "tool rows are not counted). Messages after it are left behind. "
                            "Omit to carry the whole transcript."
                        ),
                    },
                },
                "required": [],
            },
            run=_run_session_fork,
            identity="strict",
            routes=(
                "POST /api/session-control/fork",
                "GET /api/chat/slots",
                "GET /api/chat/folders",
                "POST /api/chat/folders",
            ),
        ),
        Tool(
            name="session_stop",
            description=(
                "Stop another session's in-flight turn — the same thing as pressing Stop "
                "in that tab. Use it when a peer session is working on something you now "
                "know is wrong or already done, and letting it finish would waste the run "
                "or make a conflicting change. The stop is cooperative and safe to "
                "re-send: a repeat within two minutes reports that the earlier stop is "
                "still in progress instead of escalating to a hard kill, so retrying a "
                "call that timed out cannot discard the target's queued messages. "
                "A stop card appears in the "
                "target's transcript so the person reading it sees what happened. Stopping "
                "discards the turn's work, so read the session first when you are not sure "
                "what it is doing."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_stop,
            identity="strict",
            routes=("POST /api/session-control/stop",),
        ),
        Tool(
            name="session_end_wait",
            description=(
                "Wake a session you created that is sleeping in the `wait` tool, "
                "without cancelling its turn: the same thing as pressing End wait on "
                "its countdown. The target's wait returns a normal result that names "
                "your session as the one that ended it, and its turn carries on. Use "
                "it when the thing a worker is waiting for has already happened. "
                "Only sessions you created are reachable. "
                "Unlike session_stop nothing is discarded, and unlike session_send "
                "no text reaches the target. A target that is not waiting gets an "
                "informational reply, not an error, so there is nothing to retry. "
                "The wake lands on the target's next keepalive ping, within about "
                "five seconds."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_end_wait,
            identity="strict",
            routes=("POST /api/session-control/end-wait",),
        ),
        Tool(
            name="session_retry",
            description=(
                "Re-run another session's last turn when it FAILED: it ended in an "
                "error (for example 'Request initialize timed out' at start) or with no "
                "reply. Does the same thing as pressing Resume in that tab: the session "
                "picks up its most recent request, and no second copy of the prompt is "
                "added. Use this instead of re-sending the prompt with session_send. "
                "Refused while the target is running, busy or has queued messages, and "
                "refused when its last turn finished normally or was stopped, so it "
                "cannot regenerate a good answer. After two failed starts in a row it is "
                "refused with session_start_repeat: the host needs attention, not a "
                "third retry."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_retry,
            identity="strict",
            routes=("POST /api/session-control/retry",),
        ),
        Tool(
            name="session_continue",
            description=(
                "Hand another session's thread back to its agent, the same as pressing "
                "Continue in that tab. Use it when a turn was cut short by a gateway "
                "restart or a killed process: such a turn leaves no error row and looks "
                "finished, so session_retry refuses it. The session is sent one of two "
                "fixed messages and the result says which: 'resumed' (its last turn was "
                "interrupted, pick it up) or 'continued' (its last turn ended cleanly, "
                "carry on). Takes no prompt text; to ask for something new use "
                "session_send. Refused while the target is running, busy or has queued "
                "messages, and after two failed session starts in a row "
                "(session_start_repeat)."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_continue,
            identity="strict",
            routes=("POST /api/session-control/continue",),
        ),
        Tool(
            name="session_set_model",
            description=(
                "Change the model and/or reasoning effort another session runs on; "
                "pass either or both. Only an IDLE session takes the change: if the "
                "target has a turn or sub-agents in flight the call fails with "
                "'session busy, model not changed' and nothing changes. To force it, "
                "stop the target with session_stop first, then retry. The change is "
                "applied when the target's next turn starts, after the same permission "
                "check runs again; if the target has become channel-linked or otherwise "
                "out of reach by then, the change is dropped. A model or effort the "
                "user picks in the meantime wins. The conversation is kept. 'auto', "
                "'Auto (Jev)' and sessions bound to a remote crew are refused."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Model to switch to: a canonical key or provider id, e.g. "
                            "'sonnet' or 'opus'. 'auto' is owner-only. Omit it to keep "
                            "the target's model and change only reasoning_effort."
                        ),
                    },
                    "reasoning_effort": {
                        "type": "string",
                        "description": (
                            "Reasoning effort for the target, one of the five standard "
                            "levels: 'low', 'medium', 'high', 'xhigh', 'max'. Omit it to "
                            "keep the target's current level. Other levels, and going "
                            "back to the model's default, are set from the dropdown."
                        ),
                    },
                },
                "required": ["target"],
            },
            run=_run_session_set_model,
            identity="strict",
            routes=("POST /api/session-control/set-model",),
        ),
        Tool(
            name="session_reload",
            description=(
                "Relaunch the agent process of a session you created, the same thing as "
                "Reload session in that tab's menu. Use it after a change that a running "
                "session cannot see: a newly granted or enabled MCP server, an MCP "
                "config edit, or an agent-spec fix. The new process re-reads its agent "
                "spec, environment and MCP servers; the conversation is kept, and the "
                "target's transcript shows a notice naming your session. Only an IDLE "
                "session can be reloaded: a running or starting turn, queued messages "
                "or attached sub-agents fail with 'session busy, not reloaded' and "
                "nothing changes. You cannot reload yourself, and the agent, model and "
                "workspace stay as they are."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_reload,
            identity="strict",
            routes=("POST /api/session-control/reload",),
        ),
        Tool(
            name="session_set_color",
            description=(
                "Set the sidebar color of a session you created, so a "
                'conductor can group its workers at a glance. ``color`` is one of: "0" to '
                '"6", the seven palette swatches the sidebar color menu shows (they follow '
                'the viewer\'s theme), or "none" to clear the color. Custom hex colors and '
                "anything else are refused. Metadata only: the transcript, model and any "
                "running turn are untouched. This session itself and sessions you did not "
                "create are refused, including the person's own tabs."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Session key from list_sessions or session_status, or its "
                            "exact title."
                        ),
                    },
                    "color": {
                        "type": "string",
                        "description": '"0"-"6" palette swatch, or "none" to clear.',
                    },
                },
                "required": ["target", "color"],
            },
            run=_run_session_set_color,
            identity="strict",
            routes=("POST /api/session-control/set-color",),
        ),
        Tool(
            name="session_rename",
            description=(
                "Rename a live session's sidebar title: your own session, or one you "
                "may control with session_stop. A conductor uses it to relabel the "
                "workers it created as their work changes. The new title is final, the "
                "same as a manual rename: the automatic titler stops refreshing that "
                "session. Metadata only; the transcript, model and any running turn "
                "are untouched. A crew member or an agent-created session may rename "
                "only itself and sessions it created; a scheduled run may rename only "
                "sessions it created, never its own session. ARCHIVED (history) "
                "sessions are not addressable."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Session key from list_sessions, 'dashboard:<slot>' key, or "
                            "its exact unique title."
                        ),
                    },
                    "title": {
                        "type": "string",
                        "description": (
                            "New title, 1-200 characters after surrounding whitespace is "
                            "stripped; no control characters."
                        ),
                    },
                },
                "required": ["target", "title"],
            },
            run=_run_session_rename,
            identity="strict",
            routes=("POST /api/session-control/rename",),
        ),
        Tool(
            name="session_generate_title",
            description=(
                "Regenerate the sidebar title of a live session YOU created, from its "
                "recent transcript, the same as the sidebar's 'Regenerate title' action. "
                "Costs one model call on the titling model and waits for it, so the "
                "reply carries the new title (or the unchanged one when the model had "
                "nothing better). The new title replaces the current one, including a "
                "title someone set by hand, and the automatic titler may revise it "
                "later. Only sessions this session created are reachable, never your "
                "own. One call per session every 5 minutes; a repeat inside that window "
                "is refused. To set an exact title instead, use session_rename."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_generate_title,
            identity="strict",
            routes=("POST /api/session-control/generate-title",),
        ),
        Tool(
            name="session_close",
            description=(
                "Close another session — the same thing as pressing the ✕ on that tab. "
                "The conversation is archived to history (session_revive brings it back); "
                "this is NOT a permanent delete, but it does dismiss the live tab and, "
                "if the target is mid-turn, cancels that turn first and discards its "
                "work. Use it to tidy up a peer session you created and are done with "
                "(a finished watcher, a workstream you handed off), not to interrupt one "
                "you might still need — for that, session_stop only cancels the turn and "
                "leaves the tab open. Read the session first when you are unsure what it "
                "is doing. You may also pass your OWN session key to close yourself once "
                "your one job is finished; that cancels this turn, so make it your last "
                "call and write anything the person should read before it."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_close,
            identity="strict",
            routes=("POST /api/session-control/close",),
        ),
        Tool(
            name="session_revive",
            description=(
                "Bring an ARCHIVED (history) session back into the live sidebar — the "
                "mirror of session_close. The conversation reopens as a live tab with "
                "its full transcript, the same thing as clicking it in the History tab; "
                "nothing runs until someone sends it a message. Use it when a closed "
                "session is the right home for new work (an investigation to continue, "
                "a session to tag or file), then address it with the returned key: "
                "session_send, session_read_message, chat_folder_move_session and "
                "chat_tag_assign all work on it afterwards. A session that is already "
                "open is refused with its live key — just use that. Only dashboard "
                "sessions in the caller's own workspace are addressable; a crew member "
                "or agent-created session may revive only a session it created itself, verified against "
                "the gateway's crew-log lineage (on by default; refused ownership_unverified "
                "when the log is off, unseeded, or predates the session)."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "The archived session: its slot key (``chat-7-...``), its "
                            "'dashboard:<slot>' session key, the transcript name "
                            "list_sessions reports, or its exact title when that title "
                            "is unique among archived sessions."
                        ),
                    },
                    "folder": {
                        "type": "string",
                        "description": (
                            "Sidebar folder to file the revived session into once it is live "
                            "— a folder id or a '/'-separated human path; missing path "
                            "segments are created (mkdir -p), like session_create's `folder`. "
                            "Filing is best-effort AFTER the revive has landed: the result's "
                            "`filed` says whether it happened, and a revive whose filing "
                            "failed still succeeds (the session is live, unfiled). Omit to "
                            "leave it where it was."
                        ),
                    },
                },
                "required": ["target"],
            },
            run=_run_session_revive,
            identity="strict",
            routes=(
                "POST /api/session-control/revive",
                "GET /api/chat/slots",
                "GET /api/chat/folders",
                "POST /api/chat/folders",
            ),
        ),
        Tool(
            name="session_history_list",
            description=(
                "List ARCHIVED (history) sessions the caller could bring back with "
                "session_revive, newest first: one row per session with the key "
                "session_revive takes, its title, when it was last active, and the "
                "folder it is filed in. Read-only. Use it to find the target before "
                "reviving, e.g. when asked to reopen last week's investigation in a "
                "folder. Only sessions session_revive would accept for this caller "
                "are listed: same workspace, not incognito or temporary, not "
                "app-scoped or channel-linked; a crew member or agent-created "
                "session sees only sessions it created itself (the same crew-log "
                "lineage check session_revive applies). Live sessions are not here; "
                "chat_folder_tree lists those."
            ),
            schema={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": (
                            "Only sessions filed in this sidebar folder: a folder id or "
                            "a '/'-separated human path from chat_folder_tree. Omit for "
                            "every folder and the unfiled ones."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 100,
                        "description": "Most rows to return (default 20). The reply says when more exist.",
                    },
                },
            },
            run=_run_session_history_list,
            identity="strict",
            routes=(
                "GET /api/session-control/history",
                "GET /api/chat/folders",
            ),
        ),
        Tool(
            name="session_send",
            description=(
                "Send a message into another session — the way to seed a session "
                "you just created with session_create, answer a question it "
                "raised, or correct it while it works. An idle target starts a "
                "turn on your message straight away. A BUSY target queues it for "
                "its next turn, unless you pass steer=true, which injects it into "
                "the turn already running so the target reads it mid-work; the "
                "result says which happened. The message lands in the target's "
                "transcript tagged as sent by your session, so the person reading "
                "it can tell it from their own typing. Use session_read_message "
                "afterwards to watch what the target did with it."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                    "message": {
                        "type": "string",
                        "description": (
                            "The message to deliver. It becomes the target's next "
                            "user-role turn, so write it as you would type into "
                            "that session's composer."
                        ),
                    },
                    "steer": {
                        "type": "boolean",
                        "description": (
                            "Cut into the target's RUNNING turn instead of waiting "
                            "for it to end. Use it when waiting wastes the work in "
                            "flight — the target is heading the wrong way, or the "
                            "thing it is working on is already done. Ignored when "
                            "the target is idle (the message starts a turn either "
                            "way), and when mid-turn injection is unavailable the "
                            "message falls back to the queue rather than being "
                            "dropped. Default false."
                        ),
                    },
                },
                "required": ["target", "message"],
            },
            run=_run_session_send,
            identity="strict",
            routes=("POST /api/session-control/send",),
        ),
        Tool(
            name="work_dispatch",
            description=(
                "Dispatch ONE work item in one call: work_ledger_record action=create, "
                "then session_create, then work_ledger_record action=bind, then "
                "session_send the seed. The seed is sent only after the bind succeeded, "
                "and only to the session this call opened, so no id is carried between "
                "calls. Returns the item_id and worker_session_key. On a failure it names "
                "the step (create, session_create, bind, seed), the route's refusal code, "
                "and what it cleaned up: a failed session_create closes the item, a failed "
                "bind closes the session and the item, and a failed seed cleans up nothing. "
                "outcome=outcome_unknown means a reply was lost, not that the step was "
                "refused. It never opens a work ledger: open yours first with "
                "work_ledger_record action=goal, or create is refused no_ledger. To bind an "
                "existing session instead, use the four calls one at a time."
            ),
            schema={
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "What the item is, <= 200 chars, as action=create takes it.",
                    },
                    "acceptance": {
                        "type": "object",
                        "description": (
                            "The accept_eval.py condition object, stored verbatim, as "
                            "action=create takes it."
                        ),
                    },
                    "agent": {
                        "type": "string",
                        "description": "The worker's agent, as session_create takes it.",
                    },
                    "agent_kind": {
                        "type": "string",
                        "enum": ["template"],
                        "description": "As session_create takes it.",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Sidebar folder for the session, as session_create takes it.",
                    },
                    "session_title": {
                        "type": "string",
                        "description": "The session's sidebar title. Defaults to `title`.",
                    },
                    "seed": {
                        "type": "string",
                        "description": (
                            "The seed prompt: the item's whole contract, sent with "
                            "session_send once the bind succeeded."
                        ),
                    },
                    "model": {"type": "string", "description": "As session_create takes it."},
                    "reasoning_effort": {
                        "type": "string",
                        "description": "As session_create takes it.",
                    },
                },
                "required": ["title", "acceptance", "agent", "seed"],
            },
            run=_run_work_dispatch,
            identity="strict",
            routes=(
                "GET /api/chat/slots",
                "GET /api/chat/folders",
                "POST /api/chat/folders",
                "POST /api/work-ledger/record",
                "POST /api/session-control/create",
                "POST /api/session-control/close",
                "POST /api/session-control/send",
            ),
        ),
        Tool(
            name="session_broadcast",
            description=(
                "Send ONE message to several peer sessions at once — the fan-out "
                "counterpart of session_send, for when the thing you have to say "
                "is true of every worker rather than of one. Name `targets` to reach a "
                "subset, or pass `all: true` to reach every session YOU created, "
                "which is the usual case for a conductor. A call with neither is "
                "refused and delivers nothing, so when your audience computes to "
                "nobody, skip the call. `mode` picks the delivery "
                "and is required, because the two are different instructions: "
                "`queue` waits for each target's current turn to end (use it for "
                "'the base moved, rebase before you push'), while `steer` cuts "
                "into every running turn so each target reads it mid-work (use it "
                "for 'stop, that issue is already fixed'). Every target is checked "
                "the same way a single session_send is, so this can reach nothing "
                "a session_send could not. PARTIAL DELIVERY IS NORMAL and the "
                "result says so per target: a session that was closed, went "
                "incognito, or belongs to an app is one refused row and the rest "
                "still get the message — read the rows rather than assuming all or "
                "nothing. Poll the targets afterwards with session_read_message, "
                "or take the roster with session_status."
            ),
            schema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": (
                            "The message every target receives. It becomes each "
                            "one's next user-role turn and is tagged as a "
                            "broadcast in their transcripts, so a worker can tell "
                            "an instruction its siblings also got from one aimed "
                            "at it alone. Write it so it is true for all of them."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["queue", "steer"],
                        "description": (
                            "`queue`: each target runs the message when its "
                            "current turn ends (an idle one starts immediately). "
                            "`steer`: cut into the turn already running so the "
                            "target reads it mid-work; on an idle target it starts "
                            "a turn either way, and where mid-turn injection is "
                            "unavailable that target falls back to its queue "
                            "rather than being dropped. No default — say which."
                        ),
                    },
                    "targets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Session keys or exact titles, as session_send takes "
                            "them. An empty list counts as no list, so it never "
                            "reaches anyone. A repeated target is delivered to "
                            "once. Give this or `all`, not both."
                        ),
                    },
                    "all": {
                        "type": "boolean",
                        "description": (
                            "`true` reaches every session you created. Use it "
                            "instead of `targets`, never together with it."
                        ),
                    },
                },
                "required": ["message", "mode"],
            },
            run=_run_session_broadcast,
            identity="strict",
            routes=("POST /api/session-control/broadcast",),
        ),
        Tool(
            name="session_status",
            description=(
                "List the sessions YOU stood up and what each one is doing right "
                "now — the roster a conductor patrols. Each row is `working` (a "
                "turn is in flight, wait), `queued` (idle with messages waiting), "
                "`idle` (open and doing nothing — this is the one that needs a "
                "decision), `closed` (the crew log recorded a DELIBERATE tab close "
                "and the dashboard no longer holds it: a worker that FINISHED and "
                "closed its tab — ignore it, it carries `closed_at`), `lost` (the "
                "crew log remembers the session, the dashboard no longer holds it, "
                "and it did NOT close cleanly — a turn was still open, a process "
                "recycle or destroy tore it down, or its lifecycle could not be "
                "read: treat it as lost with the process that ran it and "
                "re-dispatch it), or `unknown` (birth metadata records that you "
                "created it, but neither a live session nor an attested crew-log "
                "edge exists — it was created and its fate is not recorded, so "
                "read it before you re-dispatch it). A row with "
                "`pending_approval: true` is parked on a tool approval a person "
                "must answer (`pending_approval_tool` names the tool when known): "
                "it still reads `working`, but waiting will not move it. `closed` "
                "and `lost` are the reason to use this instead of "
                "reading sessions one at a time: a worker that vanished is absent "
                "from any live list, so a live list cannot tell a worker that "
                "finished from one lost mid-task from one you never dispatched. "
                "Read both quality fields before "
                "trusting the count: `tree` describes the crew-log roster and "
                "`history` describes transcript birth metadata. For either one, "
                "`readable` means that source was read completely, `incomplete` "
                "means its rows may be missing, and `unreadable` means that "
                "durable source was unavailable. `roster_omitted` is separate "
                "again: it counts rows this reply dropped because the three "
                "sources' union exceeded the row bound, which no source's quality "
                "field describes. The result caveats each gap "
                "under its own source name. READ-only."
            ),
            schema={"type": "object", "properties": {}},
            run=_run_session_status,
            identity="strict",
            routes=("GET /api/session-control/status",),
        ),
        Tool(
            name="session_adopt",
            description=(
                "Take another session UNDER yours, so the sidebar shows it nested "
                "beneath this one and you are recorded as the session that holds "
                "it. The sessions THEY opened come with it, because the tree hangs "
                "on the session and not on a path. A session that already has a "
                "parent can be adopted, and the parent it had is kept in the "
                "record. You can adopt only a session you already manage: one you "
                "created and did not hand away, one that was handed to you, or, "
                "with `agent.session_manager_chain` on, one managed by a session "
                "you manage. Any other target is refused `not_creator`, including "
                "a session another conductor opened. To take that over, the "
                "session that manages it (or, with the chain on, a manager above "
                "it) must first hand it to you with session_handoff; if its "
                "manager is no longer open, no fenced caller can adopt it. An "
                "agent in a person's own tab, which no session created, is not "
                "held to this reach rule, except the Conductor while "
                "`agent.session_control` is off. Refused if the target is already "
                "above you in the tree, which would make a loop. Nothing about the "
                "target's work changes: it keeps its own conversation, its turns "
                "and its tools. session_release undoes it."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_adopt,
            identity="strict",
            routes=("POST /api/session-control/adopt",),
        ),
        Tool(
            name="session_handoff",
            description=(
                "Hand a session you manage to ANOTHER session, which manages it from "
                "then on. Use it when the session that should run a worker is not the "
                "one that opened it: open a conductor, then hand each worker to it, and "
                "the conductor can send to, read, stop and close them. With "
                "`agent.session_manager_chain` off (the default), a fenced caller "
                "(see below) can no longer do those on a session it handed away; "
                "with it on, it keeps that reach while it still manages the new "
                "manager. Only the session that manages the target now may hand it "
                "over, and the recipient must be a session you also manage. The new "
                "manager cannot hand it back to you, because you manage the new "
                "manager and it does not manage you; that call is refused "
                "`not_creator`. A fenced caller takes direct management back only "
                "with the chain on, by handing the session to itself while it still "
                "manages the new manager; with it off, no fenced caller can. "
                "Fenced means a session an agent created, a crewmate, a scheduled "
                "run, a channel session, or the Conductor while "
                "`agent.session_control` is off. An agent in a person's own tab is "
                "not fenced for send, read, stop, close and adopt; "
                "session_handoff still needs it to manage the target and the "
                "recipient, or it is refused `not_manager`. Who CREATED the session "
                "stays on the record, so the new manager cannot fork it or open "
                "sessions against your memory."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                    "to": {
                        "type": "string",
                        "description": "The session that will manage the target: its key or exact title.",
                    },
                },
                "required": ["target", "to"],
            },
            run=_run_session_handoff,
            identity="strict",
            routes=("POST /api/session-control/handoff",),
        ),
        Tool(
            name="session_release",
            description=(
                "Let a session out from under its parent, so it stands on its own "
                "in the sidebar again. The counterpart of session_adopt, and the "
                "only way to undo one. You may release a session you hold, and you "
                "may release YOURSELF from whoever holds you — so a session whose "
                "conductor has stopped running is not stuck under it. Refused for a "
                "session that has no parent, and for one that hangs under somebody "
                "else. Sessions the released one holds stay with it: it keeps its "
                "own subtree and only its own edge upward goes."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": (
                            "Session key from list_sessions, or its exact title. Your "
                            "own key releases you from your parent."
                        ),
                    },
                },
                "required": ["target"],
            },
            run=_run_session_release,
            identity="strict",
            routes=("POST /api/session-control/release",),
        ),
        Tool(
            name="session_read_message",
            description=(
                "Read the tail of another session's transcript, plus whether it is still "
                "working. Use it to watch a peer session's progress: `wait`, then read — "
                "pass the ``next_since`` from the previous read back as ``since`` "
                "and you get only what arrived in between, so a poll loop does not re-read "
                "the same messages. ``running: false`` with nothing new means the target "
                "finished and is idle, which is the difference between 'not done yet' and "
                "'done'. ``pending_approval: true`` means the target is parked on a "
                "tool approval a person must answer (``pending_approval_tool`` names "
                "the tool when known); it stays ``running`` but will not progress "
                "until someone decides. READ-only: it never sends anything or changes "
                "the target's state."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default 20, max 100).",
                        "default": 20,
                    },
                    "since": {
                        "type": "integer",
                        "description": (
                            "Return messages from this index onward — pass the ``next_since`` from "
                            "your previous read to get only new ones. Omit for the newest tail."
                        ),
                    },
                },
                "required": ["target"],
            },
            run=_run_session_read_message,
            identity="strict",
            routes=("GET /api/session-control/read",),
        ),
        Tool(
            name="session_summary",
            description=(
                "Read another session's intent summary: the short digest the dashboard's "
                "summary panel shows (each goal with its status, progress and next steps, "
                "plus recurring project notes), and whether the session is still working. "
                "Use it on a patrol cycle to learn what a peer is doing without paging its "
                "transcript with session_read_message. It is a CACHED read and never spends "
                "a model call: the dashboard writes the summary at turn end, only when the "
                "operator has turned session summaries on, so it can be absent or `stale` "
                "(older than the latest turns). Fall back to session_read_message for "
                "anything newer or more exact. Authorized exactly as session_read_message "
                "is. READ-only."
            ),
            schema={
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Session key from list_sessions, or its exact title.",
                    },
                },
                "required": ["target"],
            },
            run=_run_session_summary,
            identity="strict",
            routes=("GET /api/session-control/summary",),
        ),
    )
