---
title: The sandbox seals the gateway checkout's git control files
status: accepted
author: iamwhatever
created: 2026-10-29
last-audited: 2026-10-29
audited-at: 7df9837c60
doc-pr:
implementation-prs: [24812]
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: The sandbox seals the gateway checkout's git control files

- Status: accepted. This records a maintainer decision to ship the
  sandbox-side git seal in [#24812](https://github.com/kirodotdev/KiroCrew/pull/24812)
  with the losses in section 3 and the known limits in section 6. The deciding
  maintainer is the one who merges this document, and confirms acceptance on
  its pull request; a maintainer who does not accept it sets `status: draft`
  before merge instead. Claims about main were checked at `7df9837c60`; claims
  about the change were read from #24812 at head `ee9bb8e5f5`.
- Author: iamwhatever

## 1. Problem and threat

In a source-checkout install, the agent's workspace is the install checkout
(`KIROCREW_PROJECT_DIR`), and the agent can write its `.git`. The gateway runs
git in that checkout outside the sandbox (the update check, Dev Fleet), and so
does the person at the machine. Git runs programs that its control files name:
`config` (`core.fsmonitor`, `core.sshCommand`, filter and diff drivers),
`hooks/`, `info/attributes` (which picks a driver), and `commondir` or a linked
worktree's `.git` file (which say where those files are).

A security report showed the chain: a prompt-injected agent writes
`.git/config`, the gateway's own git reads it, and a program of the agent's
choice runs on the host as the user, outside every sandbox control.

The host-side callers are fixed on main:

| PR | Fix |
|---|---|
| [#24824](https://github.com/kirodotdev/KiroCrew/pull/24824) | Host git ignores `~/.gitconfig` the agent may write |
| [#24808](https://github.com/kirodotdev/KiroCrew/pull/24808) | Update check: `git_command_env()` and `repo_exec_config_reason` (`dashboard/handlers/updates_check.py`) |
| [#24822](https://github.com/kirodotdev/KiroCrew/pull/24822) | The same protections on the other host-side callers |

Dev Fleet's git also runs with `_GIT_ENV_NEUTRALIZERS`
(`apps/builtins/dev_fleet/runtime.py`). Those guards protect the gateway's known git calls. They do not protect a
future gateway git call that forgets them, and they do not protect the person
who runs `git commit` in the same checkout from a terminal: that git runs an
agent-planted hook. #24812 is the second layer: the agent cannot write those
files in the first place.

## 2. Decision

Inside the agent sandbox, the git control files of the gateway's own
checkouts are read-only. "The gateway's own checkouts" is exactly
`gateway_checkout_roots()` in `sandbox_git_control.py`: `KIROCREW_PROJECT_DIR`
and the source checkout the gateway imports from, when each has a `.git`.

- Sealed read-only: `config`, `config.worktree`, `hooks/`, `info/`,
  `commondir`, a linked worktree's `.git` file, every `include.path` /
  `includeIf` target, and the `core.hooksPath` directory.
- Pinned (cannot be renamed or replaced): the git directory and every
  renamable directory above the checkout, so `mv .git .git.old` plus a fresh
  `.git` fails.
- Still writable: objects, refs, the index, `HEAD` and logs. `git add`,
  `commit`, `branch`, `status` and `worktree add --no-track` keep working.
- The file-tool write gate (`security/paths.py`) refuses the same paths, in
  those checkouts only.
- The shipped dev skills stop writing the sealed config: `kirocrew-worktree-dev`
  and `live_checkout_guard.py` use `git worktree add --no-track`, and
  `kirocrew-prepare-pr` pushes with `git push origin <branch>` (no `-u`).

There is no opt-out switch. Turning the sandbox off (`agent.sandbox = off`)
removes the seal along with every other sandbox control.

## 3. What agents lose, and where

Inside the sandbox, in the gateway's checkouts, these now fail (EROFS or EBUSY
on Linux, a Seatbelt deny on macOS):

- `git push -u`, `git branch --set-upstream-to`, a branch that tracks a remote
  (`checkout -b x origin/x`, `worktree add -b x origin/main` without
  `--no-track`)
- `git remote add`, `git config`, `gh pr checkout`
- `pre-commit install`, `git lfs install`, `git sparse-checkout set`,
  `git submodule update --init`

Where it applies:

| Place | Affected? | Why |
|---|---|---|
| The gateway checkout | Yes | It is a gateway checkout root |
| A linked worktree of it (`git worktree add`, e.g. `../kirocrew-wt-*`) | Yes, for the commands above | It shares the main clone's `config` and `hooks/`, which are sealed |
| A separate clone with its own `.git` | No | Not a gateway root; its `.git/config` stays writable |
| Any other repository or workspace | No | Not a gateway root; only today's file-tool rules apply |
| Packaged (non-source) installs | No new loss | No source checkout; `KIROCREW_PROJECT_DIR` only if it is itself a git checkout |

Ordinary commits, branches and status keep working everywhere.

## 4. Workaround

For work that needs an upstream, a new remote, `gh pr checkout` or installed
hooks, use a separate clone, not a linked worktree:

```bash
git clone https://github.com/kirodotdev/KiroCrew.git ../kirocrew-clone-<name>
```

In a linked worktree of the gateway checkout, create branches with
`--no-track` and push with `git push origin <branch>`; `gh pr create --head
<branch>` needs no upstream. The person at the machine loses nothing: the seal lives in the sandbox's mount
namespace (Linux) or profile (macOS), and the host files stay writable for host
git.

## 5. Per-OS behavior

| OS | Shell writes | File tools | Gap |
|---|---|---|---|
| Linux (namespace sandbox) | Bind mounts: writes fail with EROFS, renames with EBUSY. A `rename(2)` between two pinned directories fails with EXDEV; `mv` falls back to copying | Refused in gateway checkouts | A shell can still create a missing `.git/commondir`, or a missing include / `hooksPath` target: a bind mount needs an existing file, and a stand-in breaks git and libgit2. The host-side guards cover only the gateway's own git calls; a person's terminal git stays exposed (section 6) |
| macOS (Kiro Crew Seatbelt profile) | Write denies plus rename guards on the pins and every ancestor of the checkout | Refused in gateway checkouts | None known; Seatbelt also denies creating the missing files |
| Windows | No OS sandbox exists, so no seal | Refused in gateway checkouts | Shell writes are unconfined, as today |

## 6. Known limits

This decision accepts two gaps. Neither is closed by #24812.

1. **Linux: a missing `commondir` or config-named target.** A plain checkout's
   `.git` has no `commondir`, and git reads that file in every git directory.
   An agent shell can create it, pointing at a directory it controls with its
   own `config` and `hooks/`; the same holds for a missing include or
   `core.hooksPath` target. The host-side guards (#24808, #24822) neutralize
   repository config for the gateway's own git calls, so those stay safe. A
   person's terminal `git commit` or `git status` in that checkout is NOT
   covered: it can run a hook or `core.fsmonitor` the agent planted. macOS is
   not affected, because Seatbelt denies creating these files. Closing the gap
   needs a different control (for example a pin on `.git` that refuses new
   files) and is left to a later change.
2. **Every OS with a seal: a rewrite after spawn.** A seal covers the file
   present at spawn. Host git rewrites `config` by writing `config.lock` and
   renaming it over, so after a gateway-side `git config` a running sandbox sees
   an unsealed file until its next spawn.

## 7. Alternatives considered

1. **Host-side fixes alone** (#24808, #24822, #24824). Rejected as the only
   layer: they guard the calls we know about. A new gateway git call, or the
   person's own terminal git, still reads agent-written hooks and config.
   Kept as the primary control for the gateway's own git calls, including
   through the Linux gap in section 6.
2. **A narrower mask** (seal only `config`). Rejected: `hooks/` run on the
   person's `git commit`, `info/attributes` picks drivers, and `commondir` or a
   worktree `.git` file redirect git to other config. Each one alone reopens the
   chain. Sealing `.git` whole was also rejected: commits would break.
3. **Seal `.git` in every repository the agent touches.** Rejected: agents
   need `push -u` and `remote add` in their own clones, and the gateway does not
   run git there.
4. **A default-off setting.** Rejected: the exposed users are the ones who
   would not turn it on.

## 8. Rollback

Revert #24812. It adds no stored data, migration or config key, and the host
files are never changed by the seal, so a revert restores today's behavior on
the next agent spawn. The skill wording (`--no-track`, no `-u`) works with or
without the seal and need not be reverted.
