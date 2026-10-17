---
title: Mate-owned agent config — a copied custom agent in its own folder
status: draft
author: gjjnn
created: 2026-10-16
last-audited: 2026-10-16
audited-at: 0600e2804476
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: Mate-owned agent config — a copied custom agent in its own folder

- Status: draft — a proposal, not a description of what the code does today.
  "Exists today" claims cite symbols verified against the working tree at
  `0600e2804476`; re-verify at implementation time.
- Author: gjjnn
- Related: [rfc-crewmates-launch.md](rfc-crewmates-launch.md) (the product
  vocabulary — "crewmate", "custom agent", "built from" — kept separate from the
  code keys this RFC uses).
- Grounding: the design below was exercised by an implementation spike against a
  live pod. Where the spike corrected an earlier reading, the correction is
  called out inline (search "spike"). The spike proved the resolution mechanism
  and the create/edit paths live; the one path still awaiting live confirmation
  is the in-place capability SAVE (§5.4, built and offline-proven, pending a pod
  it can run against).

## 1. Summary

A crewmate's binding record (`agents.<name>` in `config.json`) carries a
`kiro_agent` field that *points at* a shared custom agent in `~/.kiro/agents/`.
Everything that decides how the crewmate behaves — prompt, tools, MCP servers,
skills — lives on that shared file, so editing "the crewmate's tools" edits the
shared agent and the change reaches every crewmate pointing at it.

**A mate is two source-defined agents — a conductor and a worker — plus one
user-editable config file that extends the worker.** This is a restructure of
the mates feature that already exists, not a new concept: the conductor and
worker are how today's mate behaviour is re-expressed, with its definition moved
into source code. The conductor the user chats with has its system prompt,
tools, MCP servers and skills defined 100% in Crew's source code; it delegates
tasks to a worker whose system prompt, tools, MCP servers and skills are *also*
defined 100% in source — that full set is the worker's base. Neither base is
user-editable. The one thing a user can change is the worker's **additional** MCP
servers and skills, and those live in a per-mate file Crew owns:

```
~/.kiro/mates/agents/<id>.json
```

To the user this still reads as "edit what my mate can do." Under the hood the
edit targets the delegated **worker**, and it is *additive*: the worker runs its
source-defined base plus whatever `mcpServers` and `skills` this file adds.

This RFC gives each crewmate that file, written at creation time into a new
folder on purpose. It is a custom-agent file in the **exact format already
written to `~/.kiro/agents/`** — no new schema. In this design only its
`mcpServers` and `skills` fields are load-bearing (they extend the worker);
every other field (`prompt`, `tools`, `model`, `permissions`) is written with a
default/placeholder value, reserved so a future design may use it without a
schema change. The folder relocation is the mechanism: it gives the mate a
config Crew owns and can edit directly, without touching any source and without
adding the file to the user-level agent catalog the session agent picker reads.

Crewmate-specific data (identity, avatar, memory store, workspace, model) does
**not** move into this file; it stays on the binding record, where it already
lives.

## 2. Motivation

### Current state

- A custom agent is a Kiro-wide config file (`prompt`, `tools`, `mcpServers`,
  `skills`, `model`) under the user-level agents directory. That directory is
  single-valued and is both the read scope and the write target:
  `kiro_agents_dir()` in `config/paths.py` documents it as the write target as
  well as the user-level read scope, and `list_agents()`
  (`agent_discovery.py`) scans it (plus a project-local read-only scope) for the
  agents selectable via `session/set_mode`.
- A crewmate's binding is a `KiroCrewAgentConfig` record (`config/sections.py`).
  Its crewmate-specific fields — `member_id`, `kiro_agent`, `workspace`,
  `memory_store`, `model`, `avatar`, `description`, `triggers`, `session_color`,
  `starred`, `source` — already live on the binding. `kiro_agent` is the pointer
  to a custom agent (its docstring: "Kiro agent name (modeId for
  session/set_mode)").
- Crew projects a config into KAS's `_meta.kiro.customAgents` array on
  `session/new`; `set_mode` then activates it. Crew does not pass a file path —
  it hands over the agent definition on the wire.
- A copy mechanism already exists, but it is **copy-on-write and lazy**:
  `api_agent_fork` (`dashboard/agent_admin/fork_publish.py`) forks a private
  copy on the *first edit*, names it `crew-<hex>`, writes it into the user-level
  agents directory, records `forked_from`/`private_to` lineage in an
  `agent_state` sidecar (the spec itself can't carry lineage — kiro-cli rejects
  unknown fields and drops the whole agent), hides it from the shared catalog,
  and rebinds. A reset re-points to the origin and deletes the copy. This fork
  then runs an **inheritance + review lifecycle** — the copy stays a child that
  tracks its parent — described in §9a.

### Problems

1. **Edits leak.** Several crewmates on one shared agent all see any edit to it.
2. **The edit path is confusing by construction.** "Edit the crewmate's skills"
   mechanically means editing the shared agent it depends on, so a crewmate-config
   editor can't be offered without that editor silently changing other crewmates.
3. **The lazy copy carries a live link back.** The fork's lineage plus
   reset-to-origin keeps a crewmate's config tethered to a shared parent until a
   reset severs it — the opposite of ownership.
4. **The copy lands in the shared catalog.** Because the fork writes into the
   same single-valued directory the picker reads, the private copy must then be
   *hidden* from the picker by a lineage filter.

## 3. Goals

- A mate owns its editable config outright: adding an MCP server or a skill to
  one mate's file affects only that mate's worker, no other mate and no source.
- The conductor and the worker's base — system prompt, tools, MCP servers AND
  skills — stay 100% source-defined and are not user-editable; the disk file is
  strictly *additional* config layered on top of the worker's source base.
- Reuse the existing custom-agent file format and its whole projection +
  governance pipeline unchanged — no new schema, no field split, no merge step.
  Only `mcpServers` and `skills` are load-bearing now; the rest carry defaults.
- No live link back to a source: the file is a stamp, not a parent.
- The mate's config is **not** offered in the session agent picker.
- Crewmate-specific data stays on the binding, as today.

## 4. Non-goals

- **Message-array control.** This still projects a config into a KAS payload, so
  it inherits that mechanism's limits — no mid-session prompt change without a
  rebuild, no dynamic tool registration, nothing the payload schema can't
  express. Out of scope.
- **A shared/importable crewmate-definition format.** Copying severs sharing on
  purpose (§9); a sharing strategy is a separate effort.
- **Renaming code identifiers.** `kiro_agent`, `agents.<name>`, and the
  custom-agent format keep their names.
- **A data-model change for `member_id`.** The spike considered whether
  `member_id`'s empty-string-means-"not a member" sentinel should become a real
  nullable. Decision: leave it. `""`-is-unset is the house convention across the
  whole `KiroCrewAgentConfig` dataclass (`model`, `reasoning_effort`,
  `display_name`, `approval_mode` … all default `""`), and "is this mate-owned
  config?" is a DERIVED fact (does `kiro_agent` resolve under
  `mates/agents/`), never a stored flag — an `is_mate` boolean would be the
  actual mistake, able to drift from where the file lives.

## 5. Design

### 5.1 The worker-extension config file

The file is a custom-agent JSON file — the same schema written to
`~/.kiro/agents/` today, no new fields — placed in a dedicated folder:

```
~/.kiro/mates/agents/<id>.json
```

It carries the full custom-agent shape (`prompt`, `tools` / `allowedTools`,
`mcpServers`, `skills`, `model`, `permissions`), but **in this design only
`mcpServers` and `skills` are load-bearing**: they are the user's *additions* to
the delegated worker. Every other field is written with a default/placeholder
value — the schema is kept whole on purpose so a future design can start reading,
say, `model` or `permissions` without a format migration. The worker's own
system prompt and tools come from Crew's source code, not from this file's
`prompt`/`tools`; those fields staying default is what makes the base
non-editable while the additions are editable.

**The folder is machine-wide, alongside `~/.kiro/agents/` — a settled team
decision.** It sits under the Kiro home (`kiro_home()`), not under a per-instance
`KIROCREW_HOME`, so the mate config files live beside the custom agents they are
copied from rather than inside one Crew instance's data home. Because the folder
is shared across instances, `<id>` alone is NOT a safe partition: `<id>` is the
crewmate's `member_id`, and `memory_stores._allocate_member_id` only uniquifies it
within one instance's config, so two instances that each create a mate of the same
name produce the same `<id>` and resolve to the same `<id>.json`. The machine-wide
location therefore ships WITH a cross-instance write guard — the shared-folder rule
the repo already uses for `~/.kiro/agents/`
(`agent._decline_shared_agent_home` / `shared_kiro_agents_writable`), an
owner-identity field in the file, or a genuinely unique id — chosen in step 3,
which is the only step that writes this file (§6, §10). Creation writes the file
atomically (`0o600`); the guard is what prevents one instance from silently
overwriting another's additions, not the id by itself.

### 5.2 Crewmate-specific data stays on the binding

`KiroCrewAgentConfig` (`config/sections.py`) is unchanged. It keeps carrying
`member_id`, `kiro_agent`, `workspace`, `memory_store`, `model`, `avatar`,
`description`, `triggers`, `session_color`, `starred`, `source`. Only what
`kiro_agent` resolves to changes — the file in `mates/agents/` instead of one in
`~/.kiro/agents/`:

```
"zxc": {
  "member_id":    "zxc",
  "kiro_agent":   "zxc",            // resolves to ~/.kiro/mates/agents/zxc.json
  "workspace":    "member-zxc",
  "memory_store": "member-zxc-<uuid>",
  "source":       "kirocrew"
}
```

`member_id` is the immutable member identity (it is also the key the member's V2
memory store is owned by — `memory_stores.py` fails closed when
`owner_member_id != member_id`), not a boolean. The convention that identifies a
mate-owned binding is: **`member_id` is non-empty AND `kiro_agent == <the
member's own id>`, resolving to a file in `mates/agents/`.** A member whose
`kiro_agent` still names a shared template (e.g. the built-in `mate` member on
`kirocrew`) is a crewmate without mate-owned config — the two facts are
orthogonal.

### 5.3 Binding resolution reads both folders — and there are FIVE readers, not two

`kiro_agent` must resolve to the crewmate file, or binding resolution sees a
dangling pointer and heals it to the default agent. But the crewmate must not
appear in the session agent picker. The folder split answers both without a
lineage flag or a hiding filter:

| Reader | Reads | Crewmates visible? |
|---|---|---|
| session agent picker ("pick an agent for a scratch session") | `~/.kiro/agents/` only | No |
| binding resolution ("does `kiro_agent` name a real config?") | `~/.kiro/agents/` **and** `~/.kiro/mates/agents/` | Yes |

The folder *is* the privacy boundary. The existing fork's lineage and
catalog-hiding filter become unnecessary: a crewmate's config is excluded from
the picker because it lives in a folder the picker does not read, and resolved
by the binding because resolution reads both.

**Spike correction — "binding resolution" is not one reader, it is five.** An
earlier reading of this RFC named two readers. The live spike found that
"resolve `kiro_agent` → spec file" is implemented at **five** distinct sites,
three of which the two-folder table did not enumerate. Each must search
user-level then `mate_agents_dir()`:

| # | Reader | Surface | In the original reading? |
|---|---|---|---|
| 1 | `acp/harness/kas.py::resolve_projected_spec` | session projection | yes |
| 2 | `config/loader.py::refresh_materialized_agents` | declared-agent index | implied, but load-bearing |
| 3 | `member_essential_context.py::resolve_template_path` | crewmate startup context | **no** |
| 4 | `agent_capabilities.py::_source` | Capabilities editor LOAD | **no** |
| 5 | `dashboard/agent_admin/agent_detail.py::_agent_detail_candidates` | "Built from" pane (GET+PATCH) | **no** |

Reader #2 is the one that prevents the dangling-pointer self-heal: unless the
materialized-agent index unions `mate_agents_dir()`, a binding `kiro_agent=<id>`
reads as undeclared and the heal resets it to the default.

Normative rule this RFC adopts: **every site that maps a binding's `kiro_agent`
to a spec file MUST search user-level then `mate_agents_dir()`; a new site is
registered against a guard test so a sixth resolver added later cannot silently
skip the mate scope** (the guard test is T1's deliverable in the migration plan).
"Resolution reads both folders" is correct — it just has five mouths, not one.

`list_agents()` is already two-scope (user-level plus project-local), so adding
a crewmate read scope to *resolution* extends an existing pattern — but the
picker's scope stays user-level-only.

### 5.4 Execution paths

**Create (eager copy)** — the file is written at creation, not on first edit,
even when it starts effectively empty:

```
name the crewmate
→ write ~/.kiro/mates/agents/<id>.json                (atomic, 0o600)
   with the full custom-agent schema: mcpServers = [] and skills = [] (the
   user's additions, empty to start), every other field a default/placeholder
→ write the binding (kiro_agent = <id>, crewmate-specific fields set fresh)
→ grant the mate's own additions (see below) by editing that owned file IN PLACE
```

The file is deliberately created even with no user additions yet, so it is
present and ready the first time a user adds a skill or MCP server. There is no
"copy a source agent's payload" step any more: the conductor and the worker base
are source-defined, so nothing is copied FROM — the file holds only the worker's
*additions*. No shared-pointer phase, no first-edit fork, no lineage sidecar.

**Spike correction — any create-time addition and the eager copy must be
ordered, and the addition must not fork.** If create needs to seed the worker
file with a default addition (for example the `kirocrew-mate` MCP server), it is
written INTO `mates/agents/<id>.json` after the file exists, not minted as a
fork. The pre-spike path drove the capability editor with `put(enroll=True)`,
which **forked** the crewmate into a `crew-<hex>` draft and repointed the binding
OFF the mate id — undoing the eager copy. The create flow therefore MUST run
**eager copy first, any addition second**, and the addition MUST edit
`mates/agents/<id>.json` directly (append to `mcpServers` / `skills` in place),
never fork. If the eager copy did not land, the step leaves the binding alone
rather than minting a fork. This is the Phase-3 "retire the fork" work pulled
into Phase 1, because it is not deferrable: the fork actively breaks the eager
copy (see §6, §9a).

**Session start:**

```
crewmate chat opens; binding has kiro_agent = <id>
→ resolve binding → member_id
→ conductor: source-defined system prompt + tools + skills + mcpServers → session/new
   (the user chats with the conductor; nothing from the file reaches it)
→ when the conductor delegates, the WORKER is projected:
   source-defined worker base = prompt + tools + mcpServers + skills
   + resolve kiro_agent: look in ~/.kiro/agents/ then ~/.kiro/mates/agents/
     └ found in mates/agents/ → read its mcpServers + skills → ADD to the
       worker's base → project to KAS customAgents → apply security ceiling
```

The worker's effective config is `source base ∪ (file's mcpServers + skills)`.
The projection and the governance ceiling are the existing ones — the file is
the same shape they already handle, so adding its two lists to the worker's base
needs no new projection path, only the union at delegation time. The ceiling
still applies to the merged result, so a user-added server or skill cannot lift
the worker above its governed grants.

**Edit — in place, never forked (the critical correction).** Editing a
crewmate's capabilities writes `mates/agents/<id>.json` directly: read the file,
apply the editor's operations, pin `name=<id>`, run the governance sanitizer,
write in place. The binding is never repointed and no `crew-<hex>` draft is ever
minted. None of the fork/inheritance/review lifecycle (§9a) applies, because
there is no parent to track.

This is a genuinely new edit path, not a destination swap: the existing
Capabilities editor **forks any binding on first edit** (the frontend pane runs
`agentFork` before `agentPatch`, calling `api_agent_fork`). So "edit in place"
requires, on both layers:

- **Backend:** when the binding resolves to a mate, bypass the
  fork/`enroll`/intent machinery and write the owned file directly;
  `api_agent_fork` and `put(enroll=True)` refuse a mate binding (no `crew-`
  draft, no lineage). The only guard kept is a plain optimistic lock
  (a `stale_revision`-style digest of the file) so two tabs editing the same
  mate file cannot clobber each other — that defends concurrent access, not
  parentage.
- **Frontend:** the pane does not call `agentFork` for a mate binding, and it
  suppresses the parent / "Follow the parent template" / "Review changes"
  lifecycle UI (there is no parent to show); the capability view reports a
  `mode: "mate"` the pane renders as an owned, in-place editor.

**Subagents** — a crewmate can spawn helper agents; a helper has no `member_id`,
so it resolves by name through the ordinary spawn path, unchanged.

### 5.5 Why a separate folder, not `~/.kiro/agents/`

A copy in `~/.kiro/agents/` re-enters the custom-agent catalog: picker-visible
(so it needs the lineage-hiding filter), rewritten on gateway start by the
managed-spec rebuild that writes into that same directory, and inside kiro-cli's
file lifecycle. A copy in `~/.kiro/mates/agents/` is Crew-owned, excluded
from the picker by location, and outside that lifecycle — while still being the
identical file format, so the projection pipeline needs no change.

## 6. Migration plan

Three independently shippable steps, each its own PR landing something testable.
Steps 1 and 2 are **behaviour-preserving** — they move the conductor and worker
onto source definitions without changing what a mate can do — so neither reads or
writes `~/.kiro/mates/agents/` and neither can leak config between instances.
Step 3 is the only one that introduces the file, and therefore the only one that
carries the user-facing capability (configurable worker additions) and the
cross-instance write question (§8).

### Step 1 — the conductor is source-defined

Take the mate conductor (the agent the user chats with) as it behaves in today's
mates feature and give its identity and whole definition — system prompt, tools,
MCP servers, skills — from Crew's source code instead of a file on disk. This is
a sourcing move on the existing conductor behaviour, not a new agent. Nothing
else moves.

Exit criteria:
- A mate's conductor behaves identically to today; only its definition source
  changed (source code, not a disk agent file).
- No conductor path reads or writes `~/.kiro/mates/agents/`.
- No user-visible behaviour change (this is a pure sourcing move).

### Step 2 — the delegated worker is source-defined

Make the worker the conductor delegates to — the one that does today's mates work
under the hood — start from a source-defined base: system prompt, tools, MCP
servers AND skills, all from source code. Still no file on disk; the worker's
whole config is its source base. Like step 1, this re-expresses existing mate
behaviour from source, adding no new capability.

Exit criteria:
- Delegating to the worker projects exactly its source-defined base.
- No worker path reads `~/.kiro/mates/agents/` yet.
- No user-visible behaviour change beyond the worker now existing as a
  source-defined delegate.

### Step 3 — the worker reads its per-mate file as *additional* config

Now introduce `~/.kiro/mates/agents/<id>.json`. It is eager-created when a mate is
created (full schema, empty `mcpServers`/`skills`, defaults elsewhere — §5.1), and
at delegation the worker's effective config becomes `source base ∪ (file's
mcpServers + skills)` (§5.4). This is the step that adds the user-facing
capability ("edit what my mate can do"), the folder resolution (§5.3), the
in-place edit path (§5.4), and the create-time eager copy. The governance ceiling
(§8) applies to the merged result.

**The cross-instance write guard is settled in THIS step, not before.** Because
only step 3 writes the machine-wide file, the leak the Design lane flagged —
`member_id` is the per-instance display-name slug, not a globally unique id, so
two instances that each make a mate of the same name resolve to the same
`<id>.json` — cannot occur until this step, and this step's PR owns the fix. The
repo already has the pattern: `agent._decline_shared_agent_home` /
`shared_kiro_agents_writable` refuse writes to the shared `~/.kiro/agents` folder
from a non-default data home, and the same rule (or an owner-identity field in the
file, or a genuinely unique id) resolves it here. The exact mechanism is an open
question for step 3's design (§10), not for steps 1–2.

Exit criteria:
- A newly created mate has a file in `~/.kiro/mates/agents/`, and a delegated
  worker projects its source base plus the file's `mcpServers` + `skills`.
- Adding a skill or MCP server writes only that mate's file; no other mate's file
  and no source is touched.
- A mate is **not** offered in the session agent picker (folder split, §5.3).
- No `crew-*.json` is written and `agent_state` records no `forked_from`: edits
  are in place, never forked (§5.4).
- The cross-instance write guard is in place: an instance with a non-default data
  home cannot clobber another instance's `<id>.json` (mechanism chosen per §10).

### Migrating existing crewmates (within step 3)

Existing crewmates move when step 3 lands. The conductor and worker base are
source-defined by then, so migration copies no prompt or tools; it carries over
only what the file may hold — the user's **additions**. For a crewmate whose
`kiro_agent` points at a shared `~/.kiro/agents/` agent, those are whatever
`mcpServers`/`skills` that agent carried beyond the worker's source base; a
crewmate with nothing extra gets an empty file. Leave the shared agent in place.

Prove it before flipping pointers:

```
for each crewmate:
  additions = (shared agent's mcpServers + skills) minus the worker's source base
  write ~/.kiro/mates/agents/<id>.json  with those additions, defaults elsewhere
  after = project(worker source base ∪ file additions)
  assert after grants exactly: source base + the carried-over additions
  (only then) repoint kiro_agent
```

Exit criteria:
- Every existing crewmate has a file and its binding points at it; identity,
  memory, workspace and open chats are untouched.
- Each migrated worker grants its source base plus exactly the additions carried
  over — no capability silently gained or lost.

### The fork lifecycle, retired across the steps

Today's lazy copy-on-first-edit fork (`api_agent_fork`,
`_unlink_copy_unless_referenced`, the `agent_state` lineage sidecar, §9a) exists
for a mate only because editing a mate forks it. Step 3 removes the mate path's
dependence on it (edits write the file in place, never fork). Any **non-crewmate**
caller that still depends on the fork is a separate audit: enumerate its callers
and either remove the path once none remains or scope it to the survivors. That
audit does not gate steps 1–2 and is not blocking for step 3's own exit criteria.

## 7. Backward compatibility

- **Custom agents in `~/.kiro/agents/` are untouched.** This changes only where a
  *crewmate's* config lives.
- **kiro-cli unaffected** — a crewmate file never enters its machinery (it reads
  `~/.kiro/agents/`, not `~/.kiro/mates/agents/`). Crewmate derivation is a
  KAS wire payload, as today.
- **Session-picked custom agents are a no-op** — they already run from their
  `~/.kiro/agents/` file, and the picker still reads only that directory.
- **Existing crewmates** are migrated in Phase 2 rather than reinterpreted; their
  identity and memory are preserved and the dry-run proves each migrated worker
  grants its source base plus exactly the additions carried over.

## 8. Security considerations

- The crewmate file is user-editable, but the **governance ceiling is applied at
  projection** — the same ceiling a custom-agent payload passes through — so a
  hand-edited crewmate file cannot escalate the crewmate's real grants. The
  crewmate payload shares one projection + ceiling with the custom-agent payload;
  governance is not bypassed. The in-place edit path (§5.4) runs the governance
  sanitizer on the way to disk as well.
- **Who writes the file, and the disposition that follows.** The writers are
  gateway/dashboard handlers that run **unsandboxed** — the create path (§5.4)
  and the in-place Capabilities edit path — exactly like today's
  fork/publish/reset writers (`dashboard/agent_admin/fork_publish.py`). No
  in-sandbox agent code writes `mates/agents/<id>.json`; it is written on the
  gateway's side of the sandbox and then projected onto the wire. So the earlier
  reasoning "`VISIBLE` because the in-sandbox agent writes it" is wrong: nothing
  in the sandbox needs write access to this folder. The only in-sandbox question
  is whether a sandboxed session must be able to READ the folder at all. Because
  the config reaches the session as a projected KAS payload, not by the session
  reading the file, the default disposition should be to **not expose the folder
  to the sandbox** — treat it like the other gateway-owned config under the Kiro
  home rather than a `VISIBLE` in-sandbox leaf. Pin the exact disposition against
  `sandbox.py` at implementation time, and if any in-sandbox read path is later
  found, ensure a session cannot reach a *different* crewmate's file than the one
  it is bound to.
- The file is written `0o600`, atomically, by those gateway writers.
- **Cross-instance writes to the shared folder.** Because the folder is
  machine-wide and `member_id` is unique only within one instance (§5.1), two
  instances can resolve to the same `<id>.json`; left unguarded, one instance's
  write silently replaces another's additions. Step 3 ships a write guard for
  this — the `~/.kiro/agents/` shared-folder rule, an owner-identity field, or a
  unique id — and the mechanism is open question 1 (§10). This is a data-integrity
  guard, chosen before step 3 lands, not deferred past it.

## 9. Alternatives considered

- **Bespoke Crew-owned crewmate schema.** A new file format holding both the
  config and the crewmate-specific fields. Rejected: it needs a new schema, a
  split between "fields that reach the model" and "Crew-only fields," and a
  separate "resolvable but not selectable" name-set to keep it out of the picker
  — all of which this design avoids by reusing the custom-agent format and
  splitting by *folder* instead of by schema. It also moves crewmate-specific
  data off the binding, where it already lives correctly.
- **Shared pointer + per-crewmate overlay.** Keep pointing at a shared agent and
  add a small per-crewmate overlay (extra skills/MCP) merged at session start.
  Rejected as the primary approach: two sources of truth per crewmate, new
  merge/precedence machinery, and the shared base keeps the stale-until-rebuild
  behavior. Its one advantage — the shared agent stays a real template, so a base
  edit reaches every crewmate — is exactly what copying severs; teams that rely
  on sharing a base agent lose that under copying (a real tradeoff, and the
  reason a sharing strategy is a §4 non-goal rather than denied outright).
- **Shared pointer, read-only in the editor (today's model made deliberate).**
  Freeze the pointer, remove config editing from the crewmate editor. This design
  is a strict superset — it adds ownership on top — so shipping it directly is
  preferred.
- **Lazy copy-on-first-edit into `mates/agents/`.** Keep the existing fork
  trigger, redirect only its destination folder. Rejected in favor of eager copy:
  lazy leaves a shared-pointer-until-touched window and keeps the lineage/reset
  machinery alive; eager makes a crewmate self-owned from birth and lets the
  lineage path be retired.
- **Keep the fork/inheritance lifecycle, just point it at `mates/agents/`.**
  Considered during the spike: teach intent/overrides/drift-guards to target the
  mate folder with the mate's source as its parent. Rejected — it reintroduces
  the parent connection the design deliberately severs (§9a), so a later edit to
  the source would raise "parent changed / Review changes" on a config that is
  supposed to be standalone.

## 9a. The fork / inheritance lifecycle we deliberately do NOT use

*(Required section — records a conscious decision not to lean into an existing
mechanism, so a future reader does not mistake its absence for an oversight.)*

There is already a fork + inheritance + review lifecycle for a crew editing a
**shared** custom agent, and mates deliberately do not use it. What it does, for
a shared template:

1. **Fork** on first edit — copy the shared template to a private `crew-<hex>`
   spec in `~/.kiro/agents/`.
2. **Record lineage** in the `agent_state` sidecar: `forked_from: <template>`,
   `private_to: <crew>` (the spec file cannot carry these — kiro-cli rejects
   unknown fields).
3. **Rebind** the crew's `kiro_agent` to the `crew-<hex>` copy.
4. Thereafter treat the copy as **`parent baseline + this crew's overrides`**,
   staged as reviewable `intent` (draft → reviewed → saved), guarded by
   **drift checks** — `parent_identity_changed`, `private_template_shadowed`,
   `materialization_changed`, `stale_revision`, `foreign_private_copy`.

**Why that machinery exists:** it is the cost of inheritance. A fork is a copy
that keeps *following* a live parent, so the system must know the parent
(`forked_from`), the sole owner (`private_to`), and must refuse rather than
silently corrupt when the parent, the file, or the editor's view drift apart.

**Why a mate does NOT use it.** A mate has **no parent to track**: the worker's
base lives in Crew's source code, and `mates/agents/<id>.json` holds only the
user's *additions* to it — there is no forked template the file inherits from and
no `file://` link back to one. Every piece of the lifecycle defends a parent
relationship or shared-file access a mate does not have: no
`forked_from`/`private_to` (no parent, and the member is already 1:1 with the
file via the binding), no intent/override/draft cycle (the base is source, not a
baseline layered in the file), no parent-drift guards (no parent to drift). A
mate edit is a plain read-modify-write of its own additions. The one guard kept
is a plain optimistic lock so two tabs don't clobber each other — that defends
concurrent access, not parentage.

**The decision, stated plainly:** a binding that resolves to `mates/agents/` is
NEVER forked; it is edited in place. The fork/lineage/drift model stays in place
for shared custom agents, and mate-owned config does not lean into it. A mate is
not a child that follows a parent; it is the authoritative file. Running a mate
through the fork lifecycle would model a parent relationship the design severed,
re-expose the mate in the `/set_mode` picker, and orphan the owned file. Revisit
only if mates are ever redefined as living templates that track a source.

## 10. Open questions

1. **Cross-instance write guard for the machine-wide file (step 3).** `member_id`
   is the per-instance display-name slug (`memory_stores._allocate_member_id`
   uniquifies only within one config), not a globally unique id, so two instances
   that each create a mate of the same name resolve to the same machine-wide
   `~/.kiro/mates/agents/<id>.json` and could clobber each other's additions —
   the very leak §2 exists to remove. The machine-wide location is a settled
   decision; the guard that makes it safe is not yet chosen. Candidates: (a) adopt
   the existing shared-folder rule — `agent._decline_shared_agent_home` /
   `shared_kiro_agents_writable` refuse writes from a non-default data home, so
   only the default install writes mate config; (b) record the owning
   home/instance identity in the file and refuse a cross-owner write (a small
   schema addition); (c) make `member_id` genuinely unique. This is a **step-3**
   decision — steps 1–2 never touch the file — and step 3 does not ship without
   one of these.
2. **Edit → re-chat staleness.** A live KAS session reads its config when the
   backend session is built, so an edit to a crewmate file isn't seen until that
   session rebuilds. Does an edit need an explicit "apply now" that rebuilds the
   backend session, or is next-rebuild acceptable? This is the least-proven path
   — the spike confirmed the editor now loads and (offline) saves a mate's file;
   whether a live session picks up the edit on next rebuild is the one live-only
   unknown.
2. **Default additions for a new worker file.** The conductor and worker base are
   source-defined, so a new file starts with empty `mcpServers`/`skills`. Should
   create seed any default additions (e.g. the `kirocrew-mate` server), and if
   so, which — versus leaving the file empty until the user adds something?
3. **Harness abstraction.** `kiro_agent` is KAS/custom-agent-shaped. Should the
   binding abstract one layer up to support other harnesses before this lands, or
   after?
4. **Crewmate visibility in the session-start switcher (new, from the spike).**
   Crewmates appear in the session-start crewmate switcher via
   `/api/agents/catalog` + `useAgents.withoutCoveredCrewmates`. This is
   PRE-EXISTING member-listing — the switcher lists crew *members*, not
   `mates/agents/` files — and is independent of the folder split, which only
   governs the Kiro-wide `/set_mode` picker. Noted as out-of-scope-but-adjacent
   so it is not conflated with this RFC; hiding crewmates there is a separate
   crewmate-visibility product decision with global blast radius.
5. **Per-crewmate file isolation.** If a crewmate's in-sandbox session must be
   prevented from reading *another* crewmate's file in `mates/agents/`, that is a
   sandbox-scoping requirement §8 must pin, not assume.
