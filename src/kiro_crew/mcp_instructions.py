"""The MCP ``instructions`` each small Kiro Crew server returns from ``initialize``.

A server's usage text travels with the server: every harness that mounts it, for
every agent, gets the text from the server's own handshake, and an agent that does
not mount it never sees it. The agent prompt keeps only what a server cannot say
about itself -- that an expected server is missing (the Computer Use pointer in
``context_assembly/sections.py``).

Each text opens with a one-line summary. pi shows only that line for a server it
does not declare directly, and tool search ranks a server by it, so it must stand
alone. Every text stays under :data:`MAX_INSTRUCTIONS_CHARS`: Claude Code cuts a
server's instructions there, and the DeepSeek Harness refuses ``session/new``
outright for instructions over 32,768 bytes.

pi does not put a directly declared server's instructions into the prompt, so its
gate extension appends them; :data:`BY_SERVER` is the table it is handed.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

#: Claude Code's cut-off for one server's instructions, in characters.
MAX_INSTRUCTIONS_CHARS = 4096

#: pi's cut-off for the summary line it lists (``MAX_SERVER_DESCRIPTION_CHARS``).
MAX_SUMMARY_CHARS = 250

CRON = (
    "Schedule recurring or one-shot jobs (reminders, polling, digests) and manage "
    "the jobs this session owns.\n"
    '- `cron_add`: recurring or one-shot jobs ("every", "daily", "remind me", '
    '"check regularly"). Prefer no-LLM `script` or mutually exclusive `command` for '
    "deterministic polling. Register Python as "
    "`script='~/.kiro/crew/crons/file.py:function'`; read `ctx.message`, deliver "
    "with `ctx.notify()`, raise `Skip()` to retry, `Done(msg)` to deliver/remove, "
    "`Report(msg)` to deliver/keep. Dry-run: "
    "`kirocrew cron preview <script:function> -m <message>`; synchronous runner "
    "refuses `async def`. For wall-clock times pass an IANA `timezone`; `cron_expr` "
    "otherwise uses global config timezone, then UTC. Polling/scanner/digest: "
    "`persistent_session=false`, `minimal_context=true` (~200 tokens/wake, not "
    "30-55k), `hide_in_chat=true`; conversational reminders keep all three "
    "defaults for continuity. `timeout` bounds the script/command subprocess; "
    "`timeout_secs` bounds the whole wake (default 1800, max 86400).\n"
    "- `cron_list`: THIS session's jobs only; empty does not mean none exist "
    "elsewhere. CLI/dashboard/other-session jobs require `kirocrew cron list` or "
    "Schedule. Mutating another session's id returns `job not found`; "
    "`cron_remove_all` reports no owned jobs. `verbose=true` returns full bodies; "
    '`ids=["<job_id>"]` drills in.\n'
    "- `cron_update(job_id=…)`: change schedule/message/agent/channel/flags without "
    "losing id/history; don't remove/re-add. `cron_trigger(job_id=…)` fires once "
    "now regardless of schedule; use to smoke-test new jobs. `cron_remove` / "
    "`cron_remove_all` / `cron_pause` / `cron_resume` manage jobs."
)

GUIDE = (
    "Answers where-is, how-to and settings questions about Kiro Crew itself, and "
    "opens guides and cards in the dashboard.\n"
    "- `find_ui`, `search_docs`, guides, cards: for where-is, how-to and setting "
    "questions about Kiro Crew itself, call these first and follow each result's "
    "`next`; guides and cards need a message the user sent from the dashboard."
)

SESSION_CONTROL = (
    "Create, drive and close other chat sessions in the user's sidebar, for work "
    "that must outlive this turn and stay visible to the user.\n"
    "- `session_create` / `session_send` / `session_read_message` / `session_stop` "
    "/ `session_close` / `session_revive`, sidebar `chat_folder_*`. Use for work "
    "that must outlive your turn and stay visible for user takeover/closure. New "
    "sessions are EMPTY: seed with `session_send`, poll `session_read_message`. "
    "`session_revive` brings an ARCHIVED (history) session back into the sidebar "
    "with its transcript; use it instead of re-creating when a closed session is "
    "the right home."
)

COMPUTER = (
    "Read and drive the user's real desktop apps through the accessibility layer, "
    "for work outside a web page; macOS and Windows.\n"
    "\n"
    "macOS and Windows both support the full tool set. They differ in ONE way you "
    "must relay to the user: on Windows there is no per-process input, so a "
    "keystroke takes their keyboard focus and a coordinate click moves their real "
    "cursor — the result text says so, and you should pass that on rather than "
    "silently succeeding. Do not assume the platform from your own knowledge — CALL "
    'the tool and act on what it returns: a "disabled" or "not supported" refusal '
    "is final (relay it and stop), while a refusal that names an alternative (an "
    '`element_index` instead of coordinates, `click_method: "global"` to accept the '
    "cursor move) is telling you the next call to make.\n"
    "\n"
    "**Tree first, always.** Call `computer_get_state(app=...)` before any action — "
    "it returns the window as a numbered element outline, and prefer addressing an "
    "element by its `element_index`: that is the only form the target can be "
    "checked against (a password field is refused by its index, not by its pixels). "
    "`computer_click` and `computer_drag` also accept `x`/`y` screen coordinates for "
    "the canvases, sliders and custom-drawn UI that expose no usable element. By "
    "default a coordinate gesture is delivered to the target app alone and **the "
    'user\'s real pointer does not move**; `click_method: "global"` is the one path '
    "that moves it — you must ask for it BY NAME (`auto` never picks it), so name it "
    "only when a click has to be physically real, and tell the user before you do: "
    "their cursor will jump out from under their hand. When the app has no window "
    'yet, `computer_launch_app(app="Paint")` opens it and returns the new window\'s '
    "tree, so no separate `computer_get_state` call is needed — give the OS's own "
    "app NAME, never a path or a command line, and never call it twice for one app "
    "(a cold start can take ten seconds). It is refused when the app already has a "
    "window; snapshot that instead of opening a second copy. `computer_list_apps()` "
    "lists what currently has an on-screen window when you do not know how the user "
    "names an app.\n"
    "Each action returns a refreshed tree, so you do not need to re-snapshot just to "
    "re-read indices. Call `computer_end_turn()` when you are done with the app. When "
    "a screenshot is attached you get a **file path**, not an image — open it with "
    "the file-read tool only when the outline genuinely cannot answer the question "
    "(it costs ~8K tokens). Password fields render as `<secure>` and their window is "
    "never captured. Kiro Crew's own dashboard is refused, for reading as well as "
    "typing, because driving it would let you change your own security settings.\n"
    "Read the `computer-use` skill before your first call."
)

#: Server name -> its instructions. The servers read their own entry; pi's gate
#: extension is handed the entries of the servers its session mounts.
BY_SERVER: Mapping[str, str] = MappingProxyType(
    {
        "kirocrew-cron": CRON,
        "kirocrew-guide": GUIDE,
        "kirocrew-dashboard": SESSION_CONTROL,
        "kirocrew-computer": COMPUTER,
    }
)
