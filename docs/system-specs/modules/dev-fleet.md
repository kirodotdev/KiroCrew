# Dev Fleet Module

## Overview

Dev Fleet is a builtin App Store app (`kiro_crew/apps/builtins/dev_fleet/`) for
managing Kiro Crew feature worktrees (git worktrees of the main repo) and their isolated
pod test instances. It runs as a managed app backend SUBPROCESS: an aiohttp server on the
backend-assigned port, reached only through the gateway proxy. Every proxied request
carries an HMAC signature (`X-KiroCrew-Proxy: <ts>:<hmac>` over
`<ts>:<METHOD>:<path>[?q]:<sha256(body)>`, +/-60s window) verified fail-closed by the
backend's middleware; the shared secret lives at `apps_dir()/dev-fleet/.app_secret`.
Gateway session auth (token/cookie) gates the proxy entrance as with all builtin apps.

## Responsibilities

1. **Worktree discovery** — enumerates git worktrees via `git worktree list --porcelain`,
   dropping records git flags `prunable` (checkout directory deleted without a
   `git worktree prune`); the primary checkout is never dropped, since it anchors `is_main`
2. **Pod integration** — spin up/down/restart isolated pod instances per worktree
3. **Pull+Build sync** — pull origin/main and rebuild (venv + frontend dist)
4. **Prune** — safely remove merged/empty worktrees with PR-shipped verification
5. **Rebase** — rebase feature branches onto main with conflict detection + abort
6. **GitHub PR status** — TTL-cached `gh pr list` queries for merge state
7. **Make Live** — repoint the live gateway at another worktree via a
   live-target pointer file (no service definition is ever mutated)

## Backend component boundaries

`server.py` is the composition facade: it registers the unchanged HTTP routes,
coordinates startup and shutdown, and owns the process entry point. The implementation
behind that facade is split by state and lifecycle ownership:

- `runtime.py` owns command security, toolchain resolution, run descriptors, active
  subprocesses, and shutdown admission.
- `repository.py` owns `MAIN_REPO`, repository discovery and validation, git/worktree
  access, and dirty-state inspection.
- `live.py` owns live-target discovery, gateway restart backends, the Make Live lock and
  committed-cutover latch, and rollback.
- `fleet_state.py` owns PR/context/resource caches, fleet projections and tombstones, and
  provision reattachment state.
- `worktree_ops.py` owns pod actions, worktree remove/sync/rebase/prune orchestration, and
  the background task handles.
- `http_api.py` owns proxy HMAC verification, request/audit adapters, and response-shape
  translation.

Dependencies run in that direction from HTTP adapters toward the lower-level owners;
lower-level components do not call back through `server.py`. Cross-component calls use
the owning module, so mutable locks, registries, caches, and scalar state each have one
authoritative instance. The facade forwards legacy private attribute reads, writes, and
deletes to that owner, but tests patch the actual owner rather than treating the facade as
a dependency-injection namespace.

The split does not change lifecycle ordering. Shutdown first closes admission and
snapshots active runs under the runtime admission lock, then kills process trees before
cancelling their workers, and only then cancels the idle refresher/reaper/prune tasks.
Destructive worktree operations retain the lock order `_wt_lock(name)` ->
`_MAKE_LIVE_LOCK` -> `_GIT_MUTATION_LOCK`; changing either ordering can strand a build or
deadlock removal against a live cutover.

## Main Checkout Discovery

Every git operation is rooted at `MAIN_REPO`, the primary checkout whose worktrees the
fleet manages. It is resolved in this order, first hit wins:

| Tier | Source | Marker-tested? |
|------|--------|----------------|
| 1 | `KIROCREW_DEVFLEET_REPO` env var | no — taken verbatim |
| 2 | `dev_fleet.repo_path` in `config.json` / `config.local.json` | no — taken verbatim |
| 3 | `KIROCREW_PROJECT_DIR` | yes |
| 4 | the checkout this gateway is executing from (`src/kiro_crew` layout walk) | yes |
| 5 | conventional clone locations under `$HOME` (`kirocrew`, `KiroCrew`, `kiro-crew` directly and under `Repos`, `repos`, `src`, `Projects`, `projects`, `dev`, `git`, `code`, `workplace`) | yes |

Tier 5 matches directory names case-insensitively against each parent's own listing rather than joining the guessed spellings, so the resolved path is spelled the way the filesystem spells it. A blind join succeeds against a differently-cased directory on a case-insensitive filesystem (macOS) and yields a path that does not match the ones git reports for the same tree.

The marker test (`_is_kirocrew_checkout`) requires `.git`, `src/kiro_crew/` and
`pyproject.toml` together. `.git` alone is insufficient on purpose: an unrelated
repository adopted as the main checkout would have its worktrees listed and Pull+Build,
rebase and worktree-removal git commands run inside it. Tiers 1–2 skip the test *during
discovery* because the user named that path — a typo must surface as an error against it
rather than be silently replaced by a discovered checkout — but the path is still validated
once, on the attempt that resolves it, and `_repo()` — the single accessor every git argv and path build goes
through — then raises `RepoUnreadable` naming it. The gate lives in the accessor rather than
in worktree discovery because sync and the background refresher reach git without passing
through discovery, and `pull --ff-only` plus `pip install -e` inside an unrelated repository
is the worst available outcome. "Not replaced by a discovered checkout" and "not validated" are separable, and
only the first is wanted: a readable-but-wrong configured path would otherwise be operated
on rather than reported.

Module import evaluates tiers 1, 3 and 4 — two env reads and a handful of stats — because
the module is imported from the async route-registration path. Tier 2 (a config-file read)
and tier 5 (up to 30 candidate directories x 3 markers) run only on the subprocess executor
in `dev_fleet_startup()`. The startup result is then normalized through
`_resolve_primary_checkout`, so a hint naming a linked worktree still manages the whole
fleet.

The `/fleet` payload reports both the resolved `main_repo` and
`main_repo_inferred`. The latter is true for tiers 3–5 and false for the two
operator-configured tiers. The page surfaces an inferred path once above the fleet,
so the checkout targeted by Pull+Build, rebase, and prune is visible without adding
noise for operators who configured it explicitly.

When no tier resolves, `MAIN_REPO` is `""` — never a synthesized path. Discovery raises
`RepoNotConfigured` and `/fleet` answers `{"worktrees": [], "needs_setup": true}` with no
`error` field, which the page renders as a setup prompt. A synthesized default instead
produces a red "Discovery Error" naming a directory the user never chose, which reads as a
broken app rather than an unanswered question.

That unresolved state is retried, not latched. `ensure_main_repo_discovered()` records
"done" only once a checkout RESOLVED, and `/fleet` calls
`worktree_ops._ensure_repo_resolved()` per poll, so an operator who writes
`dev_fleet.repo_path` while the gateway is running gets a fleet on the next poll rather
than after a restart. A resolved install returns at a truthiness guard before any await,
so the retry costs nothing once there is a fleet to serve. An unresolved one re-runs tiers
2 and 5 on the subprocess executor and spawns no subprocess, because `_load_fallback_repos`
and `_upstream_remote` both decline before reaching git while `_repo()` raises; the
credential-helper warm is guarded by its own `None` sentinel, so its two `git config` calls
stay once-per-process.

Only tier 2 self-heals. `_load_dev_fleet_cfg` re-reads `config.json` on every call, whereas
tier 1 is read off this process's own environment, which no outside shell can change, so
setting `KIROCREW_DEVFLEET_REPO` still requires a restart and the setup card names the two
routes separately. A resolved path that FAILS the marker test latches too, and renders the
`RepoUnreadable` banner naming the path and the remedy. That latch is reopened by
`_invalid_resolution_is_stale` once the configured string changes: because tier 2 is
re-read per call, an operator who corrects a typo would otherwise meet exactly the frozen
banner this chain removes for the not-found case. The test compares against the string the
latching attempt read rather than against `MAIN_REPO`, which is the `_resolve_primary_checkout`
form of it, so a path that needed rewriting does not read as changed on every poll; a valid
resolution still returns at its first guard with no await, and an env-set path cannot change
inside one process, so neither pays for the reopening. Reopening also requires the config
read itself to have succeeded. An unreadable or half-written `config.json` yields the same
empty string as one naming no path, so reopening on that difference would send discovery to
the INFERRED tiers and latch a checkout the operator never named while their own setting sat
in a file this process merely failed to read, and every later git call would target it.
`_load_dev_fleet_cfg_checked` reports whether every file present parsed, and only a whole
read can say the operator's answer changed; a parseable file carrying no `repo_path` is an
answer rather than a gap, so that case still reopens. The attempt then takes ONE checked
read and hands it to `_discover_main_repo` rather than letting that function read tier 2
again, because two reads of one file can disagree: the staleness test could see a whole
corrected path and reopen while a second read returned the empty string and sent
discovery to the INFERRED tiers. That latch passes the marker test, so it is VALID and
therefore final, nothing re-resolves it and only a restart clears it. A partial read
publishes nothing at all and the next poll retries against a settled file. Every
global the chain writes is a function of the current attempt alone, including the
invalid-path message, which an attempt that finds nothing clears rather than inherits —
`MAIN_REPO` from one attempt beside an earlier attempt's verdict would hand `_repo()` a path
whose markers were never checked. A late resolution also restarts the background refresher,
which returns rather than idles when there is no usable checkout; leaving it stopped would serve a
fleet whose rows never refresh again, so the setup card disappears and the page looks alive
while nothing fetches (`test/test_dev_fleet_repo_reresolution.py`).

Because `""` would make `git -C ""` operate on the backend's own working directory (and
`Path("")` is `Path(".")`), no consumer reads the global directly: every site that runs git
against the checkout or builds paths from it resolves it through an accessor. There are two,
and the stronger one is the default. `_repo()` returns the path or raises
`RepoNotConfigured`, `RepoUnreadable` or `RepoReadOnly`, and it is what a call site gets by
not thinking about the question; `_repo_read()` omits the read-only refusal and nothing
else, and a site reaches it only by naming it, so a foreign repository is a case a consumer
opts into rather than one it can inherit. Sites that deliberately degrade
instead of failing catch `RepoUnavailable`, the base all three share, and say what the
degraded answer is — upstream-remote
resolution falls back to `origin`, build-pending detection reports nothing pending,
fallback-remote loading leaves the list empty, sync refuses with its usual
`{"ok": false}` shape, and the background refresher stops until a later resolution
restarts it. Bare `MAIN_REPO` loads outside
the accessors are limited to truthiness guards. An AST ratchet scans every Dev Fleet backend
component (`test/test_dev_fleet_repo_accessor.py`) and permits the authoritative load only
inside `repository._repo_read()`, which `_repo()` itself reads through, so exactly one
function loads the global; helpers in every sibling module must route through one of the two.

Every OTHER route that resolves a worktree (`/worktree`, `/disk`, `/prune-candidates`,
`/prune-run`, the pod routes, `/rebase`, `/make-live`) reaches `_discover_worktrees` too, so
both unresolved states are converted once in `hmac_proxy_middleware` into a `409`:
`RepoNotConfigured` → `{"ok": false, "code": "repo_not_configured"}`, and `RepoUnreadable`
(a checkout was named but git cannot enumerate it) → `{"ok": false, "code":
"repo_unreadable"}`. The boundary lives in the middleware rather than per handler so a newly
added route cannot forget the case and answer a click with an uncaught 500. The page
suppresses the fleet toolbar, the row-action how-to, and the stat-card counts in BOTH states
— the fleet is unknown either way, so a count would assert a number nobody measured — which
means those routes are not offered in the first place; the 409 is the backstop for a direct
API caller.

`/fleet` is the one route that distinguishes them: `needs_setup` for the unconfigured state,
an `error` string for the unreadable one, which the page renders as the Discovery Error
banner naming the path (the user chose it).

When a checkout WAS named and git cannot read it, the error names the mechanism that
supplied the path (`_repo_source_hint`) — the remedy is to edit that one, and listing both
leaves the user guessing which they set.

### Read-only mode: a named checkout without the Kiro Crew markers

A path the operator named that IS a git repository but carries none of the markers is served
READ-ONLY rather than refused. The worktree list, branches, ahead/behind, own-commit counts,
the dirty split, PR state and disk usage are plain git and `gh` reads that hold for any
repository, and they are the whole value of the page to someone who wants to see that
repository's worktrees. `_REPO_READ_ONLY_MSG` holds the reason, and `_read_only_reason()` is
its only reader outside the accessors, so the route boundary, the `/fleet` payload and the
row fields consult one answer rather than three.

Nothing is adopted silently. `_discover_main_repo` adopts an INFERRED candidate (tiers 3–5)
only when it passes the marker test, so a read-only fleet exists only where the operator
typed the path; the latch tests `configured` as well, to state that dependence rather than
rely on it. `_REPO_INVALID_MSG` stays `None` here, because nothing is wrong with the path —
it is simply not this product's own checkout, which is why the page shows a fleet and a
refusal flag instead of the `RepoUnreadable` banner.

Mutating verbs are refused at the accessor, not per verb, so a call site added later inherits
the refusal. `hmac_proxy_middleware` gates every non-GET on `_repo()` and converts
`RepoReadOnly` into a `409` `{"ok": false, "code": "repo_read_only"}`. The boundary has to be
the route rather than the verb because some mutations are rooted at a WORKTREE path instead
of at the main checkout — a rebase fetches into the worktree it rebases — so a check placed
at the main-checkout argv would miss them. Pull+Build, rebase, sync, `worktree remove`,
`update-ref -d`, prune, provisioning and Make Live are all refused this way, and `/fleet`
carries `read_only_reason` so the page can state the refusal rather than offer a button that
409s.

The gateway's own routes do not cross that middleware, so they carry the refusal separately.
`_ensure_repo` is the step every gateway route needing a resolved checkout must take, and it
answers the same `409` `repo_read_only` after discovery succeeds. Make Live is the route that
makes this load-bearing: it reaches `live._make_live` → `_find_worktree_by_path` and writes
the live-target pointer, which would aim the running gateway at a stranger's tree.

The read-only denial is SEL-audited on BOTH surfaces that take it, for the same reason the HMAC
denial beside the first one is: each is a permission decision on an AUTHENTICATED request. The
backend middleware takes it before any handler runs; the gateway's `_ensure_repo` takes it
before its route reaches `_audit`, so neither would otherwise leave a record. Both emits are
wrapped, because auditing may not mask the answer.

`GIT_OPTIONAL_LOCKS=0` joins `_GIT_ENV_NEUTRALIZERS`, so it reaches every git this handler runs.
`git status` is a read to its caller and a WRITE to the repository — it refreshes the index's
stat cache and saves it back under `index.lock` — and every fleet render runs one per row, so
without it a read-only checkout is modified on the ordinary path. It sits on the env chokepoint
rather than as a `--no-optional-locks` flag per call site for two reasons: the argv this handler
builds stays the subcommand it names, and a read added later inherits the pin. Nothing is lost —
the porcelain answer is identical and a real mutation still takes the locks it REQUIRES — and
against this product's own checkout it also stops the fleet contending with the operator's git.

The centralized path gate is asked FIRST — about the path the operator NAMED, before anything
reads inside it — and asked again about the primary checkout `_resolve_primary_checkout`
rewrites that path to. Both asks run off the event loop, because `sensitive_path_refusal`
waits on the bounded resolution pool and would otherwise pause every task on the gateway loop.
The ORDER is the point: the marker tests stat the candidate, resolution runs `git rev-parse`
inside it, and the filter probe runs `git config --includes`, which follows `include.path` and
can therefore make git READ a file the fence exists to keep unread — so a verdict consulted
after those probes is consulted too late, and the earlier ordering also asked nothing at all
about a protected path that is not a git repository. Two spellings because they differ and the
difference is the hole: a fenced linked worktree can have a primary outside the fence, and a
named path outside one can resolve to a primary inside it, so clearing either by the other
clears a protected path by a path that is not it. A fenced path is refused OUTRIGHT rather than
served read-only, because reading it is what the fence forbids; it is still published to
`MAIN_REPO` for the banner alone, since `_repo()` raises on the invalid verdict and no consumer
receives the path.

The fence is asked about every worktree root too, not only the adopted checkout. A linked
worktree's location is a path written into the repository's own admin files, which this module
already treats as adversary-controlled, and `/api/disk` walks each root it is handed
recursively — so a `gitdir` record naming a credential directory has that directory read. The
question is asked in `_worktree_porcelain_entries`, the one function every worktree consumer
goes through, and both spellings are asked of each root. A fenced LINKED root is withheld and
its reason logged, because one bad record must not cost the rest of the fleet; a fenced PRIMARY
refuses the whole read, because `is_main` anchors the fleet and dropping it promotes a linked
worktree to primary — serving the operator a fleet rooted somewhere nobody named.

A gate answer that timed out is unmeasured, not a fence. The gate refuses fail-closed WITHOUT
judging the path, so that attempt holds no verdict: it says the verification did not complete
and leaves `_DISCOVERY_DONE` unset, and the next poll retries. Latched, one transient timeout
would stand as a permanent refusal asserting a measurement nobody took, and the retry the gate
itself advises could never fire.

A read-only verdict reopens on a corrected config. All three reopen gates key on
`_REPO_INVALID_MSG or _REPO_READ_ONLY_MSG` rather than on the invalid message alone, because a
read-only resolution is against a path the operator named and can still correct. The third is
`_ensure_repo_resolved`, which the `/fleet` poll calls and which returns before discovery for a
resolved checkout: keyed on the invalid message only, an operator who fixes a typo keeps serving
the stranger's repository — with `BASE_BRANCH`, the upstream remote and the fallback repos
latched to it — until the gateway restarts, and the poll is the route that would have noticed.

A repository that configures executable git filter drivers is refused, not served. A filter is
a COMMAND and `git status` — which every fleet render runs against the adopted checkout — is
what would run it. The env neutralizers pin the named execution vectors but cannot enumerate
driver names, so a repo-local filter is the one that stays reachable on a read.
`_configured_filter_commands` reads only the scopes the repository itself controls, because a
driver in the operator's own global config is the operator's decision; reading config executes
nothing, which is what makes asking first the whole remedy.

The clearance cannot be the whole bound, because a driver written between the check and the
exec still runs, and the SANDBOX cannot bound it either. A foreign read asks for the strict
tier, but this app's backend is itself spawned inside a standard sandbox and a nested wrap is
impossible by design (Linux seccomp denies the unshare, macOS Seatbelt refuses
`sandbox_apply`), so `wrap_argv` passes through and contributes the stricter tier's ENV scrub
alone — its file-level hides stay at the outer tier, where `~/.aws` and `~/.ssh` are visible by
design. So the bound is structural: on a checkout this app does not own it never asks git to
convert content, and a verb that converts nothing can reach neither a `filter.*` driver nor a
`diff.*.textconv` one. `_NON_CONVERTING_GIT_VERBS` is a SAFELIST, so a verb added later, or a
git version that teaches an existing verb to convert, costs a measurement rather than a
credential; `status` and `cherry` are outside it. The cost is that a foreign worktree's dirt is
never measured, which the row reports as unknown rather than as clean — `dirty` answers `None`,
and the consumers that act on it already test `is False` and `is True` precisely so an unknown
never passes for an answer.

Three properties make that probe's answer mean what it says, and each is a hole without it.
`--includes` is passed on every scope query: for a SPECIFIC scope git defaults
include-following OFF, so a driver reached through `[include] path = other.cfg` answers an
empty list to a probe that omits the flag while git still resolves and runs it —
`dashboard/handlers/worktree.py` proved that empirically for the same class. `--worktree` is
asked only when `extensions.worktreeConfig` is on, because without the extension git ignores
`config.worktree` entirely and refuses the query outright on any repository that HAS a linked
worktree, which is the ordinary shape of what this app enumerates; when the extension IS on the
scope is asked unconditionally and a failure is classified afterwards by
`worktree_probe_failure_is_empty_scope`, git creating that file lazily. That question has THREE
answers, not two: a non-zero exit git does not use for "key absent", and a spawn that raises,
both mean the question went unasked, and reporting that as "not live" would drop the scope and
admit a repository whose `config.worktree` holds a driver — so it is reported unread instead.
And the probe returns TWO answers, the drivers found and separately the reason a live scope
could not be read, so an unreadable scope is never reported as a configured driver: a driver
refuses the repository for good, while an unread scope leaves `_DISCOVERY_DONE` unset and the
next poll retries, exactly like the fail-closed path verdict above. `--local` is read first and
its answer returned whole, because a driver found there is a measurement and a measurement
outranks any unread verdict the worktree-scope question could produce.

`--includes` is what makes the answer true and is also what makes the read reach, but the
hazard is not confined to that flag: git parses the repository's config and follows
`include.path` on EVERY command, so the FIRST git command in a foreign checkout already opens
whatever an include names. `--includes` only decides whether an include is followed for the
value PRINTED. The strict tier does not stop it for the reason given above — the tier
contributes an ENV scrub and its file hides stay at the outer standard tier, where the
credential homes are visible. So the bound is structural and it is taken BEFORE any spawn, at
`_probe_git`, which takes the checkout path as a required argument rather than recovering it
from argv: a chokepoint that guessed its subject from a flag would be one refactor away from
guessing wrong. A git question could not be the gate, because asking would already have opened
the file — so the repository's OWN config files are read by hand, using only git's documented
one-line layout files (the `.git` pointer and `commondir`, which is how a linked worktree's
shared config is reached) and never a config parse. Those two files are written by the
REPOSITORY, though, so they are attacker-controlled input rather than a trusted layout:
`gitdir: /home/x/.aws` aims this module's own read at a credential directory, and a symlinked
`.git` does the same without naming anything. The adoption fence does not cover either, since
it judges the checkout path and the primary it resolves to rather than a third path that
checkout's metadata points at — so every target the metadata names goes through that same
central gate, and a symlink is refused outright rather than resolved, because following one
means trusting its target. Each metadata file is read under `_METADATA_MAX_BYTES`, one byte
over being enough to refuse, so an oversized `.git/config` is never held whole in the
gateway's own process — and the read itself opens `O_NOFOLLOW`, because the symlink test and
the read are a check-then-use pair whose in-between the repository owns: it can replace a file
that has just passed the test with a link to a credential store, and the flag makes that swap
fail rather than succeed quietly. Every metadata file is judged, `commondir` included and not
only the path it names, since `is_file` follows a symlinked `commondir` and would open its
target before anything judged it. One leading byte-order mark is removed at that shared decode, because git removes
it too: a config opening `\ufeff[include]` is an include as far as git is concerned, and a
scanner treating the mark as part of the section name misses it — the same mark would also
hide a `gitdir:` from the pointer parse. Detected by SECTION, `[include]` and
`[includeIf ...]` being the only two spellings git honours: an include may name a file that
includes a third, and following the chain to judge each hop would reimplement the resolution
this exists to avoid triggering. An absent `.git` is not a refusal — a path that is not a
checkout has no config of its own — while a config that exists and cannot be READ is refused,
because doubt about its content is not evidence of safety. The refusal carries its own reason
to the banner rather than the generic unread text, since it names something the operator can
see in their own file. Direct git calls inherit the same clearance for free: the refusal
surfaces as the unread answer, which `_assert_read_cleared` already raises on inside
`pre_spawn`.

Every probe's output is captured through a FILE and refused above
`_PROBE_OUTPUT_MAX_BYTES` before it is decoded or split, because `capture_output` reads a pipe
to EOF into the backend and the `tool` rlimit profile bounds the CHILD rather than what the
parent accumulates from it. These probes pass `--name-only`, so no config VALUE is ever
captured and only key volume can grow; an oversized reply routes to the same unread answer,
since a repository answering a small question with an unbounded one has not answered it. The
three probe failures share the `ProbeUnread` base for the reason `RepoUnavailable` exists: the
fail-safe is identical for all of them and a new way to go unread must not slip past a handler
that enumerated only the reasons alive when it was written.

Every read of a foreign checkout goes through ONE gated spawn, `_run_gated_git`, which forces
the strict tier and re-takes the clearance in `pre_spawn`; `_git` is a thin wrapper over it for
the common case, `_run_gated_git_soft` is the wrapper for the resolvers — each already answers a
non-zero rc with a documented default, so a clearance refusal is reported the same way rather
than raised — and a caller needing git's own stderr uses the triple directly. One chokepoint
rather than a convention, because a convention is what kept breaking: the worktree enumeration
spawned at the default `standard` tier with no clearance — the tier where `_GIT_TRUSTED_HELPERS`
is injected — and so did the base-branch resolver's `git remote`, the upstream-remote reads, the
fallback-repo comparison and the untracked-file read, while every per-row read was forcing
`strict` and re-clearing. That is why the invariant is asserted on the module's own source
instead of trusted: exactly two `runtime._run_cmd` call sites may exist, the gated spawn and the
credential-helper load, and the second is exempt because it is repo-INDEPENDENT (`--system` and
`--global` scope only, no `-C`). That pin covers the whole APP rather than this module, because
covering one module is what let `fleet_state.py` spawn `remote get-url` and a `merge-base` bare
while `repository.py` was clean. `worktree_ops.py` is exempt and its exemption is checked, not
assumed: its checkout-scoped spawns are MUTATIONS, bounded by a different mechanism — `_repo()`
raises `RepoReadOnly` for a foreign checkout and the route boundary gates every non-GET on that
accessor — so the pin allows only mutation verbs there and a plain READ added to it fails.
A path is bound to the generation it was CAPTURED under, supplied by whoever captured it, and
a read is refused when the configured checkout has moved since. Sampling the generation inside
the read was a hole with a precise shape: the snapshot build captures a foreign path, a config
switch lands before the read is entered, and because that switch also CLEARS the read-only
verdict the read samples the NEW generation, compares it against itself, passes, and runs git
in the foreign checkout at the `standard` tier — the tier that hands over `_GIT_TRUSTED_HELPERS`.
The `pre_spawn` gate never saw it, because that gate only catches a switch landing between the
sample and the spawn. The capture is taken BEFORE the enumeration that produces the paths —
afterwards, a switch landing mid-enumeration would be stamped with the new generation and the
stale paths it returned called current — and it is carried by EVERY path-based read, not only
the row build: the ahead count, the own-commit count and the real-dirty read each take it too,
since one that samples its own leaves the same hole open for its own field. The lazy
per-worktree detail captures with the lookup that resolves the name; a caller that resolves its
path fresh passes nothing, and the current generation IS its capture.

A read whose SUBJECT is repository-controlled bounds its capture too, not only the probes:
`runtime._run_cmd` takes an optional byte bound, and the commit-context read — ten subjects and
bodies from a checkout this app may only read — passes it, because the size of a foreign commit
message is the repository's choice and `communicate` would hold all of it in the gateway before
any check could look. Each retained subject and body is capped as well, so one oversized field
cannot ride through inside a capture that fits. The bounded path drains both pipes
CONCURRENTLY and the reader that overflows KILLS the child before returning: draining one pipe
at a time is what `communicate` exists to avoid, since a child writing hard to stdout blocks
forever when stderr is unread, and killing on overflow is what brings the sibling pipe to EOF
instead of waiting out the whole timeout. The retained PR body is bounded the same way and for
the same reason: it is written by whoever opened the pull request, and `_PR_CACHE` holds it per
branch for a whole TTL, so it is capped at capture and truncated again at retention.

The worktree listing is bounded in three places, because all three are chosen by whoever controls
the repository rather than by this app: the capture (512 KiB), the record count (512), and each
retained field (4096 chars). Records come from the repository's own admin files and every one is
parsed, held in the fleet cache and rendered. Overflow is REPORTED, never quiet — an over-limit
listing refuses the fleet instead of serving it short, because a missing row is a worktree nobody
was told about and one of them could be the main checkout every other row is anchored to. The
capture bound names itself before the generic git-error path, which would otherwise report a
merely large repository as corrupt.

The captured generation must be FORWARDED, never re-taken. A function that receives one and reads
it again binds the path to whatever checkout is in force after discovery already handed it over,
which is the exact window the capture exists to close; a local reading is the fallback for a
caller that has none, written as `if generation is None`. This is enforced by an AST pin over BOTH
modules that fails on two shapes: a reassignment outside that guard, and a read called from a
generation-taking function without forwarding it. It replaced a text scan of the caller module,
which by construction could not see a read living inside the callee — the blind spot that let
`_dirty_split`'s own `status` read keep its default while the signature around it was threaded.

Every metadata read happens under a PINNED DIRECTORY DESCRIPTOR, because `O_NOFOLLOW` on a file
open guards the final component only. Validating that `.git` is a real directory and later opening
`.git/config` by path leaves the window this module exists to close: the repository renames `.git`
to a symlink in between, the open walks through it, and the leaf is a genuine file at the
attacker's chosen target — a credential store. Confirmed by experiment, not reasoning: a leaf-only
no-follow open reads a planted key through a swapped ancestor, and the pin performs that swap
between the check and the read rather than asserting on source text.

So the checkout's `.git` is opened once with `O_NOFOLLOW|O_DIRECTORY`, and `config`,
`config.worktree` and `commondir` are resolved as bare NAMES against that descriptor. A descriptor
cannot be redirected by renaming a name. The descriptors are closed before returning and no caller
receives a path to reopen: config content travels as `_ConfigRead` records, since a path handed
back is a path opened by name a second time and the repository owns those names. `commondir` is
classified by `stat` under the descriptor rather than inferred from a failed read — a read that
fails is indistinguishable from an absent file, and absent admits the repository, so a symlinked
`commondir` refuses instead of being silently ignored. The linked-worktree pointer is still read by
path, which is sound because `.git` is itself the final component there, with no ancestor inside
the repository to swap.

Whether the platform can resolve a name against an open descriptor is decided ONCE, in
`_PINNED_READS`, and `os.open` and `os.stat` must both support it together: a stat that fell back to
a path while the read stayed pinned would judge one file and open another. Windows supports neither
and cannot `os.open` a directory at all, so there the pinned open is not attempted and the reads
degrade to the ordered check — the same bound as before, not a new hole. That branch is exercised on
every platform by a test that forces the flag off AND makes the directory open fail: left to the
Windows shard alone, a break in it reads as "not a git checkout" for every repository and surfaces
hours later as a red lane somewhere else.

A worktree field wider than its bound REFUSES the listing rather than being sliced. A truncated lock
reason reads as the whole reason, and an operator deciding whether to remove a worktree would be
deciding against text its author never wrote.

Every read refusal emits a denied SEL event on EVERY occurrence, and only the operator-facing log
line is deduplicated. A trail that records the first refusal per verb cannot answer how often or how
recently a repository was refused, which is most of what it is read for; the log line is advice, and
repeating it per row per refresh buries everything else. This covers the filter-driver refusal and
the unread-config refusal as well, both of which can fire on a repository that was already admitted
-- a filter can be written into a config after adoption, which is precisely the event worth having a
record of.

git is never asked anything about a foreign checkout. The config probes read a SNAPSHOT: the bytes
this module already read and vetted are written to a private 0600 file and git is asked about that,
with no `-C` and no path into the repository. This closes the last check-then-use window, and closes
it by construction rather than by narrowing it -- the scan and the question git answers are the same
bytes, so a config replaced in between cannot exist. A separate entry point enforces it: the
snapshot probe refuses an argv carrying `-C`, and it is a distinct function rather than a flag
because what it skips is the checkout probe's whole safety argument, and a boolean would put losing
that one typo away. Both entry points share one sandboxed, output-bounded spawn.

`--includes` stays on the filter read. For a specific scope git defaults include-following OFF, so
the flag is what makes the answer mean what git will DO -- a fact an earlier round established
empirically. The snapshot holds no include, because the scan refuses a config that names one, so the
flag has nothing to follow.

Whether a config names an include is ONE predicate, shared by the adoption scan and the snapshot
guard. The two asking differently is the failure mode, and it happened: the section regex is
start-anchored, so a whole-text `search` answers False for an `[include]` on any line but the first,
and a guard disagreeing with the scan let exactly what the scan refused reach a spawn. Comments are
stripped first, since git honours neither `#` nor `;` lines.

Because the scopes are files rather than flags, `config.worktree` is consulted only when
`extensions.worktreeConfig` is enabled -- read from the `config` bytes already in hand. git ignores
that file otherwise, so reading it unconditionally would refuse a driver git itself would never
resolve.

Discovery itself no longer runs git at all. Resolving a linked worktree's primary checkout and
finding a worktree's own gitdir were both `rev-parse` questions, and asking git where its own
directories are makes git parse the repository's config and follow any `include.path` it names
to answer — the hazard reached by the question that was supposed to precede the scan. Both are
answered from the layout files this module already reads, so two of the four probe sites are
gone and a source pin holds the count. The `commondir` a linked worktree names is normalised
LEXICALLY, never resolved, because `Path.resolve` follows symlinks; and since lexical
normalisation and the kernel disagree when a walked-up component is a symlink — components that
live inside the repository's own `.git` — the claimed common dir must OWN the gitdir that named
it, compared by device and inode through `lstat`, which follows nothing. A common dir carrying a
same-named `worktrees` entry that is a different directory is refused. The pointer read decodes
with `os.fsdecode`, so a path byte that is not valid UTF-8 survives as the surrogate the later
`lstat` needs `os.fsencode` to restore; the `errors="replace"` decode a config SCAN wants would
destroy it, which is why the two decoders are separate functions over one bounded read.

`merge-base` joins the non-converting safelist for thoseresolvers: it reads the commit graph and answers with an exit status, touching no blob, so
neither a filter nor a textconv driver is reachable through it. The settling window likewise
opens BEFORE every await that follows publication, the credential-helper warm included, and the
`try` covers that warm so one that raises cannot leave every mutation refused for the life of
the process.

A refusal is a permission decision, so each leaves a denied SEL event beside the banner text:the fenced path, the repository that would execute its own code on a read, a withheld worktree
root, and a refused content-converting verb — that last one once per verb, because the poll
repeats it per row per refresh and an event per occurrence would bury the log rather than
record a decision. The UNMEASURED verdicts stay unaudited on purpose: an unresolved path gate
and an unread config scope decided nothing.

Every cache derived from the RESOLVED checkout is dropped when a resolution lands on a
different path, before the resolvers re-read. Each of them returns early on a latched value, so
a memo left in place means the new checkout is served with the old repository's remote name,
`owner/repo` and base branch — which `git rebase {remote}/{base}` and the prune ancestry gate
then act on. Read-only mode is what makes this reachable: a foreign checkout now RESOLVES, so
`_upstream_remote` and `fleet_state._get_owner_repo` latch against it, where before they
declined because `_repo_read()` raised. `repository` sits BELOW the modules that own the other
caches in the component DAG, so they register a reset with `register_checkout_reset` rather
than being reached by an import that would invert the dependency; a checkout-derived cache
added later registers in the same change that introduces it.

Clearing a cache is only half of it, because a read already in flight measured the old
checkout and its write lands after the clear. So the reset also bumps a generation counter --
owned by `repository`, which owns the resolution that moves it, so every module above has one
answer to ask -- and every checkout-derived write compares it and declines when it no longer
matches. That
covers the coalesced background reads -- a fleet snapshot that finished against a different
checkout is discarded and rebuilt rather than stored, and a disk aggregation in the same
position restores the cleared state instead of publishing another fleet's worktree names -- and
equally the identity writes, which are the ones with no way back: the `owner/repo` name and the
browser base URL have no expiry at all, and a PR verdict keyed under the wrong repository is
what the unattended reaper reads as MERGED before deleting a worktree, so each of those
declines rather than caching and answers unknown for that poll. The fleet rebuild is bounded at
three attempts and the bound raises rather than serving an empty snapshot, because an empty
fleet asserts a measurement nobody took. The obligation lives in one predicate the writes call,
so a cache added later calls it too instead of joining a list that has to stay in step.

This module's own three resolvers are in that set, not only the caches above it. The base
branch names the ref `/rebase` rewrites onto, the upstream remote is the other half of
`{remote}/{base}` and is memoized with no expiry, and the fallback repo list decides which
worktree-name prefixes count as legacy and whose merged verdict is trusted — each is resolved
across awaits, so each compares the generation before publishing. The base branch publishes
through one closure every tier calls, so a tier added later cannot skip it; the remote's
degraded answer is git's own conventional default, because a wrong name fails the rebase loudly
where a stranger's name can match a same-named remote here and point somewhere else; the
fallback list is left at its not-yet-loaded sentinel so the next call re-enumerates.

Publishing the path and deriving what the path MEANS cannot be one step, so MUTATIONS wait for
the second half. The resolvers read the checkout, which means the path has to be visible to them
first — and in that window the mutation gate sees a managed checkout while `BASE_BRANCH` is still
the shared default the switch reset it to. A rebase landing there replays a worktree onto
whichever base that name happens to match in the NEW repository: it fails loudly when the name
matches nothing, and rewrites history when it matches something, which nothing in this app can
undo. `_mutations_settling_reason` therefore refuses mutations for exactly the width of the
resolver block, cleared in a `finally` so a resolver that raises cannot refuse every mutation for
the life of the process. It is asked in TWO places because not every mutation passes the
accessor: a rebase is rooted at the worktree it rebases, so the route boundary's method gate asks
as well. It is deliberately NOT folded into `_read_only_reason`: that answer feeds the `/fleet`
payload and the page's whole read-only mode, so folding a sub-second transition into it would
flash the banner and withdraw every control on each re-resolution. The READ accessor is
unaffected, because the resolvers themselves go through it.

The clearance is re-taken on every read, not once at adoption. Discovery's answer is about the
repository as it was at that moment; a driver written into the config afterwards is read by the
next `git` invocation and by nothing else, so a one-time answer stops being true exactly where
it matters. `_git` — the chokepoint every read in this module passes through — asks again
before it spawns, and raises `RepoUnreadable` on a driver or on an unread scope, which `/fleet`
renders as the discovery banner. Asked for every read rather than for a list of
content-touching subcommands: a list has to be kept in step with every read this app grows, and
the one that gets forgotten is the bug. The price is two `git config` reads per git read, paid
only on a foreign checkout an operator named explicitly, and neither opens the object database.
Nothing is latched, so a git failure that happened to be transient clears itself.

Two things are bound to the read rather than re-read from a global while it is in flight. The
read-only disposition is sampled once, where the spawn's target path is also fixed, and passed
into the clearance: sampling it again there can answer differently, because a poll re-resolving
to the app's own managed checkout clears it, and the clearance would then skip the driver probe
entirely for a read still aimed at the foreign path. The checkout generation is sampled beside
it and compared before the spawn on EVERY read, whatever the disposition said, because the
pairing of the two is the hazard: the resolution that lands on a managed checkout also CLEARS
the read-only verdict, so a read still aimed at the foreign path it captured would sample the
cleared disposition, skip the clearance and spawn in the tier that carries the trusted
credential helpers. Both refusals are transient like every other one here: the next poll reads
the checkout now configured.

The background refresher is refused with them, because its first act is a `git fetch`: a
network write into a repository the operator handed over to be read, fired on a timer with
nobody watching. `_status_refresher` resolves through `_repo()`, so `RepoReadOnly` lands in
the same idle-and-return branch the unresolved states use. Rows still refresh — the fleet
cache carries its own 10-second TTL and `/fleet` rebuilds through it on demand — so what is
lost is only the periodic fetch, which makes `behind` a reading of the remote-tracking refs
already present and therefore possibly stale. A stale number is the better answer than a
fetch nobody asked for.

Four row fields answer UNKNOWN (`null`) rather than `false`: `build_pending`, `has_venv`,
`has_dist` and `is_live` (with `is_staged` beside it). Each describes an artifact of this
product's own build and service-unit machinery, and that machinery never runs against a
read-only checkout, so `false` would assert "nothing to apply" and "not running" about
mechanisms that do not apply at all.

`live_state_known` is reported `false` there too, because the cutover routes are refused and no
row can be marked live.

The page consumes the mode rather than inferring it. `null` is falsy in JavaScript, so every
`!w.has_dist` read would treat unknown as "no": `DevFleetPage` therefore tests for `null`
explicitly and labels the build state unknown, counts only `has_dist === false` as
needs-provision, and renders `read_only_reason` as a banner above the rows. That banner also
replaces the live-state-unknown notice there — the live state is unknown BECAUSE this mode
refuses the cutover, not because the gateway failed to answer, and offering "check the gateway"
for a healthy one names the wrong cause. No control that posts is offered on a read-only
checkout, mirroring the backend's single gate on the method: the row returns no actions at all
rather than filtering them one at a time, so a control added later inherits the refusal instead
of having to remember it. `restart-gateway` is the one exception and stays offered, because it
restarts the gateway service and touches no repository.

`BASE_BRANCH` is resolved per repository once a checkout publishes (`_resolve_base_branch`)
rather than fixed at `main`, because a repository whose default branch carries another name
would otherwise label its primary row "main" and point every ahead/behind range at a branch
that does not exist. A remote's published `HEAD` is read first, since that is the
repository's own statement of its default branch, then the local names in
`_LOCAL_BASE_CANDIDATES`, then the branch the checkout is on. That last tier ranks BELOW the
candidates on purpose: a dev checkout parked on a feature branch is the ordinary state, and a
present `main` is the better base there, while a repository named `trunk` or `develop` reaches
it and stops carrying a name that matches no ref. Every tier is validated by the same
branch-name guard. Only a checkout that cannot be read at all leaves the value alone, so a
process with no repository keeps reading the name it always did
(`test/test_dev_fleet_repo_accessor.py`).

Exactly ONE remote is consulted for that published `HEAD`: `origin`, or the sole remote of a
checkout that has one under a different name. `git remote` lists names alphabetically, so
reading them in listing order lets an archive or fork remote decide the base while
`_upstream_remote` — which resolves `branch.<base>.remote` independently and falls back to
`origin` — names a different one. `/rebase` combines the two into `{remote}/{BASE_BRANCH}`,
so a disagreement there rewrites a feature branch onto a base the upstream never published,
and a stale `origin/<other-name>` left in the repository makes even the fetch step succeed.
A base taken from a local branch instead composes with the remote read by construction:
`_upstream_remote` resolves the remote that branch tracks.

## Routes

Public routes are under `/apps/dev-fleet/api/*` (gateway proxy, session auth via token
query param or cookie); the backend subprocess serves them as `/api/*` after HMAC
verification. Route names below are relative to that prefix.

### Read (GET)

| Route | Description |
|-------|-------------|
| `/apps/dev-fleet/api/health` | Liveness + gateway **start identity**: `{status, start_id}`. `start_id` is the live unit's `ExecMainStartTimestampMonotonic` (launchd: job PID; foreground last resort: run-marker pid; `null` when unavailable); the dashboard polls it to detect the NEW process after a restart (see Action narration). Served on the proxied `/api/` namespace because the gateway only forwards `/apps/dev-fleet/api/*` to the backend. (The bare `/health` carries the same body but is HMAC-exempt and reached only by the gateway's own internal liveness poll.) |
| `/apps/dev-fleet/api/fleet` | Lightweight worktree + pod list (polled every 12s), including `main_repo` and `main_repo_inferred`. `?fresh=1` forces cache bypass. Answers `{worktrees: [], needs_setup: true}` when no main checkout was found (see Main Checkout Discovery) and `{worktrees: [], error}` when a named checkout is unreadable. |
| `/apps/dev-fleet/api/worktree?name=` | Lazy per-branch detail: PR, commits, disk usage |
| `/apps/dev-fleet/api/pod/logs?name=&n=` | Pod journal tail (recent N lines, default 120) |
| `/apps/dev-fleet/api/run?id=` | Async run status + streamed output (last 60 lines), plus `cause` on a sync failure the gateway can name |
| `/apps/dev-fleet/api/prune-candidates` | List worktrees eligible for pruning |
| `/apps/dev-fleet/api/prune-status` | Live prune progress: per-item state machine (`items`) + backward-compatible top-level counters |
| `/apps/dev-fleet/api/disk` | Aggregate disk usage per worktree (async computation) |

### Write (POST)

| Route | Body | Description |
|-------|------|-------------|
| `/apps/dev-fleet/api/sync` | — | Pull main + rebuild (single-flight; a concurrent call is refused **409**) |
| `/apps/dev-fleet/api/worktree/remove` | `{name, force?}` | Remove a worktree (stops its pod and reclaims that pod's isolated HOME first) |
| `/apps/dev-fleet/api/prune-run` | `{names[]}` | Batch-remove eligible worktrees |
| `/apps/dev-fleet/api/pod/up` | `{name}` | Start isolated pod instance (re-verifies the unit is active) |
| `/apps/dev-fleet/api/pod/down` | `{name}` | Stop pod instance (re-verifies the unit is gone before reporting success) |
| `/apps/dev-fleet/api/pod/restart` | `{name}` | Stop then start pod |
| `/apps/dev-fleet/api/pod/token` | `{name}` | Mint a dashboard token for the pod |
| `/apps/dev-fleet/api/pod/provision` | `{name}` | Start async venv+dist build (returns `{run_id}`) |
| `/apps/dev-fleet/api/pod/provision/dismiss` | `{name, run_id}` | Forget a terminal provision failure when the run id still matches |
| `/apps/dev-fleet/api/rebase` | `{name}` | Rebase worktree onto origin/main |

Two routes are served by the **gateway process** rather than the backend, under the
in-gateway namespace `/api/apps/dev-fleet/` (`gateway_routes.py`, mounted by the
`BUILTIN_NAMES` loop). Both are **dashboard-owner only** and refuse any app token:

| Route | Body | Purpose |
|---|---|---|
| `POST /api/apps/dev-fleet/restart-gateway` | — | Restart the live gateway through its service-manager backend; returns the pre-restart `start_id` for the restart handshake |
| `POST /api/apps/dev-fleet/make-live` | `{path, dry_run?, expected_staged?}` | Repoint the live gateway at another worktree (see Make Live); a real cutover returns `start_id` for the restart handshake |
| `GET /api/apps/dev-fleet/live-target` | `?fresh=1` | The pointer-state read broker `{live, staged, staged_cancel_available, previous}` (`previous` is the pointer's validated one-level undo target, so the fleet's Undo banner needs no pointer read of its own) — admits ONLY Dev Fleet's own app token (how the sandboxed backend learns which row is live); a dashboard human and any other app's token are refused |

Why these two moved: they write the live-target pointer (or hold its cutover latch), and
that file is bind-masked from the sandboxed backend **and every child it spawns** — a
nested sandbox is denied by design, so a worktree's `npm ci` lifecycle script runs in the
backend's namespace. See *Make Live → Pointer file*.

### Agent surface (gateway process, `/api/apps/dev-fleet/pod/*`)

A second, deliberately small pod surface exists for AGENT sessions, served **in the
gateway process** rather than by the backend subprocess (`agent_pod_api.py`).

Why it is separate rather than a reuse of the proxied routes above: on Linux, an agent
session runs behind a sandbox with its own user namespace, so it cannot `connect(2)`
the systemd user-bus socket that pod lifecycle verbs need, and `kirocrew pod up` in
an agent shell fails with a bare `Permission denied`. The gateway is the process the
sandbox launcher descends from, so it holds the host bus. The agent reaches these routes the
way it reaches any tool — an MCP call, then loopback HTTP — with no D-Bus passthrough
into the sandbox. The proxied `/apps/dev-fleet/api/*` routes cannot serve this: they
require a dashboard cookie or token, which an agent does not hold, and admitting an
internal-secret caller there would expose the app's whole backend surface.

| Method | Route | Input | Description |
|--------|-------|-------|-------------|
| POST | `/api/apps/dev-fleet/pod/up` | `{worktree}` | Boot a pod; answers the CLI's `--json` handle (`base_url`, `token`, `port`, `ttl`). Provisioning is NOT reachable here: a cold venv + SPA build is minutes of work, and one blocking request for that dies to any timeout with no way to learn the outcome. An unbuilt worktree is refused with the CLI's own remedy; the dashboard's Provision button streams the same work under a run id |
| POST | `/api/apps/dev-fleet/pod/down` | `{worktree}` | Stop the pod and reclaim its isolated HOME |
| GET | `/api/apps/dev-fleet/pod/status?worktree=` | — | `{name, status, port, health}`, as `pod status --json` reports it |
| GET | `/api/apps/dev-fleet/pod/list` | — | `{pods: [{name, port, health}]}` for every pod active on the host, unfiltered by repo |

Contract:

- **Gated on the app being enabled** (`_require_enabled`), since routes are
  registered at startup and Dev Fleet ships `defaultEnabled: false`.
- **No operator opt-in and no per-call approval, deliberately.** A pod runs the code
  in a git worktree an agent can write, started by the per-user service manager
  (systemd `--user` on Linux or launchd on macOS), so it
  executes outside the agent's sandbox. That reachability is Kiro Crew's DOCUMENTED
  posture rather than something these routes introduce: `security.md`, under "Scoped
  user-bus locator forward", records that sandboxed agent shells legitimately run
  `systemctl --user` and the `kirocrew pod` CLI, and names the residual in the same
  paragraph; the builtin `pod-e2e` skill has always told agents to boot pods. An
  extra gate here would not close that residual — every other path to it stays open —
  it would only stop the agent-driven QA loop these routes exist to restore. Agent
  pod control is an intended capability, so it is not gated. State the residual
  precisely rather than comfortably: on a host whose OUTER sandbox denies the user
  bus, these routes are the one path from an agent-writable worktree to code running
  unsandboxed as the user, so enabling Dev Fleet on such a host now carries that
  surface. App admission policy can deny the app outright where that is unwanted.
- **Named one by one in `server._STRICT_INTERNAL_API_PATHS`**, never as a
  `/api/apps/dev-fleet/pod` prefix. That table is exact-or-prefix
  (`path == entry or path.startswith(entry + "/")`), and this app's neighbourhood
  includes worktree prune and the Make Live cutover, which must not become reachable
  by holding the internal secret.
- **STRICT, not mixed** — no browser calls them. Each handler re-asserts local origin
  AND `internal_auth`, because a `local_only=False` deployment reclassifies strict
  paths as mixed (same reason `/api/computer-use/frame` re-asserts both). Local origin
  is the UNION of the AF_UNIX socket and a loopback address, mirroring the
  middleware's own `_unix_sock is not None or is_loopback(...)`: `mcp_core` prefers
  the gateway's unix socket whenever the file exists, and `request.remote` is empty
  over AF_UNIX, so testing the loopback half alone would refuse every call on the
  platform pods actually run on.
- **No pod logic of its own.** Every handler delegates to the same `worktree_ops`
  helpers the dashboard's buttons call, so "up" means one thing and a pod's status
  has one definition.
- **Refusals are 409 with a literal `code`** (`pod_up_failed`, `pod_down_failed`,
  `pod_status_failed`, `pod_list_failed`); malformed input is 400
  (`invalid_worktree`, `invalid_body`). A refused lifecycle op is a host-state
  answer, not a gateway bug. `repository._repo()` raises when no main checkout is
  configured, and the read verbs catch that rather than letting a setup problem
  surface as a 500.
- **Every lifecycle CHANGE and every guard denial is written to the Security Event
  Log** under `dev_fleet.agent.*` (`pod_up`, `pod_down`, `machine_guard`): the caller
  is unattended, so the trail is what makes the run reviewable. The read verbs are
  not audited there — they change nothing, and the framework-level tool log already
  records the call.

The model-facing half is the `pod_up` / `pod_down` / `pod_status` / `pod_ls` tools on
`kirocrew-core` (`mcp_tools/apps.py`). The pod token is returned to the agent
verbatim — redacting it would hand back an unusable handle — which is safe because it
is a 2h credential scoped to that pod's own gateway, minted server-side from the
pod's own internal-API credential so the agent never touches the secret itself.

## Authorization

The backend-proxied endpoints (`/apps/dev-fleet/api/...`) inherit gateway session
auth with no additional RBAC — all authenticated users can manage worktrees, and
destructive operations (remove, prune) require client-side confirmation dialogs in
the frontend. The two in-gateway write routes, `POST /api/apps/dev-fleet/make-live`
and `POST /api/apps/dev-fleet/restart-gateway`, are the exception: they are
dashboard-**owner** only and refuse every app token, because the pointer they write
selects the code the gateway executes next (see *Make Live → Pointer file*). The
in-gateway read and lease routes admit only Dev Fleet's own backend token.

## Input Validation

- `name` parameter is validated against the discovered worktree set before any operation.
  The agent surface's `pod down` also accepts a missing checkout only when this
  repository retains the matching git worktree record (see *Pod identity guard*).
- Ambiguous worktree names (multiple checkouts with same basename) return HTTP 400
- `force` must be a boolean when provided
- Main worktree removal is always refused regardless of force flag

## Prune Rules

A worktree is eligible for automatic pruning if:

1. **PR merged** — GitHub PR state is `MERGED` AND `git cherry` shows 0 patch-unique
   commits ahead of main AND the worktree is not dirty
2. **Empty + stale** — zero own commits, not dirty, and older than 48 hours

Worktrees NOT pruned: dirty, active (own commits > 0), fresh (< 48h), or merged-with-
new-commits (unmerged follow-up work after the PR landed).

### Parallel execution & per-item progress (issue #435)

`prune-run` accepts a batch of names and processes them **concurrently** rather than one
at a time. The design separates the two cost classes:

- **Expensive per-item phases are concurrency-bounded.** The fresh `_prunable`
  re-verdict (which makes `gh`/`git` network calls) runs under an
  `asyncio.Semaphore(4)`, so a batch is bounded by the slowest ~4 items at a time
  instead of the sum of all of them. Pod shutdown remains inside the make-live
  exclusion window because removal must continuously protect the target from the
  final live/staged re-check through deletion.
- **Git mutations are serialized.** The `git worktree remove` + branch `update-ref -d` for
  every removal — including the single-worktree remove handler and the auto-prune reaper —
  run behind one shared `asyncio.Lock` (`_GIT_MUTATION_LOCK`), because they mutate the
  shared main-repo `.git` state (worktree admin dir + `packed-refs`). Concurrent git
  mutations would otherwise race on those lock files.
- **Lock order: `_wt_lock(name)` → `_MAKE_LIVE_LOCK` → `_GIT_MUTATION_LOCK`.**
  This order must never be reversed. Every removal first acquires the worktree lock,
  then acquires the make-live lock before the live/staged protection re-check and holds
  it through deletion. A concurrent rebase cannot claim the checkout after removal's
  initial fail-fast check, and a concurrent `/make-live` cannot stage the target between
  the protected re-check and `git worktree remove`. Forced prune delegates to the same
  internal removal path rather than pre-acquiring either lock.
- **Rebase gate.** `_worktree_remove` refuses immediately if `_wt_lock(name)` is already
  held, then acquires and holds that lock through deletion. The unlocked check and
  acquisition are adjacent with no intervening await, so acquiring a free `asyncio.Lock`
  does not yield an interleaving point. Rebase holds the same lock across fetch, rebase,
  and abort; deletion can therefore neither begin during a rebase nor race one that starts
  after the initial check.

**Failure isolation:** each item is driven to a terminal state independently — one item
failing (a `gh` timeout, a stuck pod, or an unexpected exception) never aborts the rest of
the batch, and every item is finalized exactly once (terminal status + `done` bump).

**Per-item status API:** `prune-status` returns an `items` map keyed by worktree name,
each `{status, error}` where `status` is one of `pending | verifying | stopping_pod |
removing | done | failed`. The top-level `running`, `total`, `done`, `current`, and
`results` fields are retained for API-shape compatibility (the auto-prune reaper and older
consumers). Note that under parallel execution `current` is **best-effort**: it names one
of the currently in-flight items (never a completed one; `None` when idle), not "the"
single item being processed — new consumers should read `items` instead. Duplicate names
in a `prune-run` request are deduplicated (order-preserving) before workers launch, so a
name never has two workers racing to remove the same worktree. The frontend renders
`items` as a per-item checklist (status chip + inline
failure reason); the preview dialog maps the kept-list verdict codes to human-readable
reasons so users can see why a worktree is a candidate or is kept.

**Scan feedback:** the preview that opens that dialog (`prune-candidates`) runs `git` --
and for merged- or closed-verdict candidates a `gh` lookup -- per worktree. Those
per-worktree verdicts run concurrently, bounded by `_PRUNE_CONCURRENCY` (the same bound
the parallel prune workers use), because a serial scan of a large fleet exceeds the
gateway app proxy's 30s `_PROXY_TIMEOUT` and returns a 504. The scan is read-only git
(`rev-parse`, `status`, `rev-list`/`cherry`, `merge-base`) and never takes
`_GIT_MUTATION_LOCK`, which only the destructive removal path holds; candidate and kept
lists are emitted in discovery order regardless of which verdict finishes first. Even
concurrent, the scan takes time on a large fleet, so the Prune merged button swaps its
trash glyph for a spinner and sets `aria-busy` for the duration: disabling alone is
indistinguishable from a wedged page, and a user who reads it as hung clicks again or
reloads mid-scan.

The merged/closed head-OID check (`_fetch_pr_head_oid`) resolves the PR by
`gh pr list --head <branch> --state all`, not `gh pr view <branch>`, so a merged PR whose
head branch was deleted on merge still resolves its head OID and its worktree becomes a
candidate rather than being withheld as `merged_unverified` forever. The lookup reads the
whole PR set for the head and authorizes removal only when no `OPEN` PR is present; it
requests one row beyond a fixed ceiling and fails closed if the head carries more PRs than
that ceiling, so a reused branch name can never authorize removing new work.

## Pod Integration

Relies on `kiro_crew.pod` subpackage (optional import — degrades gracefully if unavailable).
On Linux, `runtime.require_backend()` runs `systemctl --user is-system-running` once at
pod verb entry and at each Dev Fleet removal safety gate. `kirocrew doctor` runs the same
probe for its advisory row. Low-level `systemctl()`, `is_active()`, and `main_pid()` calls
keep only the cheap platform, executable, and bus-address checks, so a multi-query verb does
not pay a five-second probe before every unit command. A provably absent bus address returns
`no_session` without spawning systemctl, and that pre-spawn check is the only source of
backend absence. Every spawned probe failure is operational except a positive permission-denied
match, which is `sandboxed_away`; neither can become `PodBackendAbsent`. Probe and unit operations
resolve `systemctl` through `platform_compat.trusted_system_bin()` and pass that absolute path to
the subprocess seam; a PATH entry can neither execute code nor forge the removal-safety verdict.
If no trusted executable exists, the operation fails closed. Dev Fleet therefore refuses
removal instead of treating the host as unable to contain a live pod. Spawned probes use
`LC_ALL=C`, distinguish a reachable manager,
an outer sandbox denial, and an unclassified failure, and retain systemctl's raw
diagnostic. Dev Fleet runs both backend probes through `subprocess_executor()` so
worktree removal never blocks the gateway event loop.

- `runtime.active_names(cfg)` — one point-in-time systemctl/launchctl listing per fleet build
  (blocking, offloaded via `run_in_executor`), shared by every worktree row
- `runtime.derive_port(cfg, name)` — cksum-based port derivation (blocking, offloaded)
- `runtime.health(cfg, name, port, timeout)` — identity-gated HTTP probe (blocking,
  offloaded). Takes the pod's NAME, not just its port, because a derived port is
  routinely held by another pod or by the live gateway: `port_owner` requires the
  process a `127.0.0.1` connect reaches to be this pod's own `MainPID`, and a
  responder that is provably somebody else's returns `HEALTH_FOREIGN` (`-2`)
  instead of its HTTP status. The fleet row treats that as unhealthy, since the
  frontend's `health >= 200` test already excludes a negative value. There is
  deliberately no bare-port variant to call — see `instances/run_marker`, which
  states the rule ("no caller can mistake reachability for identity")
- `runtime.mint_token(cfg, name, ttl)` — credential minting (blocking, offloaded).
  Requires POSITIVE ownership proof and refuses when ownership is merely
  unprovable, unlike `health`, which keeps its reading: this call sends the pod's
  own internal-API credential, so failing open would hand a credential to whatever
  answered. That credential resolves per listener first, from the pod home's
  `run/gateway-<port>.secret`, and falls back to the shared `.local_secret` only
  for a gateway predating the per-listener file — the shared slot is one per data
  home, so a live second gateway leaves it naming the other generation
- `runtime.recent_journal(cfg, name, n)` — journalctl tail (blocking, offloaded)
- `provision.has_venv(path)` / `provision.has_dist(path)` — filesystem checks (offloaded)

All blocking pod operations are offloaded via `asyncio.get_running_loop().run_in_executor(
subprocess_executor(), ...)` to avoid blocking the gateway event loop.

Pod lifecycle verbs (`up`/`down`/`restart`/`provision`) shell the CLI via
`_find_cli()` = `[sys.executable, "-m", "kiro_crew"]` — the **package** entry
(`kiro_crew/__main__`, which also runs the required SSL-cert / UTF-8-console
setup), never `-m kiro_crew.cli`. `kiro_crew/cli.py` has no
`if __name__ == "__main__"` guard, so `python -m kiro_crew.cli <cmd>` imports the
module, runs no `main()`, and exits 0 with no output — which turned every pod op
into a **silent no-op the backend reported as success** (the "Stopped but still
running" bug, issue #220). As defence-in-depth, `_pod_up` and `_pod_down` both
re-check `runtime.active_names` after the CLI returns and fail closed
(`pod not active after start` / `pod still active after shutdown`) — a CLI exit 0
is never taken as proof of the state change, in either direction.

### Pod identity guard

Pod names are global basenames while Dev Fleet scopes worktrees to `MAIN_REPO`,
so every pod verb first runs `_pod_checkout_guard`. It resolves the name to this
repo's worktree, reads the pod's pinned `CHECKOUT` strictly, and refuses when the
pin names a different checkout, carries no verifiable `CHECKOUT`, or is absent
while a unit under that name is active. A matching pin proceeds, and so does no
pin with no live unit. Every refusal is about identity: acting on a basename
collision would stop another repository's pod or delete its HOME.

A missing checkout is attributed only by git's retained worktree record for this
repository. `repository._find_retained_worktree_path` includes a `prunable`
record that normal discovery omits. When no record names the worktree, the agent
surface refuses and the CLI `kirocrew pod down <name>` remains the remedy.

For a retained record, `_pod_down` submits `_reclaim_pod_locked` to the
subprocess executor. The helper runs under `pod_name_mutex`, re-reads the pin,
and refuses unless it still matches the retained path. It also refuses when that
path is back on disk, because a new pod may own the name. Pin attribution and
teardown are one locked transaction, so a same-name pod cannot be accepted
between the ownership decision and `stop_pod`. `up` requires a discovered
checkout and never takes this missing-checkout path.

### Pod HOME reclamation on worktree removal

Removing a worktree reclaims the isolated `KIROCREW_HOME` of that worktree's pod
whether or not the pod is still running, because a stopped pod still owns its
HOME and this is the last moment anything can attribute that directory to this
checkout — afterwards the per-pod env pin naming it is gone and only a bulk
`pod prune` could find it. Reclaiming only a LIVE unit would therefore reclaim
nothing on the ordinary path: the operator stops the pod when testing ends and
prunes days later once the PR merges, so the unit is inactive by then and every
removal stranded a full isolated HOME (a per-instance embedding-model copy
dominates its size).

Which directories qualify is decided by `runtime.orphan_homes`, the same
predicate `pod ls` and `pod prune` use, rather than a bare directory probe — so
symlinks are skipped and, on macOS, a name whose per-pod plist exists counts as
*installed* rather than orphaned and is never reclaimed from underneath a
concurrent `up`. That predicate keys on the pod root, liveness and plist and
never on the checkout pin, so attribution is not its job.

Attribution and teardown are ONE locked transaction. `_reclaim_pod_locked` runs
entirely inside `runtime.pod_name_mutex` — the cross-process flock every mutating
pod path cooperates on — and reads the checkout pin, decides ownership, calls
`runtime.stop_pod`, and clears the per-pod env file without ever releasing it.
Splitting those halves is what the lock exists to prevent: pod identities are
global basenames, so between an ownership check in one process and a teardown in
another, a concurrent `pod up` from a DIFFERENT checkout can claim the same name
and the teardown would stop that pod and delete its isolated HOME. Both call
sites in `_worktree_remove` — the live-unit path and the orphaned-HOME path — go
through this one helper, so neither carries that window.

That is also why the reclaim is in-process rather than a `pod down` shell-out:
the mutex is held per open-file-description and `stop_pod` re-acquires it, so a
caller holding it around a subprocess would block the child it waits on. The
mutex is reentrant *within a thread* and the helper is submitted to the executor
as a single callable, so `stop_pod`'s own acquisition nests instead of
deadlocking. The helper mirrors `_pod_checkout_guard`'s attribution rules with one deliberate
tightening: an ABSENT pin is a refusal here. The guard allows an unpinned name
when no unit is live, which is right for operating on a pod the caller located,
but this path DELETES the HOME and a same-basename leftover from another checkout
is indistinguishable from here, so deletion demands positive attribution. The
cost is that an unpinned orphan is not reclaimed automatically — `pod prune`
still takes it — which is the cheaper side of the trade. It also mirrors the
CLI's post-teardown env-file clear, and leaves that file alone when `stop_pod`
reports the name was handed to a new pod mid-teardown (it now pins the new pod's
checkout).

`handed_over` is a REFUSAL at both call sites, not a success: a new pod holds the
name, which checkout it belongs to is unknowable here, and it may be running out
of the very worktree about to be deleted. The post-stop liveness recheck is not a
substitute, since it can miss a unit that is still bootstrapping.

The two fail directions are scoped separately on the orphan path. The
ENUMERATION is best-effort cleanup — an orphan scan says nothing about liveness,
so its failure degrades to a named leftover rather than turning a lost directory
into a lost removal. The RECLAIM is teardown and fails CLOSED: a returned failure
refuses the removal, and a RAISED one is deliberately not caught there either,
because a teardown that died mid-flight (a stop that timed out against a
still-activating unit) is exactly the state in which removing the checkout is
unsafe.

The result reports the two outcomes separately: `stopped_pod` for a unit that was
running, `reclaimed_pod_home` for a HOME reclaimed with nothing running.

Two failure directions are deliberately different. A **liveness** check that
cannot run fails CLOSED and refuses the removal, because it guards against
deleting a checkout out from under a running pod. A **reclamation** step that
cannot run degrades: the orphan scan says nothing about liveness, so an
enumeration error logs the leftover (pointing at `pod prune`) and the removal
proceeds, rather than turning a lost directory into a lost removal. When the pod
backend is provably absent the HOME is left in place on purpose — liveness is
then unprovable and deleting a HOME that may belong to a live gateway is the one
outcome teardown must never risk — but the path is logged at WARNING with the
`pod down` verb that reclaims it, so the residue is visible instead of silent.

### Provisioning Dependency Install

`provision.ensure_venv` and `provision.build_dist` install the dependencies each
step needs before using them, so provisioning a **fresh** worktree (no
`.venv`, no gitignored `website/node_modules`) does not fail on missing tools:

- **venv (`ensure_venv`)** — after `python -m venv`, upgrades pip, then runs
  `pip install --editable <checkout> --group dev` so the PEP 735 `dev`
  dependency-group (pytest, flake8, isort, mypy, …) is present and the build
  gate can run inside the pod venv (issue #230). `pip --group` needs pip
  ≥ 25.1; if the command exits nonzero (older pip) it falls back to a
  runtime-only `pip install --editable <checkout>` and `_say`s a warning that
  dev tools were skipped — provisioning never hard-fails just because the dev
  extras could not be installed.
- **dist (`build_dist`)** — before `npm run build`, calls
  `ensure_node_modules(website)`: if `website/node_modules/.bin/tsc` is missing
  it runs `npm ci` (falling back to a NON-MUTATING `npm install
  --no-package-lock` on lockfile drift — the flag keeps the fallback from
  rewriting the tracked `website/package-lock.json`, so provisioning never
  dirties the worktree), otherwise
  it skips (fast idempotent path). Without this, a fresh worktree's `npm run
  build` dies with `tsc: command not found` (issue #229).

### Pod Unit Self-Heal

The unit template is written once by `pod install`, so a machine keeps whatever it
installed. On `pod up`, the pod CLI re-renders it when the installed unit is one this
build will not boot:

1. Detects a stale unit via `unit.unit_is_current(cfg)`, which fails on either of two
   triggers:
   - the baked `ExecStart` binary no longer exists — `unit.unit_exec_ok(cfg)` reads the
     unit file and checks `os.access(exe, os.X_OK)` on the baked path (typically the
     worktree it resolved into was pruned)
   - the unit carries a directive this build has removed (`unit._REMOVED_DIRECTIVES`,
     currently `ExecStopPost=` — see the pod module's teardown section)
2. Re-renders the unit with a currently-valid binary (`unit.install_unit(cfg)`)
3. Runs `daemon-reload`
4. Audits the self-heal event
5. Proceeds to start the pod normally

The first trigger prevents the permanent EXEC 203 failure loop that occurs when
worktrees are pruned after the unit was installed. The second is the upgrade path: a
unit installed by an older build would otherwise keep a teardown hook that races the
pod's own subprocesses and wipes the HOME on the stop half of a `Restart=`, and it
would keep doing so until someone reinstalled by hand.

## Background Tasks

- **Status refresher** (`_status_refresher`) — runs every 60s, fetches origin + refreshes
  fleet cache. Started via `dev_fleet_startup` on app startup.
- **Auto-prune reaper** (`_auto_prune_reaper`) — opt-in background loop that removes
  merged worktrees on a timer, reusing the manual-prune verdict (`_prune_candidates`,
  filtered to `code == "merged"` only — the stale-empty class stays manual) and
  `_worktree_remove` guards (stops the pod first, squash-safe OID race guard, never
  force). Disabled by default; enable via `dev_fleet.auto_prune.enabled: true`
  (a **literal boolean** — a truthy string like `"false"` does NOT arm it) with
  optional `interval_secs` (floored at 300s, default 3600s), re-read each cycle
  so it toggles live
  without a restart. Cycles that remove or fail anything are SEL-audited under
  `dev_fleet_auto_prune`. Cancelled on `dev_fleet_cleanup`.
- **Fleet cache** — 10s TTL. Cold requests block on fresh data; warm requests serve stale
  and background-refresh. Concurrent rebuilds (the background revalidate plus any number
  of `?fresh=1` requests) coalesce onto a single in-flight build, so a rebuild never costs
  more than one `gh pr` round-trip per branch. A successful `_worktree_remove` evicts that
  worktree from the cached snapshot and zeroes the timestamp, so the next response stops
  listing a removed worktree without waiting for a rebuild. An eviction also tombstones the
  name against an eviction counter: a rebuild that started before the removal still read the
  worktree from git, so it re-applies any eviction recorded after it began rather than
  storing a snapshot that would resurrect the row. Tombstones are reaped by the first build
  that started after them, so a worktree later re-created under the same name is not hidden.
  The dashboard refreshes with
  `?fresh=1` after every mutating action (and on the explicit Refresh button) so it never
  renders the pre-mutation snapshot.

## Async Runs

Long-running operations (sync, provision) are tracked via `_RUNS` dict with:
- Streamed stdout (last 500 lines kept **server-side**)
- Watchdog deadline (30 min default, configurable via `_RUN_DEADLINE_S`)
- Status: `running` → `done` | `timeout`

On deadline expiry the run's whole process tree is reaped, in two steps. The
spawned CLI gets its own process group, so a single `killpg` covers it and its
ordinary children (pip, git, npm). That is not sufficient on its own: build
tooling spawns grandchildren into *new sessions*, which sit in a different
process group and survive a group kill. So descendants are enumerated **before**
any signal is sent — killing reparents survivors to init and erases the PPID
links that identify them — and each survivor is then killed via its own tree
kill, so a nested group (npm → vite) goes down with it.

This matters beyond tidiness: an escaped `npm run build` keeps rewriting
`website/dist` after the run is reported dead, and its staging lock died with
the process that held it. A later sync would then stage a bundle a live writer
is still mutating, and the completeness check cannot detect it — that check only
resolves `/assets/` references reachable from `index.html`, while the
lazy-loaded chunks such a writer is mid-write on are unreachable from it.

Clients poll `/apps/dev-fleet/api/run?id=<run_id>` for progress. The endpoint
returns only the **last 60 lines** of `run.output` (a sliding tail window), not
the full server-side 500-line buffer — see the accumulation note below.

### Provision progress UX (frontend)

A worktree being provisioned renders an inline **stepper strip** spanning the
row's right columns (mirroring the main-row Pull+Build stepper): spinner +
`Provisioning` label + a coarse phase tag (`venv`/`dist`, derived from
provision.py's `[provision] creating venv …` / `[provision] building dist …`
markers) + the last output line + elapsed time + a `log ▾`/`log ▴` toggle. The
toggle expands a `<pre>` panel under the row showing the accumulated log
(auto-scrolled while streaming).

**Log accumulation (what "full log" actually means).** The `/run` endpoint only
returns the last 60 output lines per poll, so a long provision scrolls early
lines out of that window. The client therefore **accumulates** windows rather
than replacing state each poll: `mergeLogWindow(buffer, window)` finds the
longest suffix of the running buffer that is also a prefix of the newly polled
window and appends only the non-overlapping remainder. This reconstructs the
full stream across the normal case where the window advances by fewer than 60
lines between two ~2s polls. **Honest limitation:** output that scrolls more
than a full 60-line window between two polls (extremely fast-scrolling bursts)
has no overlap to anchor on and those intermediate lines are lost. When that
happens (zero overlap against a non-empty buffer), the client inserts a visible
`[… lines missed …]` marker line into the panel so the transcript never
silently overstates its completeness — the panel is the best client-side
reconstruction plus an explicit gap signal, not a guaranteed-complete
transcript. The heuristic's retirement path (a `since=<index>` cursor or raised
tail on `/run` for a guaranteed-complete log) is tracked in issue #321.

**Reattach on button-click (single-flight).** The provision endpoint is
single-flighted per checkout: if a provision is already running it replies
`{ok:false, error:"provision already running", run_id:<in-flight rid>}`. The
frontend treats **any** response carrying a `run_id` as a run to attach to and
resumes polling it — it does **not** render a failure. Only a response with no
`run_id` is a genuine "failed to start". This makes a second Provision click
during an in-flight build reattach to the live run instead of showing a false
red state.

**Failure persistence:** on failure/timeout the run is **not** cleared — the
strip shows a red `✕ Provision failed (exit N)` label with the log
auto-expanded, and both persist until the user clicks the dismiss `×`
(dismiss also refreshes the fleet). The notice's message is the failing step's
stderr tail, read from the same `::steperr::<idx>::<line>` markers the sync
runner emits (`provision.py::_run` pipes each step's two streams, relays both
to stderr line by line, and `_fail` re-emits the stderr tail of the step whose
failure ENDED provisioning — a recovered failure such as `npm ci` falling back
to `npm install` is log text only). The last output line is the fallback, for a
gateway whose provision emits no markers. See "When there is no reserved code"
below for why the last line alone names a progress line; the same relay, the
same byte-derived read cap, and the same marker filtering in the log panel
apply to both runners. On success it flashes a green
`✓ Provisioned` briefly, then clears (the fleet refetch flips the row to its
built state).

**Reattach after a page reload (server-backed).** Each `/fleet` worktree entry
carries a `provision_run_id` while that checkout's provision run is still
executing or after it finished unsuccessfully (mirroring `sync_run_id`).
Successful and registry-evicted runs are omitted — there is nothing to
reattach to. On mount the page fetches `/run?id=<rid>` for each exposed id: a
running run resumes polling into the stepper (accumulating the log window as
usual), and a failed run restores the persisted red failure state with its log
auto-expanded. Reattached and locally-started runs are deduped by run id, so a
fleet refetch never starts a second poll loop for a run already being tracked.
The dismiss `×` posts the worktree name and run id to the server before clearing
the local strip. The server removes the persisted id only when it still matches
that terminal run; a stale dismiss cannot clear a newer provision, and a running
provision cannot be dismissed. A successful response therefore survives reload.

## Action narration (restart + sync feedback)

Dev Fleet's two slowest actions — **Restart Gateway** and **Sync (Pull+Build)** —
narrate their progress so users don't read them as hung and fire them again. A
duplicate Restart Gateway causes a second real ~10s gateway outage
([issue #639](https://github.com/kirodotdev/KiroCrew/issues/639)).

### Restart identity handshake

`POST /api/apps/dev-fleet/restart-gateway` (gateway process) returns `{"ok": true, "start_id": …}`
after the platform manager accepts the restart. Linux schedules detached
`systemd-run`; macOS submits `launchctl stop` under the loaded contract described
below. The bounce happens after the response, so success does not mean the new
gateway is serving yet.

To close that gap the backend captures the unit's **start identity** BEFORE
scheduling the restart and hands it to the frontend:

- **Identity is manager-specific.** systemd uses
  `ExecMainStartTimestampMonotonic`; launchd uses the loaded job PID. Both change
  when the replacement main process starts. On a host with NO drivable manager
  (the foreground last resort below) the identity is the pid the gateway records
  in its `run/gateway-<port>.pid` sidecar — written before readiness is
  published, rewritten by the replacement, and consumed by the same
  changed-identity comparison unchanged.
- The current identity is reported by extending the existing **`/health`**
  surface (`{status, start_id}`). Because the gateway proxies only
  `/apps/dev-fleet/api/*` to the backend, the same handler is registered at
  **`/api/health`** and the dashboard polls **`/apps/dev-fleet/api/health`**
  (the bare `/health` stays HMAC-exempt for the gateway's internal liveness
  poll). The gateway is treated as recovered ONLY when the reported `start_id`
  DIFFERS from the one captured before the restart. A 200 from the old process
  still winding down returns the SAME identity and is correctly NOT counted as
  recovered.
- **None-safe degrade.** An absent/zero systemd stamp or absent launchd PID
  yields `start_id: null`; the frontend then reloads on the first reachable
  response instead of waiting forever.
- **A reachable 404 counts as recovery.** Cutting over to a worktree whose
  dev-fleet backend predates `/api/health` leaves that route answering 404
  permanently, so its `start_id` can never appear and waiting for one would burn
  the whole timeout. A 404 during the handshake still proves a gateway IS serving
  us, so it is treated as recovered and the page reloads into it. (A backend that
  is not up at all fails differently — the proxy answers 502, or the fetch
  rejects — so this rule does not fire while the new process is still starting.)
- **Make Live reuses the same handshake** — a cutover is a restart into
  different code with the identical early-200 hazard, so a real
  `POST …/make-live` cutover also returns the pre-restart `start_id` and the UI
  recovers on an identity change.

### Completed-cutover Undo banner

After the reloaded fleet proves the pointer target is the checkout actually
running, `undo_target` exposes the validated, still-discovered
`previous_checkout`. The page renders a persistent success banner naming the
current checkout and a **Switch back to `<previous>`** button. The inverse action opens a confirmation
that names the destination and accurately distinguishes an automatic restart
from a staged/manual one, then posts `{path, undo: true}` through the same Make
Live transaction and restart handshake.

The banner is deliberately absent while a pointer is staged but not running:
**Cancel staged cutover** is the inverse in that state, while Undo is the inverse
of a completed cutover. Dismissing the banner stores its current
`current→previous` pair in per-tab `sessionStorage`, so page reloads and Dev Fleet
revisits keep that pair hidden. The stored pair is spent as soon as a loaded fleet
payload reports a different pair or none at all, so a later Make Live is visible
even when it recreates the very same pair (feature → main dismissed, back to main,
the same feature live again). A successful Undo consumes `previous_checkout`, so the reloaded page has
no accidental redo banner.

### Restarting UI state

While the handshake runs, the frontend holds an explicit **"Restarting —
reconnecting"** full-screen state and disables Restart / Pull+Build / Make Live
so the slow window cannot be re-fired. The poll is bounded (`RESTART_TIMEOUT_MS`,
60s); on timeout it surfaces an actionable error ("reload manually / check
`kirocrew logs`") instead of spinning forever.

**The lockout starts before the overlay does.** The restarting flag only goes
true once `POST …/make-live` has *returned*, but that request is itself what
writes the live-target pointer and issues the restart — a Restart fired inside
that window can tear the gateway down between the pointer write and the restart,
leaving a stale process running against the new pointer. Every global action
predicate therefore also honours an in-flight cutover on ANY worktree row (the
busy flag is per-worktree; the hazard is process-wide).

### Sync single-flight + step narration

`POST /apps/dev-fleet/api/sync` is single-flight: a second concurrent request is
refused with **HTTP 409** (`{"ok": false, "error": "sync already running",
"run_id": …}`) rather than launching a second ~90s fetch → merge → pip install →
npm ci → npm build + stage. That refusal is a **state to act on, not a failure
to report**: the body names the run already in flight, and the client attaches
its progress stepper to that run. A second press is a user who cannot see the
sync, so reporting an error would leave them exactly where they started —
without progress, and with the button still inviting a third press. Because the
API client throws on any non-2xx, this path is reachable only through the
error branch, and the `run_id` reaches it via the parsed body carried on the
thrown error, never as a returned body.

The run script emits a
`::step::<idx>::<label>` marker per
step; the run worker records BOTH the authoritative step index and its **label**
onto the run entry (`step` / `step_label`), so `/run` can name the CURRENT step
even after the marker scrolls out of the 60-line output tail window. The
frontend shows that label beside the "Syncing" spinner. This reuses the
existing `_RUNS` / `::step::` / `/run` run-tracking mechanism — the same channel
the provision log panel uses (#320) — rather than adding a second one.

`sync_run_id` — the pointer a freshly-mounted page reattaches that stepper to —
and each row's `provision_run_id` are read at **request time** and overlaid onto
the fleet payload, not taken from the cached snapshot. `_FLEET_CACHE` is
stale-while-revalidate, so a pointer baked into the snapshot made a run started
after that build invisible for a full cache cycle plus a rebuild, which is the
same "no progress, press it again" trap from the other end. Both are in-memory
reads (a module global; a dict copy plus `_RUNS` lookups), so paying for them per
request is cheap. `_build_fleet` deliberately does **not** write them: one owner,
so no reader of `_FLEET_CACHE` can pick up a frozen id. The overlay is
authoritative rather than a fill-in — a provision that finished after the
snapshot has no reattachable run, so its pointer must read null instead of the id
a build-time write would have frozen. It copies the snapshot and its rows rather
than writing through them — the cached objects are shared with every other
in-flight request.

Note the two refusal conventions this leaves in place: sync refuses with 409 and
a thrown body, provision refuses with 200 and `ok: false`. Only sync's needs a
caller-side normalizer, because only a thrown error bypasses the returned-body
branch. Unifying them is a separate change; nothing here adds a second
normalizer.

Sync progress is reported as **indeterminate** — a spinner, the current step
label and elapsed time — and never as a percentage. The step index is a poor
basis for one: the five steps differ in duration by more than an order of
magnitude and shift with network and cache state, so a step-derived bar sits in
one band for most of the run and then jumps, which reads as a stall. The
spinner's `role="progressbar"` carries no `aria-valuenow`, which is the ARIA
form for "in progress, amount unknown".

The whole FRONTEND half of the sync — `npm ci` and `npm build + stage` — is
**skipped on an edition checkout** (`frontend.edition_configured()`). The build
runs under `_build_env()`, whose allowlist drops `KIROCREW_EDITION_DIR` and
`KIROCREW_ALLOW_EDITION`, so on an edition composition root it can only compile
the STOCK SPA; staging that would silently replace the edition dashboard with
upstream's. Skipping is what makes it safe, and it costs an edition nothing —
the only artifact this path could produce for it is a bundle it must never
serve.

**The frontend half is also suppressed on a backend-only sync — one whose
incoming ref changes nothing under `website/`.** Both `npm ci` and `npm build +
stage` are then work with no output: no new lockfile to install, no new source to
build, and the staged bundle is already the current one. The decision is made by
the `Verify dependencies` preflight, the one step that runs after `fetch` has
pinned the incoming ref and before `merge` makes the worktree equal to it — the
only point where "does the incoming ref touch the frontend?" has a correct answer.
It cannot be decided when the step list is assembled, because the per-PID sync ref
is not written until fetch runs; on a long-lived gateway's second sync it would
still point at the prior tip. The preflight signals the verdict by exiting a
reserved code (`EXIT_FRONTEND_SKIP`, 48) that the runner trusts ONLY from the
preflight's own label — a worktree-run step exiting the same code is demoted to a
plain failure, so it cannot forge a "skip the build". The runner then suppresses
the two frontend steps whole, transaction included: a suppressed `npm ci` must not
enter the `node_modules` transaction, whose move-aside-then-drop-backup on a no-op
exit would delete the tree.

The suppression fires only when ALL of these hold together, so the tree that
produced the staged bundle and the tree now on disk are provably identical across
tracked files, untracked files, and installed packages:

1. the incoming ref changes nothing under `website/` (the tracked `git diff` the
   probe skip already computes);
2. the working subtree is clean INCLUDING untracked files (`git status
   --porcelain --untracked-files=normal -- website` empty) — the same check the
   fingerprint is STAMPED behind, re-checked before it is TRUSTED, so an untracked
   `website/` file added between build and skip cannot ride through;
3. `node_modules` is complete against the lockfile (`npm ls --all` exits 0), which
   closes the partial-tree residual a bare "populated" check would leave;
4. a build-source fingerprint — the git tree id of `website/` stamped beside the
   staged bundle on the last successful build, and only when that build's tree was
   clean — equals the incoming ref's `website/` tree.

Any single failure, or any uncertainty (missing or failing `git`/`npm`, a
timeout), returns "run", so the unknown case always rebuilds; the suppression
cannot hold while a rebuild is owed.

**Declared bound: this is a skip optimisation, so it has an inherent
check-then-skip window.** The preflight decides before the merge, the runner
suppresses after it, and the fingerprint is read right after the build; a tree
changed by a concurrent writer in between yields a STALE build, never a wrong or
corrupt one. This is the defining window of every build cache — closing it
completely would need a lock held across the whole build, which destroys the
~28 s the suppression saves. The worst outcome is a stale build on the operator's
OWN checkout, rebuilt by re-running Pull + Build: no data lost, nothing corrupted,
and whoever changed the tree mid-sync is who sees the result. The window is kept
as narrow as it cheaply can be without a lock — the cleanliness check and the
tree-id read run back-to-back under the staging lock, and the preflight runs
immediately before the merge.

The final **npm build + stage** step builds the frontend and copies `website/dist` into
`src/kiro_crew/static/dist` under the Dev Fleet backend's OWN interpreter, with
the target repo passed as an argument. Resolving the helper from the target
instead would make the step's very existence contingent on the pulled revision
already carrying it, so an older target would turn the whole Pull+Build into an
ImportError. It is not cosmetic. On a source install `static/dist` is a *symlink*
to `website/dist` (`ensure_dev_dist_symlink`), and aiohttp resolves a static
route's directory once at registration — so a gateway started in that state is
pinned to the Vite output directory for its whole life, and every `npm build`
rewrites the tree it is serving. Staging leaves a real snapshot there, so from
the gateway's **next start** onward a build cannot touch what it serves. It
publishes the same bundle the build just wrote into `website/dist`, which keeps
the pinned `/assets` route and the staged `index.html` on the same hashed
chunks. The run script stops at the first non-zero step, so a build or staging
failure fails the sync rather than silently leaving the symlink in place.

### Dependency preflight and the `node_modules` transaction

`npm ci` deletes `node_modules` before it installs, so a registry refusal used to
leave the checkout with an emptied tree, a stale bundle against new backend code,
and no way back that did not need the registry that was unavailable. Two
independent triggers recur: a private-registry token that expires on a clock, and
a curated mirror that blocks a version the lockfile pins.

**The symptom is handled as a transaction.** The `npm ci` step carries `stash`
metadata; the generated runner moves the tree aside before the step, restores it
on any non-zero outcome, and drops the backup on success. This lives in the runner
because the runner is fail-fast — anything scheduled *after* a failed step never
runs, which is precisely the case that needs the restore. When a tree **and** a
leftover backup are both present the state is genuinely ambiguous (killed during
the install leaves a partial tree plus the good backup; killed during the success
cleanup leaves the good tree plus a half-deleted one, and nothing on disk tells
them apart), so the runner stops and touches neither, naming both paths.

**The cause is handled by a `Verify dependencies` step between `fetch` and
`merge`.** It runs a real script-free `npm ci` in a scratch directory against the
incoming lockfile, read from the fetched ref rather than the working tree. The
position is the mechanism: the lockfile is knowable as soon as fetch lands, fetch
moves only refs, so refusing there costs nothing and needs no rollback. It is not
an auth check — retrieval is integrity-addressed, so an auth probe fails while the
install it guards would have succeeded.

Fetch, probe and merge consume ONE commit. `<remote>/<base branch>` cannot serve
for that: it is mutable, and the status refresher re-fetches it every
`_NET_REFRESH_S` seconds in the same process, so with a real install between them
the probe could certify a revision the merge does not install. The fetch step also
writes the tip it brought to a per-process ref (`refs/kirocrew/sync-base-<pid>`),
which the refresher never touches; `_prune_dead_sync_base_refs` collects refs left
by gateway processes that are gone, and leaves alone any whose PID is still alive.

The probe executes a **snapshot** of `npm_preflight.py` copied into an unguessable
`mkdtemp`, run with `-I`, never imported from the checkout. The module is
stdlib-only, so the copy needs no package context. Both halves matter: `-I` drops
the cwd from `sys.path`, and the snapshot means an editable install cannot make
the tree being synced supply the code doing the verifying.

**The install rehearsal happens on the CHECKOUT's filesystem, not in `TMPDIR`.**
`tempfile.mkdtemp()` with no `dir` takes `TMPDIR`, which on a default Linux host
is `/tmp` — commonly a memory-backed filesystem whose inode count is capped at
mount time and shared with every process on the box. A `node_modules` tree is tens
of thousands of files, so unrelated litter there can starve Pull + Build while
tens of gigabytes are still free, and the install is charged to RAM. Residency is
a correctness property before it is a capacity one: this step exists to *rehearse*
the real `npm ci`, and a rehearsal held on a filesystem with a different free-room
budget answers a different question — it can pass where the real step fails for
room, or fail where the real step would have succeeded. The scratch is taken from
the repo ROOT rather than `website/`, which is the same filesystem in any ordinary
checkout but keeps the directory outside both the frontend project `npm` resolves
config against and the subtree the backend-only skip decision reads.

**The repo root is used only where git hides the name.** The probe code ships with
the installed gateway while the ignore rule covering its scratch name is a commit
in the checkout's own history, so a fleet checkout parked on an older ref can run
this code with no rule for it — and a probe killed in that window leaves an
untracked directory in the checkout root, which reads as dirty and fail-closes
`Prune merged`. So `_scratch_name_is_ignored` asks `git check-ignore` about a
generated name and the repo hosts the scratch only on a yes. `git check-ignore` is
the oracle rather than a read of `.gitignore`, because ignore resolution spans
several files with precedence and negation. An unanswerable question — missing
git, a timeout, not a repo — is read as NOT ignored: being wrong that way costs a
rehearsal on `TMPDIR`, which is the step's previous behaviour, while the other way
costs a checkout that silently reads dirty.

**Being out of room is the verdict, never a reason to relocate.** A scratch
creation that fails for room says the filesystem the real install targets has
none, which is exactly the answer the step is there to produce; retrying somewhere
roomier would certify a filesystem the install never touches. Only conditions that
make the repo unusable as a host at all — missing, not writable, or not hiding the
name — fall back. "Out of room" covers `ENOSPC` and `EDQUOT` together, because a
per-user quota is how a managed host says the same thing, and the operator-facing
sentence names neither a single filesystem nor a single budget: the install writes
both the scratch and the package cache, which need not share a filesystem, and
each can exhaust bytes or file slots while the other looks healthy.

**Abandoned scratch directories are swept before a new one is created.** `probe`
removes its own in a `finally`, so what survives is a run that never reached it —
SIGKILL, an OOM kill, a reboot. `/tmp` was age-cleaned by the host; the checkout
root is cleaned by nobody and the name is git-ignored, so without a sweeper an
abandoned tree accumulates there invisibly. The sweep removes prefix-matching
directories older than `_SCRATCH_STALE_SECS` (6 h), runs whenever the repo *could*
host a scratch — including when the ignore gate then sends this probe to `TMPDIR`,
since litter from an earlier gateway build is what an un-ignored checkout needs
cleared — and is best-effort, because housekeeping must never be why a
verification does not happen. Ordering it before creation is what makes it a
remedy rather than hygiene: the room the litter holds is charged to the same
budgets the incoming install is measured against. It takes no lock, so the age
window is the concurrency guard, and the guard holds only while the window
exceeds every deadline the module declares. That comparison is not left to prose:
a test reads `probe`'s own default timeout and each fixed helper timeout out of
the source, charges one probe with all of them back to back, and requires
`_SCRATCH_STALE_SECS` to exceed that sum by a wide margin — 21600 s against
1320 s today. Raising a deadline, or adding a helper, without widening the window
fails that test rather than shipping a sweep that deletes a live probe's scratch.
The ownership marker cannot cover this case: a running probe's scratch is
genuinely marked and genuinely prefix-named, so age is the only thing that tells
it from litter.

**The generated runner itself carries `-I` too, and for the same reason.** `python
-c` puts the inherited cwd at `sys.path[0]`, ahead of the standard library, and
the cwd a module-style app backend hands down is the gateway's own source root —
which on the editable install Dev Fleet exists to manage *is* `<checkout>/src`,
the tree being synced. So the runner's own startup imports (`os`, `shutil`,
`subprocess`, `json`) resolve against that directory first, and the runner is the
one process here that is **not** sandbox-wrapped: only the step argvs go through
`sandboxed_spawn_argv`. Without `-I`, a `shutil.py` dropped in that directory runs
arbitrary code outside the per-step sandbox. The shadowing module only has to be
on disk when the runner *starts* — a revision an earlier sync already landed, or
anything an agent wrote in the checkout between syncs, is enough — so this is not
a race with the run's own merge. It is set on the interpreter rather than scrubbed
inside the script so it holds for the process's whole life, including any import
the runner grows later after a step has merged untrusted content. It costs
nothing: the script is stdlib-only by design, and the `-E`/`-s` that `-I` implies
remove env and user-site import sources it never uses. A mask from the sandbox
could not substitute — an app backend's `extra_hidden_dirs` is silently dropped by
`wrap_argv`'s nested passthrough (pinned in `test_sandbox_argv.py`), so it would
read as a control at the call site and enforce nothing.

**The install is skipped when the answer is already on disk.** Most syncs are
backend-only and change nothing under `website/`, so paying a full scratch install
to re-derive "is this lockfile installable" on every Pull + Build is cost without
information. `_install_already_proven` skips it, and only when BOTH hold: `git
diff --name-only <ref> -- website` is empty, meaning the incoming ref changes no
path under the frontend half at all, AND `website/node_modules` is populated (not
merely present — an interrupted `npm ci` leaves an empty directory, which proves
nothing). Without a tree there is no evidence, so a fresh checkout's first sync
still probes. Anything the comparison cannot answer — a failing or missing `git`,
a timeout — probes as well: the unknown case costs an install rather than a
guarantee.

A populated tree is evidence, not a verified install. On its own that would let a
partial tree — a prior frontend sync whose post-merge `npm ci` died partway,
leaving packages missing beside the merged lockfile — pass as "populated". For
the PROBE skip that residual is benign (the skip decides only whether this sync
pays for a rehearsal, so a refusal lands one step later rather than never, and the
transaction keeps the checkout consistent either way). For the frontend-STEP
suppression below it would not be benign, so that path does not rely on the
populated check alone — see the build-currency preconditions there.

The condition is the whole subtree rather than just `package-lock.json` /
`package.json` / `.npmrc`, and the difference is load-bearing. With those three
identical but frontend SOURCE changed, a skipped probe lets the merge land, and a
failing `npm ci` afterwards leaves the checkout with new source and the
previously-built bundle — the stale-bundle half of the very defect this section
exists to prevent. Requiring the entire subtree to be unchanged makes that
unreachable: with no frontend change there is no new bundle owed, so a failed sync
leaves the frontend byte-for-byte as it was.

What makes the skip safe rather than merely cheap is where a failure lands. Under
this condition the transaction above restores the tree on any non-zero step, the
lockfile it matches did not change, and neither did the source the bundle was
built from. A skipped probe can only leave a state a later `npm ci` fixes, never
one no revision produced. A skip is reported on the run's `preflight:` detail line
rather than the generic pass line, so it is visible in the log instead of
inferable from a missing pause.

**Failure causes reach the dashboard as an exit code, not as text.** The probe
exits with a reserved code (41-45) and the runner owns two more (46 ambiguous
tree, 47 restore failed); `npm_preflight.explain_exit` maps each to one
registry-neutral sentence at run completion, surfaced as `cause` on `/run` and
preferred by the UI over the last output line. Two properties keep it honest: a
reserved code arriving from any step OTHER than the probe is demoted to a plain
failure, because every other step runs worktree-controlled code that can exit any
number it likes; and only the sync run kind is stamped at all, since `_start_run`
is shared with `provision`, whose script enforces no such reservation.

**When there is no reserved code, the failure is named from the failing step's
stderr — never from the last output line.** Every step's stdout and stderr land
in ONE pipe (`_start_run` spawns the runner with `stderr=STDOUT`, and steps
inherit it), and a child block-buffers stdout to a pipe while writing stderr
unbuffered — so the stdout buffer flushes at process EXIT, *after* the
diagnostic. The stream order is therefore not evidence of what failed. A refused
`git merge --ff-only` demonstrates it exactly:

```
error: Your local changes to the following files would be overwritten by merge:
        config-baseline.json
Please commit your changes or stash them before you merge.
Aborting
Updating 2f9ed9724..bf09e50e5     <- stdout, flushed last
```

`run_step` therefore gives each step's stderr its own pipe, pumps it through to
stdout line by line (so the log and the live "current activity" line are
unchanged), and remembers its last `_STEPERR_TAIL` non-blank lines. When the step
fails, `run_steps` re-emits those as `::steperr::<idx>::<line>` markers, and the
UI's ladder is `cause` → the `::steperr::` block → the last output line. Both
marker families are filtered out of the log panel: the stderr lines already
appear there in their own order, so the markers would only duplicate the tail.
`pod/provision.py::_run` speaks the same protocol for the provision run (its
markers go to stderr, so a `pod up --json --provision` stdout stays pure JSON),
which is why one frontend helper, `syncFailureTail`, names both failures.

Order WITHIN each stream is preserved; order ACROSS the two is unspecified. The
child writes stdout straight to the inherited descriptor while the pump relays
stderr, so the two interleave by timing rather than by causality. That is the
premise of the change rather than a gap in it — a position in this stream was
never evidence of what failed, which is why the tail is labelled instead of
located.

**The pump reads with a cap, it does not iterate the handle.** A step runs
worktree-controlled code, so it can write a newline-free blob of any length, and
`for line in stream` would allocate the whole blob inside the runner — the
unbounded-read shape `test_jsonl_util.py::TestNoUnboundedHandleIteration`
refuses. `readline(_STEPERR_READ_CAP)` bounds every allocation instead: a longer
run arrives as cap-sized pieces, each forwarded, so splitting is the only effect and
the
blob is merely split across lines. The repo's own `jsonl_util` bounded readers
are unavailable here — this module is stdlib-only and executes from a snapshot by
path — so the bound is spelled with the stdlib.

**That cap is derived from the gateway's byte limit, not chosen.** The two ends
count different units: `readline` caps CHARACTERS because the stream is a text
wrapper, while the gateway reads this pipe with `asyncio.StreamReader.readline()`,
whose 64 KiB limit counts BYTES — and a line past it raises `LimitOverrunError`
there, whose handler reaps the whole process tree. A character encodes to at most
4 UTF-8 bytes and the pump appends one newline, so the cap is
`(_GATEWAY_LINE_BYTES - 1) // 4`. A round-number character cap would satisfy the
byte ceiling only for ASCII, and multibyte stderr — a non-ASCII checkout path, a
localized git message — is ordinary. `test_dev_fleet_sync_runner.py` asserts the
ENCODED length of every forwarded line, since an ASCII fixture cannot see the
gap. A remembered tail line is separately capped at `_STEPERR_LINE_CHARS`, because
the tail is rendered in a one-notice banner rather than in a log.

**The drain after the step exits is bounded too.** `pump.join` waits
`_STEPERR_DRAIN_S` and no longer. EOF on that pipe needs every writer gone, and a
GRANDCHILD inherits the write end — `npm` spawns several — so one survivor keeps
it open and EOF never arrives. An unbounded join would turn that survivor from a
cosmetic leak into a wedged Pull+Build, so late lines are dropped instead. What
that costs is precise: everything the STEP ITSELF wrote is relayed, since its
bytes are in the pipe by the time `wait()` returns; what can be dropped is output
written after the cutoff by something that outlived the step, which is the
survivor this bound exists for.

**This runner is the SOLE writer to its stdout pipe, and that is what makes the
byte bound real.** Capping our own writes bounds nothing while a step also owns
the descriptor: it can emit a newline-free blob that prepends to a terminated
relay line, and the merged inter-newline run the gateway reads then exceeds
`_GATEWAY_LINE_BYTES` however tightly each writer capped itself — which raises in
the reader and reaps the process tree. So `run_step` pipes stdout as well as
stderr, relays each on its own pump, and every write in the module — both pumps
and every `::step::` / `::steperr::` / transaction line — goes through `emit`,
which holds one lock for the whole line. A step that deliberately interleaves
newline-free stdout blobs with terminated stderr lines produces 15 spliced lines
without that, which `test_concurrent_stdout_cannot_splice_a_relayed_line` pins by
mutation.

`run_step`'s docstring states the guarantees exhaustively, as four numbered
lines. Read them there rather than inferring them from prose here — sweeping
wording about the log being complete or in order is what made this paragraph wrong
twice.

`::steperr::` is a **label on a worktree-controlled stream, not a diagnosis.** It
never sets `lastIsCause`, so it renders as the raw tail it is — a step printing a
plausible sentence to stderr gains exactly what it already had, its output shown
verbatim. The one shape that is guarded is an all-blank forged tail, which would
resolve to the empty string and make `ErrorNotice` render nothing: blank marker
texts are dropped, and the last-line fallback skips marker lines too, so a forged
marker can neither hide the notice nor be surfaced raw.

The build and the copy are ONE step because they share ONE holder of the staging
lock (`.dist.staging.lock`, next to `static/dist`). `npm run build` empties
`website/dist` before repopulating it, so a peer flow — another sync, or the
dashboard's own update — that held the lock only for the copy could still read a
partially written tree. Inspecting the copy afterwards cannot substitute: a
bundle's lazy route chunks are referenced from inside the entry chunk, not from
`index.html`, so most of the tree is invisible to any index-based check. `npm ci`
stays a separate step since it does not touch `website/dist`.

Not covered: a gateway process that started while `static/dist` was still a
symlink to `website/dist` — the first staging sync, and equally any process
booted after something re-created the symlink (a `git clean` re-running
`ensure_dev_dist_symlink`). Such a process is pinned to `website/dist`, so its
dashboard still 404s while Vite rewrites it; pairing Pull+Build with Restart
Gateway is what closes it. A process that booted against a staged real
directory is unaffected.

## Make Live

`POST /api/apps/dev-fleet/make-live` — served by the **gateway process**, not the
Dev Fleet backend — repoints the live gateway at a different worktree by writing a
**live-target pointer file** (`live_target.json`). The gateway resolves this
pointer at startup and `execve`s into the named checkout's own `kirocrew` binary
— moving the working directory and `PATH` with it. No service definition is
ever mutated.

The mechanism is the version-selector shape used by `rustup` (reads
`rust-toolchain.toml`), the Go toolchain (`go` execs from the `toolchain` line
in `go.mod`), and `pyenv`/`rbenv` shims.

### Pointer file

Location: `config_dir() / "live_target.json"` (inside the active data home,
typically `~/.kiro/crew/live_target.json`). Contents:

```json
{
  "checkout": "/absolute/path/to/worktree",
  "previous_checkout": "/absolute/path/to/previous-worktree"
}
```

`previous_checkout` is optional and records exactly one completed cutover for the
post-restart **Undo** banner. Both fields validate as executable Kiro Crew
checkouts before they are written. At read time, `checkout` follows the normal
boot validation, while the Undo path validates `previous_checkout`; rebuilding
the checkout already running does not hide an otherwise safe return target.
Ordinary Make Live replaces the history with the checkout currently
running when that checkout validates; otherwise the cutover succeeds without an
Undo destination. Undo consumes the stored history (the rewritten pointer omits `previous_checkout`) rather than turning the inverse into an
implicit redo. Legacy pointers containing only `checkout` remain valid and
simply expose no Undo action.

Written atomically (temp file + `os.replace`) with mode `0o600`. The file is
**keystone-fenced** (in `_CREW_SECRET_LEAVES`) so agent tools can neither read
nor write it, and **bind-masked at the OS level in every sandbox tier**
(`sandbox._CREW_HIDDEN_LEAVES`) — including the Dev Fleet backend's own
namespace. The only writer is the gateway process, on the dashboard owner's own
authenticated request; the gateway's startup reader (`live_target.maybe_reexec`)
opens it directly rather than through the gate.

**Why the backend does not get the file back.** The backend is a sandboxed
spawn, and it spawns `npm ci` / build steps for arbitrary worktrees. A nested
sandbox is denied on both platforms, so `wrap_argv` runs those children *inside
the backend's namespace* with no re-mask: any file the backend could write, a
worktree's lifecycle script could write. A per-backend carve-out (the shape
md-notebook's Notes state uses) therefore hands a routine Pull+Build the power to
choose the gateway's next image. Instead:

- **The cutover runs in the gateway** (`gateway_routes.handle_make_live` →
  `live._make_live`). `_make_live` refuses with `wrong_process` if it is ever
  invoked in a process that has a pointer provider installed (i.e. the backend).
  `_MAKE_LIVE_LOCK` / `_MAKE_LIVE_COMMITTED` live in the gateway with it, so
  `restart-gateway` moved too.
- **The backend reads pointer state through the gateway.** `server.main` installs
  a `GatewayPointerBroker` (`pointer_broker.py`) as `live`'s pointer provider: it
  exchanges the app secret at `POST /api/apps/dev-fleet/token` and reads
  `GET /api/apps/dev-fleet/live-target` (30 s display cache; `fresh=1` for the
  removal guards). The broker aims at `KIROCREW_BOUND_PORT`, which
  `apps/backend.py` hands to this one backend at spawn from the gateway's own
  environment — the port is exported the moment it is reserved
  (`dashboard.server._reserve_dashboard_port`, before any backend spawns;
  `_export_bound_port` republishes it once the site serves), and
  `dashboard.server.start_dashboard` spawns this backend in a second wave
  (`apps.backend.DEV_FLEET_APP_NAME`; the main wave still runs before
  `runner.setup()` so every other app's startup hooks find their backend up). A
  backend spawned before the bind would have no port for its whole lifetime;
  `test_bound_port_backends_start_only_after_the_export_and_the_rest_before_setup`
  pins both orders.
  `_live_worktree_path`, `_staged_target_resolved` and
  `_staged_cancel_available` route through it. A broker outage raises
  `PointerUnavailable` — never `None`: the fleet view degrades (no row is marked
  live or staged, the payload carries `live_state_known: false` and the fleet view renders an
  error notice above the rows — "Live state unavailable" — so "state unknown" is never
  read as "nothing is live", and the backend logs why), while
  worktree removal and the prune override screen **refuse**, because "nothing is
  live" from an outage would let a removal delete a staged cutover target. Toasts
  on those refusals carry the plain sentence; the exception text stays in the log.
- **No inline filesystem reads on the gateway loop.** `_make_live_inner` now runs
  on the loop that serves the whole dashboard, so every probe of the pointer, the
  checkout path or the service drop-in (`snapshot`, `_staged_target`, `exists`,
  `_in_pod`, `_same_path`, the plan's `validate`, `write_target`, `restore`,
  artifact validation), the worktree selector's `resolve()` walk
  (`repository._find_worktree_by_path`) and the live-path resolution
  (`_live_worktree_path`'s pointer/running-checkout comparison and the launchd
  link read) hop to the subprocess executor, and the service backends' own
  filesystem work (the systemd drop-in write, the launchd launcher write, plist
  probes before status and restart, the foreground confinement/marker scan and
  detached spawn) does the same inside `gateway_service.py`; only in-memory state
  and the `_MAKE_LIVE_LOCK` checks stay inline.
- **Removal leases, held in the gateway's memory** (`live.acquire_removal_lease` /
  `renew_removal_lease` / `release_removal_lease` / `removal_in_progress`; routes
  `POST` / `PUT` / `DELETE /api/apps/dev-fleet/live-target/removal-lease`), replace
  the exclusion `_MAKE_LIVE_LOCK` provided when the cutover and a worktree removal
  ran in one process. The backend takes a lease on the worktree it is about to
  remove (`live.removal_lease`, via `GatewayPointerBroker`) and holds it across the
  protection re-check and `git worktree remove`; the gateway refuses a lease while
  `_MAKE_LIVE_LOCK` is held or a cutover has committed, and `_make_live` /
  `_restart_gateway` refuse `busy` while any lease is live — checked again under
  `_MAKE_LIVE_LOCK`, where no new lease can be granted, so the window is closed from
  both sides. A restart tree-kills the backend, which is why it must not land
  mid-removal. Three properties carry the design:
  - **A lease is a capability.** `POST {path}` returns an unguessable token
    (`secrets.token_urlsafe(24)`); `PUT {token}` (heartbeat) and `DELETE {token}`
    require it, and a wrong token is a no-op. The shared Dev Fleet app credential is
    readable by the backend's build children, so a path-only release would let any
    of them cancel a removal's lease; the capability travels only in the `POST`
    reply. A child can still *acquire* leases and so delay a cutover — which it could
    already cause by running `git worktree remove` itself.
  - **A lease is short (30 s) and heartbeated (every 10 s)** by its holder for as
    long as the removal runs, through `_GIT_MUTATION_LOCK` queueing and the mutation
    itself. The gateway holds at most `_REMOVAL_LEASE_MAX_OUTSTANDING` (32) leases,
    live or inside their grace barrier — an order of magnitude above the parallel
    prune width — and refuses acquisition at the cap (a normal `busy` answer): the
    acquiring token is readable by the backend's build children, so an unbounded
    table would be a memory and sweep-cost lever. A *refused* renewal (the gateway restarted and forgot the lease) marks
    the lease lost; the removal checks `live.removal_lease_lost(path)` after taking
    `_GIT_MUTATION_LOCK`, and proves the lease FRESH (`live.confirm_removal_lease`, a
    renewal through the gateway) at each point of no return: immediately before the
    user-approved untracked-file discard, and again — via `_run_cmd`'s `pre_spawn`
    gate — after sandbox preparation and immediately before `git worktree remove`
    is spawned. A refusal before the discard deletes nothing and says so; a refusal
    after it is reported through the discard-aware path ("discarded N untracked
    file(s), but then could not remove the worktree"), never as a bland retry. A
    transient broker error on renewal is retried next tick. A mutation already under
    way is never cancelled — cancelling `git` mid-write is the corruption this
    exclusion exists to prevent. A refused acquisition is reported by cause — the
    gateway declined (a cutover is in progress: wait) versus the gateway could not be
    reached (check it is running) — and every later refusal leads with the
    consequence ("the gateway could not confirm that no cutover overlaps this
    removal") rather than the mechanism.
  - **Simplification of last resort.** If the lease protocol proves flaky in
    practice, the stateless fallback is to refuse removal outright whenever a cutover
    is staged or in flight — coarser, but with no timers to tune. Reach for that
    before adjusting the TTL, heartbeat or grace values.
  - **A lapsed lease keeps blocking for a grace barrier** (90 s past its TTL, i.e.
    longer than the 60 s mutation timeout plus margin) unless explicitly released:
    the holder may be inside the uninterruptible mutation with no way to be told, so
    cutovers and restarts stay excluded until it must have finished. Renewal is
    refused for the whole barrier so a holder that fell behind learns the loss rather
    than resuming on a barrier about to end.
  Deliberately *not* a lock file: a file in the crew data home is replaceable by any
  same-uid process in the backend's namespace, so two sides can end up holding
  different inodes and stop excluding each other. A refused lease — or a broker
  outage at acquisition — makes the removal refuse.
- **Authorization is the owner's request, never a backend credential.** The
  write routes refuse every app principal, including Dev Fleet's own token: that
  token is readable by every build child in the backend's namespace. The read
  route and the removal-lease routes admit ONLY Dev Fleet's own token. The read's
  one consumer is the backend's broker (the dashboard reads the redacted fleet
  payload, never this route) and its answer carries unredacted checkout paths;
  the token buys a build child two paths and no write — the running checkout,
  which `sys.executable` already tells it, and the staged checkout, which it could
  not otherwise learn in-sandbox: a path to a checkout the fleet already lists,
  not a capability to select it. A lease
  latches `busy` on the owner-only cutover and restart, so it is not a read
  either. A dashboard human without an app principal — a Slack-allowlisted
  non-owner holding a plain dashboard token included — is refused on all of them.
- **Every pointer write stages in the masked directory.** `write_target` and
  `restore` publish through `live_target._publish_pointer`: the owner-only temp is
  written into `~/.kiro/crew/live-target-staging/` (the same masked, precreated
  directory the launcher's stub uses, spelled once per module and pinned equal by
  test), renamed onto the pointer, and the published inode is then checked to be a
  regular file with exactly one link — otherwise the pointer is unlinked again and
  the write fails, leaving the safe default (no pointer: the gateway boots its own
  image). A plain `atomic_write` would stage beside the target, in the data-home
  root every sandbox can see and, being same-uid, `link(2)` before the rename.
- **Absent-equivalent document.** `mount(2)` cannot target a path that does not
  exist, so an absent pointer would be an unmasked pointer, and an agent namespace
  spawned while it was absent could *create* one the next boot execs. Before every
  Linux namespace spawn the launcher materialises `live_target.json` as
  `live_target.NO_TARGET_DOCUMENT` (`{"checkout": null}`), owner-only. The temp is
  staged inside `~/.kiro/crew/live-target-staging/` — a directory masked in every
  mode and precreated — never beside the target: the data-home root is visible in
  every sandbox, and a temp there is a name a concurrent namespace could `link(2)`,
  keeping a second writable path to the inode the gateway later reads (a bind mask
  covers a path, not an inode). The materialiser also refuses to launch when the
  pointer — pre-existing or just published — has a link count other than one. This
  stub is a point fix for the one hidden leaf whose absence is a code-execution
  input; the other file leaves in `sandbox._CREW_HIDDEN_LEAVES` share the
  absent-mask gap (secret disclosure, not execution) and are tracked as a separate,
  class-level follow-up rather than closed here.
  `read_target_reason` reads the stub as `(None, None)` exactly like an absent
  file. A present `checkout` of another type, or a missing key, is still reported
  as a defect. **Downgrade note:** the stub is an ordinary file and survives a roll
  back to a build that predates this reader. That older reader logs
  `the live-target pointer has no 'checkout' string` at every boot — behaviour is
  still correct (both readers resolve it to "no live target" and start the
  installed build), only the wording is alarming. Delete
  `~/.kiro/crew/live_target.json` on the downgraded host to silence it.
- **Foreground restart.** `ForegroundBackend` used to refuse from the backend
  (`backend_confined`: a replacement spawned inside the sandbox would inherit its
  confinement). In the gateway there is no confinement, so the last-resort
  foreground restart is now *attempted* where the backend could only advise a
  manual one — the same detached `kirocrew restart` the CLI's own restart uses.

### Live-worktree resolution

`_live_worktree_path()` checks `live_target.read_target()` FIRST (after the
TTL cache), before any launchd/systemd service-definition probe. A cutover
writes the pointer and never touches the service definition, so the unit's
`WorkingDirectory` still names the checkout the gateway was installed from.
Reading the definition first would report that stale checkout as live.

### Request / Response

Request body: `{path, dry_run?, undo?}` — `path` is a worktree path
(validated against the discovered set, never an arbitrary path); `dry_run` (bool,
default false) returns the plan without writing the pointer. `undo` (bool,
default false) requires `path` to equal the pointer's validated
`previous_checkout`; that binding is checked again under the Make Live lock, so
a stale banner cannot reverse a newer cutover. `undo` and `expected_staged` are
mutually exclusive.

- **dry_run success:** `{ok: true, dry_run: true, plan: {mechanism, pointer_path,
  exec, restart, target, [manual_restart]}}`
- **cutover success (automatic restart):** `{ok: true, cutover: true, target,
  plan, start_id}`
- **cutover success (staged only):** `{ok: true, cutover: true, staged_only: true,
  target, plan, manual_restart, notice}` — the pointer is written and correct;
  the operator finishes the cutover by restarting the gateway themselves.
- **Undo success:** the same automatic/staged result shapes; the target becomes
  the prior checkout and `previous_checkout` is consumed before the restart is
  scheduled.
- **refusal:** `{ok: false, code, error}` — `code` is one of the values below.

The handler additionally returns HTTP 400 for a missing/non-string `path` or a
non-boolean `dry_run`.

The `plan` object describes the cutover mechanism:

| Key | Value |
|-----|-------|
| `mechanism` | `"live-target pointer"` |
| `pointer_path` | absolute path to the pointer file |
| `exec` | the target worktree's `kirocrew` binary that the gateway execs into |
| `restart` | `"automatic"` when a drivable service manager is present; `"manual"` otherwise |
| `manual_restart` | (only when `restart` is `"manual"`) the shell command the operator runs |

### Error codes

| Code | Meaning |
|------|---------|
| `unknown_path` | `path` is not a discovered worktree |
| `missing_path` | the worktree path no longer exists on disk |
| `pod` | called from inside a pod — a throwaway test instance must never repoint the live gateway |
| `pod_indeterminate` | pod status could not be resolved (config home unresolvable) — **fail-closed**, never treated as "not a pod" |
| `already_live` | the target is already the live gateway |
| `undo_changed` | the requested Undo path no longer equals the pointer's validated previous checkout (stale banner or newer cutover); refresh before retrying |
| `undo_not_ready` | the requested inverse is still the running checkout, so the original cutover is only staged; use Cancel staged cutover instead |
| `missing_venv` | the worktree has no `.venv/bin/kirocrew` (Provision it first) |
| `venv_not_executable` | the worktree's `.venv/bin/kirocrew` exists but is **not executable** (`chmod +x` it or re-Provision) — a non-executable binary would stop the live gateway but could not start the replacement, leaving no gateway running |
| `missing_dist` | the worktree has no built `src/kiro_crew/static/dist/index.html` (Pull+Build first) — a cutover without a built dist serves a broken dashboard |
| `unsafe_path` | the worktree path cannot be used as a live target (control characters, unresolvable, missing binary, no `src/kiro_crew` dir) |
| `write_failed` | writing the pointer file failed — rolled back to prior state |
| `restart_failed` | the detached restart failed to launch — the pointer is rolled back before returning (response carries `rolled_back`) |
| `busy` | another make-live cutover is already in progress — the mutation sequence is single-flighted, so a concurrent request is refused immediately (no queueing) rather than racing the in-flight pointer write/rollback |
| `restart_pending` | a cutover has already been **successfully scheduled** in this gateway process — the restart is still pending, so a process-local latch refuses every further request (cutover **and** `dry_run`) until the pending restart replaces the process. The fresh gateway starts with the latch clear |

On a `write_failed` / `restart_failed` refusal the response includes
`rolled_back: true|false` — whether the pre-cutover pointer state (prior
content, or absence) was successfully restored on disk.

### Three outcomes: automatic, foreground last resort, staged-only

The cutover writes the pointer on every platform. What differs is whether Dev
Fleet can also bounce the gateway:

- **Automatic restart** (`can_restart = True`): the gateway runs as an active
  systemd `--user` unit or a current macOS LaunchAgent that Dev Fleet can drive.
  After writing the pointer, Dev Fleet asks the manager to restart it (`systemd-run`
  on Linux, bounded graceful `launchctl stop` on macOS), sets the
  `_MAKE_LIVE_COMMITTED` latch, and returns `start_id` for the restart handshake.
  The next gateway process reads the pointer and execs into the target checkout.
- **Foreground last resort**: when the manager probe reports one of
  `no_systemd` / `no_user_unit` / `no_launchd` / `no_agent` — nothing to drive at
  all, e.g. a terminal-launched gateway on a host whose per-user systemd cannot
  be used — `gateway_service.ForegroundBackend` finishes the cutover by
  establishing a **detached `kirocrew restart --port <port>`** (new session, so
  it survives the gateway it kills), reusing the CLI's whole kill-and-respawn
  path instead of reimplementing it. Selection is strictly
  systemd > launchd > foreground, POSIX-only, and requires: an UNCONFINED
  backend (no `KIROCREW_SANDBOX_ACTIVE` marker, not inside
  `kirocrew-agents.slice` — a replacement spawned from inside the sandbox or
  cgroup scope would inherit that confinement for the gateway's whole life);
  exactly ONE run-marker whose recorded pid is alive; and the marker's own
  recorded `kirocrew` launcher (keystone-fenced `run/` dir — there is
  deliberately NO `PATH` fallback, which an agent-planted `~/.local/bin/kirocrew`
  could poison). The mis-set-up manager codes (`user_unit_inactive`,
  `agent_not_indirected`, `agent_restart_contract_outdated`,
  `live_program_missing`) keep their named remedies and are never bounced
  behind the manager's back. On success the response looks exactly like an
  automatic cutover (`start_id` = pre-restart marker pid, latch set). **Fail
  safe:** the backend never signals any process itself — if any requirement
  above fails or the detached spawn cannot be established, nothing has been
  killed and the request degrades to the staged-only outcome below, pointer
  intact.
- **Staged only** (`can_restart = False`, no usable foreground gateway): no
  drivable service manager is available (system unit via `kirocrew service
  install`, macOS without a launchd agent or with a legacy restart contract, or
  another unsupported manager). The pointer is still
  written and the cutover is reported as a success carrying `staged_only: true`,
  plus `manual_restart` (the shell command that finishes it) and a human-readable
  `notice`. The latch is deliberately NOT set — no restart is pending, so a
  subsequent cutover to yet another worktree stays allowed.

### Concurrency

The cutover mutation (prior-state snapshot → atomic pointer write → optional
restart → any rollback) runs under a single module-level `asyncio.Lock`. Two
concurrent cutovers would otherwise race on the shared pointer — one request's
failure rollback could restore or delete the other's successful write. A second
request that arrives while the lock is held is refused immediately with `busy`
(fail-fast, **not** queued). The `dry_run` validation path mutates nothing and
runs outside the lock.

**Committed latch.** The detached restart returns immediately while the restart
is still pending. A process-local `_MAKE_LIVE_COMMITTED` flag is set to `True`
— before returning success, inside the lock — the moment a restart is scheduled.
It is checked both at function entry and again after the lock is acquired
(closing the entry-check-vs-acquire race), so any further request is refused
with `restart_pending`. The latch is never persisted: the fresh gateway starts
clear. Failure paths before successful scheduling never set it, so a rolled-back
cutover leaves the process free to retry. In the `staged_only` path the latch is
never set because there is no pending restart to race against.

### Validation order

Every check runs for `dry_run` too, in this order (first failure wins):

`path` (exists as a known worktree) → **pod guard** (fail-closed on
indeterminate) → `already_live` → `missing_venv` → `venv_not_executable` →
`missing_dist` → `_make_live_plan` (runs `live_target.validate`, catching
`InvalidTarget` as `unsafe_path`).

The pod guard precedes the venv/dist checks so an operator inside a pod gets an
actionable refusal before any per-worktree state matters. The plan step validates
the target path the same way the real write does, so a dry run reports an
unusable worktree instead of promising a cutover that would then be refused.

### Pointer validation (`live_target.validate`)

Rejects with a distinct message for each: empty/blank value; control characters
(ord < 0x20 or 0x7F); unresolvable path; path is not a directory; missing
`target_bin` (`.venv/bin/kirocrew`, or `.venv/Scripts/kirocrew.exe` on Windows);
`target_bin` not executable; no `src/kiro_crew` directory in the checkout.
Returns the resolved checkout path on success.

### Rollback semantics

Before writing the pointer, the prior state is snapshotted via
`live_target.snapshot()` — the raw file content, or `None` when the file is
absent. An UNREADABLE (as opposed to absent) pointer aborts here: `restore(None)`
interprets `None` as "there was nothing" and unpins the target, so continuing
would let a failed restart destroy a live target the code merely could not read.

If the pointer write raises `InvalidTarget` the cutover is refused without
rollback (no state was changed). If it raises `OSError`, or if the detached
restart fails to launch, the pointer is restored to its prior state via
`live_target.restore(prior)` — rewriting the old content, or, when there was
none, publishing the absent-equivalent `NO_TARGET_DOCUMENT` stub rather than
unlinking. The stub reads exactly as absence to every consumer, but it keeps a
maskable regular file under the pointer's name at every instant: the sandbox
mask cannot cover a name that does not exist, so an unlink here would open a
window between the rollback and the next launcher's own materialising stub in
which an agent could create the pointer and select the checkout the gateway
executes next. Both branches go through the same hardened publisher (masked
staging, owner-only mode, single-link check). The refusal response carries
`rolled_back: true|false`.

### Platform scope

Staging (writing the pointer) works on every platform — Linux, macOS, and
Windows. Automatic restart requires a drivable manager: an active systemd
`--user` unit or an active macOS per-user LaunchAgent with the current restart
contract. Without one, the cutover succeeds as `staged_only` and the operator
restarts manually. Cutover from inside a pod is always refused (`pod` /
`pod_indeterminate`).

On macOS, Restart and automatic Make Live submit `launchctl stop <label>`.
Disk and loaded launchd definitions must both report `KeepAlive=true` and
`ExitTimeOut=TOTAL_SHUTDOWN_BUDGET_SECS` (20s). The Gateway's cooperative cap is
`GRACEFUL_SHUTDOWN_SECS` (10s), leaving the remaining budget for cleanup and
exit before launchd escalates to SIGKILL. An agent with a legacy contract falls
back to staged-only Make Live and names `kirocrew service install` as the repair.

## Git environment hardening

Every git invocation from this module — foreground inspection, the unattended
background fetch, rebase, the sync pull, and any git a build step runs — carries
`runtime._GIT_ENV_NEUTRALIZERS`, injected as **environment** rather than as
per-call-site `-c` flags so one chokepoint covers call sites that have not been
written yet. The environment form has the same precedence as `git -c`, so it
overrides every config file, including an agent-writable repo-local one.

Two different jobs live in that one dict, and they are worth keeping apart:

- **Config-driven execution.** `GIT_ALLOW_PROTOCOL` / `GIT_PROTOCOL_FROM_USER`
  make git itself refuse `ext::` and custom remote helpers; the four
  `GIT_CONFIG_KEY_*` / `VALUE_*` pairs disable `core.fsmonitor` and
  `core.hooksPath`, reset `credential.helper` to empty, and pin `core.sshCommand`
  to plain `ssh`. Each of those is a config key a repo can set to name a program
  git will spawn. (The operator's own *global* credential helpers are re-pinned
  after the reset — see `_GIT_TRUSTED_HELPERS` — because a global config is
  operator-owned rather than part of the repo attack surface.)
- **Which object graph git answers from.** `GIT_NO_REPLACE_OBJECTS=1` is not a
  config key and is not about code execution. A `refs/replace/<oid>` ref
  substitutes one object for another in *every* read, so `log`,
  `rev-list --count`, `merge-base` and `merge --ff-only` all answer about the
  substitute graph — a history no checked-out commit names. Every git answer this
  module acts on is a statement about the checkout on disk, so the real graph is
  the only one that answers the question asked, and "behind by N commits" derived
  from a grafted walk is simply wrong. `git replace` is a legitimate local
  operation, so this is a correctness pin first and a tamper pin second. It is
  therefore an env var in its own right and **not** one of the counted config
  pairs: `GIT_CONFIG_COUNT` stays at 4. `platform/update_governance.py` and
  `auto_improvement`'s clone setup pin it for the same reason.

## Output Redaction

All user-visible output passes through `redact_credentials()` and
`redact_exfiltration_urls()` before HTTP response serialization.

## Platform Behavior

The app declares `platform.os: ["macos", "linux", "windows"]` in `app.json`,
because that is where it genuinely runs: the fleet view, PR status, commit and
disk figures, Provision, Sync, Rebase and Prune are git and filesystem work with
no service-manager dependency in them. The pod plane needs systemd `--user` on
Linux or launchd on macOS; Windows has no supported pod backend. Make Live stages
its pointer on every platform (only the automatic restart needs a drivable service manager).
The app says so in the UI rather than in the manifest — a `highlights` line
states the pod requirement, and `GET /api/fleet` carries the reason that renders
as a banner.

Declaring one platform per capability is not expressible here: `os` is a single
list describing the whole app, so any value is a summary. `["linux"]` was the
wrong summary — it read as "does not run on macOS" for an app whose non-pod half
runs there fine, which is the same misinformation in the opposite direction from
the pre-#1254 silence (an absent `platform` block defaults to
`["macos", "linux"]`, quietly advertising macOS parity).

The declaration is **not** an install gate for this app: `installMode` is the
default `"server"` and the App Store's platform check at `registry.py` only
refuses `installMode: "client"` apps, so dev-fleet installs and enables
everywhere regardless. What the list drives is the App Store detail page, which
renders it verbatim (`AppDetailPage.tsx` → "Platform: macos, linux, windows").

Two separate capability flags drive the degradation, because they gate different
things:

| Flag | Meaning | True when |
|---|---|---|
| `_POD_IMPORTED` | the `kiro_crew.pod` modules imported, so its platform-neutral helpers are callable | the import succeeded (any platform) |
| `_POD_AVAILABLE` | pods can actually **run** here | Linux with `systemctl` on PATH, or macOS with `launchctl` on PATH |

Conflating the two used to report every worktree as "not built" on hosts without a runnable pod backend, since
the `prov.has_venv` / `prov.has_dist` calls — plain filesystem checks — sat
behind the pod-runnable gate. Build state is now computed on every platform.

`GET /api/fleet` reports host support so the UI can explain itself rather than
offering controls that fail:

| Field | Meaning |
|---|---|
| `pods_available` | `_POD_AVAILABLE` — whether pods can run on this host |
| `pods_unavailable_reason` | the human-readable reason, or `null` when pods are available |

Before this existed, the reason string was computed into `_POD_ERROR` and then
**never read by anything** — a user on a host without a runnable pod backend saw
pod controls that silently failed with no explanation.

Per-platform behavior:

- **Linux + systemd `--user`** — everything works.
- **macOS + launchd** — pod lifecycle and the non-pod fleet actions work. macOS
  pods have no enforced memory/CPU ceiling. Automatic Make Live additionally
  requires the current LaunchAgent restart contract; otherwise it stages the
  pointer and asks the operator to restart manually.
- **Windows / macOS without `launchctl` / Linux without `systemctl`** — the Fleet
  view, per-branch PR status, commit counts, disk usage, Provision, Sync (pull
  main + rebuild), Rebase and Prune all work. The UI shows a notice carrying
  `pods_unavailable_reason` and hides the actions that cannot work: Spin up /
  Restart / Stop pod, Open, QA + video. Make Live and Provision are **not**
  hidden — `kirocrew pod provision` does not touch a service manager, so building
  a worktree's venv + dist works anywhere; Make Live stages the pointer on any
  platform and reports `staged_only` when it cannot bounce the gateway itself.
- **Make Live** — staging (pointer write) works on every platform. Automatic
  restart requires an active systemd `--user` unit or a current macOS
  LaunchAgent; without one the cutover succeeds with `staged_only: true` and
  the operator restarts manually.
- **git** and **gh** CLI required for full functionality; missing binaries produce
  graceful degradation via OSError catch in `_run_cmd`.

## Bundled Skills

The app bundles two skills declared in `app.json`:

- `skills/pod-e2e` — end-to-end test harness for isolated pod instances.
  Every phase is time-bounded: the Playwright phase runs under `timeout`
  (`POD_E2E_PW_TIMEOUT`, default 600s) and each browser-teardown step under
  `POD_E2E_TEARDOWN_TIMEOUT` (default 30s), because video finalization
  (`context.close()`) can block indefinitely. On expiry the runner keeps the
  artifacts, kills the browser descendants, and reports a timeout as a distinct
  outcome. Per-phase results are appended to `verdict.jsonl` as they are decided
  so a killed run still yields a verdict.
- `skills/feature-demo-recording` — records a demo of a web feature, in one of
  two modes (see below).

### Using `feature-demo-recording`

Two modes, and picking the wrong one wastes a recording:

- **Narrated film** — someone sits and watches it (a launch clip, a feature
  intro). The VOICEOVER drives the timeline: narration is synthesised and
  measured FIRST, and the recorder then paces the browser to those measured
  times. This is the order that keeps sound and picture together; pacing the
  recording first and fitting audio afterwards is what accumulates drift.
- **Silent evidence clip** — proof that a feature works, for a PR or a review.
  No narration, no measuring; `narrate.py --silent` writes a timeline from
  durations you state. The QA + Video row action uses this mode.

The five steps, each a script under the skill's `references/`:

| Step | Script | Produces |
| --- | --- | --- |
| 0 | `deps.py` | a report of what is missing, and installs what it may |
| 1 | `narrate.py script.json` | narration audio + `narr.json` (measured timeline) |
| 2 | `record_template.py` (copy and adapt) | the screen capture + `events.json` |
| 3 | `compose.py` | `index.html` — the composition, as a real web page |
| 4 | `verify_align.py` | pass/fail on drift, audio, picture and streams |

Two things about the shape of this that are easy to get wrong:

- **The composition is generated.** Slides, subtitles and camera moves live in
  `index.html`, which `compose.py` writes. Changing a word means re-composing,
  not re-recording — but it also means a palette or layout fix belongs in the
  generator. Editing the generated file alone gets silently overwritten by the
  next compose.
- **Delivery is decided by `verify_align.py`, not by eye.** It exits non-zero on
  drift beyond budget, on a silent audio track, on a picture that is black or
  blown out, on a render whose dimensions do not match the capture, and on
  missing streams. A film that has not passed it is not finished.

Speech providers are `piper` (local, nothing leaves the machine) and `polly`
(the operator's own AWS account, so it costs them money); `--provider auto`
prefers local and refuses rather than reaching for a third-party endpoint. Text
sent to the cloud provider is scrubbed of credential-shaped content first.

Rendering needs Node and pulls `hyperframes` plus GSAP from public registries,
so the render step is not offline. `deps.py` reports every one of these and says
which it can install without root.

Prefer `browser-recording` instead when a short silent clip of a UI interaction
is all that is wanted: it is a smaller tool and needs no narration script.

`kirocrew-worktree-dev` carries no app-bridged copy: the canonical copy is
owned by the `kirocrew-dev` development-skills suite under
`src/kiro_crew/builtin_skills/`, and the app-bridged duplicate was removed
because two copies of the same skill drift and get loaded nondeterministically
against each other (PR #353 arbiter finding). That single-copy rule is what
matters here; where the one copy lives is a packaging question, and it lives in
the packaged tree so `_ensure_builtin_skills` reaches every distribution. The
project-dir mechanism reaches only some: `_project_skills_dir()` reads
`KIROCREW_PROJECT_DIR`, which a repo checkout provides, but a `pip install` from
the wheel or sdist does not — and neither does the desktop bundle, whose builder
stages no top-level `skills/` tree.

Skills are registered as symlinks into `~/.kiro/crew/skills/` via the app bridge at
two lifecycle points:

1. **On enable** — `register_app()` in `bridges.py` creates namespaced + flat symlinks
2. **On gateway startup** — `reconcile_app_skills()` in `bridges.py` (called from
   `start_enabled_app_backends()`) ensures manifest-declared skills are linked for
   already-enabled apps, creating missing symlinks and removing stale ones for skills
   dropped from the manifest since the last registration

This reconcile step addresses the upgrade gap: an in-place version upgrade that adds
new skills would otherwise never get symlinks without a disable/enable cycle.

## QA + Video Row Action

Each worktree row in the frontend exposes a "QA + video" action (Video icon) that:

1. Composes a seeded prompt (pod-e2e suite + feature-demo-recording)
2. Dispatches `setPendingInput(prompt)` to the chat store
3. Navigates to `/chat?autoSend=1&newSession=1`

This launches an agent session that runs the full QA cycle (pod up, API + Playwright
tests, demo video recording, summary) without any backend route — it is entirely a
frontend-only seeded session pattern.

## Live Worktree Removal Guard

The `POST /apps/dev-fleet/api/worktree/remove` endpoint (and its `force` variant)
performs a fresh uncached resolution of the live gateway's worktree path before any
removal. If the target worktree is the one currently running the live gateway process,
the request is refused with a descriptive error — regardless of the `force` flag.

The check uses `_live_worktree_path()` which performs a fresh filesystem resolution
(no caching) to avoid TOCTOU issues where a previously-cached path is stale.

## Forced Removal Refusal Matrix

The `_worktree_remove` decision surface evaluates `force × PR-merged × dirty`
and emits one audit action per outcome. `--force` is NEVER passed to
`git worktree remove`; every path either refuses or removes without `--force`
so that git's own dirty check is the atomic last line of defence.

| force | PR merged | dirty | Outcome | Audit action |
|-------|-----------|-------|---------|--------------|
| True | No | True | **Refuse** — uncommitted changes on unmerged branch | `refused_dirty_unmerged` |
| True | No | None | **Refuse** — cannot verify cleanliness | `refused_unverifiable` |
| True | No | False | **Remove** (no `--force`); git's own check guards the TOCTOU window | `unmerged_clean_no_git_force` |
| True | Yes | True | **Refuse** — fresh-MERGED confirms merge but tree is dirty | `refused_dirty_merged` |
| True | Yes | None | **Refuse** — fresh-MERGED confirms merge but tree is unverifiable | `refused_unverifiable_merged` |
| True | Yes | False | **Remove** (no `--force`); mirrors unmerged-clean TOCTOU pattern | `merged_clean_no_git_force` |
| False | Yes | * | Non-forced path (squash-safe OID race guard) | n/a (no force audit) |
| False | No | * | Non-forced path | n/a (no force audit) |

Additional pre-gates (evaluated before the matrix above):

| Condition | Outcome | Audit action |
|-----------|---------|--------------|
| Branch OID unpinnable | **Refuse** | `refused_unpinnable` |
| Cached MERGED but fresh verification fails | **Refuse** | `refused_stale_merged` |
| Fresh-MERGED but branch OID not contained in PR head | **Refuse** | `refused_uncontained_fresh_head` |
| Target is the live gateway worktree | **Refuse** | (live-worktree guard) |
| Worktree inside another worktree (containment) | **Refuse** | `refused_containment` |

**Invariant:** `git worktree remove --force` is unreachable from any code path.
Both clean-removal branches (unmerged and merged) set `force_use_git_force = False`
explicitly, and every non-clean state is a hard refusal. A late dirty edit in the
check-to-removal window is caught by git's own atomic dirty check (exit code != 0),
surfaced as `refused_dirty_at_removal`.
