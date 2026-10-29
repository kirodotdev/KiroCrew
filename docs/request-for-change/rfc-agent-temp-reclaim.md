---
title: A checked way for agents to remove temp trees they left behind
status: draft
author: pearcekieser
created: 2026-10-28
last-audited: 2026-10-28
audited-at: c6789799df
doc-pr: 25401
implementation-prs: []
tracking-issues: [25400]
supersedes: []
superseded-by: []
---

# RFC: A checked way for agents to remove temp trees they left behind

Agents leave directories in `/tmp` and cannot remove them. The deny row
`local-destructive-rm-rf-root` refuses `rm -rf` on any absolute path, and the
other ways to delete a tree are respellings the `blocked-by-policy` skill tells
agents not to try. This RFC proposes a `tmp_reclaim` tool and a
`kirocrew tmp reclaim` command. Both remove a named temp tree only after the
gateway has checked that the tree is the caller's to remove and that nothing is
using it. The deny rules stay as they are.

## Summary

- One reclaim routine, `kiro_crew.temp_reclaim`, decides per path whether it
  may go and removes it by descriptor through `pinned_fs.remove_tree_pinned`.
- It accepts two scopes. The caller's own scratch tree can be removed at any
  time. A child of `/tmp` or `/var/tmp` can be removed when the user owns it,
  it holds no repository, nothing has written to it for 30 minutes, and no
  process uses it.
- Agents reach the routine through the gateway, which runs outside the agent
  sandbox. Inside the sandbox, `/proc` shows 13 of the user's 1,273 processes,
  so a check run there cannot show that a tree is unused.
- People reach it through `kirocrew tmp reclaim` in their own terminal.
- No deny row, floor or approval classifier changes.

## Motivation

### Current state

Measured at `c6789799df` on one Linux host.

- `src/kiro_crew/security/denied_rules_catalog.py` defines
  `local-destructive-rm-rf-root` with the pattern `rm -rf /.*`. Its description
  says the row is for deletion "rooted at the filesystem root", but the pattern
  matches every absolute target, so `rm -rf /tmp/kc-c3-home` is refused too.
  It also matches the text anywhere in a command, so a command that only
  quotes it in a note argument is refused as well.
  `security/rm_floor.py` adds a structural check for `/` and `~` themselves.
- The system prompt (`config/prompt.md`) tells the agent not to rewrite a refused command
  "into a form that dodges the check". A lesson on this host adds that
  throwaway checkouts are removed with `git worktree remove` and nothing else.
- Agents put temp trees in `/tmp` mostly as fake `HOME` directories for pytest
  runs. `test/conftest_floor_home.py:_refuse_a_real_data_home` refuses the live
  data home and says to "point it at a scratch dir" without naming one, so
  agents pick `/tmp/<name>-home`. Each fake home rebuilds the uv, npm and
  bytecode caches under it, which is 10,000 or more inodes per run.
- The hourly scratch sweep (`agent_scratch.sweep_dead_scratch`) reclaims only
  `~/.kiro/crew/scratch`. Nothing reclaims `/tmp`.

### The problem

On 2026-10-28 `/tmp` ran out of inodes. A survey of the leftovers found 23
directory groups made by agent sessions, 21 of them fake test homes. One
session made 55 of them. None of the sessions removed what it made. Two named the
policy: "since cleanup is blocked" and "the cleanup rules here forbid recursive
deletes". A third wrote "since other processes share that space, I shouldn't
clean it myself". As of this writing, 134 entries in `/tmp` match
`*home*`.

The third reason is a fair one. A fake home in `/tmp` is often shared. A ledger
note can hand `HOME=/tmp/kc-sq-home` to every wake of a worker, and
`/tmp/kc-sq-deps` was on another session's `PYTHONPATH`. An agent that deletes
by name can break a sibling's run. What the agent lacks is a way to delete that
first checks the facts it cannot see.

### Why a check inside the sandbox is not enough

The obvious tool is a CLI that the agent runs in its shell. It would scan
`/proc` for processes whose working directory, open files, mapped files or
bound sockets sit inside the tree. Inside the agent sandbox that scan reads
the working directory of 13 of the 1,273 processes owned by the user. Reads of
the other 1,260 fail with `EACCES`, because they run outside the sandbox's user
namespace. That includes the gateway, every other session's kiro-cli and every
detached test run. A scan that sees 1% of the processes cannot say a tree is
unused. The gateway runs as the user outside any sandbox, so the check belongs
there.

## Goals

- An agent can remove a temp tree it made, in one call, without asking a
  person, when the tree is plainly its own and unused.
- A tree that another process uses, that holds a repository, or that the
  caller's sandbox could not have deleted is kept. The caller is told which
  check kept it.
- A person can run the same checks from a terminal to clean up the existing
  leftovers.
- Every removal is in the security event log (SEL) with the caller and the path.

## Non-goals

- Changing or narrowing any deny row or floor, including the over-wide
  `rm -rf /.*` pattern. That is a separate decision with its own review lane.
- A background reaper for `/tmp`. Reporting `/tmp` headroom and an optional
  reaper are a separate workstream (`kirocrew doctor`,
  `doctor_checks/resources.py`).
- Moving test homes out of `/tmp`. The conftest change that makes a per-run
  home under `$TMPDIR` and keeps caches shared is a separate workstream. It
  cuts how many leftovers appear. This RFC handles the ones that do.
- Keeping session scratch alive across restarts and resumes. A separate RFC
  covers that.
- macOS and Windows in the first phase (see Open questions).

## Design

### The rule

`reclaim(path, caller)` returns `removed`, `kept: <reason>` or
`would-remove` for a dry run. It removes nothing unless every check passes.

1. **Shape.** `lstat(path)` is a directory or a regular file, not a link,
   socket or device. The path, after `realpath`, is not a temp root itself.
2. **Scope.** The resolved path is in one of these two scopes:
   - **S1, own scratch.** It lies strictly inside the caller's own scratch
     tree. For an agent, that is the `$KIROCREW_SCRATCH` directory and the
     per-process `$TMPDIR` directories the gateway allocated for the caller's
     session tree (`AcpRuntime.work_scratch_dir`, the allocations in
     `agent_scratch.allocate_scratch`). The admission check is the same one
     `messaging/outbound_files.session_scratch_root` applies to a staged
     file: one allocation, not a link, whose parent is the managed scratch
     root.
   - **S2, system temp.** It lies inside `/tmp` or `/var/tmp`, and both the
     path and the root's direct child that contains it are owned by the
     gateway's uid.
   Anything else is kept with `outside-scope`.
3. **Sandbox parity.** For an agent caller, the path is not inside any tree
   the caller's sandbox plan seals or hides. The gateway must never delete
   something the calling agent could not delete itself. Today no tier seals
   `/tmp`, and other sessions' scratch is hidden (the `scratch` leaf in `sandbox_leaves.py`),
   which S1 already excludes. The check is still made, so a future tier that
   seals `/tmp` is respected.
4. **Idle.** In S2, the newest `mtime` anywhere in the tree is at least 30
   minutes old. This is the same tree walk as
   `agent_scratch._tree_newest_mtime`. For a regular file, the routine reads
   the file's own `mtime` from the `lstat` in step 1, because the tree walk
   cannot open a file. A tree written more recently is kept with `recent`.
   S1 has no idle check, because the tree belongs to the caller's own
   session.
5. **Not a repository.** This check applies to directories only. A regular
   file cannot be a repository, so the ancestor check below is all it gets.
   For a directory, `agent_scratch.holds_git_repository(tree)` is false,
   and no ancestor up to the temp root holds a `.git` entry. A tree that fails
   is kept with `holds-repository`, and the reason points at
   `git worktree remove` or moving the work. One addition is needed. A
   `.cache/uv` directory directly under the tree is treated as regenerable,
   like `node_modules`. uv writes `.cache/uv/sdists-v10/.git`, which makes 8
   of the first 40 `/tmp/*home*` trees look like repositories today. The rule
   names `.cache/uv` only, so a clone someone put elsewhere under `.cache` is
   still found and kept.
6. **Not in use.** The gateway scans `/proc/<pid>/cwd`, `root`, `exe`,
   `fd/*` and `maps` for every process, and the bound paths in
   `/proc/net/unix`. It also reads `/proc/<pid>/environ` and treats a
   `HOME`, `TMPDIR`, `PYTHONPATH`, `XDG_*` or `*_CACHE_DIR` value that names
   a path inside the tree as use. A sibling that has a shared home on its
   `PYTHONPATH` but no file open there is the case this catches. A hit
   inside the tree keeps it with
   `in-use: pid <n> (<comm>)`. If the gateway cannot read one of the user's
   own processes, the tree is kept with `unprovable: <n> processes
   unreadable`. A process owned by another uid is skipped. In S2 that process
   could only reach the tree through a mode the user chose, and the ownership
   check in step 2 limits the scope to the user's own trees.
7. **Remove.** `pinned_fs.remove_tree_pinned` with an `approve` callback that
   re-checks, through the pinned descriptor, that `(st_dev, st_ino)` and
   `st_uid` match what the checks saw. A regular file is unlinked through its
   pinned parent after the same identity check. A tree that does not fully go
   reports `kept: partial (<survivors> left)`.

The routine runs off the event loop and stops after 120 seconds. The largest
leftover, `/tmp/p14-floor.B10Z` with 57,858 entries, takes 0.6 seconds for a
`find` walk on this host.

### Surfaces

- **`tmp_reclaim` on `kirocrew-core`.** Arguments are `paths` (1 to 20
  strings) and `dry_run` (default false). The tool posts to a new gateway
  route, `POST /api/tmp/reclaim`. The gateway takes the caller's session from
  the request's caller identity (`mcp_caller.CallerContext`) and runs the
  routine with S1 bound to that session's tree. The result is one line per
  path. The default agent config grants the whole `@kirocrew-core` server
  in `allowedTools` (`config/defaults.json`), so `tmp_reclaim` runs with no
  approval card, the same as `artifact_delete` today. This is deliberate.
  The tool reaches nothing the agent's sandbox could not already delete
  (step 3), and a card on every cleanup would push agents back to leaving
  trees in place. The safety rests on steps 2 to 6, not on a person reading
  each call. No approval rule changes. Open question 5 asks whether to carve
  the tool out of the server-wide grant instead.
- **`kirocrew tmp reclaim <path>... [--dry-run] [--json]`.** It runs the same
  routine in-process with scope S2 only, for a person in their own terminal.
  Run inside an agent sandbox, it finds unreadable processes and keeps every
  S2 tree with `unprovable`, and its message points at `tmp_reclaim`. Exit 0
  when every path was removed, 1 when any was kept, and 2 for usage errors.
- **Guidance.** `config/prompt.md` gets one sentence after the destructive-commands rule naming
  `tmp_reclaim` for temp trees the session made. The `blocked-by-policy`
  skill gets a "recursive delete" class with the same pointer. The
  `writing-tests` skill's test-home section names it for cleanup.

### Audit

Each call writes one SEL event per path with the caller session key, the
resolved path, the scope, the outcome and the reason. A removal also records
the inode count removed.

## Security considerations

### Threat model

A prompt-injected agent calls `tmp_reclaim` on a path it should not delete.

- **Another session's scratch.** Kept by step 2. S1 is bound to the caller's
  session server-side, not to a path the caller names.
- **A sibling's live test home in `/tmp`.** Kept by step 6 while any running
  process has it open or names it in its environment, and by step 4 within 30 minutes of a write. A home that is idle
  and unused between a worker's wakes can be removed. The agent could already
  delete it with any non-`rm` tool its sandbox allows, so the tool adds no
  reach. It adds checks the sandbox cannot make. No person approves the call
  under the default config (see Surfaces), so steps 4 and 6 are the whole
  protection for such a home. Open question 2 covers narrowing S2 further.
- **The person's own `/tmp` trees.** S2 is not limited to test homes. Any
  idle, unused, user-owned tree in `/tmp` or `/var/tmp` that holds no
  repository qualifies, including a download or build output the person put
  there. A prompt-injected agent can remove those without a card. The
  sandbox already lets it, so this is not new reach, but the tool makes it
  one call. The marker rule in open question 2 would close this.
- **Real work.** A clone or worktree is kept by step 5 whatever its age.
- **A swap between check and delete.** Step 7 pins the parent chain and
  re-checks identity through the descriptor, which is the guarantee
  `pinned_fs` exists for.
- **The data home, `$HOME` and the worktree.** None is inside `/tmp`,
  `/var/tmp` or a scratch allocation, so step 2 keeps them.

### What the gateway gains

The gateway gains a delete it performs on an agent's request. Step 3 limits it
to paths the agent could delete itself, so an agent gains no reach outside its
sandbox. What it gains is a delete that knows about every process on the host.

### Why this is not a deny-rule change

The deny rows decide which shell text an agent may run. This RFC adds no shell
text and does not change which text is refused. `rm -rf /tmp/x` is still
refused, and the refusal stays the same. An open question asks whether the
refusal's second line, which only explains a refusal, should name the tool.

## Migration plan

### Phase 1: the routine and the CLI

`kiro_crew/temp_reclaim.py` and `kirocrew tmp reclaim`, with the `.cache/uv`
regenerable rule scoped to reclaim, so the sweep's quarantine behaviour does
not change.

Exit criteria:
- Unit tests cover every `kept` reason, a dry run, a file target, and a tree
  swapped for a link between the scan and the removal.
- Run from an unsandboxed terminal on the host above, `--dry-run` over
  `/tmp/*home*` lists each tree as `would-remove`, `in-use`, `recent` (a
  write within 30 minutes, step 4) or `holds-repository`. The count of
  unreadable same-uid processes is recorded in this document (Open
  question 1).

### Phase 2: the gateway route and `tmp_reclaim`

Exit criteria:
- An agent in a pod removes a tree it made under its own `$KIROCREW_SCRATCH`
  and a `/tmp` tree it made more than 30 minutes earlier.
- The same agent is refused, with the named reason, on another session's
  scratch directory, on a tree a background `sleep` holds open, and on a
  worktree.
- One SEL event per path.

### Phase 3: guidance

The prompt sentence and the two skill edits. Exit criterion: the
`blocked-by-policy` skill's tests and the prompt snapshot tests pass, and a
pod agent asked to clean its test home calls `tmp_reclaim` without first
trying `rm`.

Each phase can ship alone. Phase 2 depends on Phase 1. Phase 3 depends on
Phase 2.

## Backward compatibility

Additive. No existing command, rule, route or tool changes behaviour. The
`.cache/uv` rule applies only inside the reclaim routine.

## Test plan

Unit tests in `test/` for the routine, following the `writing-tests` skill.
Every test tree is made under the test's own temp dir and every process
holding a tree is a child the test starts and reaps. `/proc` reading is
behind one function, so the unreadable-process case is tested by injection
rather than by needing a second user. One route test covers the caller
binding. The Phase 2 pod checks above are the end-to-end proof.

## Alternatives considered

- **Narrow `rm -rf /.*` to the root itself.** This would match the row's
  description, but it loosens a floor agents rely on, and it still gives an
  agent no way to know whether a tree is in use.
- **The CLI alone, run in the agent's shell.** Rejected because the sandbox
  sees 13 of 1,273 processes (see Motivation).
- **Record which `/tmp` paths a session made, and allow only those.** Agents
  create these trees with free-form shell (`mkdir -p`, `mktemp`, a tool
  making its own directory), so the gateway cannot observe creation reliably.
  Process use, ownership and the repository check together are facts it can
  check.
- **A `/tmp` reaper only.** It would clean up eventually but gives an agent no
  way to clean up after itself now. It belongs to the reporting workstream and
  can reuse this routine.

## Open questions

1. **Can the gateway read the `/proc` entries of sandboxed processes?** They
   run in a child user namespace that the user owns, so the read should pass
   the ptrace access check, but this has not been measured. If many processes
   stay unreadable, step 6 keeps every S2 tree and Phase 2 needs another
   source, such as the launcher reporting each sandbox's open working
   directories.
2. **Is 30 minutes the right idle bar?** A worker that sleeps between fleet
   wakes can leave a shared home idle for hours. Requiring a Kiro Crew marker
   in S2, such as `.cache/kirocrew`, would narrow the scope to test homes at
   the cost of other leftovers like `e5scratch`.
3. **Should the refusal for `local-destructive-rm-rf-*` name `tmp_reclaim`
   on its second line?** That line explains a refusal and changes no
   decision, but it lives in the policy engine's refusal producer, so it is
   left to the security owners.
4. **macOS and Windows.** macOS has no `/proc`. `lsof` can answer step 6 but
   is slow on large trees. Windows has neither, and this RFC does not cover
   it. The proposal is Linux-only until there is demand.
5. **Should `tmp_reclaim` keep an approval card?** `allowedTools` has no
   exclusion syntax, so there are two ways to get one. The default could list
   every other core tool instead of `"@kirocrew-core"`, or the tool could go
   on the operator confirm list, `hooks.confirm_tools`. A confirm-list
   pattern that names one tool withholds that whole server's static grant
   (`docs/architecture/mcp.md`), so the second route sends every
   `kirocrew-core` call through the gate, not only `tmp_reclaim`. Either way
   it is an approval-rule change, and it would make agents ask before every
   cleanup. This RFC proposes no card and leaves the choice to the security
   owners.
