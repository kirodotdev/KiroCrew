---
title: With KIRO_HOME set, Kiro Crew reads and writes kiro-cli's user directory where kiro-cli does
status: draft
author: jstrunk (drafted with an agent session)
created: 2026-11-19
last-audited: 2026-11-19
audited-at: 5c9bf25f3e
doc-pr: 39590
implementation-prs: []
tracking-issues: [39215, 12986]
supersedes: []
superseded-by: []
---

# RFC: `KIRO_HOME` consistency for kiro-cli and KAS

Status: draft. Nothing is built and this document changes no code. It answers
[#39215](https://github.com/kirodotdev/KiroCrew/issues/39215), where a
maintainer asked for a design sign-off through this process, and it takes a
position on the open question left in
[#12986](https://github.com/kirodotdev/KiroCrew/issues/12986). Acceptance is
requested from a maintainer, who records it on this Status line. Every code
reference below was read at `5c9bf25f3e` (origin/main, 2026-11-19). Code is
cited by symbol and file, because `scripts/docs_lint.py` flags `file.py:NNN`
citations.

## Summary

`KIRO_HOME` is kiro-cli's documented override for its whole user directory.
KAS reads its own files under it as well. Kiro Crew follows it for the agents
directory, the session transcripts, the skill projection and the security
fences. It does not follow it for the MCP registry, skills, prompts, hooks, or
the global steering tree that the dashboard shows and the folder-steering dedup
assumes. An install with `KIRO_HOME` set therefore reads one instance from two
directories.

This RFC proposes that, with `KIRO_HOME` set, every Kiro Crew reader and writer
of kiro-cli's user directory resolves it through `kiro_home()` **at call time**.
That covers:

- the MCP registry (`settings/mcp.json`) and its lock file;
- skills, prompts and hooks;
- on kiro-cli, the folder-steering dedup. Its skip root must equal the
  `$KIRO_HOME/steering` entry that the skill projection already hands to
  kiro-cli. The Steering tab, the member snapshot and the per-chat list keep
  showing the host tree in this RFC. That leaves a known gap for kiro-cli
  chats, and Design section 4 lays out three options and why this one comes first.

For the MCP question left open in #12986, it proposes that **the registry
follows `KIRO_HOME` and the host copy is not merged in.**

With `KIRO_HOME` unset, nothing changes.

`KIRO_HOME` means the whole instance. For the MCP registry, skills, prompts and
hooks, one authored spec and one dashboard serve every harness on an instance,
so with `KIRO_HOME` set those readers follow it for every harness, Claude Code,
Codex, OpenCode, Pi and KAS included. This is decided (see "Shared readers").
Global steering is the exception: for Claude Code, Codex, OpenCode, Pi and KAS
it stays as on main, and only the kiro-cli folder-steering dedup moves.
"Effect on other harnesses" shows why.

## Motivation

### Current state

`kiro_home()` in `config/paths.py` returns `$KIRO_HOME` when set (resolved,
and refused if it names a system directory), else `~/.kiro`. Its docstring says
`KIRO_HOME` "is not yet a supported way to isolate an instance", which is the
documented limit the maintainer's reply on #39215 cites.

Who sets `KIRO_HOME` on main (read in the tree at `5c9bf25f3e`):

| Setter | Value | Where |
|---|---|---|
| Pods (Dev Fleet) | `<pod home>/kiro`, with the real `HOME` | `build_pod_env` in `pod/runtime.py` |
| End-to-end test harness and scenario fixtures | a per-run folder | `testing/harness.py`, `test/e2e/scenarios/conftest.py` |
| Test seeds | `<gateway home>/kiro` | `scripts/gui-user-test/boot.sh`, `src/kiro_crew/docs/scripted-test-model.md` |
| Installer smoke scripts | `<gateway home>/kiro` | `scripts/smoke-windows-install.ps1`, `.github/scripts/test-windows-installer.ps1` |
| Eval runs | `dirs.kiro` under an isolated `HOME`, per arm | `eval/tasks.py` |
| The aws_control container | `<data home>/kiro` | `apps/builtins/aws_control/crew/runtime/container/supervisor/bundle.py` |
| A person, by hand | any folder | shell profile |

No code on main sets it for a relocated `KIROCREW_HOME` (#9797, which proposed
that, was closed without merging), so an install that does not set it by hand
is not affected.

What follows `KIRO_HOME` today, and what does not:

| Area | Follows `KIRO_HOME`? | Where |
|---|---|---|
| Agent specs | yes | `kiro_agents_dir` in `config/paths.py` |
| Session transcripts | yes | `kiro_sessions_dir` in `config/paths.py` |
| Skill projection (workspace views, steering entry) | yes | `acp/skill_projection_parts/preparation.py`; the view lists `file://<kiro_home()>/steering/**/*.md` |
| KAS skill scope | yes | `acp/kas_skill_scope.py` |
| KAS exclusion list for `$KIRO_HOME/settings/mcp.json` | yes, as an extra file | `_kiro_home_servers` in `acp/kas_settings_mcp.py` (#36145) |
| Write-protection tier for the registry and agents | yes | `security/paths_targets.py` |
| MCP cleanup, OAuth discovery | yes | `_kiro_mcp_json` in `mcp_cleanup.py`, `_kiro_settings_mcp_json` in `mcp_oauth_discovery.py` |
| kiro-cli workspace trust | yes, with kiro-cli's own rule | `_kiro_home` in `kiro_cli_workspace_trust.py` |
| Agent skill scope (the trees a custom agent inherits) | yes | `inherited_skill_globs` in `agent_discovery_parts/skill_scope.py` |
| **MCP registry** (merge into specs, session settings, dashboard editor, lock, discovery, app bridges, one built-in app policy) | **no, host `~/.kiro`** | see the reader list below |
| **Skills** (dashboard catalog, listing, read roots) | **no** | same |
| **Hooks default directory, prompts** | **no** | same |
| **Global steering** (Steering tab, member snapshot, per-chat list, dedup root) | **no** | same |

So a gateway with `KIRO_HOME` set already splits in two. The security tier
treats `$KIRO_HOME/settings/mcp.json` as the registry whose `autoApprove`
skips the tool gate, while the dashboard edits `~/.kiro/settings/mcp.json`.
kiro-cli chats get `$KIRO_HOME/steering` through the projection, while the
Steering tab lists `~/.kiro/steering`.

### Reader list

Verified by grep at `5c9bf25f3e`. The spelling searched is
`Path.home() / ".kiro" / ...`. Other spellings (a `home` variable joined with
`".kiro"`, `os.path.expanduser`) need the same sweep in Phase 1; see the test
section.

MCP registry:

- `agent._KIRO_MCP_JSON` (`agent.py`), a module constant. It feeds the spec
  merge in `agent_materialization/mcp_sources.py` and the session settings in
  `acp/session_mcp.py`, and its parent folder is read by `acp/kas_settings_mcp.py`.
- `_GLOBAL_MCP_JSON`, `_MCP_LOCK_PATH` and the label `_KIRO_GLOBAL_SURFACE` in
  `dashboard/handlers/mcp_config_files.py`, used by `dashboard/handlers/mcp.py`,
  `mcp_config_purge.py`, `mcp_probe.py`, `mcp_config_entries.py`,
  `mcp_agent_sync.py` and `dashboard/change_cards_readers.py`.
- `_LEGACY_SHARED_MCP_PATH` in `apps/bridges_mcp.py` (a scrub that writes), and
  the read in `apps/bridges_agents.py`.
- `SCOPE_KIRO_GLOBAL` in `mcp_discovery_server_info.py`.
- `_configured_server_names` in `apps/builtins/mochi/agent_policy.py` (a
  built-in app; see Follow-ups).

Skills: `_shared_skill_catalog.py`, `_shared_skill_listing.py` and
`_shared_skill_roots.py` (two sites) under `dashboard/handlers/`. They back the
dashboard's skill catalog and listing and resolve the `kiro-user/` skill keys.
`_all_skill_paths` in `agent.py` also builds `~/.kiro/skills`, but it has no
caller outside tests, so it changes nothing at run time. Phase 2 moves it with
the rest so the sweep stays clean.

Hooks: `_DEFAULT_KIRO_HOOKS_DIR` in `agent.py`, read by
`agent_materialization/kiro_hooks.py` (the script autoimport into the spec's
`hooks`) and by `resolved_kiro_hooks_dir`, which `user_hook_files` in
`agent_sdk/spec_hooks.py` uses for the standalone `*.json` hook files.

Prompts: two sites in `dashboard/handlers/prompts_paths.py` (the create root and
the entry root that the chat `@prompt` expansion and the detail routes share),
one in `dashboard/handlers/__init__.py` (the global prompts scan), and
`_user_prompts_dir` in `quick_prompts.py`.

Global steering: `_double_load_roots` in `folder_steering.py` moves on kiro-cli
(Design section 4). Three readers keep the host tree in this RFC, by decision
and not by omission: `steering_roots` in `dashboard/handlers/steering.py`,
`_tree_root` in `steering_plan.py`, and two reads in
`member_essential_context.py` (the snapshot, and
`global_always_steering_documents`).

Not in scope: `~/.kiro/crew`, which is Kiro Crew's own data home and has its own
override, `KIROCREW_HOME`.

### Problems

1. **One instance, two directories.** A pod or a hand-set `KIRO_HOME` writes
   an MCP server in the dashboard to the host file, while kiro-cli and KAS read
   `$KIRO_HOME`. The edit appears to succeed and has no effect where it matters.
2. **A pod can edit the host.** A pod keeps the real `HOME`. Its dashboard
   writes the host `~/.kiro/settings/mcp.json`, and its own boot notes
   (`pod/runtime_boot.py`, on why `kirocrew app` is refused in a pod) call that
   file host-scoped, "which neither KIROCREW_HOME nor KIRO_HOME covers".
3. **Double send and a dropped rule.** The folder-steering dedup skips the host
   `~/.kiro/steering` as already delivered. On kiro-cli with `KIRO_HOME` set,
   `$KIRO_HOME/steering` is what kiro-cli loads. Measured on main with a folder
   that declares both trees: a rule file under `$KIRO_HOME/steering` reached the
   chat twice, once from the projection and once from the folder. A rule file
   under the host `~/.kiro/steering` never reached it, because the dedup skipped
   a tree that nothing delivered.
4. **Wrong docs.** The `kiro_home()` docstring and the comment in
   `dashboard/handlers/mcp_agent_sync.py` tell operators not to rely on
   `KIRO_HOME`. The docstring still lists session transcripts as read from the
   host, though `kiro_sessions_dir` follows `KIRO_HOME`.

## Goals

1. With `KIRO_HOME` set, each reader and writer in the list above resolves
   kiro-cli's user directory through `kiro_home()`, evaluated at call time.
2. The MCP registry and its lock file are one file, `$KIRO_HOME/settings/mcp.json`,
   for the dashboard, the spec merge, the session settings and the bridges.
3. On kiro-cli, the folder-steering dedup skips the `$KIRO_HOME/steering` entry
   the projection passes, and on every other harness it skips what it skips
   today.
4. With `KIRO_HOME` unset, every path resolves to the same value as today.
5. With `KIRO_HOME` set, the MCP registry, skills, prompts and hooks readers
   resolve the same directory for every harness on the instance.

## Non-goals

- Global steering on Claude Code, Codex, OpenCode and Pi. Their steering and
  project paths do not change. (The MCP registry, skills, hooks and prompts do
  move for them; see "Shared readers".)
- Changing what the KAS engine reads natively. KAS loads global steering from
  the host tree and ignores `KIRO_HOME` for it, so Kiro Crew cannot change that
  (see Known limits).
- Making the Steering tab, member snapshot and per-chat list follow the harness.
  Design section 4 records that as a follow-up.
- Pods on Claude Code. They keep reading the operator's real global steering.
- Setting `KIRO_HOME` for any instance. Nothing here adopts a private
  kiro-cli home for a relocated `KIROCREW_HOME` (that was #9797).
- Moving data. No file is copied, merged or deleted on upgrade.
- Changing the sandbox fences, or `~/.kiro/crew-auth-staging` (see Follow-ups).

## Design

### 1. One resolver, evaluated at call time

Most of the sites above bind the path when the module is imported
(`_KIRO_MCP_JSON`, `_GLOBAL_MCP_JSON`, `_MCP_LOCK_PATH`,
`_DEFAULT_KIRO_HOOKS_DIR`, `_LEGACY_SHARED_MCP_PATH`). A constant cannot follow
a `KIRO_HOME` that changes after import, and tests set it after import.

The pattern already exists. `_kiro_mcp_json()` in `mcp_cleanup.py` returns an
explicit override hook if one is set, else `kiro_home() / "settings" / "mcp.json"`.
The change gives that shape one home in `config/paths.py`: a small set of
accessors for the five subtrees (`settings/mcp.json` and its lock, `skills`,
`prompts`, `hooks`, `steering`), each `kiro_home() / <leaf>`. Every site in the
list calls one. The `steering` accessor has one caller outside tests, the
dedup in Section 4. Names and the exact count are left to the implementation PR.

Two rules for the sites:

- A function that uses the path twice in one operation (lock, then file) binds it
  once, so a `KIRO_HOME` change between the two calls cannot split the lock from
  the file.
- The existing module-level names that tests patch stay as override hooks that
  default to `None`, as `mcp_cleanup._KIRO_MCP_JSON` does, so the test seams keep
  working.

With `KIRO_HOME` unset, `kiro_home()` returns `Path.home() / ".kiro"` unresolved,
which is the value every site builds today. The accessors add nothing on that
path.

### 2. MCP registry: it follows `KIRO_HOME`, no host merge

With `KIRO_HOME` set, Kiro Crew reads and writes only
`$KIRO_HOME/settings/mcp.json`. The host `~/.kiro/settings/mcp.json` is neither
read nor merged into the specs Kiro Crew renders.

Why this is the proposal:

- It is what kiro-cli and KAS do. Both read the registry under `KIRO_HOME` only.
  The spec Kiro Crew renders pins `includeMcpJson: false`
  (`docs/architecture/mcp.md`), so the rendered `mcpServers` are what kiro-cli
  loads. A host server merged into a pod's spec is a server the pod's own
  registry never named.
- The write-protection tier and the registry's `autoApprove` handling already
  treat `$KIRO_HOME/settings/mcp.json` as the registry (`security/paths_targets.py`).
  The dashboard editor and the spec merge are what disagree with it.
- It makes `KIRO_HOME` mean isolation, which is what pods and test rigs set it for.
- It leaves one rule to document: the registry is the file under `kiro_home()`.

Dead code that follows. `_kiro_home_servers` in `acp/kas_settings_mcp.py`
(added in #36145) names the servers of `$KIRO_HOME/settings/mcp.json` when that
is not Crew's global file. Once Crew's global file is that file,
`path == agent_mod._KIRO_MCP_JSON.resolve()` is always true and the function
always returns `{}`. This RFC proposes deleting `_kiro_home_servers`,
`KIRO_HOME_SOURCE` and the second read in `_file_servers`, and rewriting
`test/test_kas_settings_mcp_kiro_home.py` to assert the same behaviour through
the single path. If the alternative below is chosen instead, the function stays
live and none of this is removed. That is a reason to settle the question first.

The one-time scrub of legacy app entries (`_LEGACY_SHARED_MCP_PATH`) follows the
same rule: it touches the registry under `kiro_home()` and no other file. An
entry an older version wrote into the host file stays there. See Open
questions.

### 3. Skills, prompts, hooks

These follow `kiro_home()` the same way, for every harness on the instance
(see "Shared readers" for what each harness reads before and after). An absent
folder reads as empty, as an absent host folder does today, and writers create
it under `kiro_home()`. A configured `agent.kiro_hooks_dir` still wins over the
default hooks folder and keeps its own rule that it must resolve under `$HOME`;
only the default moves.

### 4. Steering on kiro-cli

kiro-cli's global steering tree is `$KIRO_HOME/steering`. The projection says so
for every workspace view it writes (`prepare_native_skill_projection`, the
`file://<kiro_home()>/steering/**/*.md` entry). Nothing in the code shows
kiro-cli also reading the host tree, and kiro-cli resolves its own home from
`KIRO_HOME`.

**What changes.** Only the folder-steering dedup, and only on kiro-cli.

- A new accessor in `config/paths.py`, `kiro_global_steering_dir()`, returns
  `kiro_home() / "steering"`.
- A helper in `context_assembly/inclusion.py`,
  `_delivered_global_steering_root(provider_type)`, returns that tree for the
  kiro-cli label (`PROVIDER_ACP`, the positive label main already uses) and `None`
  for every other harness. It also returns `None` while the kiro home is the
  default `~/.kiro`, so with `KIRO_HOME` unset the root is the one main computes.
- `collect_folder_steering` takes the root as an argument, and
  `_double_load_roots` in `folder_steering.py` uses it. `None` keeps main's
  `resolve(home)/.kiro/steering`.
- Every place that collects folder steering passes it: the shared renderer
  `_render_folder_steering_section` in `context_assembly/inclusion.py`, which
  serves both the reinjection path (`steering_parts` in
  `context_assembly/turn.py`) and the fresh-turn path
  (`context_assembly/turn_message.py`); the member envelope in
  `context_assembly/member.py`; and the per-chat list in
  `dashboard/handlers/chat_steering.py`.
- The projection keeps its own `kiro_home` binding, because tests patch it
  through the facade mirror. A test runs the real
  `prepare_native_skill_projection` with `KIRO_HOME` set and asserts that its
  global resource equals `kiro_global_steering_dir()`. That pin, not a shared
  string, is what keeps the dedup and the projection from drifting.

**What it fixes.** The double send and the dropped rule in Problem 3. With
`KIRO_HOME` set, a kiro-cli chat now gets the `$KIRO_HOME/steering` rule once,
from the projection, and the host-tree rule once, from the folder.

**What stays the same.** `_GLOBAL_DEFAULT_RESOURCE` in
`context_assembly/agent_spec.py` (its comment is reworded to name the harnesses
it serves), the projection, `view_ownership.py`, and everything for KAS.

#### The Steering tab, member snapshot and per-chat list

These three read the host tree today: `steering_roots` in
`dashboard/handlers/steering.py`, the global scans in
`member_essential_context.py`, and `_tree_root` in `steering_plan.py`. The tab
is one list per instance, but one instance can run a kiro-cli chat and a Claude
Code chat at the same time, and the two read different global trees. So the
choice below is a product decision. With `KIRO_HOME` unset, nothing changes
under any option.

| Option | What it does | Cost | What it leaves wrong |
|---|---|---|---|
| **A. Keep the host tree** | The tab, snapshot and per-chat list stay as on main. The gap is written into the steering viewer spec and the `kiro_home()` docstring. | No code beyond the dedup. | See below. |
| **B. Follow the harness where it is known** | The per-chat list and the member snapshot know the chat's provider, so they use `$KIRO_HOME/steering` on kiro-cli and the host tree elsewhere. The tab is instance-wide and keeps the host tree. | A root argument in `list_steering_blocking`, `steering_roots` and the snapshot's global scan. Roughly 40 to 80 lines plus tests. | The tab still does not list the kiro-cli tree. |
| **C. Show both roots in the tab when they differ** | Each root is labelled with the harnesses that read it. | Largest: a new source kind, write and trust-base rules for the second root, i18n strings, frontend and tests. | Nothing known. |

A fourth option, following the default agent's harness, is rejected. It changes
what a Claude Code chat sees whenever the default agent runs on kiro-cli.

**Recommendation: A in this RFC, B as a later change.** A is the smallest,
leaves the Crew-loaded harnesses and KAS correct, and keeps the tab's `$HOME`
trust base, so the question of admitting a `$KIRO_HOME/steering` outside `$HOME`
does not arise. B is the natural next step and does not depend on anything here.

**What A leaves inconsistent, plainly.** Under a relocated `KIRO_HOME`, for a
kiro-cli chat:

- the Steering tab does not list `$KIRO_HOME/steering`, so the person cannot see
  or edit, in the tab, the global rules kiro-cli applies;
- the per-chat list marks host global files as reaching the chat through the
  harness, though kiro-cli does not load them;
- the member snapshot reads the host tree for the same reason.

For Claude Code, Codex, OpenCode, Pi and KAS these three views are right.

### 5. KAS

KAS gets no projected view and reads global steering natively. Its engine builds
the `.kiro` path from the operating system's home folder and ignores `KIRO_HOME`,
so it reads the host `~/.kiro/steering`. This was read from the engine source.
Kiro Crew cannot change it.

The dedup therefore keeps the host tree for KAS, which matches what KAS reads.
The KAS label (`PROVIDER_LABEL_KAS` in `acp/provider_labels.py`) gets the `None`
root of Section 4. `acp/kas_steering.py` does not change, except that its
comment, which says KAS's home steering folder "follows its own config dir", is
corrected. That comment is stale.

A live KAS chat with a marker file in each tree is kept as a confirmation in
Phase 0. It is not an open question.

### Effect on other harnesses

Goal: for global steering, Claude Code, Codex, OpenCode and Pi are exactly as
they are today, and so is KAS and every other harness. The MCP registry, skills,
prompts and hooks are covered separately, under "Shared readers".

Measured on a base of `6bd6d5b3ce` and on the reference implementation rebased
on `3b4e971072`. Symbols were re-read on main at `5c9bf25f3e`.

**Claude Code.** No change.

- Crew loads global steering itself, through
  `_GLOBAL_DEFAULT_RESOURCE = "file://~/.kiro/steering/**/*.md"` in
  `context_assembly/agent_spec.py`, which `steering_resource_documents` adds to
  the spec's own resources. The glob anchors at the real home in
  `_steering_glob_anchor`, so a Claude Code chat reads the host tree whatever
  `KIRO_HOME` says.
- In this design the dedup root for the `claude_code` label is `None`, which
  means the host tree (the proposed `_delivered_global_steering_root`, then
  `_double_load_roots` as on main).
- The delivered check is unchanged: `_project_steering_delivered` in `context.py`,
  through its `crew_loads_spec_steering` branch.
- Behaviour test. A decoy `HOME` holds `.kiro/steering/host.md` (marker
  MARK-HOST). A separate `KIRO_HOME` holds `steering/kh.md` (MARK-KIRO-HOME). A
  project holds `.kiro/steering/p.md` (MARK-PROJECT) and `AGENTS.md`
  (MARK-AGENTS). The folder declares the `$KIRO_HOME` tree, the host tree and a
  folder of its own (MARK-FOLDER). Crew's steering block and its folder section
  are byte-identical between base and the reference implementation, with
  `KIRO_HOME` set and unset. The same blocks went to `claude -p` (Claude Code
  2.1.327) with a request to list the markers, and in all four runs it returned
  MARK-PROJECT, MARK-HOST and MARK-AGENTS.

**Codex, OpenCode, Pi.** No change. All three are in
`ACP_BACKENDS_CREW_LOADS_SPEC_STEERING` (`agent_sdk/backends_routing.py`, read
through `crew_loads_spec_steering` in `agent_sdk/drivers/acp.py`), so they take
the same load and the same `None` root. All three also read
`<project>/AGENTS.md` themselves (`ACP_BACKENDS_NATIVE_PROJECT_AGENTS_MD`, same
file), and the load leaves that file out for them. In the reference
implementation two parametrized tests pin each of the three:
`test_crew_loaded_harnesses_still_skip_the_host_tree` and
`test_only_kiro_cli_moves_the_skipped_global_tree`.

**KAS.** No change. See Section 5. It reports `native_steering`, which is what
makes `_project_steering_delivered` true for it.

**Every other harness (Goose, DeepSeek and the rest).** No change. They match no
branch of `_project_steering_delivered`, so the dedup skips nothing, as today.

**kiro-cli.** The one change, in Section 4.

**Shared views.** The Steering tab, the member snapshot and the per-chat list's
tree verdicts are unchanged for all harnesses under option A.

#### Installs that set `KIRO_HOME` and run these harnesses

Pods keep the real `HOME` and set `KIRO_HOME=<pod home>/kiro`. The end-to-end
harness, the test seeds, the eval runs and the aws_control container also set
`KIRO_HOME`, but their `HOME` is isolated, or the harness is a stand-in for
kiro-cli, so the question of a host tree that differs from the `KIRO_HOME` tree
does not come up for them. Eval arms can run Claude Code, with an isolated `HOME`.

So only a pod can run Claude Code with the real `HOME` and a relocated
`KIRO_HOME`. A pod on Claude Code reads the operator's real global steering. This
RFC does not change that, because it would change what Claude Code sees. It is
left to a follow-up.

### Shared readers: `KIRO_HOME` applies to the whole instance

**Decision: accepted. With `KIRO_HOME` set, the MCP registry, skills, prompts
and hooks readers follow it for every harness on that instance.** `KIRO_HOME`
means the whole instance, not only its kiro-cli chats. Global steering keeps the
per-harness rule above.

Why these four areas cannot be split by harness: each one feeds a single
instance-wide artifact. The MCP registry is merged into the one authored
`kirocrew.json` (`agent_materialization/mcp_sources.py`), and every harness's
servers come from that spec. The hooks folder is folded into the same spec's
`hooks`. Skills and prompts are dashboard libraries with one list per instance,
and Crew expands a prompt into message text before any harness sees it. Keeping
the host folder for some harnesses would need a second authored spec and a
second dashboard list per instance, which main does not have.

What each harness reads, before (main at `5c9bf25f3e`) and after, with
`KIRO_HOME` set:

| Area | Harness | Before | After |
|---|---|---|---|
| MCP registry | kiro-cli | The spec's `mcpServers`, with the host `~/.kiro/settings/mcp.json` merged in. kiro-cli loads only the spec (`includeMcpJson: false`). | The spec, with `$KIRO_HOME/settings/mcp.json` merged in. |
| MCP registry | Claude Code, Codex, OpenCode, Pi | These are in `ACP_BACKENDS_SESSION_MCP_ARRAY` (`agent_sdk/backends_mcp.py`). Their servers arrive only through `session/new` `mcpServers`, translated from the same spec by `acp/session_mcp.py`, so they include the host registry's servers. The global `disabledTools` deny rules are read from the host file too (`_global_settings` in the same module). | Servers and deny rules come from `$KIRO_HOME/settings/mcp.json`. A server that exists only in the host file stops reaching these sessions. |
| MCP registry | KAS | The spec carries the host servers, and the KAS engine reads `$KIRO_HOME/settings/mcp.json` natively. `_kiro_home_servers` in `acp/kas_settings_mcp.py` bridges the two. | One file for both. `_kiro_home_servers` is removed (Section 2). |
| Skills | every harness | The dashboard's skill catalog and listing, and the `kiro-user/` keys they resolve, read the host `~/.kiro/skills`. The skill scope an agent gets already uses `kiro_home()` (`inherited_skill_globs`, `acp/kas_skill_scope.py`). On the public edition, Crew's own skill index does not read `~/.kiro/skills` (`extra_skills()` returns nothing in `platform/defaults.py`). | The dashboard lists and resolves `$KIRO_HOME/skills`, the tree the agent scope already uses. What a chat's skill scope holds does not change. |
| Prompts | every harness | The dashboard's global prompt library, the chat `@prompt` expansion and quick prompts read the host `~/.kiro/prompts`. The expanded text is what any harness receives. | All of them read `$KIRO_HOME/prompts`. A global prompt that exists only on the host stops expanding. |
| Hooks | kiro-cli | Scripts under the host `~/.kiro/hooks` are autoimported into the spec's `hooks`, which kiro-cli runs. | Autoimported from `$KIRO_HOME/hooks`. |
| Hooks | KAS, OpenCode | Crew fires the spec's hooks (`ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS` in `agent_sdk/backends_routing.py`). On KAS it also fires the standalone `*.json` hook files from the same folder (`merge_user_hook_files` in `agent_sdk/spec_hooks.py`). | Same events, from `$KIRO_HOME/hooks`. |
| Hooks | Claude Code, Codex | Crew fires the observer events only (`postToolUse`, `userPromptSubmit`, `agentSpawn`, `stop`; `ACP_BACKENDS_CREW_FIRES_SPEC_OBSERVER_HOOKS`). | Same events, from `$KIRO_HOME/hooks`. |
| Hooks | Pi | The spec's hooks do not run on Pi (`docs/system-specs/modules/agent-host-contract.md`). | No change. |

With `KIRO_HOME` unset, every row reads `~/.kiro/<leaf>` before and after.

**Migration impact for installs that set `KIRO_HOME` today.** These are the
setters in the table under Current state.

- **Pods.** A pod keeps the real `HOME`, so today every harness in a pod sees the
  operator's host MCP servers, global prompts and hook scripts. After the change
  a pod sees only what is under `<pod home>/kiro`. A pod that relied on a host
  server, prompt or hook loses it until the operator puts a copy there. This is
  the isolation pods set `KIRO_HOME` for, and it ends the pod dashboard writing
  the host registry (Problem 2).
- **Test rigs, installer smoke runs, eval arms.** Their `HOME` is isolated or
  their harness is a stand-in for kiro-cli, so the host folders are already empty
  or private to the run. No visible change beyond reading one folder instead of
  two. An eval arm on Claude Code reads its run's `KIRO_HOME`, which is under the
  same run directory.
- **The aws_control container.** Its `HOME` is the container's, so the host and
  `KIRO_HOME` folders are both inside it. Only the folder name changes.
- **A second instance, or a person who set `KIRO_HOME` by hand.** Every harness
  stops seeing the host registry, prompts and hook scripts. Anything that was
  shared on purpose must be copied into that `KIRO_HOME`, or `KIRO_HOME` pointed
  at `~/.kiro`.

The start-up log line in "Backward compatibility" names both folders when the
host still holds content, so the loss is findable.

**Rejected alternative: keep the other harnesses on the host readers.** Claude
Code, Codex, OpenCode and Pi would keep reading the host registry, prompts and
hooks, and only kiro-cli and KAS would move.

- For it: nothing a non-kiro chat sees changes on an instance with `KIRO_HOME`
  set, which matches the steering rule, and a pod running Claude Code keeps the
  operator's servers and prompts.
- Against it: the registry, the hook folder and the prompt library each feed one
  instance-wide artifact. Splitting them needs a second authored spec (or a
  per-harness merge at spawn), and a dashboard that shows two registries and two
  prompt libraries and says which harness each edit reaches. The pod dashboard
  would keep writing the host registry for those harnesses, which is Problem 2
  again. `KIRO_HOME` would mean "the kiro-cli part of the instance", a rule that
  is harder to state and to test.
- Rejected, because `KIRO_HOME` should name one instance and a harness choice
  should not change which MCP servers, prompts and hooks that instance has.

## Known limits

- **KAS ignores `KIRO_HOME` for global steering.** Its engine reads the host
  tree. Kiro Crew cannot change that, so on KAS the host tree stays the delivered
  tree. The engine does read `settings/mcp.json` under `KIRO_HOME` (#36145) and
  `acp/kas_skill_scope.py` already uses `kiro_home()` for skills. Hooks on KAS
  are fired by Crew, so they follow Crew's folder. Whether the engine reads any
  prompts of its own from under `KIRO_HOME` was not checked; the prompts Crew
  expands are covered by "Shared readers".
- **The Steering tab gap on kiro-cli.** Option A in Section 4 leaves the tab, the
  member snapshot and the per-chat list on the host tree. For a kiro-cli chat
  under a relocated `KIRO_HOME` they do not show the tree kiro-cli loads.
- **Pods on Claude Code** read the operator's real global steering. Unchanged.
- **Display labels.** `_KIRO_GLOBAL_SURFACE` (`"~/.kiro/settings/mcp.json"`) and a
  few user-facing strings name the host path. They are wrong under `KIRO_HOME`.
  Follow-up.
- **Windows.** The `file://` spelling of an absolute `kiro_home()` entry on Windows
  is built in the projection today and is reused as is. Not re-checked here.

## Migration plan

Phases are separate pull requests. Each can ship and be reverted on its own.

**Phase 0: decisions and a confirmation.** This RFC accepted, including the MCP
ruling and the Steering tab option. (The shared-readers question is already
decided; see "Shared readers".) One live
KAS chat, with a marker file in each of `$KIRO_HOME/steering` and
`~/.kiro/steering`, confirms that KAS loads only the host marker. The result is
written here. The source reading in Section 5 is the basis of the design, and the
probe does not gate it.
*Exit:* the Status line is `accepted`; the Steering tab option is in this
document; the KAS probe result is recorded.

**Phase 1: accessors and the MCP registry.** Add the accessors. Move every MCP
registry reader and writer, the lock, the discovery scope, the bridges and the
scrub. Delete `_kiro_home_servers` and its test seam. Rewrite the `kiro_home()`
docstring and the `mcp_agent_sync.py` comment to say what is true.
*Exit:* with `HOME` pointing at a decoy folder that holds a different registry and
`KIRO_HOME` at another, a dashboard edit, the spec merge, the session settings
and a bridge registration read and write only the `KIRO_HOME` file, and the lock
file is beside it. The `session/new` `mcpServers` built for a Claude Code session
names only the `KIRO_HOME` registry's servers. With `KIRO_HOME` unset, the same
test reads and writes `~/.kiro/settings/mcp.json` as before.

**Phase 2: skills, prompts, hooks.**
*Exit:* the same decoy-home test for each subtree. For hooks, the autoimported
scripts in the rendered spec and the KAS standalone hook files come from
`$KIRO_HOME/hooks`. For prompts, a chat `@prompt` and a quick prompt expand from
`$KIRO_HOME/prompts` on a kiro-cli session and on a Claude Code session.

**Phase 3: kiro-cli steering dedup.** The `kiro_global_steering_dir()` accessor,
the provider-dependent root, the folder-steering callers, the projection pin test, and the
docs for option A. It is not blocked: it needs only the option A decision. Option
B, if chosen, is a later pull request.
*Exit:* on a kiro-cli chat with `KIRO_HOME` set, the dedup skip root equals the
global resource the real projection writes, and a folder document under each tree
reaches the chat once. On each of Claude Code, Codex, OpenCode, Pi and KAS, the
dedup root equals today's value and the Crew steering block is byte-identical to
base.

**Phase 4: docs.** Update the specs the touched rows name (the MCP architecture
doc, the memory-skills-hooks and providers specs, the steering viewer spec with
the option A gap, and the packaged steering-and-hooks guide) in the same pull
request as each phase, not after. This phase is the
check that none was missed.

Follow-ups, each its own pull request after this RFC is accepted:

- **Bundled mochi app.** `_configured_server_names` reads the host registry.
  Mochi is pinned in `apps/adoption-pins.json`, so the fix lands in its app
  repository first and the core pull request names it
  (`App repo: <owner>/<repo>@<sha>`), per AGENTS.md. Until then it keeps
  reading the host file, which is the same split on one app.
- **`~/.kiro/crew-auth-staging`.** A Kiro Crew credential leaf that sits inside the
  host `~/.kiro`. It is spelled that way in the identity store
  (`kiro_prerequisite_identity_store.py`) and in the sandbox fences
  (`sandbox_tiers.py`, `security/paths_lists.py`). It is not a kiro-cli reader,
  and what the sandbox seals is the operator's decision. This RFC does not move
  or re-fence it. Whether a pod with `KIRO_HOME` set needs its own staging
  folder is a separate question.
- **Display labels** such as `_KIRO_GLOBAL_SURFACE`.
- **Pod boot notes.** `pod/runtime_boot.py` lists `kirocrew app` as refused
  because it edits the host registry. After Phase 1 that reason is gone for
  `KIRO_HOME` pods. Whether to allow the verb is a separate decision.

## How it is tested

- **Decoy home.** Each phase has a test that sets `HOME` to a folder holding a
  decoy copy of the subtree, sets `KIRO_HOME` to another, and asserts which
  file each reader opens and each writer creates. The test must fail on main
  for the right reason: the reader opened the decoy. It must not set the
  override hooks to `None`, which fails on a missing attribute and proves
  nothing.
- **`KIRO_HOME` unset.** Each accessor returns the same path as the old
  expression, compared against the old expression.
- **Call time.** One test changes `KIRO_HOME` after import and asserts the next
  call follows.
- **Lock and file together.** A test that changes `KIRO_HOME` between two
  operations asserts that the lock and the file of one operation are in the same
  folder.
- **Sweep.** A ratchet that fails on a new `Path.home() / ".kiro" / <subtree>` for
  the five subtrees outside an allowlist, so a new reader cannot reintroduce the
  split.
- **Steering.** For kiro-cli, KAS, each of the Crew-loaded harnesses, and one
  harness with no steering path, the dedup root equals the root that provider
  delivers. A projection pin runs the real `prepare_native_skill_projection`
  and compares its global resource with `kiro_global_steering_dir()`. On the base,
  the kiro-cli turn tests fail for the behaviour (a host-tree rule missing from
  the folder section), and the Crew-loaded cases pass, which shows they are
  unchanged.
- **Claude Code.** The marker test in "Effect on other harnesses", run with
  `claude -p`, with `KIRO_HOME` set and unset.
- **Shared readers on other harnesses.** For Claude Code and Codex, a decoy-home
  test asserts that the session's `mcpServers` and deny rules come from the
  `KIRO_HOME` registry, and that an observer hook from `$KIRO_HOME/hooks` fires.
- **Real machine.** One pod run (the pod sets `KIRO_HOME` itself): add an MCP
  server in the dashboard, check the file under the pod's `kiro` folder changed
  and the host file did not, and a kiro-cli chat lists the server. Run it on main
  first to show the host file changed.

## Backward compatibility

With `KIRO_HOME` unset: no change. Every accessor returns `~/.kiro/<leaf>`.

With `KIRO_HOME` set, an install changes in these ways:

| Install | Today | After |
|---|---|---|
| Pod (Dev Fleet) | Spec merge, skills, prompts and the dashboard read the **host** `~/.kiro`. kiro-cli reads the pod's `kiro` folder. | All read the pod's `kiro` folder. A pod that relied on the host's MCP servers, skills or prompts loses them unless they are put in `<pod home>/kiro`. |
| Test rig or installer smoke run | Same split. | Same as a pod. The folders are usually empty, so these runs see fewer inputs and are more repeatable. |
| A second instance that sets its own `KIRO_HOME` | Its spec merge, skills and prompts read the host copy it shares with the first instance. | Isolated. Anything it shared must be put in its own `KIRO_HOME`. |
| A person who set `KIRO_HOME` by hand | The dashboard edits the host copy, which kiro-cli does not read. | The dashboard edits the file kiro-cli reads. Host-only content stops appearing. |

On kiro-cli, a folder document under `$KIRO_HOME/steering` is no longer sent
twice, and one under the host `~/.kiro/steering` is now sent once.

The table holds for every harness on the instance: a Claude Code, Codex,
OpenCode, Pi or KAS chat loses host-only MCP servers, prompts and hook scripts
the same way a kiro-cli chat does ("Shared readers" has the per-harness detail).
Their global steering does not change.

Nothing is copied or deleted. The old files stay where they are. Anyone who wants
the host's content under `KIRO_HOME` can point `KIRO_HOME` at `~/.kiro` or copy the
files themselves.

How a person would notice: servers, skills or prompts that came from the host stop
appearing in the dashboard lists and in the rendered spec. To make that findable,
the gateway logs one line at start when `KIRO_HOME` is set and the host folder
still holds content in one of the five subtrees. The line names both folders. It
is a log line only. A `kirocrew doctor` line is not proposed (see Open questions).
The release PR carries the changelog entry, as `CHANGELOG.md` rules require.

## Rollback

Each phase reverts with `git revert`. Readers go back to the host folder. Files
written under `$KIRO_HOME` while a phase was live stay there. Kiro-cli and KAS
still read them. Kiro Crew's own readers stop showing them after the revert.
Nothing is lost.

An operator can also avoid the change without a revert: `export KIRO_HOME=~/.kiro`.
Everything then agrees on the host folder.

## Is this a one-way door?

No. By the CONTRIBUTING.md definition it is a two-way door. It adds no persisted
format and no config key. It moves no data and deletes none. A `git revert`
restores the old read locations.

It is still large, for two other reasons in the same list. It changes what
`KIRO_HOME` means for every install that sets it, which other code and people
build around. And a maintainer has to decide the MCP registry question, which is
a product-shape choice. The one soft edge is the one in Rollback: edits made
while the change was live stay in the new place.

## Security considerations

- **Direction.** The change makes Kiro Crew read and write fewer places, not more.
  A pod's dashboard no longer writes the host registry.
- **Same fence.** The write-protection tier already covers
  `$KIRO_HOME/settings/mcp.json` and `$KIRO_HOME/agents`
  (`security/paths_targets.py`). The change moves readers to the file that is
  already fenced. It adds no seal, mask or refusal, and it does not widen or
  narrow the sandbox.
- **Trust base.** Under option A the Steering tab's root does not move, so its
  `$HOME` trust base is unchanged. Under option C a second root would need its
  own trust-base rule. The dedup only compares paths and reads nothing new.
  `kiro_home()` already refuses a system directory and falls back to `~/.kiro`.
- **Registry `autoApprove`.** With no host merge, a host server's `autoApprove`
  entry cannot reach a pod's spec, on any harness. With Alternative A it can.
- **Hooks.** A hook script runs with the user's rights. After the change, the
  default hooks folder is the one under `KIRO_HOME`, an operator-set environment
  value rather than the agent-writable `config.json`. The `agent.kiro_hooks_dir`
  override keeps its `$HOME` containment rule.
- **Credentials crossing a boundary.** None changes. The token and lock files stay
  where they are. The change does not touch `crew-auth-staging`.

## Alternatives considered

### A. Host copy as a merge input (the other answer to #12986)

With `KIRO_HOME` set, Kiro Crew writes `$KIRO_HOME/settings/mcp.json` and reads the
host registry as an extra source into the specs it renders, as #36145 does for KAS
exclusions. This is what the `main` docs describe for the host file: a merge
input, resolved from the real home.

For it:

- A pod or second instance keeps every server it loads today. Smaller change for
  existing installs.
- A person who set `KIRO_HOME` by hand and still wants the host's servers keeps them.

Against it:

- It is not isolation. A pod reads the host's servers, their environment values
  and their `autoApprove`, which skips the tool gate.
- kiro-cli and KAS read only `$KIRO_HOME`. The rendered spec would name servers
  that the native read never sees, so the three sources disagree in a new way.
- The dashboard would show two scopes and must say which one an edit writes and
  which a delete can touch. Precedence on a shared name needs a rule (today
  `_shared_entry` in `acp/kas_settings_mcp.py` keeps one file's `disabledTools`).
- `_kiro_home_servers` stays live, with its second read.
- A rig that merges host state is not repeatable across machines.

Rejected, because the point of setting `KIRO_HOME` is to stop sharing the host's
directory. A person who wants host servers in a pod can put them in
`<pod home>/kiro/settings/mcp.json`.

### B. One-time copy of the host subtrees at first use

Suggested on #9797 and #9816. It copies host steering, skills, prompts, hooks
and the registry into `$KIRO_HOME` once. Rejected: it copies `autoApprove` entries
and server environment values into a new place without anyone choosing that, and
it needs a marker for "already copied", which is new persisted state.

### C. Leave it and document it

Keep the `kiro_home()` docstring and fix the stale parts. This is cheap and it
leaves the split in place: an edit in the dashboard that kiro-cli ignores, and a
dedup that sends a document twice. Rejected as the goal, though Phase 1's
docstring rewrite is needed under every option.

### D. Route only the skills, hooks, steering and prompts readers

#12986's own triage warned against it: four subtrees would move and the registry
would not. That is a worse split than today's. Rejected.

### E. Keep the other harnesses on the host readers

Move the MCP registry, skills, prompts and hooks readers for kiro-cli and KAS
only, and keep Claude Code, Codex, OpenCode and Pi on the host folders.
Rejected; the trade-offs are under "Shared readers" in the Design.

## Open questions

1. **The MCP ruling.** This RFC proposes "follows `KIRO_HOME`, no host merge".
   A maintainer decides. If Alternative A is chosen, Sections 1 to 3 still apply,
   with the registry reader reading two files and `_kiro_home_servers` kept.
2. **The Steering tab.** A, B or C in Section 4. This RFC recommends A now and
   B later. A maintainer decides.
3. **KAS engine prompts.** Global steering is settled from the engine source, and
   KAS hooks are fired by Crew. Whether the KAS engine itself reads prompts from
   a home folder was not checked. Sections 2 and 3 rely on the engine only for
   the registry, which it reads under `KIRO_HOME` (#36145).
4. **The legacy scrub.** An older version wrote some app entries into the host
   registry. Following `KIRO_HOME` leaves them there. Should the scrub also run
   once against the host file? That writes the host file from a relocated
   instance, which is what this RFC removes elsewhere. The proposal is no.
5. **A notice beyond the log line.** Is a `kirocrew doctor` line wanted, such as the
   "host home still holds" line that #9797 proposed and that never shipped? Not
   proposed here to keep the change small.
6. **An unsafe `KIRO_HOME`.** `kiro_home()` falls back to `~/.kiro` when the value
   names a system directory. kiro-cli's own handling of the same value is not
   verified here. `kiro_cli_workspace_trust._kiro_home` mirrors kiro-cli's rule
   and ignores a relative override, while `kiro_home()` resolves one against the
   working directory. The case is rare. It is listed so the implementation does
   not assume the two agree.
7. **`kiro_home()` resolves links, and the old paths did not.** With `KIRO_HOME`
   set, the accessors return a resolved path. For a path shown to the person, keep
   the spelling the person set, or the resolved one? The dedup compares resolved
   paths either way.
