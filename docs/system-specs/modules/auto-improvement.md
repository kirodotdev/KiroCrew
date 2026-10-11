# Auto-Improvement Module

## Overview

> **Using the app rather than changing it?** The operator's guide is
> [`src/kiro_crew/apps/builtins/auto_improvement/docs/MANUAL.md`](../../../src/kiro_crew/apps/builtins/auto_improvement/docs/MANUAL.md).
> This spec describes internals.

Auto-Improvement is an opt-in (`defaultEnabled: false`) built-in app that runs a
measurement-first self-improvement loop against a GitHub repository. It calibrates
a metric (the "ruler"), **proves** the metric can detect a known win, and only
then runs keep-or-revert improvement cycles. Candidates that survive a
deterministic gate and an A/B (or RED/GREEN) measurement are opened as **draft**
GitHub pull requests for human review.

The load-bearing design property is that every *decision* is deterministic Python
and every *proposal* is an agent: the agent writes candidate fixes, and the gate,
measurer, keeper, and PR pipeline decide what survives. The agent never grades its
own work.

The app's origin as a port of an upstream app, and the design decisions the port
made, are recorded in
[`PORT_PLAN.md`](../../../src/kiro_crew/apps/builtins/auto_improvement/docs/PORT_PLAN.md).

## Routes

All routes live under `/api/apps/auto-improvement/` and are registered by
`apps/builtins/auto_improvement/backend/routes.py:register_routes`, mounted
in-process on the gateway's own aiohttp app. Every handler is wrapped in
`_require_enabled` (403 when the app is disabled). The 17 routes marked `owner`
also call `require_owner_dashboard_request` first: a dashboard subject that is
not the owner, and any app token, gets 403 `owner_only`, and an allowed call
writes an SEL row (`_audit_owner_route_allowed`, because the owner gate audits
only its refusals).

`_require_enabled` closes the API, not the work. `register_routes` therefore stands
both in-process workers down on both ways the app can be switched off: `on_cleanup`
hooks for gateway shutdown (`_stop_watchers`, `_stop_run`), and an `apps.teardown`
`register_app_disable_hook` for the operator disabling the app. Without the second,
a disable leaves the routes answering 403 while a watcher keeps running agent turns
in per-PR clones and an in-flight run keeps the clone lock and keeps spending
budget. The disable hook fires inside the disable request, before the `enabled` flag
is written, so both are signalled at the click rather than at the next sweep, and
each is error-contained separately — the shutdown path gets that from holding two
hooks, the disable path has to spell it out.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | enabled | liveness; echoes the app name |
| GET | `/config` | enabled | current run configuration |
| PUT | `/config` | owner | update configuration (allowlisted keys only) |
| POST | `/setup-clone` | owner | clone or re-point the working repository the run operates on |
| GET | `/branches` | enabled | branches available in that clone, for choosing a base |
| GET | `/pr-status?url=&refresh=` | owner | live PR status, CI checks, watcher verdict |
| POST | `/run` | owner | start an improvement run |
| GET | `/run` | enabled | the in-flight run's state |
| POST | `/run/stop` | owner | wind down the in-flight run |
| GET | `/ruler` | enabled | ruler calibration state |
| GET | `/findings` | enabled | ledger entries, newest first |
| GET | `/findings/{fp}` | enabled | one ledger entry in full |
| POST | `/findings/{fp}/commit` | owner | commit an accepted change from the ledger |
| POST | `/calibrate` | owner | prove the ruler (Phase 1) before any improvement cycle |
| GET | `/progress` | enabled | the cumulative-best staircase for the chart |
| GET | `/events` | enabled | server-sent live run events |
| GET | `/deps` | enabled | external tool availability (git / gh / ruff) |
| POST | `/deps/install` | owner | install the optional linter |
| POST | `/draft-pr/{fp}` | owner | draft a PR from an already-queued change |
| GET | `/profiles` | enabled | every captured profiler frame tree |
| GET | `/profile/{fp}` | enabled | one normalized frame tree (flame / sunburst) |
| POST | `/findings/{fp}/forget` | owner | mark purged so dedup lets it retry |
| POST | `/findings/{fp}/purge` | owner | forget and remove artifacts |
| POST | `/findings/purge-dead` | owner | sweep records that can never progress |
| GET | `/watchers` | owner | per-PR watcher sessions |
| POST | `/watchers/{fp}/start` | owner | start/re-attach a PR watcher |
| POST | `/watchers/{fp}/stop` | owner | stop a watcher after its current pass |
| GET | `/watchers/{fp}/log` | enabled | what a watcher has done (`?since=` tails) |
| GET | `/sessions` | enabled | every linked chat-session record |
| GET | `/sessions/{key}` | enabled | one session record |
| PUT | `/sessions/{key}` | owner | link/update a session (drives resume) |
| DELETE | `/sessions/{key}` | owner | forget a session link |

`register_routes` mounts exactly these 32 routes. No test asserts the mounted set
against this table, so an added endpoint has to be added here by hand in the same
change. Beside the routes, `register_routes` appends three gateway lifecycle
hooks: `_bind_watcher_loop` on startup (it binds the watcher registry to the
gateway loop and attaches the crew runtime), and `_stop_watchers` and `_stop_run`
on cleanup; it also registers `_stop_on_disable` as the app-disable hook (above). `app.json` also declares `backend.hooks` `on_startup` and
`on_shutdown`, both in `backend/crew.py`.

`PUT /config` is an **allowlist**, not a merge. `clone` and `target_url` are
deliberately excluded: they decide which repository the agent is turned loose on,
so they cannot be changed through the generic config endpoint. Rejected keys are
echoed back in `rejected` rather than silently dropped.

**Pinned invariants.**

- `POST /setup-clone`'s success path is exercised end to end, asserting the persisted config, not only the 200. (D-82)

## PR status and the watcher verdict

`backend/pr_checks.py` is an interpreter over
`kiro_crew.dashboard.handlers.source_providers`, reusing the core's cached (30 s
TTL), request-coalescing, credential-redacting `gh`/`glab` reader rather than
introducing a second GitHub client.

It reduces a PR to one of three verdicts the watcher loop switches on:

| Verdict | Meaning |
|---|---|
| `READY` | merged, or green checks + no conflicts + no open threads |
| `PROGRESS` | failing required checks, conflicts, open threads, pending checks, or mergeability not yet known |
| `BLOCKED` | closed without merging — the only `BLOCKED` path |

`derive_verdict` checks in this order: merged (`READY`), closed (`BLOCKED`),
failing checks, conflicting, unresolved threads, pending checks, unknown
mergeability (each `PROGRESS`), else `READY`. So **failing checks beat a clean
mergeable flag**, and the function is fail-safe toward `PROGRESS` — declaring an
unfinished PR ready ends the watcher early, which is the expensive mistake; an
extra cycle is cheap. A PR status that cannot be read is not a verdict at all:
the watcher logs it, waits and reads again, and the pass still counts against
its budget, so a provider that stays down ends the watcher.

Checks that set `allow_failure` (GitLab's advisory opt-out) are counted separately
and never drive the verdict, or one flaky optional job would nudge forever.

**Pinned invariants.**

- `auto_publish_gate` reads `unresolvedThreads`, counted from `resolvable` + `resolved`, so an unresolved review thread blocks auto-publish. (D-108)

## Safety controls

| Control | Where | Behavior |
|---|---|---|
| Push-disabled clone | `backend/clone_setup.py:_disable_push` + `profiles/github_repo/profile.py:RepoIsolation.push_disabled` | **Every** configured fetch/push URL is replaced with exactly one `DISABLED_NO_PUSH` value; setup and runtime fail closed on extra values. The real remote lives in config (`origin_url`), handed only to trusted publishers |
| Draft-only PRs | `profiles/github_repo/pr_recipe.py` | `gh pr create --draft`; never `--web`, merge, ready, or auto-merge |
| Generated head branch | same | `auto-improvement/<kind>-<fingerprint>`; never a human's branch |
| Protected-branch denylist | `spine/push_policy.py` | non-overridable; a hand-edited config cannot widen it |
| Edit allowlist | `spine/gate.py` + profile | ruler/harness/tests/auth are mechanically off-limits |
| Do-not-pollute gate | `spine/pollute.py` | host state hashed before/after; drift blocks the run |
| Second reproduce | `spine/pr_pipeline.py` | an independent A/B must confirm before a PR is drafted |
| Pre-push review gate | `spine/driver.py:_prepush_review_clean` | optional, fail-closed; an unfounded "clean" cannot pass it |
| Tool-request gate | `spine/agent_runner.py:_tool_permitted` + `shell_command_refusal` | the caller's `allowed_tools` is ENFORCED, and state-mutating shell verbs are refused even when Bash is allowed |
| Audit-or-deny approval | `spine/agent_runner.py:_approve` | the unattended auto-approve is logged to the SEL with `critical=True` **before** it is granted; an unwritable audit REJECTS the tool |
| Audited MCP dispatch | `backend/mcp_server.py:_audit` | every `tools/call` is logged, rejected ones included; the pre-dispatch `invoked` event is `critical=True` (audit-or-DENY — an unauditable call is refused), outcome events stay fail-soft since the handler has already run |
| Redacted evidence | `backend/routes.py:_redact_for_display` / `_redact_tree` | EVERY agent-authored field served to the browser is scanned — diff, PR body, and the candidate's signature/hypothesis/evidence/severity/blast-radius, plus the gate tree recursively; **fail-closed** |
| Sandboxed fallback agent (NOT SELECTED) | `spine/agent_runner.py:_spawn_sandboxed_agent` | nothing selects this path — both selection sites go offline/refuse instead (see "The subprocess AgentRunner is not selected"). Retained hardening, for any future caller: it runs through `sandboxed_spawn_argv(mode="strict")` + `popen_limited` (post-exec resource ceiling): worktree visible, credential dirs bind-mounted empty, env scrubbed, `PYTHONPATH` stripped. Hides credentials; does NOT confine writes — see "Known limitation: the sandbox hides credentials, it does not confine WRITES" below |
| Pre-push content scan | `spine/push_policy.py:scan_content_for_secrets` | ONE scanner behind all three exits — draft-PR push, F10 direct push, one-click commit. The full pushed range is scanned; a hit **refuses** the push and the change stays in the local queue; **fail-closed** |
| Audited subprocess agent (NOT SELECTED) | `spine/agent_runner.py:_audit_unattended_agent` | same — retained for a future caller. The `claude -p` path passes `--dangerously-skip-permissions`, so the launch is one blanket approval — logged `critical=True` before the spawn, and an unwritable audit REFUSES to launch |
| Redacted PR prose | same, `_redact_prose` | title and description are redacted (prose survives rewriting; a diff does not) |

### Clone setup and reuse contract

`setup_safe_clone` derives the canonical destination from the validated GitHub
`owner/repo`; it never treats clone contents as trusted. The improvement agent is
expected to edit that checkout, so reuse attests only properties the host can
enforce: the scratch root/destination and Git metadata contain no links or
redirections, local Git config has no includes/URL rewrites/worktree override,
and every origin fetch/push URL is exactly `DISABLED_NO_PUSH`.

A documented setup re-run accepts that sentinel and reasserts the controls, which
is the idempotency guarantee. A live mismatched or multi-valued fetch origin,
unsafe Git metadata/config, linked scratch path, or non-repository destination is
refused without mutation. Push URL values are instead always replaced with
exactly one sentinel before success is reported. Initial clone ignores
global/system Git config, pins hooks and fsmonitor off, allows only the validated
transport, and removes partial output on failure. Branch listing, checkout, and
runtime isolation repeat the metadata and URL-set checks. Git metadata entries must
be regular files (object files may remain hardlinked); FIFOs, sockets, devices,
links, and hardlinked non-object metadata are refused before host Git. Repository integrity
is an unconditional runner startup prerequisite; direct-commit authorization can
relax only push posture, never metadata/config validation. The attributes pin is
published by atomic replacement, so a hardlinked path cannot truncate another
inode. Each trusted publisher validates full repository isolation (metadata/config
safety plus exact disabled URL sets) after the agent's final write and before its
first trusted Git scan/mutation. Perf preflight uses the same capture → attest/retire
→ interpret ordering before progress, archive, or HEAD reads. The one-click route holds
the clone lock and proves the runner idle for that whole critical section; the
driver additionally revalidates after a rebase before its retry push. Local
configuration that can invoke host-side helpers (including diff, filter, merge,
editor, askpass, hook, fsmonitor, proxy, signing, attributes-file, and
excludes-file helpers) is refused; trusted Git pins both external paths to Git's
cross-platform `/dev/null` spelling, and content scans additionally clear `diff.external` and disable external diffs. If safety
changes after an agent/build step, the run stops before further Git, atomically
moves the canonical clone into a private `.unsafe-*` incident directory, reports
the retained path, and caps those incident copies at three per repository;
ordinary setup mismatch never creates these copies.

This contract deliberately makes no HMAC or clean-checkout claim: clone content
and history remain agent-writable, while trusted publishing is contained by
protected-branch policy, content scanning, final repository-config validation,
and explicit config-held push destinations.

**Pinned invariants.**

- `setup_isolated_clone` removes the clone and returns an error when the PR head-branch checkout fails, so a watcher never works on the base tree. (D-87)

### Why the unattended runner consults the platform governance gate

`SessionAgentRunner` auto-approves the provider's permission requests at its OWN gate (an
app-local allowlist + the shell denylist). That gate alone is not enough: the enterprise
governance PROFILE ceiling, the builtin denied-command rules and the `~/.aws`/`~/.ssh`
sensitive-path blocks live only in the platform's `hooks.on_tool_call` chokepoint. The agent is
UNATTENDED and its prompt embeds outsider-writable PR-comment text, so an injected instruction
could otherwise drive an auto-approved call the central gate would deny (e.g. a credential-path
read). Every request therefore passes through the same `HookManager` the dashboard and Slack
paths use, BEFORE the app-local checks, and a platform `deny` refuses the call; the app-local
allowlist/denylist remains an additional restriction on top. The hook layer fails CLOSED:
the platform gate is the only thing that carries the enterprise ceiling,
`BUILTIN_DENIED_RULES` and the `~/.aws`/`~/.ssh` path blocks, so failing open would drop exactly
the checks the app-local list does not make.

**Pinned invariants.**

- `SessionAgentRunner._run_async` consults `hooks.on_tool_call` BEFORE the app-local checks and refuses on a platform deny (a `~/.aws/credentials` read is denied, a benign read is not); a structural test pins the order. (D-32)
- A broken or unavailable `hooks.on_tool_call` layer DENIES the unattended tool rather than authorizing it. (D-37)
- `_governance_denial` builds its hooks config with the keystone `denied_commands.json` overlay (`hooks_config_from_config_dict`) and reads the event's own shell flag, keeping the command-derived signal only as a fallback. (D-93)
- `_credentials_are_unconfined()` requires an effective `strict` sandbox mode before the runner is built, unless `acceptUnsandboxedAgentRisk` is exactly `true`; an unreadable config fails closed and leaves the loop offline. (D-137)

### The subprocess AgentRunner is not selected

`AgentRunner` (the `claude -p` subprocess runner) is a class nothing constructs.
Both selection sites refuse instead of falling back to it: `runner._build_runner`
returns `None` (offline) and `pr_watchers._make_runner` raises, which
`_run_watcher` turns into `STATUS_ERROR` on that watcher — a failed pass, not a
dead gateway. That covers a provider whose `ensure_agent_registered()` fails as
well as no provider at all. Shelling out there would run an unattended agent with
`--dangerously-skip-permissions`, outside the provider's permission gate, exactly
when the platform is unhealthy. Both sites' selection paths are pinned by tests.

`SessionAgentRunner.available()` is `cfg.create_provider_factory() is not None`,
and `create_provider_factory` never returns `None`; `available()` is False only
when the config load or the factory raises, which is a broken install rather than
an unconfigured one. So no working configuration loses its fix author.

The class keeps its hardening for any future caller that routes it properly:

- **Strict sandbox.** The spawn runs through `sandboxed_spawn_argv(mode="strict")`
  plus `popen_limited`. `standard` leaves `~/.aws` readable so a test suite can use
  the AWS CLI, which is right for the gate's `_run` and wrong for a fix-authoring
  agent that needs no credentials; under `strict` the child sees no `~/.aws`
  entries. `TestFallbackAgentCannotSeeCredentials` pins the mode rather than the
  filesystem, so it stays meaningful on a host without user namespaces.
- **Credential-shaped env names stripped.** `kiro_crew.sandbox.scrub_env` does not
  cover `GITHUB_*`, so after the sandbox builds the env the spawn also strips
  credential-shaped names (`*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*API_KEY*`,
  `*CREDENTIAL*`) through `push_policy.strip_credential_env`, the same filter the
  gate uses, with `PATH` kept.
- **Per-tool audit.** See "Why the subprocess fallback audits every tool it uses".

The spawn lives in its own uniquely named method, `_spawn_sandboxed_agent`,
because `test_spawn_audit` keys findings by `file::function` and this module has
two `run` methods (`AgentRunner` and `SessionAgentRunner`); inline, the spawn would
be attributed to the wrong one and reported as unrouted.

**Pinned invariants.**

- `runner._build_runner` returns `None` (offline) when `ensure_agent_registered()` fails; it never falls through to `AgentRunner`. (D-59)
- `pr_watchers._make_runner` RAISES on the same fall-through, and `_run_watcher` turns it into `STATUS_ERROR`; all four selection paths are pinned for both sites. (D-60)
- Neither selection site selects the subprocess fallback; `AgentRunner` is kept only for a future caller. (D-62)

### Detect-and-refuse vs redact

There are **three** ways content leaves this app — the draft-PR push, the F10 direct
push, and the operator's one-click commit — and they share one scanner in
`spine/push_policy.py`. A credential gate guarding only some of the exits is not a gate.
`TestEveryPushPathScansContent` asserts structurally that each path delegates rather than
keeping its own copy.

The push scan **detects and refuses**; it never rewrites. Redacting a code diff would
corrupt the very fix the gate just proved, so a credential hit sends the change to the
durable queue (`pr_queue/<fp>.diff`) for a human to look at rather than publishing a
silently-altered patch. PR *prose* is the opposite case — a rewritten sentence is still a
valid sentence — so the title and body are redacted in place.

Every PR-queue text artifact (the verified ``.diff`` and its ``.pr.md`` description) is
written as UTF-8 by the GitHub recipe, the direct-commit fallback, and the dry-run recipe.
Candidate content is arbitrary Unicode, so the durable queue must not depend on the host's
legacy Windows code page.

Prose has two distinct failure modes. "Scanned, and it had a hit" is redacted and ships.
"Could not scan at all" raises `ProseRedactionUnavailable`, so `draft()` degrades to the queue
instead of calling `gh pr create`. The prose is a separate artifact from the diff, it is the part
the agent wrote most freely, and a published PR description cannot be un-published — it persists
in the API's edit history even after an edit — so this path fails closed like every other egress
path in the app (`mcp_server._redact_result`, `routes._redact_for_display`). The queue copy is
still written from the raw text, because it never leaves the host and a human needs to see what
the agent actually wrote.

The direct-push scan diffs `HEAD~1..HEAD` — the verified commit — NOT `<branch>..HEAD`: the
commit sits on the tip of that local branch, so a branch-vs-HEAD diff is EMPTY and the
fail-closed scan would pass on 0 bytes, a blind gate.

**Pinned invariants.**

- A prose redactor that RAISES degrades `draft()` to `QUEUED:<fp>` with `gh` never invoked; the queue copy is still written. (D-17)
- Every reader of run evidence redacts its whole payload with `_redact_tree`, the progress series and the finding-detail `run`/gate blocks included, and the reader sweep enumerates every reader so a new one cannot be added unscanned (`TestEveryReaderOfRunEvidenceRedacts`). (D-19)
- `commit._commit_message` and `Driver._redact_commit_message` fall back to a fixed prose-free subject when the redactor errors, never committing unscanned agent text. (D-38)
- `runner._redact_activity` and `pr_watchers._redact` fail CLOSED, substituting a fixed placeholder for an unscannable string, because each is the only pass before the browser. (D-58)
- Provisional commit messages are fixed text with no model-chosen `cand_id`, because a pushed message cannot be redacted afterwards; the id stays in the archive and ledger. (D-69)
- Every exception-derived run error (`_state.error`) is redacted before the browser; the numeric canary message is the one marked `redaction-exempt`, and the structural guard excludes `_fail` by source range. (D-81)
- Watcher `as_dict()` snapshots and the chat-session list/get/save responses are redacted before the browser. (D-92)
- The commit route and both draft paths redact git-derived `error` text before the browser; a structural guard keyed on `.get("error")` catches a reintroduction. (D-102)
- `GET /profile/{fp}` and `GET /profiles` redact the profiler frame tree with `_redact_tree`, and the reader sweep requires the call form `redactor(`. (D-122)

### Why the watcher keeps its shell

`allowed_tools` was accepted by `SessionAgentRunner.run` and never forwarded to the event
loop, so the approval granted whatever a request asked for. That is worst for a **watcher**:
its prompt is built from PR-comment text an outsider can write, and it runs against an
authenticated `gh`. The prompt fences that text as untrusted DATA, but a fence the model
must choose to obey is not a control.

Removing Bash for watchers would break the feature — the watcher's task is "run the repo's
build, test and lint commands, find the root cause, and fix it". So the shell stays and the
**verbs** are denied. The source of truth is `spine/agent_runner.py` `_FORBIDDEN_SUBCOMMANDS`
and `_FORBIDDEN_BINARIES`; they cover, among others, `git push` and `git remote set-url`,
`gh pr merge`/`ready`/`close`/`comment`/`review`/`edit`/`create`, every `gh issue` and
`gh release` verb, `gh api`, `gh auth`, `gh secret`, `gh workflow run`, and the
`curl`/`wget`/`nc`/`ncat`/`netcat`/`ssh`/`scp`/`sftp`/`telnet` binaries. The repo's own
`is_sensitive_bash_command` does not cover this — it allows `gh pr merge`.

**What that leaves un-confined, stated plainly.** The denylist gates the command a request
ASKS for, not what that command then does, so it is a first barrier and not a boundary:

* **Credentials ARE confined.** Under `sandboxed_spawn_argv(mode="strict")` a NESTED process
  sees `~/.aws`, `~/.config/gh` and `~/.docker` as empty on a host where they are populated,
  and `~/.ssh` exposes only `known_hosts` (host-key verification needs it) while `id_rsa` and
  `*.key` stay hidden. A nested `gh auth status` reports "not logged into any GitHub hosts".
* **Network egress is NOT confined.** The sandbox never enters a network namespace —
  `CLONE_NEWNET` appears nowhere in `sandbox.py`, and its own docstring explains that agentic
  commands need reachable networking. `curl`/`wget`/`nc` are denied, but `python helper.py` is
  allowed and can open a socket.

**Watchers therefore need two operator consents, both default OFF.**

- `watcherAcceptEgressRisk` must be exactly `true`, or every watcher runner build is
  refused (`pr_watchers._make_runner`, read fresh on each build), including a manual
  `POST /watchers/{fp}/start`. It acknowledges that an unattended agent driven by
  PR-comment text can reach the network with the host's credentials.
- `watcherAutoStart` gates promotion only: without it, `GET /watchers` stays read-only and
  starts nothing, so the self-healing loop is something an operator switches on
  deliberately (same shape as `autoPublish`). Orphan-clone reclamation runs either way — it
  only deletes scratch directories.

So the operating rule is: **turn these on only for repositories whose pull-request comments
you would be willing to execute.** Closing this properly needs a network-isolating
sandbox primitive at the platform layer, which is a change to shared infrastructure and not
something this app should grow privately. Recorded at the `security_posture` disclosure sink
so it appears in the posture snapshot rather than only here, and pinned by
`TestWatcherSandboxConfinesCredentialsButNotEgress` — which asserts the half that IS enforced,
so a regression in the credential confinement fails loudly instead of silently widening the
gap.

Two asymmetries worth keeping straight:

* `allowed_tools=None` means "no restriction imposed" (pre-existing callers); `[]` means
  "no tools at all" — `agent_discovery` uses the empty list to force an answer from context.
  Conflating them would invert that call site into granting everything.
* The denylist is matched loosely (substring, normalized). For a denylist, erring toward
  refusal is the safe direction — but the read-only diagnostics the prompts actually name
  (`gh pr checks`, `gh pr view --comments`, `gh run view --log-failed`) are pinned by test,
  because a new entry that caught one would break the watcher while looking like a
  hardening.

**Pinned invariants.**

- The `gh` denylist refuses every publishing verb (`pr comment`/`review`/`edit`/`create`, `issue comment`/`create`/`edit`/`close`) through every evasion form; the app's own draft PR builds its argv directly and never passes the denylist. (D-73)
- A watcher pass whose work was not exported durably keeps its isolated clone (`_export_is_durable`), and the orphan sweep still reclaims a genuine orphan. (D-83)
- The watcher keeps `Bash`; its credentials are confined by `strip_credential_env` (GH tokens) plus `sandboxed_spawn_argv(mode="strict")` hiding `~/.config/gh`. (D-84)
- `_export_is_durable` treats a FAILING diff as "cannot tell" and retains the clone; only an artifact or a succeeding empty diff counts as durable. (D-100)
- The watcher's network-egress limit is disclosed at the `security_posture` sink, and four tests pin the half that is enforced. (D-105)
- `GET /watchers` promotes watchers only with `watcherAutoStart`; orphan-clone reclamation runs either way. (D-107)
- `_export_is_durable` also checks the tree for UNCOMMITTED work, so a watcher's uncommitted fix is not deleted as if exported. (D-114)
- No watcher runner is built unless `watcherAcceptEgressRisk` is exactly `true`, read fresh on every build. (D-118)
- The orphan-clone sweep re-checks ownership at the point of deletion (`_delete_clone_if_unowned`), so a clone registered mid-sweep is spared. (D-119)
- `_delete_clone_if_unowned` holds `reg._lock` across both the ownership check and the `rmtree`. (D-121)
- Every exit from `_run_agent_pass`, the exception and timed-out paths included, checks `_export_is_durable`. (D-136)

### Why the approval audit is `critical`

The loop's auto-approve is unconditional and unattended, so the SEL entry is the
only record that a tool ran at all. Losing it loses the event, so the tool is
refused when the log cannot be written. The MCP dispatch follows the same
audit-or-deny rule for the same reason; see "Why the MCP dispatch is audited
before the handler runs".

### Why BOTH urls, not just the push url

`git push --push` is honored only when the caller pushes **by remote name**.
`git push "$(git remote get-url origin)" HEAD` ignores the push url entirely and writes to
the fetch url — and the loop's agent runs auto-approved Bash *inside this clone*, so a
repository instruction could do exactly that: with only the push url disabled, pushing by
name is refused while pushing to the fetch url lands a new branch upstream, and
`push_disabled` would report true for a clone that can still write to the remote.

Both urls are therefore neutralized, and `_ok` verifies both before reporting success. The
real remote is carried in config as `origin_url` — deliberately NOT in `_CONFIG_WRITABLE`,
since like `clone`/`target_url` it decides where a push can land — and handed explicitly to
the three trusted publishers (PR-draft recipe, F10 direct push, one-click commit). Each
still falls back to reading the clone's remote, which yields the sentinel and is
refused: an older config degrades to "queued locally", never to an unguarded push.

**Pinned invariants.**

- `RepoIsolation.push_disabled()` checks BOTH urls, matching `clone_setup._ok`: a push-only-disabled clone reports False. (D-33)

### Why the stored push destination is pinned to the validated repository

``resolve_origin_url`` is the single place the push destination is resolved for all three
exits — the draft-PR push, the F10 direct push and one-click commit — so whatever it returns
is where a verified change goes. It never returns a stored ``origin_url`` verbatim; every
caller treats ``""`` as "no push target", so each refusal below fails closed.

1. **Host allowlist.** The network host must match exactly, never by ``endswith`` —
   ``evilgithub.com`` and ``github.com.attacker.net`` both fail. ``http://`` and ``git://``
   are refused because cleartext is never this app's push transport, and the
   ``DISABLED_NO_PUSH`` sentinel is refused because it is a marker rather than a
   destination. ``validate_target_url`` cannot be reused for this check: it accepts only
   ``https://`` input, while ``setup_safe_clone`` stores the SSH form
   ``git@github.com:owner/repo.git`` whenever ``gh`` prefers ssh.
2. **Repository identity.** ``github.com`` is itself an allowed host, so the host alone
   would let an edited ``config.json`` keep the host and swap the path. A network
   ``origin_url`` must therefore name the same ``owner/repo`` as the validated
   ``target_url``, compared transport-agnostically through ``_remote_slug``. When the two
   differ, or ``target_url`` is missing or does not validate, there is no push target.
3. **Local paths stay allowed.** ``/tmp/x.git`` and ``file://`` have no network host to
   redirect to; they are what the app's own tests push to, and a local bare repo is a
   legitimate offline setup. On Windows a drive path (``C:\work\repo.git`` or
   ``C:/work/repo.git``) is local too, even though ``urlparse`` reads the drive letter as a
   scheme. Elsewhere the same text stays refused, because git on POSIX parses ``C:path``
   as scp-like ssh to a host named ``C``.

With no ``origin_url``, the legacy ``target_url`` is re-validated and its rebuilt clone URL
is used. The security guidance on untrusted URL destinations asks for exactly this:
allowlist the destination rather than trust persisted input.

**Pinned invariants.**

- `resolve_origin_url` host-allowlists a stored `origin_url` (exact host, no `http://`/`git://`, no sentinel); local paths stay allowed. (D-57)
- A Windows drive path is a local `origin_url` only when running on Windows; on POSIX the same text is ssh to a one-letter host and stays refused (`test_a_drive_shaped_origin_stays_refused_where_git_reads_it_as_ssh`).
- A network `origin_url` must name the same `owner/repo` as the validated `target_url` (`_remote_slug`); a mismatch or an invalid `target_url` leaves no push target (`test_origin_url_wins_when_present`). (D-138)

### Why tool approval is one-shot and a queued change is not "filed"

Two rules keep a single success from granting a permanent exemption.

``_approve`` runs the per-tool allowlist check and a ``critical=True`` audit-or-deny write, then
calls ``approve_tool(rid, always=False)``. ``always=True`` would mean "the user picked 'always
allow'" — ACP backends may turn it into an ``addRules`` suggestion — so the provider would stop
sending permission requests and every LATER matching call would skip both gates. The unattended
loop is precisely the caller that must not buy a blanket exemption with its first approval, so
the approval is one-shot.

``pr_recipe.draft`` returns ``QUEUED:<fp>`` when the change is on disk but no pull request could
be opened (no ``gh``, no network, a refused push). The pipeline records that as SOFT-terminal
``STATUS_ERROR``, retryable once the cooldown elapses, not as ``filed``: ``filed`` is
HARD-terminal in ``Ledger.known`` — "a filed CR is never re-filed" — so the locus would be
deduped forever, and ``filed_crs()`` would hand the PR watchers a non-URL.

``CrOutcome.filed`` nevertheless stays **True**. In the driver ``filed`` means "this was a
realized win", and a False there would also roll the provisional commit back and decrement
``kept`` — throwing away a change that passed RED×2 → GREEN → STAYGREEN merely because ``gh``
was absent. The win is real and the durable queue copy holds it; only the publication failed,
which is what the retryable ledger status records.

**Pinned invariants.**

- Tool approval is one-shot (`always=False`), so every later call still passes the per-tool allowlist and the `critical=True` audit. (D-55)
- A `QUEUED:<fp>` outcome is recorded as soft-terminal `STATUS_ERROR` (retryable after cooldown) while `CrOutcome.filed` stays True, so the win and its counters are kept. (D-56)

### Known limitation: a second perf PR carries the first perf fix

The perf loop is EVOLUTIONARY: "current best == HEAD" is its durable state, `base_sha` is
re-read from HEAD every cycle, and every measurement is reported as "Δ vs current best". So a
kept perf winner deliberately stays on the local branch — that is what the next cycle measures
against.

The consequence: the draft PR pushes the clone's whole `HEAD`, and the PR is opened against the
REMOTE base, so a SECOND cycle's PR contains the first cycle's fix as well.

Resetting after the filed-PR event, as the bug track does, would invert the premise: each cycle
would re-measure against the ORIGINAL base, so a second improvement to the same hot path could
never register as an improvement. The bug track has no such property (independent loci, one PR
each), which is why the reset is correct there and not here.

Rebuilding a per-winner branch from the remote base would satisfy both goals in principle, but
it is not a safe drop-in: two cycles improving the SAME line produce a patch that does not apply
to the untouched base, and a naive rebuild produces a branch containing NEITHER fix. Doing it
properly needs a cherry-pick with conflict handling plus a decision about what to publish when
the replay fails — a design change, not a bug fix.

The limitation is latent: the perf track has not kept a measured win on a real repository (see
the target-suitability limit above), so no perf PR has been filed for a second cycle to
contaminate. A maintainer enabling perf on a suitable target should close this first.

**Pinned invariants.**

- The perf filed path deliberately does NOT reset, keeping cumulative measurement; two tests pin the perf/bug asymmetry. (D-71)

### Known limitation: the sandbox hides credentials, it does not confine WRITES

This bounds every "sandboxed" claim above. `sandboxed_spawn_argv(mode="strict")` bind-mounts
credential directories empty and scrubs the environment, so agent-authored code cannot READ the
operator's secrets (under "standard" a child sees `~/.aws`; under "strict" it sees nothing). It
does **not** make the rest of the filesystem read-only: a strict-mode child can open a same-user
file such as `~/.probe` for writing.

So a candidate's own `conftest.py` or reproducing test — code the model wrote, executed by the
gate — can modify same-user files outside the worktree.

**Kiro Crew's own control files are closed.** The paths in `security.write_protected_home_paths()`
are protected by the platform HOOK layer, which a sandboxed subprocess never passes through. So
`_run` passes the PARENT directory of each write-protected path as `extra_hidden_dirs`,
bind-mounting an empty dir over it, and the write fails at the kernel; the interpreter and the
real-subprocess gate tests are unaffected.

The mask must name a DIRECTORY: `extra_hidden_dirs` reaches the launcher's `sensitive_dirs` loop,
which is guarded by `os.path.isdir(target)`, so a file path is silently skipped (files go through
a separate `sensitive_files` list the public helper does not expose). And the mask cannot be
widened to `$HOME`: the interpreter's own stdlib can live there — hiding `~/.local/share` breaks
`import platform`.

**What remains open**: arbitrary same-user files elsewhere (`~/notes.txt`) are still writable.
Kiro Crew's own home is sealed per leaf (`sandbox.py` marks each crew-home leaf hidden,
read-only or visible), but `kiro_crew.sandbox` has no general write confinement for arbitrary
same-user files, and adding one changes the shared sandbox for every caller. Failing closed at
`profile._run` until then would disable the entire bug track (no test could run at all).

**Pinned invariants.**

- `_run` masks the PARENT directory of each `write_protected_home_paths()` entry through `extra_hidden_dirs`, so a strict-mode child cannot write Kiro Crew's own control files. (D-53)

### Why the credential scan resolves its base (and a refused push rolls back)

The pre-push credential scan must cover what is pushed: the scan RANGE and the pushed RANGE must
agree.

`pr_recipe._scan_pushable_content` diffs `base...HEAD`. `base_ref` is `config["branch"]` — a
plain LOCAL name if the operator set one — and with HEAD on that branch the diff would be EMPTY,
so `scan_content_for_secrets("")` would report clean. `_scannable_base` therefore resolves to a
ref distinct from HEAD (trying the remote-tracking form) and REFUSES when it cannot: refusing
beats degrading to the narrower single-commit scan, because a narrower range that happens to pass
is exactly the silent downgrade this guards. `driver._direct_push` follows the same rule.

A REFUSED direct push rolls its commit back. The direct-push scan range is `HEAD~1..HEAD` — one
commit — so a refused commit left at HEAD would be outside the next winner's scan range while its
push published both. Both tracks therefore `_reset_provisional(pre_sha)` on a failed push.

**Pinned invariants.**

- The F10 direct push scans `HEAD~1..HEAD` (the verified commit), never `<branch>..HEAD`, which is empty when HEAD sits on that branch's tip. (D-26)
- `_scannable_base` resolves a base distinct from HEAD (trying the remote-tracking form) and REFUSES when it cannot, so the recipe's scan can never self-diff to zero bytes. (D-64)
- A refused direct push rolls its commit back (`_reset_provisional(pre_sha)`) on both tracks, so the next winner's push cannot publish it outside its own scan range. (D-65)

### Why the shell denylist unwraps before it matches

One rule governs this parser: **a check that inspects ONE position is evaded by adding a
position.** So the denylist tokenizes and skips global options (`gh --repo o/r pr ready`),
splits on every separator (`&&`, `||`, `;`, `|`, a bare `&`, `$(`, backtick, `(` and `)`), and
does not trust `words[0]`: anything that RUNS another command — `sudo`, `env`, `timeout`,
`nohup`, `xargs`, `nice`, `setsid`, `stdbuf`, every `sh -c "…"` form — is unwrapped.

Wrappers are stripped and the command behind them checked instead, recursively (so
`sudo env timeout 3 git push` and `sh -c "sudo git push"` both resolve), and a shell's `-c`
argument is re-analyzed from the top so separators and further wrappers inside the string
are seen too. An option's VALUE goes with it (`nice -n 5 git push` must not leave `5` looking
like the command), and the recursion budget REFUSES on exhaustion — for a denylist, "gave up"
must not mean "allowed".

Reaching the subcommand also means skipping GLOBAL OPTIONS correctly. A valueless option must not
swallow the verb: `git --no-pager push`, `--paginate`, `--bare`, `--literal-pathspecs` and
`--no-replace-objects` must be refused like a bare `git push`. Value-taking global options are
therefore enumerated per binary (`_VALUE_TAKING_OPTIONS`) and anything unlisted is treated as
valueless. That direction is the safe one for a denylist: an unlisted value-taking option means
its value is read as a subcommand, which can only over-refuse a benign command, never
under-refuse a forbidden one. `--exec-path` is deliberately NOT in the table because its value is
*optional* (`--exec-path[=<path>]`), and listing it would let `git --exec-path push` swallow the
verb. Value-taking wrapper LONG options (`_WRAPPER_VALUE_TAKING_LONG_OPTIONS`, such as
`env --unset`) consume their value the same way.

The table also covers SHELL BUILTIN wrappers, not just binaries on PATH: `command`, `exec` and
`builtin` never appear as executables, but a nested `sh -c "…"` argument is re-analyzed by this
same table, so `command git push`, `exec git push` and `sh -c "command git push origin main"`
are refused.

`_COMMAND_WRAPPERS` is deliberately not a "forbid these binaries" list. `env`, `timeout`
and `nice` are legitimate and the gate's own test runs use them, so `timeout 5 pytest -q`
stays allowed; a denylist that breaks the build while looking like a security improvement
is the failure mode to avoid. The same holds for the builtins — the wrapper is stripped only
so the real verb behind it can be judged, so `command -v git` (asking where git is, not
pushing) and `exec pytest -q` stay allowed. A dedicated test pins that non-over-refusal.

**Pinned invariants.**

- Wrappers (`sudo`, `env`, `timeout`, `nohup`, `xargs`, `nice`, `setsid`, `stdbuf`, `sh -c`) are stripped recursively, an option's value goes with the option, and a nested `-c` script is re-analyzed from the top; `timeout 5 pytest -q` stays allowed. (D-18)
- The wrapper table covers the shell builtins `command`, `exec` and `builtin`; `command -v git` and `exec pytest -q` stay allowed. (D-52)
- Value-taking global options are enumerated per binary (`_VALUE_TAKING_OPTIONS`) and everything else is valueless, so `git --no-pager push` is refused like `git push`; `--exec-path` is excluded because its value is optional. (D-63)
- Value-taking wrapper long options (`_WRAPPER_VALUE_TAKING_LONG_OPTIONS`, such as `env --unset`) consume their value, so the command behind them is inspected. (D-117)
- A bare `&` splits commands like `;`, and `(` and `)` split too, so `true & gh pr comment …` and `echo $(gh pr ready)` are refused; a benign trailing `& true` passes. (D-128)

### Why the protected-branch denylist normalizes before it matches

A denylist has to see a branch the way **git** does, not the way it was typed.
`refs/heads/main` is the same ref as `main` — `git push <url> HEAD:refs/heads/main` accepts it
verbatim — so `is_protected_branch` must match it. `branch` is in `_CONFIG_WRITABLE`, so a
`PUT /config` sets it with no shape check on write and both one-click commit and the driver's F10
path feed it straight through, which makes this reachable rather than theoretical.

`normalize_branch` therefore strips `refs/heads/`, `refs/remotes/`, `origin/` and
`upstream/` **repeatedly until the value stops changing**, not in one ordered pass, so
`origin/refs/heads/main` is caught too. The loop is bounded at six iterations rather than
`while True` so a crafted `origin/origin/origin/…` cannot spin.

The general rule is the shell denylist's: **any normalization that runs once can be re-nested**,
so the test enumerates the respellings git accepts.

**Pinned invariants.**

- `normalize_branch` strips ref and remote prefixes repeatedly until stable, so `refs/heads/main` and `origin/refs/heads/main` hit the protected-branch denylist like `main` (`branch` is client-writable through `PUT /config`). (D-13)

### Why branch checkout prefers the remote-tracking ref over a fetch

**Code inside a deliberately push-disabled clone cannot reach the remote for READS either.**
A fetch of `origin/<branch>` always fails there (exit 128 — the origin is neutralized), and in a
fresh clone a local branch exists for the DEFAULT branch only. So `checkout_branch` tries the
remote-tracking ref first (`checkout -B <branch> origin/<branch>`), which the clone already has
for every branch that existed at clone time, then a local branch for the genuinely-offline case,
and only then fails. A branch that exists in neither still fails — turning a missing branch into a
false success would let a run proceed on the wrong tree with an `ok` verdict.

A run on the wrong branch would discover, edit and measure `main` while the operator believes it
is working on the branch they configured. So a failed checkout makes both the run and calibration
refuse to start, whether or not `scopeDiffBase` is set; a configured `scopeDiffBase` only adds
detail to the error.

The DRIVER follows the same rule from the other side: its stage steps check out
`normalize_branch(self.branch)`, the local branch `runner` created before the loop started, never
the config form (`origin/main`), because `git checkout origin/main` detaches HEAD onto the
remote-tracking ref and would orphan each cycle's kept commit. So cycle N+1 builds on cycle N.

**Pinned invariants.**

- In a push-disabled clone `checkout_branch` lands on the CONFIGURED branch through `origin/<branch>` (no network), and the test asserts the working tree, not only the return value. (D-16)
- Both driver stage sites check out `normalize_branch(self.branch)`, the local branch, so cycle N's kept commit stays an ancestor of HEAD after cycle N+1's checkout. (D-21)

### Why one-click commit fetches through the configured url, not `origin`

Neutralizing both origin urls means **nothing inside the clone can reach the remote, including
the reads**: `git fetch origin <branch>` exits 128 in every clone the loop works in.

`commit_finding` therefore fetches through the same validated `origin_url` the push uses — one
lookup serving both — into `refs/auto-improvement/commit-base`, a ref this module owns, and
checks out / resets / diffs against that ref. It never falls back to `origin/<branch>`: in a
frozen clone that tracking ref is a snapshot from clone time, so committing on it would silently
drop whatever landed upstream since — a lost update that reports success. With no configured url
at all the function commits on the stale local ref (the push cannot succeed either, so the
outcome is "committed locally only") rather than pretending to publish.

The regression test drives the real function against a real bare repo with the origin
neutralized exactly as production leaves it, so the success path is covered, not only the
refusals; a second case advances the branch upstream first to pin that the base is fetched rather
than remembered.

**Pinned invariants.**

- `commit_finding` fetches its base through the validated configured url, never `git fetch origin`, because every clone the loop uses has both origin urls neutralized. (D-12)
- One-click commit's no-remote and push-rejected exits reset `--hard` to the fetched base and drop the `sha` from the payload; the queue copy survives for a retry. (D-54)

### Why drafting a PR needs a push

The upstream review CLI uploaded commits through a side channel that was not the
git remote, so it could draft a review from inside a push-disabled clone. GitHub
has no such channel — a PR is a comparison between two refs that both exist on
the remote. The fix branch is therefore pushed, and the relaxation is narrowed the
same way the spine's direct-commit mode narrows it: a generated app-namespaced
branch, pushed to the origin **fetch** URL for that one ref while the push remote
stays `DISABLED_NO_PUSH` for everything else, and run through the protected-branch
denylist first. A refused or failed push degrades to the durable queue
(`pr_queue/<fp>.diff` + `.pr.md`) so a verified change is never lost.

### Why every ``{fp}`` route validates at the boundary

The finding fingerprint from a URL is interpolated straight into a filesystem path —
``pr_queue/<fp>.diff``, the per-repo ledger subtree, a watcher clone dir — so an unvalidated
``fp`` is a path-traversal vector (``..``, an absolute path, a value with a slash). Every handler
that takes ``fp`` from ``match_info`` calls ``_validated_fp``, which runs
``ledger_admin.validate_fingerprint`` (allowlist ``^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`` — no
``.``, ``/`` or ``..``; rejects rather than sanitizes) at the HTTP boundary, before any sink:
allowlist at the point of origin, block traversal, fail closed.

**Pinned invariants.**

- All `{fp}` handlers validate through `validate_fingerprint` (`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`) via `_validated_fp`; a traversal value returns 400/404 before any path is built. (D-22)

### Why calibration pins its workspace

Findings, the ruler, the PR queue and profiles are scoped per repository+branch, and the
path helpers derive that scope from live ``config.json``. Calibration runs on a background
thread and can take seconds to minutes, so a scope read at WRITE time would let an operator
retarget mid-run and land the ruler in a different workspace — overwriting a ruler calibrated on
unrelated code. ``_calibrate_loop`` therefore derives its path from the config the worker was
LAUNCHED with (``workspace_key(config)``), not the live file.

**Pinned invariants.**

- `_calibrate_loop` derives the ruler path from the CAPTURED config, so a retarget mid-calibration cannot overwrite another workspace's ruler. (D-23)
- Calibration holds the clone lock across its whole loop body, checkout and `baseline_samples` included. (D-80)

### Why operator clone mutations hold one lock

The run-active gate stops the commit and draft routes racing the LOOP. It does not stop them
racing EACH OTHER, and both mutate the same `config["clone"]`, each in its own
`asyncio.to_thread` thread. Interleaved, B's `checkout -B <branch> <base>` does **not** discard
A's staged diff (the branch is already at that base, so no files change), B's `git apply --index`
stacks on top, and B's commit publishes both findings; A's now-empty commit then fails and its
`reset --hard` rewinds the local branch past B's already-pushed commit.

`commit.clone_lock()` (an `RLock`, module-level because there is exactly one configured clone)
serializes every operator-triggered mutation. The draft route holds it across its **whole**
sequence — materialize → commit → draft → rollback — not around each call, because the race is
between the steps. The button is additionally disabled while any commit is in flight, which stops
the operator queueing work rather than being the correctness mechanism.

The regression test FORCES the interleaving by parking thread A immediately after it stages,
rather than starting two threads and hoping, so it is deterministic.

**Pinned invariants.**

- One-click commit refuses with 409 `run_in_progress` while the supervisor is RUNNING, CALIBRATING or STOPPING, and never calls `commit_finding` then. (D-20)
- The draft route refuses with 409 `run_in_progress` while a run is live, and a structural test asserts every clone-mutating handler carries the gate. (D-49)
- Operator clone mutations serialize on `commit.clone_lock()` (an `RLock`) across the whole sequence, and the commit control is disabled while a commit is in flight; the test forces the interleaving. (D-61)
- `PUT /config` and `POST /setup-clone` refuse with 409 `run_in_progress` mid-run, through the one `_refuse_while_running` helper every handler uses. (D-68)
- Run startup holds `commit.clone_lock` across `_build_driver` (`_build_driver_locked`), and the lock is re-entrant. (D-74)
- `POST /setup-clone` holds the clone lock across one `_clone_and_persist` section and run startup holds it from config read to `start`, so a run cannot launch against a repository being replaced. (D-77)
- `start`/`calibrate` set a `_reserved` flag under the lock that the worker clears as its first act, so a run cannot double-start in the assigned-but-unstarted window. (D-94)
- `POST /setup-clone` re-checks `_run_is_active()` inside `clone_lock` and answers 409 `run_in_progress`. (D-95)
- `PUT /config` takes `clone_lock` and re-checks `_run_is_active()` inside it; a structural test asserts both workspace-mutating handlers do. (D-96)
- The draft and commit routes re-check the run status inside `clone_lock`; a structural guard asserts all four clone-mutating handlers do. (D-115)

### Why the manual draft path materializes its own diff

``pr_recipe.draft(diff=...)`` only WRITES the queue copy; the branch content comes from
``_push_fix_branch``, which pushes the clone's ``HEAD``. In the loop that is correct — the
driver's ``_stage_winner`` applies the winner into the shared clone before the pipeline drafts.
The backend's **manual** draft button has no loop around it, so without its own staging it would
publish whatever a LATER cycle left at ``HEAD``: the pull request's metadata and its content
would disagree, which is worse than a failed draft because it looks successful.

The fix reuses the one place that already does this correctly. ``commit.py``'s one-click commit
fetches the real base, ``checkout -B``, then ``git apply --index``; that block is
``materialize_queued_diff`` and BOTH paths call it, so the draft route stages the finding's own
diff before drafting, and a failed staging returns an error instead of drafting anyway. One copy
matters here: the base-ref resolution carries the fetch-through-configured-url rule and never
trusts the frozen ``origin/<branch>`` tracking ref.

Staging is not enough on its own: ``git apply --index`` populates the index but does not move
``HEAD``, and ``_push_fix_branch`` pushes ``HEAD:refs/heads/<branch>``. So the route commits via
``commit_staged_for_draft`` (reusing the redaction-hardened ``_commit_message``, since a pushed
commit message cannot be edited without rewriting history), and the regression test performs a
real push and reads the content back off the remote rather than asserting the worktree.

Committing means every later failure has to ROLL BACK. The commit sits on the configured branch,
and ``clone_setup.checkout_branch`` prefers an existing local branch — so a draft that published
nothing (no ``gh``, no network, a refused push, or an unexpected raise) would leave the next run
starting from an unfiled commit. All post-commit exits ``reset --hard`` to the fetched base, as
``commit_finding`` does at each of its own failure points, and a successful draft resets in
``finally`` after its ledger row is written; the durable queue copy is untouched, so a retry still
has everything it needs.

Materializing the diff makes the route clone-MUTATING, so it carries the run-active gate every
other clone-mutating handler has: `checkout -B` / `apply --index` (plus `reset --hard` on a failed
apply) run in `config["clone"]`, the same tree the driver's worker thread may be mid-cycle on. The
route returns 409 `run_in_progress` while the supervisor is RUNNING/CALIBRATING/STOPPING,
identical to `_handle_commit`, and a structural test asserts every clone-mutating handler carries
the gate.

Queued-diff materialization encodes patch text as UTF-8 with `surrogateescape`
and sends binary stdin to Git, preserving line endings and non-UTF-8 bytes on
every platform. Apply-failure stderr is decoded with replacement before redaction
and truncation; failure still resets the clone to its selected base.

**Pinned invariants.**

- Commit and draft both call `materialize_queued_diff` (fetch → `checkout -B` → `apply --index`), so drafting an older finding publishes that finding's diff; a failed staging returns an error rather than drafting. (D-47)
- `commit_staged_for_draft` COMMITS the staged diff (with the redaction-hardened `_commit_message`) before drafting, so the pushed branch holds the fix; the test reads the content back off the remote. (D-50)
- Every post-commit exit of a draft that published nothing resets `--hard` to the fetched base, so the next run never adopts an unfiled commit as its baseline. (D-51)
- A successful manual draft resets the clone in `finally`, after writing its ledger row, so its commit never leaks into the next draft's branch. (D-79)
- Ledger writes after a publish are best-effort and logged at ERROR, so a ledger failure never turns a published PR into a raised exception, and the manual path's reset is in `finally`. (D-91)

### Why an unproven ruler halts the perf track

``canaryAdvisory`` defaults to ``False``, so a perf run whose canary fails to clear the band halts
before Phase 2 and cannot keep and draft a "win" measured by a ruler that was never proven
(03_metric §7.1: "an unproven ruler must HALT"). Strictness costs the bug track nothing:
``Driver.run`` skips Phase-1 preflight *entirely* there — "preflight: skipped for bug track
(RED→GREEN gate is the verdict; no noise band)" — so a strict canary cannot reach a bug run.

The target-suitability limit is real: on an arbitrary Python repo there is no genuine known win to
force, so ``measure_canary`` forces the one mechanical win available (collect-only on the
candidate arm), which is a real correctly-signed delta but a **lower bound** on sensitivity rather
than proof the ruler resolves a 3% win; on a repo whose suite runs in about its own collection
time there is no win to force at all. The answer for such a target is an explicit operator opt-in
(``canaryAdvisory: true``) or a ``benchmarkCommand`` pointing at a real workload — not a default
that quietly lowers the bar for everyone. ``TestAnUnprovenRulerHaltsThePerfTrack`` pins the strict
default, the opt-out, and the bug-track premise, so if preflight ever stops being skipped for the
bug track the justification has to be re-derived.

A perf pull request also says whether its ruler was proven: ``perf_pr_description`` takes
``ruler_proven`` and emits a caveat block when it is False; the driver sets it from
``PreflightResult.canary_cleared`` after preflight (the pipeline is built before preflight runs,
so it cannot be a constructor argument). It defaults True so a proven ruler stays silent — crying
wolf would train reviewers to ignore the warning.

**Pinned invariants.**

- `perf_pr_description(ruler_proven=False)` carries a "Ruler not proven on this target" caveat, set from `PreflightResult.canary_cleared`. (D-46)
- `POST /calibrate` reuses the spine's `_canary_clears_band` (ok, None and direction), so a failed or regressing canary never records `calibrated`. (D-66)
- `canaryAdvisory` defaults to False, so a perf run whose canary failed halts before Phase 2; the bug track skips ruler pre-flight, and a test pins that premise. (D-72)
- `measure_canary` refuses (`ok=False`) when a custom `benchmarkCommand` is set, so the canary never certifies a ruler it cannot force a win on. (D-133)

### Why the subprocess fallback audits every tool it uses

The fallback's launch is one blanket event (``tool_name="claude-cli"``, ``critical=True``)
before spawning, which records "an unattended agent started" and nothing about which tools it
then ran. So ``_audit_fallback_tool`` also records each ``tool_use`` block the fallback parses out
of the stream (the same blocks that drive the UI activity feed), so a forensic query can answer
"did this run touch a shell?". These per-tool events are deliberately NOT ``critical=True``,
unlike the launch event: by the time one fires the tool has already run inside the sandbox, so
raising could not prevent anything and would only turn an audit-sink problem into a failed run.
The audit-or-DENY half of this path is the launch event plus the pre-spawn governance gate and the
shell denylist; this is the audit-or-RECORD half. The target hint is agent-influenced text landing
in a log that is signed as-written, so it is redacted and truncated, and a redactor failure emits
``[redaction unavailable]`` rather than raw text.

Nothing selects the fallback today (see "The subprocess AgentRunner is not selected"); the
per-tool record is part of the hardening the class keeps for a future caller.

**Pinned invariants.**

- `_audit_fallback_tool` records each tool the fallback runs (`outcome="invoked"`, redacted and truncated target hint), not `critical=True`. (D-48)

### Why the MCP dispatch is audited before the handler runs

Outcome events fire after ``fn(args)`` returns or from an ``except`` block, so a handler that dies
in a way that frame cannot catch — a killed process, an interpreter-level failure — would run a
tool with no audit trail. The dispatch is therefore audited with ``outcome="invoked"`` *before*
the handler runs (``invoked`` is the established SEL token), and the outcome event still follows.
A served call logs twice, which a test pins as an ordered pair.

That pre-dispatch event is ``critical=True`` — audit-or-DENY: it is written synchronously and a
filesystem failure is re-raised, so a call that cannot be recorded is REFUSED (``INTERNAL_ERROR``,
"the security audit log is unavailable") rather than served untraced. The OUTCOME events stay
fail-soft, because by then the handler has already run and raising could not prevent anything.

All six handlers are reads, but the criterion ``sel`` states for ``critical=True`` is
attendedness, not blast radius: "pass ``critical=True`` when the caller enforces
audit-or-deny (e.g. an unattended heartbeat auto-approve)". This server has no human in the
loop and hands its results to an LLM, so the audit event is the only record that a read ever
happened.

**Pinned invariants.**

- `tools/call` with present-but-non-dict `arguments` is rejected INVALID_PARAMS before coercion; absent, `None` or `{}` stay valid. (D-34)
- Every MCP dispatch logs `outcome="invoked"` with `critical=True` BEFORE the handler runs, so an unauditable call is refused `INTERNAL_ERROR`; a served call audits as an ordered pair. (D-45)
- MCP error paths are redacted like results: the SEL record and the JSON-RPC error never carry raw `str(exc)`, the schema validator's message included. (D-88)
- An untrusted MCP tool NAME is redacted before the SEL record and the model-facing error. (D-126)

### Why no builtin agent pre-authorizes a tool

``allowedTools`` auto-approves a tool, and an auto-approved tool never reaches the platform
governance chokepoint. This is the repo's documented architecture: ``hooks.on_tool_call`` runs
only from the ``EVENT_PERMISSION_REQUEST`` branch, while the ``EVENT_TOOL_CALL`` branch is
informational-only ("the tool is already running (auto-approved by kiro-cli), so hook results
cannot block execution"), and [governance.md](governance.md) states the consequence outright: an
agent that writes itself into ``allowedTools`` makes kiro-cli stop sending permission requests and
**Plane A never runs at all** for that tool.

So every builtin agent config ships ``allowedTools: []`` — a pre-authorized ``fs_read`` /
``fs_write`` / ``execute_bash`` would leave the enterprise ceiling, ``BUILTIN_DENIED_RULES`` and
the ``~/.aws``/``~/.ssh`` sensitive-path blocks inert for precisely the tools a repository prompt
injection would reach for. ``tools`` is deliberately RETAINED: the agent can still request those
tools, and each request is then governed and audited. This matches the sibling ``pr-author.json``
(which ships no ``allowedTools``) and the computer-use precedent of granting ``tools`` but not
``allowedTools``, and it is the posture the GenAI tool-use security guidance asks for — least
privilege, default deny.

**Pinned invariants.**

- Discovery runs under a tool-scoped agent with no `@kirocrew-core` (so no `spawn_sub_agents`) and a read-only stage allowlist, and ends in a terminal state with no `Reaper: force-killing` lines. (D-8)
- Every config under `agents/` has an empty `allowedTools`, so no tool is auto-approved past `hooks.on_tool_call`; `tools` stays so requests are governed and audited. (D-43)
- Discovery's allowlist has no `Bash` (`Read`/`Grep`/`Glob` only), and its prompt offers no shell commands. (D-89)

### Why a landed manual commit supersedes its ``filed`` row

The ledger is last-write-wins per fingerprint, and ``filed`` is what ``filed_crs()`` feeds the
pull-request watchers and what the UI reads to decide whether to offer the commit button. So
one-click commit records a ``committed`` row carrying the landed sha through
``ledger_admin.record_committed``, as the loop's own direct-commit path does; otherwise a change
already on the branch would keep reporting as an open pull request and invite a second commit.

It writes ``cr`` and never ``pr``, for the reason spelled out for the purge event:
``LedgerEntry(**row)`` is a fixed-field dataclass, so an event carrying an unexpected key raises
``TypeError`` inside ``_load()``'s torn-line handler and vanishes — leaving the record ``filed``
after all. Bookkeeping failure is logged and returns False rather than raising: the push already
succeeded, so it must not surface as an error the operator retries.

**Pinned invariants.**

- `committed` ledger rows and the success log carry the sha read back after the push, so a rebase cannot leave a sha the remote does not have. (D-41)
- `ledger_admin.record_committed` appends a `committed` row with the landed sha (writing `cr`, never `pr`), failing soft because the push already succeeded. (D-44)

### Why a rejected provisional commit discards its diff

The provisional commit stages the winner with ``add -A`` and then commits. When the commit FAILS
— a rejecting ``pre-commit`` hook, gpg/signing trouble — the helper returns False, and the diff
must not stay in the index, where the next candidate's ``add -A`` would sweep candidate A's
rejected, never-verified change into candidate B's commit. That is the same failure class as
publishing an unverified rebase — unmeasured code reaching a branch.

``_discard_staged`` (called from both the perf and bug helpers) collects the patch's ADDED
paths from the index *before* resetting, then ``reset --hard`` and removes those paths.
The order matters: ``reset --hard`` un-stages a created file but leaves it untracked on
disk, where the very next ``add -A`` picks it straight back up. Removing only the paths the
patch added keeps this targeted — a blanket ``git clean`` would also delete unrelated build
output in the operator's clone.

**Pinned invariants.**

- Both provisional-commit helpers check `git commit`'s return code; a rejected commit returns False and leaves HEAD unmoved (`TestProvisionalCommitFailsClosed`). (D-27)
- A rejected provisional commit calls `_discard_staged`, which removes the patch's ADDED paths collected from the index before `reset --hard`, so the next candidate's `add -A` cannot absorb them. (D-42)

### Why a rebased commit is re-verified before it is published

A run takes tens of minutes, so the authorized branch can legitimately advance between the
clone's fetch and the winner's push. A bare push then dies ``! [rejected] ... (fetch first)`` and
would strand a fully verified fix, so ``_push_with_rebase`` makes a single narrow
rebase-and-retry.

**A clean rebase is a statement about text, not about behaviour.** The gate result the driver is
holding was measured against the PRE-rebase base; replaying the commit onto a moved branch
produces a tree nothing has built or tested — a disjoint new file on the branch can make the
combined tree RED with no conflict. So the replayed tree is re-verified through the profile's own
build gate (``_reverify_head``) before the retry push, fail-CLOSED: a gate that returns False *or*
raises returns the original rejection, leaving the verified commit local and recoverable. The
retry stays, because dropping it would strand verified fixes on every concurrent push.

The ledger records the sha that actually **landed**: a rebase rewrites HEAD, so the caller's
pre-push snapshot can name a commit absent from the remote. ``_direct_push`` reads HEAD back after
the push and both committed-status rows record that, so an audit of "what did the bot land?"
resolves.

**Pinned invariants.**

- A `non-fast-forward` push retries once after fetch+rebase; a conflict aborts; the app never pushes with `--force`. (D-6)
- `_push_with_rebase` re-verifies the replayed tree through the profile's build gate before pushing; False or a raise returns the original rejection. (D-40)

### Why host-side git cannot run repository-controlled code

The sandboxed agent edits the same worktree and clone the app's OWN git commands (`add`,
`commit`, `checkout`, `diff`, `push`) then run in on the HOST as the gateway user. Anything the
repository can make git execute — a hook pointed at by `core.hooksPath`, `core.fsmonitor`, an
attribute-bound filter or diff driver — would therefore run outside the sandbox. `spine/git_safety.py`
is the one place that closes this: every host-side git call goes through its config flags and its
attributes pin, and a structural test enumerates the helpers that must.

**Pinned invariants.**

- Every host-side git command runs with `-c core.hooksPath=<devnull> -c core.fsmonitor=false`, so a repository-planted hook or fsmonitor never runs outside the sandbox. (D-120)
- `git_safety` pins `.git/info/attributes` to `* -filter diff`, unbinding repository-controlled filter and diff drivers while keeping content readable for the credential scan. (D-123)
- The attributes pin is written without following links and fails CLOSED, and the gitdir's own backpointer must name this worktree. (D-125)
- For a linked worktree the attributes pin lands in the COMMON gitdir, which must itself hold a `HEAD`. (D-127)
- `clone_setup.checkout_branch` routes through the shared `git_safety` config and pin. (D-129)
- `proposer._capture_diff` calls `require_pinned` first, and the structural helper list includes it (`test_every_host_side_git_helper_injects_the_safe_config`). (D-132)
- Host-side `git` decodes output leniently (`errors="replace"`), so a non-UTF-8 byte in a changed file cannot kill a watcher. (D-142)

### Gate and measurement invariants

The deterministic gate, measurer and pollution check decide what survives; these are the
properties each must keep.

**Pinned invariants.**

- The gate collects the reproducing test the agent actually wrote, never a name invented from the target slug (which also collides between targets). (D-2)
- Gate pytest runs pass `-o addopts=`, so the target's coverage/xdist `addopts` do not apply; `PYTEST_ADDOPTS=""` is not a substitute because env addopts are appended to the ini value. (D-3)
- Full-suite gate steps use xdist and scope to the edit-allowlist region when it is narrowed, so a monorepo suite does not time out into the unparseable sentinel that reads as "regressed". (D-4)
- `_lint_findings` strips ANSI SGR codes before parsing, so T1's rule-code set difference compares real codes. (D-5)
- With the default budget a multi-surface discovery does not leave most findings at `seen`: the cycle cap does not starve discovery. (D-7)
- The driver's `--dry-run` completes, and `spine/stub_profile.py` is importable. (D-9)
- The do-not-pollute gate hashes host state before and after a run, and a non-zero diff blocks the run. (D-10)
- A candidate that deletes tests is rejected (`test_count_unchanged`), and the perf track's edit fence forbids `tests/**`. (D-11)
- `spine/pollute.py` reports a non-zero diff for a same-length in-place edit, a deletion, a re-pointed symlink and a created-from-absent path, and an exclude never blinds the rest of its root. (D-14)
- Pre-flight refuses rather than permits: a canary inside the band raises `RulerNotTrustedError`; a host leak raises `HostPollutionError` even in advisory mode; zero baseline samples raise; a negative `floor` never yields a negative band. (D-15)
- `spine/measurer.py` discards warmups, aggregates by MEDIAN, checks same-sha before any boot, aborts on any rep's ruler error, ANDs RH-A/B across reps, and runs REPRODUCE as an independent larger run. (D-25)
- `pollute._is_excluded` matches a subtree with `os.sep`, so on Windows the orchestrator's own data dir is not reported as a leak. (D-31)
- An unresolvable `scopeDiffBase` refuses rather than widening the edit fence; the legitimate base==HEAD empty diff is allowed (see D-67, D-86). (D-39)
- `scoped_relpaths` returns `set()` for a successful empty diff and `None` only for a blank ref or a git error, so an empty scope means no file may be edited. (D-67)
- `profile.py` imports `kiro_crew.sandbox` at module scope, and `test_the_gate_requests_strict_mode` patches the consuming module so it asserts the strict mode for real. (D-78)
- A configured diff scope that cannot be computed, for any cause, refuses rather than running unscoped. (D-86)
- `RepoEditAllowlist` receives the track (default `TRACK_BUG`), and the added-reproducing-test carve-out applies only on the bug track, so the perf track may not touch `tests/**` at all; a structural test asserts the profile passes its track. (D-101)
- `Gate._stage_test_only_base` copies the candidate tree with `symlinks=True`, so a planted link to a credential is copied as a link, never its contents. (D-131)

## Storage

Under `app_data_dir("auto-improvement")` (i.e. `$KIROCREW_HOME/apps/auto-improvement/data`):

```
config.json                    active run configuration
crew.json                      app role-to-Crew-Member mapping
sessions/<key>.json            chat-session records (resume)
repos/<repo-branch-key>/
├── ledger.jsonl               append-only findings ledger
├── ruler/ruler.json           calibrated ruler (atomic write)
├── results/                   run metadata, results.tsv, per-candidate diffs
├── pr_queue/                  <fp>.diff + <fp>.pr.md — durable draft-PR queue
├── profiles/                  normalized profiler frame trees
└── logs/
```

All archive text is UTF-8, including raw candidate diffs and TSV descriptions. When an
existing `results.tsv` is not valid UTF-8, Archive initialization fails closed with an
error naming the file and the remedy — it never decodes with the current host locale,
because an archive written under one legacy code page and opened under another would be
silently transcoded to mojibake. An incomplete UTF-8 sequence at EOF is reported as a
crash-torn append (recoverable by deleting the incomplete final line) rather than
misread as legacy text. Agent output is not restricted to the gateway host's locale, so
durable state must not inherit a legacy Windows code page that cannot represent the
candidate text.

Disposable clones and worktrees live **outside** `data/` under
`~/.autoimprove-scratch` (override: `AUTO_IMPROVEMENT_SCRATCH`), because they are
large, regenerable, and must not be mistaken for the durable record.

`write_json_atomic` (tmpfile + `os.replace`) is load-bearing rather than
stylistic: the upstream app used a plain write for the ruler and readers caught it
mid-truncate ~31% of the time, reporting a calibrated ruler as uncalibrated.

Session record keys are validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,249}$` and
rejected — not sanitized — when unsafe, because silently rewriting a key would
make two subjects share one record. The frontend sanitizer strips dot-runs and
path separators before the key is ever sent, so the two gates agree.

**Pinned invariants.**

- `GET /findings/{fp}` joins evidence by the target's BASENAME slug (`mod_py_sym`, matching `proposer._short`), so a nested target like `src/pkg/sub/mod.py::Sym` shows its `signature`/`hypothesis`. (D-1)
- The direct-commit queue copy writes `<fp>.pr.md`, the name every reader opens; a structural test pins that no non-test source writes `.cr.md`. (D-24)
- `results.tsv` cells collapse tab, CR and LF to a space (`_cell`); the unaltered value is kept in `candidates.jsonl`. (D-35)
- Workspace keys include a digest of the repository identity, so repositories that slug alike (`owner/a-b`, `owner/a_b`) get distinct workspaces, while `origin/main` and `main` still share one. (D-85)

## Chat integration

Three tiers, each where it fits:

1. **Resumable per-subject sessions** (`website/src/apps/auto-improvement/lib/agentSession.ts`)
   — `createSlot` (the title on the create payload, `memory_mode: 'persistent'`) →
   `sendTurn` with the seed prompt → `PUT /sessions/{key}` persisting
   `{slot_key, folder_id}` → `switchSlot`, so a repeat click resumes through
   `switchSlot`. Sessions are filed into an `Auto-Improve - <repo>` folder. A 404 from
   `switchSlot` (and only a 404) means the slot is gone and a fresh one opens;
   treating a transient error that way would orphan a live session.
2. **Member assignments** for the autonomous loop. Enabling the app provisions
   `auto-improvement-scout` and `auto-improvement-engineer` as ordinary Crew Members,
   each with its own immutable member ID and empty V2 memory store. The app keeps
   the role-to-ID mapping at `<home>/apps/auto-improvement/data/crew.json`;
   re-enabling preserves owner edits, rules and memory, and follows member renames.
   A missing recorded member or store, an occupied foreign name or an incompatible
   template refuses initialization.
   Disable and uninstall retain the members and their history.
   [Owner recovery](../../../src/kiro_crew/apps/builtins/auto_improvement/docs/MANUAL.md#recover-a-missing-or-changed-member)
   restores an intact member's template through the existing editor, retaining its
   identity. Fresh replacement requires a stopped gateway and backups of `config.json`
   and `crew.json`: the owner frees any canonical-name collision by renaming the
   `agents` key and its store's descriptive `owner_member` label, preserving the
   existing IDs and store binding, then removes only the broken role mapping.
   Re-enabling provisions that role afresh while preserving the healthy role's ID
   and renamed label. Retained memory, manual documents and transcripts remain
   associated with the old identity and are never transferred to the replacement.
   `backend/crew.py` owns provisioning and binds the gateway session runtime.
   `spine/crew_runner.py` assigns discovery to the Scout and bug/performance
   authoring to the Engineer. Each member is governed under its own member alias,
   so a task-scoped profile bound to that alias holds as it does on the dashboard;
   provider allocation and the tool set still come from the shared role template. Handoffs carry the candidate evidence explicitly;
   the members do not share memory. Before provider allocation, the assignment
   explicitly scans its working directory's `.kiro/agents` and refuses any spec
   declaring either role template. The shared bounded JSON/Markdown reader handles
   declared names, filename fallbacks and symlinks; JSON twins take precedence.
   The scan includes native skill-view filenames and refuses unreadable directories
   or unverifiable specs instead of treating them as absent. It does not walk
   parents or change global discovery. When cwd is omitted, the provider factory's
   session-workspace resolver supplies the path, which is checked and then passed
   explicitly to allocation. This prevents project preapprovals from bypassing
   governance and stage permission gates; credential-risk consent does not waive it.
   Every assignment uses a fresh managed session
   with a captured `ExecutionContext`, current member context, transcript and
   member activity pointer. The app feed payload includes the member and session
   key; the current feed UI shows the member, and member activity opens the session.
   Generated assignment prompts, assistant text and assignment error metadata pass
   through the platform context's credential and exfiltration redactor before
   storage. Completion activity uses the same sanitized error copy; the actual
   assignment prompt and returned `AgentResult` remain intact for the deterministic
   driver. Failed redaction withholds the affected field while session release
   and removal still run.
   Stop and timeout cancel the active assignment and release its session.
   Deadline errors use the existing `timeout after Ns` contract so bug and
   performance authoring retain completed edits for the deterministic gate.
   A turn the backend ends unfinished (stop reason `timeout` or
   `stale_recover`, or a stream that ends with no turn end) returns `ok=False`
   with an error that begins `the backend did not finish the turn`; authoring
   reads it the same way and retains completed edits.
   Per-role provider-reported USD totals feed the existing combined run budget.
   Kiro credit usage is not converted to dollars; the USD cap does not bound
   credit spend. Cycle, time and tool-call limits still apply. The deterministic
   gate, ruler, keeper and publication policy retain their authority.
   Only the two named memory tools (`memory_recall`, `learn_add`) supplement a
   stage's existing tool allowlist, and only with trusted MCP identity; an empty
   allowlist still grants no tools. Governance, audited approvals and the app's
   shell refusal gate apply to every permission request. PR watchers retain their
   separate opt-in runner and egress controls.
   The Scout offers `execute_bash` for opt-in pre-push git review; discovery's
   per-stage allowlist still denies Bash.
   Native permission frames that omit a kind use the preceding tool-call
   classification with the same call ID. Neither a title nor the agent-written
   `tool_purpose` is ever a tool's identity, so an unnamed request stays unnamed and
   is refused.
   The recovered kind is shared by hook/governance evaluation and the app allowlist,
   so both authorize the same tool classification.
   The credential-confinement preflight reads `config.agent.sandbox` and applies
   `effective_sandbox_mode`, including the governance `sandbox.min_level` floor.
   Only an effective `strict` mode satisfies it; other modes require the
   existing explicit `acceptUnsandboxedAgentRisk` decision.
3. **Fire-and-forget launcher** for one-shot discussions.

Subject kinds are `pr | finding | ruler | run`, and the record key is
kind-namespaced so `finding-7` and `pr-7` are different conversations.

Seed prompts (`lib/prompts.ts`) all append the same two constraints — never
publish/merge the PR, and never edit the ruler or harness to improve a number.
A frontend test asserts every surface carries them, so a new surface cannot forget.

**Pinned invariants.**

- Chat-session keys are `kind-repo-id`, with the repo slugged through the filename-safety pass and `norepo` for a missing repo, so two repositories' subjects never share a record. (D-75)
- `_SAFE_KEY_RE` admits a max-length GitHub PR key while keeping `<key>.json` under 255 characters, and the character fence is unchanged. (D-104)
- `sessionKey(kind, id, repo)` appends an FNV-1a hash of the raw values, so repos or ids that sanitize alike never share a key. (D-134)
- `openSession` guards re-entry with a synchronous `useRef` latch per session, taken before the first `await` and released in `finally`. (D-140)

## Keep/draft ordering invariant

A cycle must **apply → draft → commit**, in that order, in both tracks.

`pr_recipe._push_fix_branch` pushes the shared clone's `HEAD`, so the winner has to be in
that tree before the pipeline drafts. Drafting first published a branch that did not contain
the fix — or one carrying a previous cycle's commit. Three things look like they would
prevent that and do not: the queue copy carries `winner.diff` (so the *queued* artifact was
always right), `gated_commit_sha` feeds the reproduce **measurement** rather than the draft,
and `gate_res.commit_sha` is the throwaway **worktree's** head.

Staging is not enough: `git push HEAD:refs/heads/<b>` sends the **commit** HEAD points at,
while `git apply` + `git add -A` only touch the index, so a staged-but-uncommitted fix would push
the ORIGINAL file content. The fix has to be a real commit.

But the commit MESSAGE needs `outcome.reproduce`, which only the pipeline produces, and
authoring the final message first would silently degrade it to echoing VERIFY (§3.1/§3.2).
So the sequence is **provisional commit → draft → amend**:

1. `_commit_winner_provisional` / `_commit_bug_winner_provisional` apply the diff and commit
   it with a placeholder message, so HEAD carries the fix when the recipe pushes.
2. The pipeline reproduces and drafts.
3. `_finalize_winner_commit` / `_finalize_bug_winner_commit` `--amend` that commit with the
   attributable message and the real reproduce numbers. The tree is untouched, so the
   commit the PR points at and the commit on the branch stay identical.
4. `_reset_provisional` hard-resets to the pre-commit sha when nothing was filed (fluke,
   duplicate, error), so a non-win never advances HEAD.

A diff that will not apply is refused *before* the expensive reproduce A/B.
`TestWinnerIsInTheTreeBeforeDrafting` pins the order and the rollback in both tracks.

**Pinned invariants.**

- A FILED bug winner's provisional commit is reset before the next winner, so each bug PR carries only its own fix. (D-70)
- A bug PR's `base_anchor` is `{branch} @ {pre_sha}`, captured before the provisional commit, never the fix commit. (D-112)
- `_apply_bug_winner`'s failed direct push rolls back without decrementing `kept`, which only the perf path increments eagerly. (D-116)

## Startup ordering invariant

`backend/runner.py` must check out the configured branch **before** calling
`build_profile`, in both the run path and the calibration path.

The profile resolves `scopeDiffBase` in its *constructor*, via
`scoped_relpaths(clone, base)`, which diffs `base...HEAD`. Built while the clone is
still on the repo default branch, that diff comes back empty, `scoped_relpaths` returns
`None` meaning "no scope", and the edit fence silently widens from "what this branch
changed" to the whole repository — the opposite of what setting a diff scope is for.
Calibration has the same requirement for a different reason: it *measures* the suite, so
a baseline and noise band collected on the default branch would be used to judge
candidates on a feature branch.

When the checkout fails, both the run and calibration **refuse to start**, with or
without a configured `scopeDiffBase` (which only extends the error message):
`checkout_branch` already tries the remote-tracking ref and a local ref, so a failure means
the branch exists nowhere. `TestCheckoutPrecedesProfileBuild` pins the ordering.

**Pinned invariants.**

- A failed `checkout_branch` makes both the run and calibration RAISE rather than operate on the wrong revision. (D-36)

## Spine / profile seam

The engine (`spine/`) consumes a target only through the `TargetProfile` protocol
(`spine/profile.py`): six data fields (`ruler`, `build_gate`, `edit_allowlist`,
`isolation`, `pr_recipe`, `calibration`), plus `id` and `track`, an optional
`bug_runner`, and the `discover()` and `propose()` callables. The protocols are
`runtime_checkable`, so the loader validates a profile object before the driver
trusts it. Adding a target never edits the engine, but it does touch
`profiles/__init__.py`: `build_profile` always builds `github_repo` today.

`profiles/github_repo/` is the reference profile.

**Pinned invariants.**

- `ensure_agent_registered` writes `~/.kiro/agents/<name>.json` only when it is absent or byte-identical; a differing file is left intact and registration returns False. (D-28)
- `runner` imports `build_profile` lazily, keeping the profile and spine off the gateway boot path; `test_perf_boot_path.py` ratchets it. (D-76)
- MCP stdio registration resolves a bare `python`/`python3`/`py` to the running interpreter (`bridges.resolve_stdio_command`), so the server launches on every platform. (D-90)

## Frontend

`website/src/apps/auto-improvement/AutoImprovementPage.tsx`, routed at
`/auto-improvement` via `builtinRegistry.ts`, code-split into its own chunk.
React Query for server state, except `lib/agentSession.ts`, which reads and writes
session records with a direct `fetch`; `i18nT` for every user-facing string (keys under
`autoImprovement.*`, present in all 12 production catalogs); lucide icons only.

**Pinned invariants.**

- The commit control's `mutationFn` checks `res.ok`/`ok:false` and throws, and the row shows `commitFinding.error` scoped by `variables`. (D-97)
- The commit control confirms first, naming the BRANCH, carries a text label, and its confirm copy is a catalog key registered as destructive. (D-98)
- Ledger statuses render as catalog labels for every `VALID_STATUSES` value, through an inline `i18nT(MAP[k])` lookup, with the raw token as fallback. (D-99)

## Parity with the upstream app

All 26 upstream endpoints are covered. The vocabulary is renamed (change request →
pull request) and four upstream paths map onto differently-named equivalents:
``cr-checks`` → ``pr-status``, ``cr-sessions*`` → ``watchers*``, ``draft-cr`` →
``draft-pr``, and ``status``/``activity``/``stop`` fold into ``run``/``run/stop``.

### Audited gap state

An audit against the upstream engine diffed the engine
module-by-module. The ``spine/`` port is faithful — six modules byte-identical modulo
whitespace, ``driver.py`` structurally line-for-line, and all six safety invariants
(push-disabled clone, draft-only, protected-branch denylist, edit-allowlist reward-hack
guard, do-not-pollute gate, second independent reproduce) present and equivalent.

Every gap the audit identified is now closed:

| Gap | Severity | Resolution |
|---|---|---|
| ``--dry-run`` crashed on entry | high | ``spine/stub_profile.py`` re-export shim restored; 4 tests |
| Perf track could not propose at all | high | ``author_perf_fix`` + per-track dispatch in the proposer; 17 tests |
| Profiler capture never ran (endpoints always empty) | med | driver calls ``profile.capture_profile`` per perf candidate, after the timed arms; 9 tests |
| Auto-fixer reconciler absent | med | ``reconcile_failing_prs`` + ``promote_deferred`` + ``MAX_ACTIVE_WATCHERS`` cap, driven from the polled ``GET /watchers``; 32 tests |
| Activity buffer 25× smaller | low | ``ACTIVITY_MAXLEN`` 200 → 5000 |
| ``autoPublish`` gate dropped | low | ``auto_publish_gate`` + the ``autoPublish`` key — fail-closed, ``gh pr ready`` only |
| ``autoPublish`` could never fire | med | ``summarize_checks`` omitted ``total``, which that gate reads to prove a PR is green rather than merely un-red — so every green draft was refused with "no checks ran". ``total`` is now derived from the four buckets, with a regression guard that fails without it |
| Orphan-clone sweep absent | low | ``sweep_orphan_clones``, name-shape-matched and symlink-safe |

Three deliberate, documented DIVERGENCES remain — they are design decisions, not
missing work, and each is narrower than upstream on purpose:

* **No mechanical perf seeds.** Upstream shipped ~24 hand-written seeds per target
  because its two profiles optimized one specific service. A target-agnostic profile
  cannot ship those, so the perf edit is authored by the model and judged by the same
  A/B measurement. The seam is open for a repo-specific profile to add seeds.
* **A watcher's fixes cannot reach the PR head branch.** The per-watcher clone has a
  dead origin (the isolation control), and GitHub has no upload side channel, so each
  pass exports its diff to the PR queue instead. See ``pr_watchers``.
* **``autoPublish`` marks ready-for-review only.** Never merges and never enables
  auto-merge, even when upstream would have.

State of the two tracks: the **bug track** has discovered, fixed, gated and
auto-committed a real defect end-to-end (``keeper.py`` rendering ``±None``). The **perf
track** is now structurally able to run end-to-end but has not yet kept a measured win
against a real repo — a wall-clock suite ruler needs a target whose suite is long enough
to resolve a win above the noise band (see the advisory-canary note above), which is a
target-suitability limit rather than a wiring gap.

Three upstream modules are deliberately NOT ported, because an in-process builtin
makes them dead weight rather than because they were missed:

| Upstream | Why it is gone |
|---|---|
| `proxy_auth.py`, `middleware.py` | the gateway authenticates same-origin requests; there is no proxy hop to sign |
| `app.py`, `bin/` launcher | `register_routes(app)` mounts on the gateway's own aiohttp app — no second process, no port |
| `config.py` | paths come from `store.py`, which reads the Kiro Crew data home |

One transport difference: the upstream served its MCP tools over HTTP on its own
allocated port. A builtin has no port, and the app bridge deliberately SKIPS a
URL-based MCP entry when there is no live backend (a dead default-port URL would
poison every session's provider config), so the tools ship as a **stdio** server
instead — `backend/mcp_server.py`, six read-only tools, all auto-approvable. Its
loop reads stdin as bytes through `json_line.parse_json_object_line`, so a line
that is not UTF-8, not a JSON object, or nested past the decoder's ceiling is
skipped (a request, one with a top-level `method`, is answered `-32700` under
its top-level id when one is recoverable at either end of the line, so its
caller does not wait out its own timeout), and a
request whose handling raises is answered `-32603`: neither ends the server
mid-session.

## Tests

Automated coverage is the unit and regression suites below. The endpoint surface,
the frontend cases and the full keep-or-revert loop against a real GitHub repository
are covered by the **manual acceptance plan** further down, not by an automated
integration test — nothing in the tree runs the full loop against a live repository.

- `src/kiro_crew/apps/builtins/auto_improvement/tests/` — tests covering verdict
  derivation, check summarization, provider-error degradation, PR-recipe protocol
  conformance, branch naming, draft-only policy, queue degradation, the audit-or-deny
  approval, MCP dispatch auditing, and evidence redaction. `setup.cfg` `testpaths`
  collects all of `src/kiro_crew/apps/builtins`, so the app's safety-control tests
  run in CI. Four sandbox-dependent files (`test_lint_findings.py`,
  `test_github_profile.py`, `test_profile_capture.py`, `test_runner.py`) are only
  omitted from coverage, a rule `test/test_coverage_omit_contract.py` enforces.
- Repo-level handler and coverage tests under `test/`: `test_ai_backend_routes_coverage.py`,
  `test_ai_agent_runner_coverage.py`, `test_ai_runner_coverage.py`,
  `test_ai_pr_watchers_coverage.py`, `test_ai_spine_driver_coverage.py`,
  `test_ai_github_profile_coverage.py`, `test_ai_runner_purpose_not_identity.py`,
  `test_auto_improvement_owner_gate.py`, `test_auto_improvement_members.py` and
  `test_handlers_auto_improvement_deny_notice.py`.
- `test/test_bug_keeper_maximize_direction.py`, `test_bug_keeper_noise_band_none.py`,
  `test_bug_proposal_skip_status_default.py` and `test_bug_proposer_fan_out_overlap.py`
  — reproducing tests for defects the app found while dogfooding.
- `website/src/test/autoImprovementSession.test.ts` — session-key
  namespacing/sanitization and prompt-constraint coverage.
- `website/src/test/autoImprovementActivity.test.ts` — activity-feed line-builder
  coverage, including app-locale (not host-locale) time formatting.

### Manual acceptance plan

Run by hand before accepting a change to the loop; no automated test covers these.

#### Frontend cases

Driven headless with Playwright. Use the website's own `node_modules`
(`import { chromium } from 'playwright'`) and run the script **from inside `website/`** so
ESM resolves.

| Case | Action | Expected |
|---|---|---|
| B-1 | Click **Auto-Improve** in the left nav | routes to `/auto-improvement`, renders `<h1>Auto-Improvement</h1>` (a partially-staged `dist` renders a dead click) |
| B-2 | Paste the chess_test URL → **Connect** | clone succeeds; repo shown; push-disabled state visible |
| B-3 | Base-branch dropdown | shows the **configured** branch, not "default" — config loads async, so the control must re-sync |
| B-4 | Config names a branch absent from the list | that value still appears — a `<select>` whose value matches no option silently renders the first entry, misreporting the target |
| B-5 | Toggle **autocommit** | persists via `PUT /config`; survives reload |
| B-6 | Click **Run** | activity feed streams; **Stop** reaches a terminal state |
| B-7 | Activity feed shapes | renders all four backend shapes (`{t,note}`, `{t,cycle,stage}`, `{t,cycle,stage,discovered,fresh}`, `{t,agent:{…}}`) without a React error — typing the feed as `string[]` crashes with a minified React error |
| B-8 | Expand the **3rd** finding | the 3rd panel opens (duplicate React keys open the 2nd instead) |
| B-9 | Finding detail | **Defect** and **Hypothesis** populated — not an unexplained diff |
| B-10 | A `committed` finding | shows **View commit** linking to `https://github.com/Zedmor/chess_test/commit/<sha>`, **not** a re-commit button |
| B-11 | A `filed` finding | still shows the commit **action** |
| B-12 | PR row | PR link renders (the ledger field is `cr`; a UI reading only `pr` shows nothing); verdict badge + checks label present |
| B-13 | Refresh-PR and discuss buttons | refetch works; discuss opens a resumable session |
| B-14 | Colors | app design tokens only (`text-muted`, `text-accent`, `border-border`, `hover:bg-accent/20`) — no bespoke palette, no undefined CSS vars |
| B-15 | i18n | no hardcoded English; `viewCommit` resolves in every locale `i18n/languages.ts::SUPPORTED_LANGUAGES` registers (`website/src/test/i18nAllLanguagesEntry.test.ts` pins that every entry point reaches every catalog) |
| B-16 | Console | no errors beyond the pre-existing CSP / Google-Fonts warnings |

#### Full-loop acceptance against a reference repository

The example reference target is a small public chess engine repository
(`https://github.com/Zedmor/chess_test`); any repository with a small, fast suite works. A small, fast suite is what makes a full-loop
test practical: the gate runs the suite several times per candidate, and a whole-repo
suite cannot finish inside `_SUITE_TIMEOUT_S` (`profiles/github_repo/profile.py`).

Run config. `target_url` is set by **Connect** (`POST /setup-clone`), never by
`PUT /config`, which refuses it; the remaining keys go through `PUT /config`:

```json
{
  "target_url": "https://github.com/Zedmor/chess_test",
  "branch": "origin/main",
  "directCommit": false,
  "maxCycles": 6,
  "maxHours": 1.0,
  "proposerWide": 1,
  "proposerDeep": 1,
  "forceBugSeeds": true,
  "canaryAdvisory": true
}
```

> `directCommit: false` on the first pass so the loop drafts **PRs** rather than pushing —
> and `main` would be refused by the protected-branch denylist anyway. Re-run with
> `directCommit: true` **against a feature branch** to exercise the autocommit path.

**Expected trace, in order:**

1. **Preflight** — push-disabled asserted; deps green; do-not-pollute snapshot taken.
2. **Calibrate** — ruler `calibrated`, noise band recorded. The canary is *advisory* here:
   a suite ruler cannot always force a known win, and that must WARN, not halt.
3. **Discover** — the agent reads real files and emits surfaces pinned to
   `file:line:symbol`. Assert `runner_ok: true`, `raw_items > 0`, every surface a real path.
4. **Propose** — wide and deep author in **separate** worktrees on **different**
   candidates (overlapping candidates spend two agent passes on one locus).
5. **Gate** — bug candidate: T0 build → T1 lint → T2 collect → RED (×2 flake check) →
   GREEN → STAYGREEN. Assert each flag explicitly true in the artifact.
6. **Keep** — accepted only on a real transition; a perf candidate must clear the noise
   band in the **improving direction** for the ruler's `direction`.
7. **Draft PR** — a draft PR appears on the repo, body carrying the RED→GREEN narrative.
   Assert `--draft`; assert nothing merged or marked ready.
8. **Ledger** — the finding ends `filed` (or `committed` in autocommit mode) with a
   fingerprint, and a re-run does **not** re-file it (dedup invariant).

**Acceptance:** ≥1 finding reaches `filed`/`committed` with a real draft PR or commit,
**and** its detail panel explains the defect. A run that legitimately finds nothing must
end `no_defect` — an honest "no defect" is a pass; a *fabricated* fix is a failure.
