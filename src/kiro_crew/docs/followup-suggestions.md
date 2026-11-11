# Follow-up Suggestions

At the end of a turn the agent can offer concrete next steps as a card above the
chat composer. Each suggestion carries an **expanded handoff prompt** and three
actions: start it in a new chat (in a separate copy of the project, a git
worktree, when the project is a git repository), add it to the current session,
or skip.

**Start pre-fills the new session.** It opens the new session with the prompt in
its message box, and nothing is sent until you press send. **Add to this
session** puts the prompt in the current message box and sends nothing.

**One-click send is opt-in.** Turn on **Send follow-ups with one click**
(Settings → Chat → Composer) and Start sends the prompt as the new session's
first message instead, so one click starts an agent turn. That turn runs in the
background: you stay on the session you are in, the new session shows in the
sidebar, and a notice names it with an **Open session** button. The button then reads
**Run in a separate copy** (or **Run in new chat**), so a press that runs the
prompt never looks like the one that only pre-fills. The setting is off by
default and kept in this browser only: it is not part of the settings backup the
gateway keeps, so it never follows you to another browser, and neither an agent's
change card nor an edit to that backup can turn it on. The card footer always
says which of the two Start does. With the setting on, the footer starts with
"Sends right away because Send follow-ups with one click is on. Turn it off in
Settings → Chat → Composer.", and the setting's name links to that setting.

Every item shows the first three lines of its prompt on the card, and a **Show
prompt** toggle expands the rest of a longer one. The title and description are
a label the agent chose; the prompt is what runs, so read it before you press
Start, and before you press send when Start pre-filled it.

## Using it

The agent calls the `suggest_followup` MCP tool (kirocrew-core) with up to three
items:

```json
{
  "items": [
    {
      "title": "Add rate limiting to the upload endpoint",
      "description": "POST /api/upload is unbounded — a single client can saturate the worker pool.",
      "prompt": "In src/kiro_crew/dashboard/handlers/files.py, add a per-caller token-bucket limiter to api_file_upload ... (full standalone instruction)",
      "branch": "feat/upload-rate-limit"
    }
  ]
}
```

`title` and `description` are the human-facing label. `prompt` is the payload:
it is written to be self-contained, because the agent that receives it may have
none of the originating session's context. `branch` is optional — the card
derives a `followup/<slug>` name from the title when it is absent.

Calling the tool is the agent's own judgement call; there is no turn-boundary hook that forces a suggestion every turn. Silence is the intended default when there is no substantive follow-up. A situational reminder is injected into the per-turn context, gated on an open dashboard surface (where the tool works) AND on the agent not having opted out of Crew context (`includeCrewContext: false`). It raises awareness — MCP Tool Search means the tool's own spec is not always in context — while telling the agent to DEFAULT TO SILENCE: raise a card only after a genuinely large task (multi-file changes, a full PR cycle, a major investigation), never after small ones, never per-turn, and never to ask a clarifying question.

### Actions

| Action | Effect |
| --- | --- |
| **Start in a separate copy** | Shown when the session's project directory is a git repository (or the repository check has not answered yet). The tooltip names the copy: a git worktree. Creates `<parent>/<repo>-wt-<slug>` on a new branch off the repo's default branch, opens a new chat session scoped to that directory, and switches to it with the prompt in its message box. With one-click send on, it sends the prompt as the session's first message instead and does not switch. | When git cannot open the directory as a repository, the error under the suggestion says why: no repository, a `.git` file naming a git directory that is missing, a repository another user owns, or one git may not read. |
| **Start in new chat** | Takes the worktree button's place when the session has no project directory, or its project directory is not a git repository (a folder whose `.git` file names a git directory that no longer exists is not one), or is only a folder inside a repository rooted higher up (a worktree would branch that whole enclosing repository; the server refuses that unless another session is scoped to the repository root, and the card does not offer it either way). Opens a new chat session in the same project directory (or, when the session has none, wherever a new chat starts by default) and the same sidebar folder, with no worktree and no git call, and hands it the prompt the same way as the button above (pre-filled by default, sent with one-click send on). The button's tooltip says why no worktree is created, and in a project directory the card footer says where the new chat opens. With no project directory, or one that is not a git repository root, the tool result also tells the agent which button the card shows. |
| **Add to this session** | Pre-fills the current session's composer with the prompt and sends nothing. An unsent draft is preserved — the prompt is appended below it, not written over it. |
| **Show prompt** | Appears when the prompt is longer than the three lines the card shows. Expands the item's full prompt on the card, so you can read all of what Start hands over. |
| **Skip** | Dismisses that one suggestion; siblings remain. The card disappears when its last item is gone. |

Every action removes only its own suggestion. Starting one item (in a new worktree or a new chat) or adding it to this session takes that item off the card and leaves the others there, so returning to the originating session shows the remaining suggestions. The removal is pinned to the card the click came from: a newer card that arrived while a start was in flight is left untouched. A start that fails keeps its item on the card with the error, and you stay where you are. With one-click send on there are two exceptions: a refused send that left its new session open, and a send whose delivery could not be confirmed. Then the item leaves the card and a notice points at that session, so the prompt cannot run twice. For a send whose delivery could not be confirmed, that notice has the same **Open session** button, so you can check whether the turn is running.

## Scope and limits

- **Dashboard surface only.** The tool is **stateless**: `mcp_tools/control_parts/directive.py::suggest_followup` validates the items and returns a session-directive marker carrying no session key. The session-aware consumer (`dashboard/session_directive_apply.py::apply_session_directive`) resolves the authoritative session and applies the card to ITS OWN slot, so a cron, sub-agent, or otherwise tabless caller is refused there rather than posting a card into someone else's session. The gate is a live card surface AND authenticated-human provenance, not where the conversation started: `suggest_followup` requires a chat slot, `has_dashboard_surface(session_key)`, and a user-origin producer, so a channel-born session with its dashboard tab open qualifies when a human is driving it, while a slot-less caller (a channel transport's `TurnDriver`) is refused even when a tab happens to be open. The provenance half is what makes the refusal above true of a cron or sub-agent turn that INHERITED a tab it never opened: a surface check alone asks only whether a tab exists, which such a turn satisfies without a human having asked for a card. Every path emits a SEL audit event. Only a turn a person sent can post a card: the session's own machine-driven turns — a sub-agent completion/synthesis turn, a queued completion event, a monitor or auto-nudge wake, a task-runner summary — carry no human provenance and are refused too, even on their own slot. Unlike `reset_conversation`, there is no self-wake carve-out.
- **Three items max**, one card **per session**. Cards are slot-keyed, so a
  suggestion arriving in one session never evicts another's. A second call for
  the same session replaces its unacted-on card rather than stacking.
- **Ephemeral.** The card lives in frontend state only. It survives switching
  between sessions, but a full page reload drops it. Because delivery is
  broadcast-only, both delivery paths — the directive applier and the HTTP
  endpoint — **await** `deliver_ws_owners` and report how
  many sends completed; a zero count returns text telling the model to restate the
  follow-ups in its reply — so an unattended turn cannot silently lose the
  prompts. Counting connected sockets instead would be a false success: the count
  is taken before any send runs, so a window that closes in between yields a
  failed send already reported as delivered. Delivery is to **owner** sockets
  only: an app token can open `/api/ws`, and an all-clients broadcast would hand it
  another user's complete handoff prompts.
  Parking the card server-side and replaying it on reconnect is a possible
  follow-up.
- **Retry-safe.** If the worktree is created but opening the session fails, the
  worktree is left in place and the create endpoint recognizes its own
  destination on the next attempt (`reused: true`) instead of refusing. A
  `worktree add` that fails or times out part-way is unwound, so a retry is not
  blocked by half-created artifacts.
- **Sent once (one-click send).** With **Send follow-ups with one click** on,
  the prompt is sent to the new session by its key before the
  switch, while the card is still on screen. A refused send discards the new
  session while it is still empty, keeps the card and shows the error on that item, so pressing
  Start again retries cleanly (the worktree is reused). If the answer to the send
  is lost, the prompt may already be running, so that item leaves the card and a page
  notice asks you to check that session rather than offering a second run. Once
  the send is accepted that item leaves the card, so its Start button cannot run
  the same prompt twice; the other suggestions stay. If you open another session while a start is in flight, you
  stay there and a notice points at the new session in the sidebar.

The gates behind those limits — argument validation, the branch-name filter, and
the sandboxed `git` invocation — are a design record for contributors, not part
of using the feature.
