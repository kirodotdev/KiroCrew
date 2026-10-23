---
title: Quarantine keeps only unsaved work, not the whole clone
status: draft
kind: change
author: iamwhatever
created: 2026-10-20
last-audited: 2026-10-20
audited-at: 6432cad9e
doc-pr: 22428
implementation-prs: []
tracking-issues: [22423]
supersedes: []
superseded-by: []
---

# RFC: Quarantine keeps only unsaved work, not the whole clone

When the hourly scratch sweep finds a dead, idle scratch tree that holds a git
repository, it moves the whole tree into `.quarantine` and keeps it 14 days.
That keeps the user's work safe, but it keeps a few gigabytes to save a few
kilobytes. This RFC proposes keeping a small **rescue** instead: the commits a
trusted source does not have, plus the changed and untracked files. The sweep
still moves the tree whole, as today; a new pass over `.quarantine` then
replaces it with its rescue. Whenever that pass cannot be sure, it keeps the
whole tree, as today. It never falls back to deleting.

Measured against main at `6432cad9e`. Nothing here is built.

## Summary

| | Today | Proposed |
|---|---|---|
| What `.quarantine` keeps for a dead clone | the whole tree | a rescue: bundle + changed files |
| Who decides a commit is saved | nobody (all of it is kept) | a trusted object store, never the clone |
| Where git runs | nowhere (no git runs) | inside the agent sandbox, no network |
| Any failure or doubt | n/a | today's whole-tree quarantine |
| Expiry | 14 days from the move | 14 days from the move (unchanged) |

## Motivation

### Current state

`sweep_dead_scratch` in `src/kiro_crew/agent_scratch.py` runs hourly from
the gateway (called from `src/kiro_crew/slack/gateway_runtime/process_entry.py`).
A tree whose owners are all dead and which has been idle for
`_UNOWNED_GRACE_SECONDS` (one hour) is deleted, unless `holds_git_repository`
says it holds a repository. Then `_quarantine` renames the whole tree into
`<scratch root>/.quarantine` as `<UTC move time>-<name>`, and
`_expire_quarantine` deletes it once `QUARANTINE_TTL_SECONDS` (14 days) have
passed. All six names are in `agent_scratch.py`.

This came from #19897. Before it, the sweep deleted clones that still held
uncommitted edits and local commits.

### The problem

The quarantine keeps every byte of every dead clone for 14 days.

- On one host, `.quarantine` reached **28 GB over 24 trees in about one day**,
  and the 80 GB disk reached **99%**.
- One Kiro Crew clone is about **4.2 GB**: `.git` 1.8 GB, `node_modules`
  0.95 GB, `.venv` 0.63 GB, source 0.8 GB.
- The work that is actually unsaved in such a clone is usually a few local
  commits and a few edited files: kilobytes to a few megabytes.

At that rate, 14 days of quarantine is hundreds of gigabytes. The disk fills
long before anything expires.

A sibling PR (branch `fix/scratch-delete-clean-trees`) removes the regenerable
directories (`node_modules`, `.venv` and similar) before a tree is quarantined.
That takes a Kiro Crew clone from about 4.2 GB to about 2.6 GB. The rest is
`.git` plus the checked-out source, and that is what this RFC covers.

### Why #19897 did not just ask git

#19897 deliberately runs no git and opens no file. Two reasons, both still true:

1. **The clone's answer cannot be trusted.** Everything in the clone was
   written by the agent. Its refs can be made to say "this is pushed" when it
   is not:
   - a fetch from the clone itself (or any local path) into
     `refs/remotes/origin/*` makes local commits look like remote ones;
   - `git tag -f` or `update-ref` moves any ref anywhere, and ref logs can be
     turned off so no trace of the old value is left;
   - the remote URL can point at a local directory or a throwaway path;
   - `git replace` objects and `info/grafts` rewrite history, so an unsaved
     commit can look like an ancestor of a pushed one.
2. **Running git there is running the agent's programs.** The gateway is not
   sandboxed. Repository config can name programs that git runs: hooks
   (`core.hooksPath`), `core.fsmonitor`, clean/smudge/process filters,
   `core.sshCommand`, `core.pager`, `diff.external`, textconv, credential
   helpers, `include.path`. Running git in the gateway on an agent-written
   repository hands those programs the gateway's privileges.

#19897's review found these in four rounds, and each one deleted the only copy
of the work. Its conclusion was that a check of agent-written state cannot be
made complete, so it must not be what deletes.

This RFC keeps both rules. It changes **who** answers "is this saved?" and
**where** git runs.

## Goals

- A dead clone in `.quarantine` costs about the size of its unsaved work.
- No commit is judged saved on evidence the agent wrote.
- No program named by the clone runs outside the agent sandbox.
- Every failure or doubt keeps today's behaviour: the whole tree, 14 days.
- A user can recover the work from the rescue with ordinary git commands.

## Non-goals

- Changing the 14-day expiry, the naming, or what counts as a repository.
- Removing regenerable directories. The sibling PR does that.
- Saving work the agent itself deleted while it was alive (dangling objects,
  dropped stashes). The agent can always destroy its own work; this RFC only
  stops the **sweep** from doing it.
- Pushing anything anywhere on the user's behalf.

## Design

### The rule

A commit is **saved** only if it exists in a **trusted object store**. Anything
the trusted store does not have goes into the rescue. The clone is used only to
list candidates. It is never asked whether something is saved.

Listing candidates from an agent-written clone is still safe. A lie in the list
can only *leave out* a commit the agent hid from its own refs. That is the
same as the agent deleting its own work, which it can already do. A lie can
never make an unsaved commit count as saved, because that answer comes from
the trusted store.

### What "trusted" means

A trusted object store is one **no sandboxed agent process can write**, filled
only by fetches the **gateway** runs, from URLs in the **gateway's own config**.
Two kinds are proposed. For both, the rule is the same: a commit is saved only
if it is **reachable from a branch or tag** of the store. An id the store does
not have, or has only as a loose object, is not saved.

| Kind | Good | Fails how |
|---|---|---|
| Gateway-owned mirror | local, fast, no credential | stale mirror says "not saved" (safe: bigger rescue) |
| Hosting provider by SHA | always current, no extra disk | needs network and a credential; rate limits; see below |

This section and the ones below state **requirements and invariants**. The
exact git commands are Phase 1b's to choose and its reviewers' to check.

**Gateway-owned mirror.** A bare, full (not shallow, not partial) clone under
the data home, for example `<data home>/git-mirrors/<host>/<owner>/<repo>.git`.
Being outside the scratch root is not enough: the sandbox hides a fixed list
of paths, not everything else. So `git-mirrors` is a new top-level leaf added
to `_CREW_HIDDEN_LEAVES` in `src/kiro_crew/sandbox_leaves.py`, which every
sandbox mode masks. The one exception is the verify helper below: its launch
names that one mirror in `extra_visible_dirs`, which lifts the mask for that
one fixed-argv child only, the way the `aws-control-staging` leaf is already
lifted for its CLI child. For this child the mirror is mounted read-only (the
Phase 1a mount), so it can read objects but never write the store it is
checking against. Agents and every other helper still see nothing there. The
gateway runs `git fetch` there itself, on its own
schedule, from a URL the user configured. The gateway owns its config, so
running git there is not running agent programs.

Its branches and tags track upstream, pruned, and it never holds
`refs/pull/*`. Having the object is not enough: a commit force-pushed off its
branch can stay in the mirror's object store while upstream is free to garbage
collect it, which is why the rule above is reachability.

**Hosting provider by SHA.** Useful when no mirror is configured, or the
mirror is stale. GitHub can answer "yes" for a commit that is only reachable
from a fork, a pull-request ref, or nothing at all, and it may later garbage
collect such a commit. So a "yes" counts only when the commit is reachable
from a branch or tag of an allowlisted repository. Anything else is "not
saved".

**Which store to ask.** The clone's remote URL is agent text. It is used only
to *pick* among stores the user already configured, by exact match after
normalising the URL. It is never fetched, and an unknown URL means "no trusted
store", which means the whole-tree fallback.

**With nothing configured.** A host with no mirror and no provider credential
has no trusted store, so every clone takes the whole-tree fallback and the
disk keeps filling as it does today. This RFC alone does not shrink that host's
quarantine; only the sibling PR's regenerable-directory removal does. Turning
the saving on by default therefore needs a trusted store that exists without
setup, which is open question 9 and an entry condition of Phase 3.

**For default hosts, the fix is the sibling PR**, which drops the regenerable
directories with no setup and takes a Kiro Crew clone from ~4.2 GB to ~2.6 GB.
This RFC accepts that trade. Phases 1a, 1b and 2 ship **only for operators
who opt in** by configuring a mirror (or, after Phase 2, a provider
credential). A default host gains nothing from them until open question 9 is
answered. They are still worth shipping first, because the hosts that fill
their disk this way are the ones that develop Kiro Crew itself, where a
mirror of one repository is a one-line setup.

**Hash strength.** Object ids are compared as full SHAs: 40 hex digits for
SHA-1, 64 for SHA-256, validated before use. Git's SHA-1 uses collision
detection; crafting a colliding commit to fool this check is outside the threat
model, and is listed as an open question for SHA-256 repositories.

### What goes in a rescue

For each repository in the tree, under `repos/<path of the repository in the
tree>/`:

| Part | Holds |
|---|---|
| `commits.bundle` | every unsaved commit, with the saved commits it builds on as prerequisites |
| `refs.txt` | branch names, `HEAD` and upstream names, each with its SHA |
| `changed/` | tracked files whose raw bytes differ from the index **or** from `HEAD`, copied verbatim |
| `staged/` | index entries that differ from both `HEAD` and the worktree (staged, then edited again), as raw bytes |
| `deleted.txt` | tracked files in `HEAD` that are gone from the worktree |
| `untracked/` | untracked and ignored files, verbatim, minus the regenerable set |

Everything in the tree that is **outside** any repository is copied verbatim
into the rescue too. A `RESCUE.md` at the top lists the parts.

Requirements on what goes in:

- **Unsaved commits.** Candidates come from every ref, reflog entry, `HEAD`
  and the stash. Each walk back stops at the first commit the trusted store
  says is saved. Every unsaved tip is named in the bundle (a tip with no
  branch gets a generated name), and never in the clone, which is read-only.
- **Changed files without filters.** "Changed" means the raw bytes differ
  from the index or from `HEAD`, judged with no repository-configured filter
  involved. A clean filter is repository config, so an agent could make a
  changed file look unchanged to `git diff`; raw bytes cannot be faked that
  way. A repository with end-of-line conversion will show many changes; the
  size check below then sends it to the whole-tree fallback, which is safe.

### Where git runs

Git runs in a helper process the gateway starts through the same sandbox
wrapper as agent spawns (`wrap_argv` in `src/kiro_crew/sandbox_wrap.py`, re-exported as
`kiro_crew.sandbox.wrap_argv`). The sandbox is the safety.

**Invariant: the helper runs only inside an enforcing sandbox.** The wrapper
must confirm, for this launch, that sandboxing is on (sandbox mode is not
`off`, and a backend was found), that the network is denied, and that the tree
is read-only. If it cannot confirm all three, the helper never starts and the
tree takes the whole-tree fallback. Running the helper unsandboxed would run
the clone's hooks and filters with the gateway's rights, which is the exact
risk #19897 avoided.

Two of the properties below **do not exist in the wrapper today**, and Phase 1a
has to add them. `wrap_argv` never enters a network namespace (the network
egress note in `src/kiro_crew/security_posture_sinks_gateway.py` says so: agent commands need
the network), and it has no parameter to mount a directory read-only. The
Linux launcher already has a read-only bind (`seal_readonly` in
`src/kiro_crew/sandbox_launcher_parts/mask_stages.py`), which is the likely base. Until
both exist on a host, that host always takes the whole-tree fallback.

- The tree is mounted **read-only**. One fresh output directory is writable.
  (New in the wrapper.)
- **No network.** (New in the wrapper.)
- No gateway secrets: an empty `HOME`, a scrubbed environment, no credential
  helper, no SSH agent socket.
- A wall-clock timeout, a CPU limit and an output size limit.

As defence in depth, git also runs with system and global config off, replace
objects and grafts off, and every setting that names a program (hooks,
fsmonitor, pager, SSH command, credential helper, external diff, textconv)
forced off. That list is not what makes this safe, and it cannot be complete:
repository config can still name a filter by an arbitrary name, or pull in
more config with `include.path`. Those programs run **inside the sandbox**,
with no network, a read-only tree and no secrets. That is less than the agent
could already do while it was alive.

The helper talks to the gateway in two steps over files in its output
directory:

1. **List.** The helper writes `candidates.json`: tips and their ancestry, as
   SHAs. The gateway reads it with a size cap, accepts only valid hex ids,
   asks the trusted store, and writes back `saved.json`.
2. **Pack.** The helper builds the rescue from `saved.json`.

The gateway never parses anything else the helper wrote. It renames the output
directory, and it sizes it with the same `lstat` walk the sweep already uses.
The counts in the log line come from `saved.json`, which the gateway wrote,
and from that walk.

### Order of steps, and the fallback

```
sweep (unchanged): dead, idle tree with a repo -> rename whole into .quarantine

shrink pass (new), for each whole tree in .quarantine, idle for an hour:
  -> trusted store for every repo?        no -> keep the whole tree (today)
  -> enforcing sandbox confirmed (on, no network, read-only tree)?
                                          no -> keep the whole tree
  -> helper: list -> gateway: ask store -> helper: pack
  -> any error, timeout, doubt?           yes -> keep the whole tree
  -> verify the rescue                    fail -> keep the whole tree
  -> rescue smaller than the tree?        no -> keep the whole tree
  -> no write in the tree since the helper's snapshot?  no -> discard rescue
  -> swap: rescue takes the tree's name, then the tree is deleted
```

**Shrink after the move, not before.** The sweep's own step does not change:
under `_SWEEP_LOCK` it takes today's cheap look and renames the tree, whole,
into `.quarantine`. That rename is what takes the tree away from spawns. A
tree in `.quarantine` is never handed to a spawn, so nothing can adopt it
while its rescue is built, and the shrink pass needs no lock at all. It never
holds `_SWEEP_LOCK`, and the lock's contract ("only across lstat/utime and the
rmtree; never across marker I/O") stays as it is.

Only a person can still write into a quarantined tree, for example while
recovering work from it by hand. So the pass skips a tree written in the last
hour, and right before the swap it walks the tree again, without a lock, and
discards the rescue if anything is newer than the helper's snapshot. A write
in the seconds between that walk and the swap is the one window left; it is
listed in the threat table.

**Doubts** that send a repository to the fallback: a `.git` file or link
(linked worktree, separate git dir, submodule), a bare repository, a `shallow`
file, `objects/info/alternates`, a partial clone (promisor remote), Git LFS
pointers, a submodule with unsaved commits, an in-progress merge, rebase or
cherry-pick, and any repository whose remote matches no trusted store.

**Verify.** Before deleting anything, there must be evidence that the rescue
reproduces every unsaved tip and every changed, deleted and untracked file
byte for byte. Any gap means the fallback. Requirements:

- **The verdict is the gateway's.** Nothing the pack helper reports counts,
  because repository-named filters run there. Any git step verify needs runs
  in a second sandboxed helper whose repository has the gateway's config only,
  and the final comparison of bytes, modes and names is done by the gateway
  itself, with no git.
- **No working tree is checked out.** Verify costs about the rescue's own
  size, and the pass handles one tree at a time, so it still makes progress on
  a nearly full disk.
- `staged/` (index state) has no trusted copy to compare against, so it is
  kept unverified and `RESCUE.md` says so. Every worktree file is proven.

How the mirror kind meets these is Phase 1b's to choose; one way is a bare
repository that borrows the mirror's objects and lists each tip's tree for the
gateway to hash against. How the provider kind verifies is open question 3;
until it is answered, the provider kind always falls back.

**Crash safety.** A crash at any point leaves the whole tree, the rescue, or
both, never neither, and never restarts the 14-day clock. The rescue is built
under a name the expiry ignores, and the tree is deleted only after the rescue
holds its name.

**Saved commits stay reachable.** Recovery needs the saved commits a bundle
builds on, for the whole 14 days, even if upstream force-pushes them away. How
the mirror keeps them is open question 10.

### Expiry

A rescue is a plain directory named `<UTC move time>-<name>`, like a
quarantined tree today. `_expire_quarantine` handles both without change: 14
days from the move, and not while something wrote into it in the last hour.

### Recovery

The INFO line changes to say what was kept:

```
agent-scratch: kept unsaved work of <name> (3 commits, 2 changed files, 41 KB)
in <path in .quarantine> and deleted the rest; it is deleted after <date>
```

Recovery uses ordinary git. The user clones the upstream, fetches from
`commits.bundle`, checks out each tip by the full SHA that `RESCUE.md` lists,
and copies `changed/` and `untracked/` over the checkout. `RESCUE.md` is
written by the gateway, from its own template, and says this.

**Invariant: rescue contents are data, never instructions.** Every file the
helper wrote was shaped by the agent's repository. So `RESCUE.md` never puts a
helper-written name or path into a command. The only rescue file it hands to
git is `commits.bundle`, which verify has proven, and tips are given as SHAs
the gateway validated. `refs.txt` and `deleted.txt` are lists for the user to
read and act on by hand, and the same goes for names inside `changed/` and
`untracked/`.

A whole-tree fallback keeps today's log line and today's recovery steps in
`src/kiro_crew/docs/troubleshooting.md`. A `kirocrew scratch restore` command
is left as an open question.

### Worked sizes

One Kiro Crew clone with 3 local commits and 2 edited files:

| Stage | Size |
|---|---|
| Today, whole tree | ~4.2 GB |
| After the sibling PR drops regenerable dirs | ~2.6 GB |
| After this RFC (bundle + changed files + `RESCUE.md`) | ~50 KB to a few MB |

The host from the problem section:

| | Today | Sibling PR only | Sibling PR + this RFC |
|---|---|---|---|
| 24 trees, one day | 28 GB | ~17 GB | well under 1 GB |
| 14 days at that rate | ~390 GB | ~240 GB | a few GB |

The estimates scale today's 28 GB by 2.6/4.2 for the sibling PR, and assume
each rescue is at most a few MB. Trees that fall back stay at their sibling-PR
size.

## Security considerations

### Threat model

| Actor | Trusted? | Controls |
|---|---|---|
| Agent process | no | every byte of its scratch tree, including `.git` |
| Gateway | yes | the sweep, the trusted stores, the sandbox wrapper |
| Trusted store | yes, for "is this commit on a branch or tag" | nothing in the tree |

The agent may be confused or prompt-injected. Two outcomes must not happen:

1. The sweep deletes work that is not saved somewhere trusted.
2. Bytes in the tree make the gateway run a program or read a secret.

| Attack | Why it fails |
|---|---|
| Fake `refs/remotes/*`, `tag -f`, `update-ref` | refs only list candidates; savedness comes from the trusted store |
| Remote URL to a local path | URL only picks a configured store; no match means fallback |
| `git replace`, `info/grafts` | replace objects and grafts off; savedness is the store's, not the clone's history |
| `shallow`, alternates, promisor | listed doubts: fallback |
| Hooks, fsmonitor, pager, sshCommand, credential helper | forced off; and they run only inside the sandbox |
| Clean/smudge/process filter, textconv | changed files found by raw-byte hash; filters run only inside the sandbox |
| Huge or endless output | timeout, CPU limit, size cap: fallback |
| Hostile `candidates.json` | size cap, hex-only ids; nothing else is parsed |
| Sandbox mode `off`, or a backend that cannot deny network or mount read-only | the helper never starts: whole-tree fallback |
| Crafted names in `deleted.txt`, `changed/`, `untracked/` | rescue contents are data; `RESCUE.md` never feeds them to a command |
| Building dir named to collide | names are composed by the gateway from the stamp |
| A spawn adopts the tree while its rescue is built | cannot happen: the shrink runs only on trees already moved into `.quarantine`, which no spawn is handed |
| A person writes into a quarantined tree while it shrinks | idle hour plus a lock-free re-walk before the swap; a write in the seconds after that walk is the residual risk |
| Upstream drops a saved boundary commit within 14 days | requirement: the mirror keeps it until the rescue expires (open question 10) |

### What the gateway gains

The gateway gains a sandboxed helper and, for the provider kind, an outbound
API call with a credential it already holds. The helper never sees that
credential. The mirror kind adds a gateway-run `git fetch` from a configured
URL, the same thing a user's own clone does.

## Migration plan

Each phase is one PR and can stop without the next.

**Phase 1a: sandbox modes.** Add a no-network mode and a read-only directory
mount to `wrap_argv`, used only by the helper. Agent spawns keep the network.
Exit criteria: a helper started in that mode cannot open a socket and cannot
write into the read-only directory, on Linux and macOS; a backend that cannot
provide either reports so, and the caller can tell; with sandbox mode `off`
the wrapper reports "not enforcing" and the helper is not started; a child
launched with one hidden leaf lifted read-only can read it and cannot write
it, and a sibling launch without the lift still sees it masked.

**Phase 1b: rescue behind an opt-in.** Add the helper, the mirror kind of
trusted store, the shrink pass over `.quarantine` with its verify step and
swap, and the fallback, behind a
config flag that is off by default. Exit criteria:
- with the flag off, every test in `test/test_agent_scratch.py` passes
  unchanged;
- with the flag on, a test clone with local commits, staged and unstaged edits
  and untracked files yields a rescue that passes the gateway's verify, and
  a rescue with one byte changed fails it;
- verify checks out no working tree, and its extra disk is at most the
  rescue's size plus a fixed few KB;
- each doubt in the list above yields the whole-tree quarantine, one test per
  doubt;
- a repository whose config names a hook, fsmonitor, filter and pager that
  each write a marker file outside the tree leaves no marker;
- a write into a quarantined tree while its rescue is being built keeps the
  whole tree and discards the rescue;
- the shrink pass never takes `_SWEEP_LOCK`, so a spawn's scratch handoff
  during a shrink is not delayed;
- after an upstream force-push drops a boundary commit, recovery from the
  rescue still succeeds from the mirror;
- a commit the mirror holds only as an object, not reachable from a branch or
  tag, goes into the bundle.

**Phase 2: provider by SHA.** Add the provider kind for GitHub, reachable from
a branch only. Blocked on open question 3 (how it verifies); until then the
provider kind always falls back. Exit criteria: a commit reachable only from a
fork or a PR ref is not saved; an API error means the fallback.

**Phase 3: default on.** Blocked on this RFC being `accepted`, because it
changes what the sweep does by default, and on open question 9, because
without a trusted store the default changes nothing. Exit criteria: a week of logs from
opted-in hosts shows no verify failure that was not a listed doubt.

## Backward compatibility

`.quarantine` keeps its name, naming, expiry and "plain directories only"
rule. Old whole-tree entries expire as before. A user who knows today's
recovery steps still finds whole trees for anything that fell back. A rescue is
new content in the same place, described by its own `RESCUE.md`.

## Test plan

The phase exit criteria above are the main tests. Beyond them:

- Unit: the candidate parser rejects oversized files, non-hex ids and
  duplicate keys.
- Unit: a store that answers "saved" for nothing yields a bundle of every
  commit; one that answers "saved" for everything yields an empty bundle and
  still keeps changed and untracked files.
- Integration (Linux and macOS): verify on real clones, including binary
  files, renames, deletions, file modes and symlinks.
- Fault injection: kill the sweep between each step; the next sweep finds the
  tree, a complete rescue, or both, never neither.
- Disk nearly full (99%): the pass still shrinks trees one by one; a disk with
  less free space than one rescue keeps the whole tree.

## Alternatives considered

| Option | Verdict | Why |
|---|---|---|
| Shorter expiry (for example 3 days) | rejected as the fix | still keeps whole trees; at 28 GB a day, 3 days is ~84 GB; and it shrinks the time a user has to notice |
| Size cap on `.quarantine` | rejected | it deletes by age or size, not by savedness, so it deletes unsaved work again, which is what #19897 fixed |
| Dedupe or reflink between trees | complementary | only some filesystems; clones fetched over the network share no blocks; offline dedupe is slow and needs extra tools |
| Guide agents to `git clone --reference` or `git worktree add` | complementary | makes `.git` small, but it is guidance, not a guarantee; alternates break if the reference repo is cleaned up; and such trees are a listed doubt here |
| Ask the clone (`git status`, `git log @{u}..`) | rejected by #19897 | the clone's refs and config are agent-controlled |
| Delete clean regenerable dirs only | the sibling PR | does not touch `.git` or source |

## Open questions

1. Is the gateway-owned mirror acceptable disk-wise? One full mirror per
   configured repository (about 1.8 GB for Kiro Crew) is paid once, against
   ~4.2 GB per quarantined clone today, or ~2.6 GB after the sibling PR.
2. Should Phase 1b ship the provider kind instead of the mirror kind, because it
   needs no extra disk?
3. Should verify be required always, or only when the rescue holds commits?
   And how does the provider kind verify, given no local copy
   of the saved commits and no network in the sandbox: a verify step outside
   the sandbox that touches only gateway-made data, or a mirror after all?
4. What is the right size cap for a rescue, relative to the tree?
5. Are dangling commits (not on any ref or reflog) worth a `git fsck` pass, or
   is "the agent hid it" a fair line?
6. Do SHA-256 repositories need anything extra?
7. Is a `kirocrew scratch restore <path>` command wanted, or are `RESCUE.md`
   commands enough?
8. Which config key and default name the opt-in flag?
9. How does a host get a trusted store without setup? See "With nothing
   configured" above. Options: the gateway offers to create a mirror the
   first time a dead clone of an unknown remote falls back, or Phase 3 waits
   for the provider kind so a host with a provider credential needs no setup.
10. How does the mirror keep a rescue's saved boundary commits until it
    expires, if upstream force-pushes them away? One way: a pin ref per
    boundary commit, dropped when the rescue expires. The provider kind
    cannot pin at all.
