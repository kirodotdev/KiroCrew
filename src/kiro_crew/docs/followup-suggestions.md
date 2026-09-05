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

- **Dashboard surface only.** The tool is **stateless**: `mcp_tools/control.py::suggest_followup` validates the items and returns a session-directive marker carrying no session key. The session-aware consumer (`dashboard/session_directive_apply.py::apply_session_directive`) resolves the authoritative session and applies the card to ITS OWN slot, so a cron, sub-agent, or otherwise tabless caller is refused there rather than posting a card into someone else's session. The gate is a live card surface AND authenticated-human provenance, not where the conversation started: `suggest_followup` requires a chat slot, `has_dashboard_surface(session_key)`, and a user-origin producer, so a channel-born session with its dashboard tab open qualifies when a human is driving it, while a slot-less caller (a channel transport's `TurnDriver`) is refused even when a tab happens to be open. The provenance half is what makes the refusal above true of a cron or sub-agent turn that INHERITED a tab it never opened: a surface check alone asks only whether a tab exists, which such a turn satisfies without a human having asked for a card. Every path emits a SEL audit event.
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

## Trust model

Every string in an item is LLM-authored, and one of them (`branch`) reaches a
`git` invocation. Two gates apply:

1. **MCP layer** — `SUGGEST_FOLLOWUP_SCHEMA` in `validation.py` enforces item
   count, per-field types and lengths, rejects unknown fields, strips hidden
   Unicode, and full-matches `branch` against `FOLLOWUP_BRANCH_RE`. That grammar
   excludes a leading `-` (git would read it as a flag), `..`, `~`, `^`, `:`,
   `?`, `*`, `[`, `\`, and whitespace.
2. **Gateway** — `POST /api/chat/slots/{slot}/followup` re-validates against the
   same schema (the endpoint is reachable over loopback from inside the kiro-cli
   process group, so it is a trust boundary, not a relay) and redacts
   credentials and exfiltration URLs from every string before broadcasting.

Both endpoints are **owner-only**. They act on owner-scoped resources — the
card renders in the owner's composer, and the worktree allow-list spans every
slot's project — so a dashboard claim alone is not enough: the caller must match
the configured owner, or be a signed local bootstrap subject when no owner is
configured (the standalone-local case, where the browser's own credential is
minted for `local-app`). App callers are refused outright. The one exception is
the loopback internal-secret path every MCP call arrives on, which is granted
with no app identity to check.

`POST /api/worktree/create` adds its own checks:

- `repo` must resolve **inside a directory some existing chat slot is already
  scoped to**. The card only ever sends the active session's own `project`, so
  this costs nothing in practice while removing the endpoint's arbitrary-path
  surface. Both the submitted path and the git toplevel it resolves to are
  checked, so resolving upward out of an allowed subdirectory is refused.
- git runs with an argv list and no shell, a credential-scrubbed environment,
  the POSIX resource-limit ceiling, and a 120s timeout.
- **No repository-controlled code executes.** `git worktree add` would normally
  run the repo's `post-checkout` hook, and repo-local config can name commands of
  its own (`core.fsmonitor`). Both are suppressed with `-c` overrides, which beat
  every config file. `core.hooksPath` points at `os.devnull` — a non-directory OS
  device, so there is no `post-checkout` to find and nowhere to plant one. Both
  earlier shapes left a writable window: an in-repo sentinel path sits in a
  directory the checkout's preparer controls, and a gateway-owned temp directory
  is still same-uid writable between calls. Checkout content filters are the one
  such vector `-c` cannot
  close — `.gitattributes` names a filter, and its `filter.<name>.process` /
  `.smudge` driver comes from config under an arbitrary name — so a repo whose
  **repository-scoped** config declares one is refused with a 409 telling the user
  to create the worktree manually. Both scopes git reads inside a repo are probed:
  `--local` (`.git/config`) and, when `extensions.worktreeConfig` is on and a
  `config.worktree` file exists under the repo's **per-worktree** `$GIT_DIR`,
  `--worktree` — `--local` alone does not report worktree-scoped keys, and for a
  linked worktree that file lives under `<common>/worktrees/<id>`, not the common
  dir. Both probes pass `--includes`, which git defaults OFF for a specific-scope
  query: a driver reached through `include.path` would otherwise be invisible to
  the probe yet still run on checkout. A scope that cannot be read at all also refuses, since an
  unreadable scope cannot be proven filter-free. (Global config is not probed: that is the user's own
  machine setup, e.g. `git lfs install`, not something the repository supplies;
  and `git clone` never transfers config from a remote.) These guards sit on top
  of OS isolation, not instead of it: the git spawn is routed through the
  `sandboxed_spawn_argv` chokepoint in **strict** mode (matching `git_coord.py`'s
  treatment of agent-influenced git). Strict matters because `include.path` is
  repo-controlled and the filter probe passes `--includes`: a hostile checkout
  could otherwise point it at `~/.aws/credentials` and have git read that file as
  config. Nothing here needs a credential — the base ref comes from local refs
  and no remote is contacted, and a host with no sandbox backend — and no explicit
  `agent.sandbox_allow_unsandboxed_exec` opt-in — gets a **503 telling the user to
  create the worktree manually** rather than an unisolated spawn. The same 503
  covers a host that passes the backend probe but denies `unshare(NEWNS)` at exec
  time (GitHub Actions runners do this): the launcher reports the refusal from the
  child, and that is surfaced honestly instead of being misread as "Not a git
  repository". Sandboxing
  bounds what a hook could reach; the `-c` overrides and the filter refusal are
  what stop one running at all.
- **The branch name must be a ref git will accept.** Beyond the character
  grammar, `foo..bar`, a component ending in `.` or `.lock`, and the reserved
  name `HEAD` are rejected up front — git refuses them too, but only after the
  branch has been claimed, which surfaced as a misleading "Branch already
  exists". A component whose stem is a Windows device name (`CON`, `NUL`, …) is
  rejected on every platform for the same reason: a branch is a loose ref FILE, so
  `feat/CON` claims fine and then fails the checkout, surfacing as the same
  misleading error.
- **Concurrent requests cannot destroy each other's work.** The branch is claimed
  atomically before anything is created (`update-ref <ref> <base> ""`, where the
  empty old value means "must not exist"), so git's ref lock picks one winner and
  the rest get a 409. Cleanup after a failed create removes only what that
  request can prove it created: the branch only if it won the claim, the
  destination only if git registers it against that same branch. Deletion is
  compare-and-delete, and it is additionally skipped when another worktree has
  since checked that branch out — `update-ref -d` has none of `branch -D`'s
  "used by worktree" protection, so deleting would leave that worktree on a
  dangling ref; an unreadable worktree listing keeps the branch for the same
  reason, since adoption cannot be ruled out. Same-repo
  requests are additionally serialized in-process.
- **Reuse is keyed on path *and* branch.** The destination slug keeps only a
  branch's last segment, so `feat/foo` and `fix/foo` derive the same directory;
  an existing worktree is reported as `reused` only when `worktree list
  --porcelain` shows it checked out on the requested branch. Otherwise it is a
  409, never a session opened against the wrong branch.
- Sensitive paths are refused, and the destination directory is derived
  server-side (never supplied by the caller) and must not already exist.

If the new session cannot be scoped to the worktree, the frontend deletes the
session it just created rather than leaving an unscoped one behind; the worktree
survives and the create endpoint is idempotent for it, so pressing the button
again reuses it. On success the new session is explicitly activated before its
composer is pre-filled, so a session switch during creation cannot land the
prompt in an unrelated session.

Both endpoints emit SEL audit records.

## Files

| Layer | Path |
| --- | --- |
| Tool declaration + dispatch | `src/kiro_crew/mcp_tools/control.py` |
| Directive marker codec | `src/kiro_crew/session_directive.py` |
| Directive applier (the effect) | `src/kiro_crew/dashboard/session_directive_apply.py` |
| Arg schema | `src/kiro_crew/validation.py` |
| Card endpoint | `src/kiro_crew/dashboard/chat_handlers.py` |
| Worktree endpoint | `src/kiro_crew/dashboard/handlers/worktree.py` |
| WS event → state | `website/src/hooks/useWebSocket.ts`, `website/src/store/chatSlice.ts` |
| Card UI | `website/src/components/FollowUpCard.tsx` |
| Render site | `website/src/pages/ChatPage.tsx` |
