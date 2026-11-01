---
title: Per-person execution and roles - a shared Crew runs each person's work as that person
status: draft
author: bolichen
created: 2026-11-01
last-audited: 2026-11-01
audited-at: cc669a83a5
doc-pr:
implementation-prs: []
tracking-issues: [27006]
supersedes: []
superseded-by: []
---

# RFC: Per-person execution and roles

- Status: draft. Nothing built; it changes no code. Acceptance requested from a
  maintainer; the status flips to `accepted` when one records it here. Verified
  at `cc669a83a5`: every ACP runtime starts in the gateway's own uid
  (`acp/launch.py`), Shared Crew states "There are no roles and no per-session ACLs" and reports
  containment as `best_effort` (`docs/system-specs/modules/shared-crew.md` §1,
  §4 Containment), and terminal routes require the dashboard owner.

Shared Crew lets several people work in one Crew. Today every agent, MCP server,
shell and terminal still runs as the host account that owns the gateway. So when
Ana asks the agent to post a comment, the comment is posted as the owner. Shared
Crew attributes the turn to Ana, but the outside world sees the owner.

This RFC does two things:

1. A turn runs **as the person who asked for it**: under their own OS account,
   with their own credentials, in their own home. The Crew keeps the
   conversation, memory, approvals, gate and audit. It never holds or reads the
   person's credentials.
2. Each admitted person gets a **role**, the way IAM does it: who may hold the
   role, what it may do in the dashboard, how far its agent may go, and which
   account it runs as. Roles work on both identity paths, Tailscale and SSH.

Related: [#12673](https://github.com/kirodotdev/KiroCrew/issues/12673) asks
for a hosted multi-user Crew with per-role permissions, and the Slack channel
roles RFC ([#23362](https://github.com/kirodotdev/KiroCrew/pull/23362), draft)
proposes roles for Slack. The rules below also draw on studying a shipped
multi-user gateway that runs a single-user agent app for a team on one shared
host.

## 1. The model in one picture

```
person ──(tailnet login)──> Principal ──(keystone map)──> OS account
                                                           │
turn ─> admission ─> launch tail ─> run-as helper ─> sandbox ─> harness + MCP + shell
                                     (root, fixed argv)   (as the person)
```

Four parts, each with one job:

| Part | Job |
|---|---|
| Principal (exists) | Who asked. Derived by the gateway, never from a body or header. |
| Account map (new, keystone) | Which OS account a principal may run as. Operator-written. |
| Run-as helper (new) | The only privileged step. Switches uid, then execs the normal launch. |
| Person setup (new) | The person's home has what a turn needs: harness login, agents, skills. |

Everything else (gate, approvals, transcript, memory, admission, containment)
stays where it is and gains one fact: the run-as account of the runtime.

### What Shared Crew already gives

Shared Crew is on main (`docs/system-specs/modules/shared-crew.md`, checked at
`cc669a83a5`, 2026-11-01), behind `KIROCREW_SHARED_CREW_PREVIEW=1`. This RFC
builds on it and does not redo any of it.

| Need | Shared Crew today | This RFC |
|---|---|---|
| Who is asking | `Principal` from the tailnet login (`tailscale serve`, direct listener `whois`), never from a header or body | input to the account map (R2) |
| Who gets in | Tailscale ACL and Crew invites; People page | unchanged |
| Attribution | message and session creator bylines, audit records, All / Mine filter | unchanged |
| The agent knows who asks | one `[from: <name> (<login>)]` line in the prompt | unchanged |
| Fair use | fair admission, per-principal cgroup slice | the slice now holds the person's own processes |
| Typing collisions | typing lock and takeover | unchanged |
| **Run as the person** | every runtime runs as the gateway uid | **new** (R1-R7, R11) |
| **Who decides an approval** | any collaborator, first submit wins | **run-as person only** (R8) |
| **Rights** | every collaborator has the same rights, no roles | roles (§7); R8 is a per-runtime rule, not a right |
| **Channel identity** | dashboard and tailnet only | **new** (§6) |
| **Containment strength** | `best_effort`, because every runtime shares one uid | becomes a real uid boundary between people |

Two Shared Crew statements change when this ships, and its spec must be updated
in the same change:

1. "Every authorized collaborator has the same application rights": still true
   for the Crew, but a run-as runtime's decisions belong to its person (R8).
2. Containment's `adversarial_escape_closed: false`: between two people's
   runtimes the kernel now separates them; against the gateway account it does
   not, because the gateway can still start runtimes.

Shared Crew identifies people only on paths that carry a network identity. A
dashboard reached over an SSH port forward is the local operator with the
shared token, so everyone on that path is one person. Hosts reached that way
need one more identity adapter (an SSH peer uid over a Unix socket) before
run-as can tell them apart. That adapter fits the existing `adapters` slot of
the mode route.

### Three kinds of host

The gateway runs as one account on every host. What differs is whether the
people who reach it have accounts there.

| Host | Example | Run-as behaviour |
|---|---|---|
| Personal | the owner's own laptop or desktop | off, or every person maps to the owner: nothing changes |
| Shared, everyone has an account | a team server whose accounts come from a directory (LDAP, SSSD); the gateway runs as a service account | each mapped person runs as their own account |
| Shared, visitors have no account | the owner invites friends over Tailscale to their own machine | an unmapped person runs as the guest account or not at all, never as the owner (R10) |

The third kind is the common one for Shared Crew today, so "no account" is a
first-class outcome of the map, not an error path.

On the second kind the gateway is a **service account**: a dedicated OS
account with no person behind it, started by a system unit. This RFC supports
that deployment shape (see §9). It does not create accounts, change a
directory, or manage how accounts are provisioned; it reads them (`getent`)
and may create a missing home (`mkhomedir`).

## 2. Rules

These are the invariants. Each one names the failure it prevents.

**R1. Fail closed, always.** A turn whose principal maps to an account runs as
that account or does not run. A missing map entry, a missing helper, a refused
switch or a half-applied setup refuses the turn with a reason. It never falls
back to the gateway account. (The reference system's first outside user found
every teammate's actions landing under his name: one missing patch made the
spawn take the "no user" branch.)

**R2. Identity comes from the gateway, never from the caller.** The run-as
account is decided from the derived Principal and the keystone map. No request
body, header, MCP argument, cron field or agent text can name it.

**R3. One privileged step, with fixed argv.** The gateway account gets one
sudoers rule: run one root-owned helper. The helper takes `(account, launch
plan id)`, re-validates the account against the map, refuses uid 0, the gateway
account and any account outside the Crew's member group, builds the
environment from nothing, and execs the launch. No blanket sudo, no shell
string, no per-feature `sudo cat` / `sudo test`.

**R4. Switch first, then sandbox.** The helper switches uid and then applies the
same OS sandbox the launch applies today, with the person's home as `$HOME`.
Every tier keeps its meaning for the person's files. A run-as runtime is never
less sandboxed than an owner runtime.

**R5. The Crew never reads a person's credentials.** Credential checks run as
the person and return a status (valid, expiring, missing), never a value. A
check is an edition adapter (CPP slot), public no-op; an enterprise edition
plugs its own sign-in check there.

**R6. Ask the kernel, do not imitate it.** Whether a person may read a folder is
answered by doing the read as them through the helper, not by re-implementing
permission or ACL rules in the gateway.

**R7. One runtime serves one person.** A runtime's account is fixed at start.
When a different person sends the next turn in a shared session, the runtime is
replaced, and the new one is seeded from the Crew's transcript. Subagents,
forks, retries and workflow steps inherit the initiator's account, never the
gateway's.

**R8. Only the run-as person decides for their runtime.** Tool approvals and
agent questions from a run-as runtime can be answered only by that principal.
Other collaborators see the card read-only.

**R9. Using someone's identity for someone else needs their consent, every
time it is used.** A cron job or channel answer that runs as Ana when Ben set
it up needs Ana's standing consent, checked at save AND at every fire, against
everyone who shaped the job. Each such use is logged where Ana can see it.

**R10. Unknown people are never the owner.** Work for an unlinked channel user
or a guest runs as a dedicated guest account with no enterprise credentials,
or not at all. It never runs as the owner or as "whoever has the freshest login".

**R11. A person's runtime cannot speak for another person.** A run-as child
authenticates to Crew MCP servers and the gateway over a gateway-owned Unix
socket. The gateway reads the peer uid (`SO_PEERCRED`, already used by
`mcp_gateway/socketsec.py`) and requires it to equal the session's run-as
account. A shared secret or a token file is not enough.

## 3. What changes, surface by surface

| Surface | Today | With per-person execution |
|---|---|---|
| Dashboard turn | gateway account | the sender's account (R7 replace on change) |
| Subagent, fork, workflow step | gateway account | initiator's account |
| MCP servers of the harness | gateway account | the runtime's account (they are its children) |
| Crew MCP servers | gateway account, socket attested | same, plus peer uid = run-as account (R11) |
| Tool gate, hooks | in the child | unchanged; the gate's policy files stay keystone |
| Approvals, questions | any collaborator, first wins | run-as person only (R8) |
| Cron job | gateway account | job owner's account, or a consenting person's (R9) |
| Channel (Slack, ...) | gateway account | linked person's account, else guest account (R10) |
| Terminal | owner only | the opener's account, clean environment |
| Files, diff, git panes | gateway account | as the viewer through the helper (R6), or read-only |
| Scratch, temp | under the data home | per-person scratch root owned by that account |
| Kill, reap, containment | signal own pids, own slice | through the helper, per-person slice |

## 4. Person setup

A person can run a turn only when their home is ready. Setup is a journal per
person, owned by the gateway:

1. **Account exists.** The OS account comes from the host's directory. The Crew
   never creates accounts; it may create the home (`mkhomedir`).
2. **Harness login.** Each harness signs in as the person. When the harness says
   it is signed out, the gateway starts its device flow as the person and shows
   the link to that person only. A login is never shared across people.
3. **Materialize.** The person's effective agents, skills and MCP config are
   written into their home by the helper, from the Crew's sources.
4. **Gate.** While step 3 runs for a person, their turns wait (423 with a
   reason), and the launch refuses on a lock whose writer is gone (checked by
   pid and start time). Additive changes do not hold turns; removals and
   tightenings do.
5. **Credential status.** The edition adapter reports status, and may refresh a
   short-lived part at turn start, as the person (R5).

## 5. Consent and the use log

Each person owns one consent record, which only they can change. Admins can read
it; no agent token can write it.

| Setting | Default | Meaning |
|---|---|---|
| Who may run jobs as me | only me | `self`, a list of principals, or anyone in the Crew |
| Channel answers as me | on, for my own messages | the channel binding below |

Cron jobs record the creator and every later editor. A fire runs only if the
target's consent allows all of them at that moment. Revoking consent stops jobs
already saved. Every use of a person's account for someone else is one SEL
record and one row on that person's "My account" page.

## 6. Channels

A channel user becomes a person in two steps, and both are needed: the person
clicks a link in a DM from the bot, and then signs in to the dashboard as
themselves. Only then is the binding `(channel, user id) -> Principal` written.
Nobody types an identity, and a display name or email never decides it. An
unbound user is a guest (R10).

## 7. Roles for incoming people

Shared Crew admits a person or refuses them; everyone admitted has the same
rights. A shared host needs more: the owner, a teammate, a viewer and a guest
should not all be able to change Settings or run shell as themselves. Roles
add that, on both identity paths (tailnet and SSH peer uid), with the IAM
shape people already know.

### The IAM mapping

| IAM | Crew | Notes |
|---|---|---|
| Principal | Shared Crew `Principal` | tailnet login, tagged node, or SSH peer uid |
| Role | a named entry in `people_policy.json` | operator-written, keystone |
| Trust policy (who may assume it) | the role's `members` | logins, tailnet tags, POSIX groups, edition directory groups |
| Permissions policy | the role's `rights` + its governance `profile` | rights gate the dashboard; the profile gates the agent |
| Permissions boundary | `security_policy.json` (POLICY) | a role can only narrow it |
| Session policy | the session's own approval mode and profile | narrows the role again |
| Explicit deny wins | tightest-wins (POLICY ∩ PROFILE) | unchanged |
| Assume role | resolved per request, never chosen by the caller | one role per person at a time |

### What a role holds

```json
{
  "name": "member",
  "members": { "logins": [], "tailnet_tags": [], "posix_groups": ["crew-members"] },
  "rights": ["chat", "sessions.read_all", "cron.own", "memory.write"],
  "profile": "member",
  "run_as": "self"
}
```

| Field | Meaning |
|---|---|
| `members` | who resolves to this role; an empty list matches nobody |
| `rights` | dashboard and API rights from a fixed, enforcer-owned list |
| `profile` | a Level-2 governance profile in `profiles/` that narrows what the agent may do for this person |
| `run_as` | `self` (the person's own account, R1-R11), `guest` (the guest account), or `none` (no turns: read only) |

**Rights** are a closed list, owned by the enforcer like `_ORDINAL_SCALES`, so
no file can invent one:

| Right | Allows |
|---|---|
| `chat` | start sessions and send turns in sessions the person created |
| `chat.others` | send turns in other people's sessions |
| `sessions.read_all` | see every session, not only their own |
| `cron.own` | create and edit cron jobs that run as themselves |
| `memory.write` | approve memory and lesson changes |
| `apps.manage` | install, enable and remove apps |
| `settings.write` | change Settings and config |
| `people.manage` | invites, revocations, role assignment |

Approving a tool call is not a right: it always belongs to the run-as person
(R8).

### Built-in roles

| Role | Rights | `run_as` | Default members |
|---|---|---|---|
| `owner` | all | `self` | the local operator and this machine's own tailnet login |
| `member` | `chat`, `sessions.read_all`, `cron.own`, `memory.write` | `self` | nobody until the operator lists them |
| `viewer` | `sessions.read_all` | `none` | every other admitted person |
| `guest` | `chat` | `guest` | nobody until the operator lists them |

An admitted person who matches no role is a `viewer`. That keeps today's
admission behaviour safe by default: an invited friend can watch, not act,
until the owner gives them a role.

### Resolution

1. The gateway derives the `Principal` (Shared Crew §2).
2. It collects the person's groups for that path: tailnet tags for a tailnet
   node; POSIX groups (`getgrouplist`) for an SSH peer uid; the edition's
   directory groups when an adapter supplies them.
3. The most specific match wins: a login match beats a group or tag match; a
   tie between groups is an error in the file, rejected at load.
4. No match: `viewer`. An unreadable or invalid file: every person except the
   local operator resolves to `none`, and People shows the reason, the same
   way Shared Crew's `invalid` mode fails closed.

The role's profile composes the way the Slack channel roles RFC (draft,
[#23362](https://github.com/kirodotdev/KiroCrew/pull/23362)) proposes: one new
`principal` bind type, and the role profile is intersected **on top of** the
profile the turn already gets, never instead of it:

    effective = POLICY ∩ active profile (app | task | surface) ∩ role profile

The evaluator does not change; it runs once per profile and the tighter answer
wins. That RFC's roles (`teammate`, `maintainer`, per channel) are this RFC's
channel side; this RFC adds the dashboard paths (tailnet, SSH peer uid), the
dashboard rights and `run_as`. The two should land as one role model.

### Where it is enforced

| Plane | Check |
|---|---|
| Dashboard and API routes | one `require_right(actor, right)` after identity is published; a route without a declared right is owner-only |
| Agent tools | the existing PreToolUse gate, through the role's profile |
| Execution | the launch tail, through `run_as` and the account map |
| Channels | a bound person gets their role; an unbound one gets `guest` or nothing |

## 8. Edition split

| Piece | Public | Enterprise edition |
|---|---|---|
| Account map, helper, launch, gate, consent, use log, roles | yes | same |
| Credential status adapter | no-op (`unknown`) | its sign-in check |
| Directory lookup for accounts and groups | `getent`, `getgrouplist` | its directory |
| Channel identity hints | none | may pre-fill the link |

Linux only. Run-as is refused at start on macOS and Windows with
`run_as_unsupported_os`. Roles work on every OS: off Linux, a role's `run_as`
of `self` or `guest` reads as `none`.

## 9. Configuration

One keystone file, `people_policy.json` (READONLY disposition, like
`security_policy.json`):

```json
{
  "run_as": {
    "enabled": false,
    "guest_account": "crew-guest",
    "map": { "rule": "same_login", "overrides": {} }
  },
  "default_role": "viewer",
  "roles": []
}
```

`map.rule` is `same_login` (the tailnet login's local part, or the SSH login
itself, names the account) or `explicit` (only `overrides`). `roles` adds to or
replaces the built-in ones by name.

Off by default. Turning run-as on is an operator action on the host, never a
Settings card, because it widens what the gateway account can do. Roles can be
edited from People only by a person with `people.manage`, and the edit is a
change card that writes the keystone through the gateway, never through the
agent.

The service-account deployment needs, beyond the file: the gateway as a system
unit with `User=<service account>`, one sudoers rule (or a root socket-activated
helper) for the run-as step, and a guest account. `kirocrew doctor` checks each.

## 10. Phases

| Phase | Ships | Done when |
|---|---|---|
| 0 | helper, keystone map from the Shared Crew `Principal`, fail-closed refusal, peer-uid check | a turn as a mapped person runs as them; every error path refuses |
| 1 | dashboard turns, subagents, replace on person change, kill and slices | two people alternate in one session, each change shows a banner |
| 2 | person setup journal, harness device flow, credential adapter, "My account" page | a new person goes from first sign-in to first turn with no shell |
| 3 | cron run-as with consent and use log | revoking consent stops a saved job at its next fire |
| 4 | terminals and file panes as the person | a non-owner terminal shows the person's `id` |
| 5 | channel binding, approvals bound to the run-as person, guest account | a guest's answer cannot read the owner's files |

Roles do not wait for run-as. They can ship first, on their own, because they
only narrow what an admitted person can do:

| Phase | Ships | Done when |
|---|---|---|
| R0 | `people_policy.json` roles, `require_right`, People shows each person's role | a `viewer` cannot send a turn; a `member` cannot change Settings |
| R1 | role profiles through the `principal` binding | a role's profile narrows that person's tool calls |
| R2 | SSH peer-uid identity adapter, POSIX groups as members | two people on one SSH-forwarded dashboard get different roles |

The "My account" page and the team view (members, sign-in state, setup state)
can ship as an app on App Kit over core read APIs. The run-as path itself
cannot: it lives in the launch tail and the gate.

## 11. Alternatives rejected

- **Workspace tools only run as the person** (the Session Environments shape:
  engine stays as the gateway, tools move). Most actions on outside systems go
  through MCP servers, not file tools, so they would still run as the gateway.
- **A shared secret that mints a session for any person** (the reference
  system's trigger and bot path). Anything that reads the secret becomes
  everyone. R11 uses the kernel's peer uid instead.
- **Guests run as the freshest signed-in person.** Anyone in a channel then
  reads as that person. R10.
- **Re-implementing ACL checks in the gateway.** It drifts from the kernel. R6.

## 12. Decisions needed

1. The account map rule: tailnet login local part, an explicit table, or both.
2. Whether a shared session may switch people at all, or locks to its first
   person (simpler; R7 then becomes "refuse").
3. Whether the model login is always the person's, or the Crew may lend its own
   for people who have none.
4. Whether run-as requires Shared Crew's preview gate, or ships with its own
   switch in `people_policy.json` and reads the Principal either way.
5. Roles reverse Shared Crew's "There are no roles and no per-session ACLs" boundary. That needs a
   maintainer decision recorded under `docs/decisions/`.
6. Whether an admitted person with no role is a `viewer` (proposed) or `none`.
7. One role model with the Slack channel roles RFC
   ([#23362](https://github.com/kirodotdev/KiroCrew/pull/23362)): its
   out-of-role calls ask the owner in the thread; this RFC's R8 gives a
   run-as runtime's in-role asks to the person. Proposed merge: in-role asks go
   to the run-as person, out-of-role asks go to a person holding
   `people.manage`, and a role's profile decides which is which.

## 13. Open questions

- Containment of another account's processes: the gateway's `systemd --user`
  cannot own them. Likely a system slice set up by the helper with
  `systemd-run --uid`.
- Session resume across a person change loses harness-side state (it lives in
  the previous person's home). The transcript reseed covers the conversation;
  native subtasks are not carried.
- Which backends support run-as in Phase 1. Each needs a per-person login story.

## 14. Risks

- The helper is a root entry point. It must be small, fixed-argv, and covered by
  its own tests and review as a security-sensitive change.
- A wrong map entry runs Ana's turn as Ben. The map is keystone, every run-as
  start is an SEL record, and the session shows whose account it runs on.
- The owner keeps root on the host. This design protects people from each other
  and from the agent, not from the host owner. Say so in the UI.
