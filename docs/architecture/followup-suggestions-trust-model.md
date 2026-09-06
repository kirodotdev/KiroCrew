# Follow-up suggestions: trust model

The user-facing behaviour of the follow-up card — what it offers and what it
will not do — is [`src/kiro_crew/docs/followup-suggestions.md`](../../src/kiro_crew/docs/followup-suggestions.md).
This document is the design record for the gates behind it.

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
