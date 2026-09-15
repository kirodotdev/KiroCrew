# Follow-up Suggestions

At the end of a turn the agent can offer concrete next steps as a card above the
chat composer. Each suggestion carries an **expanded handoff prompt** and three
actions: start it in a new git worktree, add it to the current session, or skip.

Both non-skip actions **pre-fill a composer and stop**. Nothing is sent until
you press send, so a single click can never launch an unattended agent turn.

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
| **Start in new worktree** | Creates `<parent>/<repo>-wt-<slug>` on a new branch off the repo's default branch, opens a new chat session scoped to that directory, and pre-fills its composer with the prompt. Disabled — and demoted from the accent style to the secondary look — when the session has no project directory; the card footer says why, and the tool result tells the agent so it can steer to "Add to this session" instead. |
| **Add to this session** | Pre-fills the current session's composer with the prompt. An unsent draft is preserved — the prompt is appended below it, not written over it. |
| **Skip** | Dismisses that one suggestion; siblings remain. The card disappears when its last item is gone. |

## Scope and limits

- **Dashboard surface only.** The tool is **stateless**: `mcp_tools/control.py::suggest_followup` validates the items and returns a session-directive marker carrying no session key. The session-aware consumer (`dashboard/session_directive_apply.py::apply_session_directive`) resolves the authoritative session and applies the card to ITS OWN slot, so a cron, sub-agent, or otherwise tabless caller is refused there rather than posting a card into someone else's session. The gate is a live card surface, not where the conversation started: `suggest_followup` requires both a chat slot and `has_dashboard_surface(session_key)`, so a channel-born session with its dashboard tab open qualifies, while a slot-less caller (a channel transport's `TurnDriver`) is refused even when a tab happens to be open. Every path emits a SEL audit event.
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

The gates behind those limits — argument validation, the branch-name filter, and
the sandboxed `git` invocation — are a design record for contributors, not part
of using the feature.
