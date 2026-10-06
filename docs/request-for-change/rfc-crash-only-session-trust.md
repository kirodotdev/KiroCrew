---
title: Crash-only session trust — "Trust this session" survives a restart nobody asked for
status: draft
author: billygerhard
created: 2026-10-01
last-audited: 2026-10-01
audited-at: 8ae5dff6ee
doc-pr: 15903
implementation-prs: [15894]
tracking-issues: [15892, 6379]
supersedes: []
superseded-by: []
---

# RFC: Crash-Only Session Trust

> **Status:** `draft`. Acceptance is requested from a maintainer; the status
> flips to `accepted` when one records it here. Nothing is on main. Verified at
> `8ae5dff6ee`: `messaging/session_trust.py` holds the grant "in memory only, so
> it dies with the process", and `messaging.md` records the same invariant
> ("an ad-hoc auto-approve grant dies with the process"). The implementation is
> [#15894](https://github.com/kirodotdev/KiroCrew/pull/15894). This document
> lands first because it changes what a gateway restart does to a user-visible
> grant, and the First Principles lane reads that decision from the base branch.
>
> **Rule amendment requested.** The backend security controls the reviewers
> enforce say: "Never persist an ad-hoc grant across a restart." This design
> keeps the rule for every restart the owner chooses and asks maintainers to
> amend it to: "Never persist an ad-hoc grant across a restart the owner chose;
> a grant may survive an exit nobody asked for (a crash, an OOM kill, a watchdog
> exit) only from a gateway-sealed, signed record that a deliberate stop
> clears." The rule's other arm, a time limit, is not taken (see Non-goals),
> because a gateway that never crashes already keeps the grant indefinitely.

## Summary

A chat the owner trusted with "Trust this session" stays trusted when the gateway
**crashes and relaunches itself**. A stop the owner **chooses** still clears it,
exactly as today. Re-consent after a deliberate restart is kept; re-consent after
an accident is dropped.

## Motivation

Today every gateway exit drops every chat's trust, including exits nobody asked
for:

- the loop-stall watchdog's dump-then-exit, typically after the host swaps under
  memory pressure;
- the stale-asset watchdog's self-restart after an install update prunes the
  running build;
- an OOM kill or another crash.

The supervisor relaunches the gateway and the chats come back untrusted.
Unattended work in them (monitor loops, long tasks) then hits approval prompts
nobody is there to answer. Each prompt times out, and each loop stops itself. The owner
finds everything stalled hours later, with no notice of why.

The in-memory rule exists so that a restart is a re-consent point. A crash is not
a point at which anyone is asked anything: there is no prompt, no person and no
decision. It silently withdraws trust the owner never withdrew. Meanwhile a
gateway that never crashes keeps that same trust for its whole uptime, however
long. So dropping trust on a crash adds no time bound. It only adds a failure
mode.

[#6379](https://github.com/kirodotdev/KiroCrew/issues/6379) asked for trust to
survive **every** restart, and was closed because that removes the re-consent
point. Its implementation ([#6381](https://github.com/kirodotdev/KiroCrew/pull/6381))
was also blocked for reading trust back from the agent-writable transcript. This
RFC answers both: it keeps the re-consent point for every restart the owner
chooses, and it keeps the record out of the agent's reach.

## Goals

- Trust the owner granted survives a gateway exit the owner did not ask for.
- Every exit the owner did ask for still clears it, so the next boot asks again.
- No agent can create, extend or restore trust by writing a file.
- Every restore and every clear is audited.

## Non-goals

- Trust surviving `kirocrew stop` / `restart`, a signal stop or a host reboot,
  including an unattended one (§1, Limitation).
- Persisting any other grant: trust read-only bash, per-command patterns, app or
  crew-worker trust, inherited subagent trust, or the safety override.
- An agent tool that grants trust. Granting trust stays a human click.
- A time cap on restored trust. Restored trust lasts no longer than trust that
  never saw a restart, so a cap would make a crash *shorten* a grant the owner
  made.

## Design

### 1. Which exits clear trust

The gateway already encodes whether the owner asked for an exit in its exit
status. `_shutdown_and_exit` in `slack/gateway.py` exits **0** only for an owner
stop: SIGTERM or SIGINT (`kirocrew stop`, `kirocrew restart`, Ctrl-C,
`systemctl stop`, and the SIGTERM a host reboot delivers) and the owner shutdown
route. Every self-initiated shutdown that exists to be relaunched exits non-zero
so a restart-on-failure supervisor brings it back. That includes the stale-asset
watchdog (`STALE_ASSET_EXIT_CODE = 75`) and the listener guard.

| Exit | Exit status | Trust |
|---|---|---|
| `kirocrew stop` / `restart`, Ctrl-C, `systemctl stop`, host reboot, owner shutdown route (including the desktop app's stop before it installs an update) | 0 | **cleared** |
| Second signal (force exit) | 0 | **cleared**: the first signal already wrote the owner-stop marker; the force exit never waits, and holds once if that marker is not down yet |
| Dashboard Restart button, an applied in-app update (`_restart_gateway`, which `os.execv`s in place) | none: the image is replaced | **cleared** before sessions drain; the restart is refused if the clear fails |
| Slack owner `/kirocrew restart` (exits non-zero for the supervisor) | non-zero | **cleared** first; the restart is refused if the clear fails |
| `kirocrew stop` / `restart` on Windows (`taskkill /T /F`, no signal delivered) | none | **cleared**: the CLI leaves the owner-stop marker before the kill, and takes it back only if no kill landed; if it cannot write the marker it warns and stops anyway |
| `kirocrew restart` after a crash (nothing running to clear its own record, or a crashed service unit the manager restarts) | the crashed gateway's | **cleared**: the CLI writes the owner-stop marker before it starts (refusing if it cannot while a record exists), and takes back one it wrote only if the restart fails before it delivered any stop while the gateway it found still holds the lock (a gateway still shutting down past the timeout keeps it) |
| Stale-asset self-restart, listener-guard self-restart | non-zero | kept |
| Loop-stall watchdog (faulthandler dump-then-`_exit`) | never enters the shutdown path | kept |
| OOM kill, segfault, any crash | never enters the shutdown path | kept |

**Invariant for future exit paths.** The exit status is chosen for the
supervisor, so trust only follows it today by construction. Any new
self-initiated shutdown, restart or exec path must decide its trust disposition
explicitly -- clear for an owner action, keep for an exit nobody asked for --
rather than inherit it from the exit code or exit mechanism it uses.

On exit status 0 the gateway clears the record as the **first** step of
shutdown, before any teardown that could outlast the supervisor's stop timeout.
The signal handler also starts a clear on receipt of the first SIGTERM/SIGINT,
on a thread the event loop does not wait on, so a supervisor or reboot that
SIGKILLs a slow shutdown leaves nothing to restore.
The first signal's worker writes the owner-stop marker before it starts
clearing, and the force-exit signal handler only starts one more attempt and
exits without waiting: the first signal's clear may still be running -- often on
the very I/O whose stall made the owner signal again -- when `os._exit` kills it,
and the marker alone makes the
next boot restore nothing. A clear that cannot rewrite the file
removes the file. If even that fails, it leaves the owner-stop marker in a
sealed directory of its own (`session-trust-stop`, hidden and gated like the
record, apart from it so a store directory refusing writes does not take it
down; a marker path that is a link, even a dangling one, or that cannot be
inspected reads as marked), and the next boot -- or a chat rebuilt before the boot restore runs --
restores nothing and retries the clear, removing
the marker only once the clear succeeds. The in-place restart refuses to restart
when its clear fails, rather than exec into a boot that restores what it
withdrew. The owner's next grant settles that
stop first and stays live-only if it cannot, so a click made after such a boot does
survive the next crash. Every
failure falls toward re-consent.

**Limitation: an unattended host reboot counts as an owner stop.** The init
system stops the gateway with the same SIGTERM as `systemctl stop`, and the
process cannot tell an OS auto-update or hypervisor restart from an owner who
rebooted on purpose. So trust is cleared on every host reboot, and overnight work
stalls after one exactly as it does today. This design accepts that residual
deliberately: the only signal that would separate the two lives outside the
gateway (who issued the reboot), and guessing would turn a re-consent point the
owner may have chosen into one they never get. It is open question 3.

**Residual: a stop that arrives while the loop is already wedged.** The signal
callback runs on the event loop, so a loop wedged at the moment of the stop runs
no clear before the follow-up SIGKILL, and that stop keeps trust as a crash
would. It is accepted rather than closed: a handler that bypassed the loop would
race the loop's own state, and this window is the same one the loop-stall
watchdog already treats as a crash.

### 2. Where the record lives

`session-trust/grants.json` under the data home holds session keys and nothing
else, signed with a MAC keyed from the dashboard token-signing key, a secret no
agent plane can read or write (deliberately not the SEL trust root, which the
sandbox reads by design). A record no gateway signed -- including one an agent
planted on an older build, before this leaf was fenced -- restores nothing, and
the record is read bounded, with no symlink followed -- for the record and for
its directory, which every read, write and remove refuses unless it is a real
directory, so a link an older build left in its place redirects nothing. Store
writes run one at a time in the order they were issued, so a revoke issued
before a grant cannot erase that grant after it was reported saved. An owner
stop's clear first fences new grants out of the store, then clears in that same
order, so a grant already writing finishes and is cleared with the rest rather
than landing after the clear and surviving the stop. If that ordered clear times
out behind a held write, the owner-stop marker, which needs no store order, is
written directly. Only the gateway reads or writes it, so the directory is **HIDDEN** in the
sandbox (`_CREW_HIDDEN_LEAVES`, pre-created so the mask always has a name to bind
over) rather than READONLY: no in-sandbox code needs to read it, an agent learns
nothing about which chats are trusted, and a masked record resolves to "no chat
trusted", the re-consent direction. The file-edit gate also blocks reading and
writing it (`_CREW_SECRET_LEAVES`), so on a host with no OS sandbox an agent
file tool can neither change it nor learn which chats are trusted.
It is deliberately **not** a field on the transcript's metadata line. A trust bit
an agent could write would be trust the agent could grant itself, which is the
gap #6381 could not close.

### 3. Who can make a grant durable

Only the owner's own dashboard session, through the two sites a person clicks:
the chat mode switch and the approval card's "trust" action. A grant is saved
before it takes effect: if it cannot be saved it still takes effect, live-only --
what trust is today -- and the owner is told a crash or restart will turn it off,
so a host that cannot write the record loses nothing it has now. The store remembers at most 512 chats
and tells the owner when a new grant pushes the oldest out. App tokens, the
internal agent credential, non-owner logins and app-owned chats are refused.
Every revoke removes the record, including the all-chats switch, which also
covers chats that are not currently open -- before it turns trust off live, by
the key it read before any wait, so a crash between the two steps cannot leave a
restorable grant. A grant saved after a revoke that could not remove its record
rewrites the record without the withdrawn grants, and a permanently deleted chat
removes its saved grant before its transcript is unlinked -- a removal that fails
refuses the delete, and a delete that is then refused or fails puts it back (never during
an owner stop, after a later revoke, or for a grant a revoke withdrew); a grant
made while the delete runs is not saved; a channel row also revokes every saved key whose transcript
it is, found from the signed store rather than the agent-writable transcript -- so a chat recreated under the same key never
inherits it. A revoke is withheld in memory before its removal is written, and a
grant settles any owner-stop marker a previous run left first, so a late boot restore
cannot clear it; a marker written since this run started is a stop in progress (the
Windows CLI marks before it ends the gateway) and is never settled by a grant or a
restore; a restore during it restores nothing.
A revoke whose saved grant cannot be
removed reports an error rather than success and posts an owner notice, so the
owner learns it may come
back after a crash; the gateway also remembers that revoke in memory, so no
restore in the same process hands the grant back. A grant write that races a
revoke re-reads the live flag and revokes itself, and one whose chat's session
key is rebound, or any of whose chats is removed, while it saves is refused and its saved keys
taken back -- a removed chat's keys even if they were saved before, since a chat recreated
under them must not inherit the grant -- so the key on disk is always the key trusted. The
grant write is bounded like a revoke: one that stalls is live-only, so a wedged disk never
hangs the trust click or the call an approval card approves. The write itself is never
cancelled: it finishes in the store's write order, so a grant that is then refused takes
back what it saved after any save that ran, timed out or not, and that take-back waits
behind the late write instead of overtaking it. A grant that does not persist (an app
token) reads nothing from the store first, so no wait follows its authorization during
which a disabled app could still slip its trust through. A grant also refuses to
write if any revoke started after it read what the chat already held, and it writes
exactly the keys it captured with the click, so a refused grant never writes back a
saved grant that a racing revoke removed, and a chat rebound in between leaves nothing
behind. A delete that is
then refused puts trust back only under its own removal's generation, so a revoke that
lands during the removal blocks the put-back, and a delete removes only keys that name
its own transcript file, so a stacked duplicate row never takes its sibling's trust.
Nothing is saved when the
token-signing key is the in-memory fallback used on a host that cannot persist
it, because no later boot could verify a record signed under it; the grant there
is live-only, as above. A stored MAC that is not ASCII is rejected before it is
compared, so a planted one restores nothing rather than crashing the read.

### 4. Restore

At boot, and when a chat is rehydrated later, a stored key restores exactly what
the click sets (`slot._trust` and the session's `auto` approval policy) and
nothing more. Every gate that decides a tool call on the live path decides it the
same way after a restore: the deny list, the sensitive-path keystone,
`human_only` cards, the governance ceiling. There is no approval-mode ceiling to
re-check: `trust` is a non-deniable approval mode, so the grant itself consults
none either.
A chat's session key normally comes from its own name; a chat linked to another
session (a channel-born chat) takes it from a link stored in its transcript, which
agents can rewrite. A link is honoured only when it names the very transcript the chat
is stored in, as a channel-born chat's does; any other link (a resumed or cron-bound
chat) restores nothing and the chat asks again, so a rewritten link cannot hand one
chat another chat's trust.
The record is read off the event loop, and the verdict is applied only if no
revoke landed during that read; a revoke always wins over a stale verdict. The
boot restore runs in the background, so nothing on the boot path waits on it.
Each restored chat writes a SEL `session_trust:restored` event. An unreadable,
malformed or wrong-version record restores nothing and tells the owner so.

### 5. Audit

| Event | When |
|---|---|
| `session_trust:restored` | one per chat restored at boot or rehydration |
| `session_trust:cleared_on_stop` (`cleared` / `clear_failed`) | on every owner stop |

## Alternatives

- **Persist across every restart** ([#6379](https://github.com/kirodotdev/KiroCrew/issues/6379)).
  Rejected by the maintainers: it removes the re-consent point a deliberate
  restart provides.
- **Signed record on the transcript** ([#6381](https://github.com/kirodotdev/KiroCrew/pull/6381)).
  An agent that kept a copy of its own earlier signed line could write it back
  after a revoke. A gateway-only leaf has no such replay, because the agent
  cannot write there at all.
- **A time-to-live on restored trust.** It would make a crash shorten a grant
  that an uninterrupted gateway would have kept, which penalises the accident
  rather than bounding the grant.
- **Leave it, and tell the owner after a crash that trust was reset.** That makes
  the failure visible but still stalls the work it interrupts, which is the
  problem the owner hits.

## Open questions

1. Should an automatic self-update restart count as an owner stop? It depends on
   the route. The desktop app stops the gateway through the owner shutdown route
   before installing (exit 0), and an applied in-app update restarts in place;
   both are owner actions and both clear trust. The stale-asset self-restart,
   which follows an install the gateway did not start, exits non-zero and keeps
   it. The question is whether that last route should agree with the others.
2. Should the dashboard show which chats had trust restored after a crash? This
   design only audits it. A one-line notice would make the carry-over visible
   without requiring an action.
3. Should an unattended host reboot keep trust? This design clears it (see §1,
   Limitation), because the gateway cannot tell an OS-initiated reboot from an
   owner's. Keeping it would need a signal from outside the process, such as an
   owner-set "keep trust across reboots" switch, which is a separate decision.
