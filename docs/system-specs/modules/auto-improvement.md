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
| GET | `/pr-status?url=&refresh=` | live PR status, CI checks, watcher verdict |
| GET | `/ruler` | ruler calibration state |
| GET | `/findings` | ledger entries, newest first |
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
| Push-disabled clone | `backend/clone_setup.py:_disable_push` + `spine/driver.py:assert_push_disabled` | **BOTH** origin urls are `DISABLED_NO_PUSH`; the run refuses to start otherwise. The real remote lives in config (`origin_url`), handed only to the trusted publishers |
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
| Sandboxed fallback agent (NOT SELECTED) | `spine/agent_runner.py:_spawn_sandboxed_agent` | nothing selects this path any more — both selection sites go offline/refuse instead (see "Why the subprocess fallback is sandboxed rather than deleted"). Retained hardening, for any future caller: it runs through `sandboxed_spawn_argv(mode="strict")` + `resource_limit_preexec()`: worktree visible, credential dirs bind-mounted empty, env scrubbed, `PYTHONPATH` stripped. Hides credentials; does NOT confine writes — see "Known limitation" below |
| Pre-push content scan | `spine/push_policy.py:scan_content_for_secrets` | ONE scanner behind all three exits — draft-PR push, F10 direct push, one-click commit. The full pushed range is scanned; a hit **refuses** the push and the change stays in the local queue; **fail-closed** |
| Audited subprocess agent (NOT SELECTED) | `spine/agent_runner.py:_audit_unattended_agent` | same — retained for a future caller. The `claude -p` path passes `--dangerously-skip-permissions`, so the launch is one blanket approval — logged `critical=True` before the spawn, and an unwritable audit REFUSES to launch |
| Redacted PR prose | same, `_redact_prose` | title and description are redacted (prose survives rewriting; a diff does not) |

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
traversal, fail closed. Raised by the GPT review of this branch.

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

An audit against ``MeshClawApp-AutoImprovement/mainline`` diffed the engine
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

Integration coverage (all endpoints, the UI, and a full loop against a real repo) is
specified in [auto-improvement-test-plan](auto-improvement-test-plan.md).

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
