---
title: Terminal exit closes its tab — what the dashboard does when a shell ends on its own
status: accepted
author: jjaskula
created: 2026-09-29
last-audited: 2026-09-30
audited-at: c1a02ce976
doc-pr: 15144
implementation-prs: [14509, 14511]
tracking-issues: [14584, 13189]
supersedes: []
superseded-by: []
---

# RFC: Terminal Exit Closes Its Tab

> **Status:** `accepted` on 2026-09-30 by maintainer bolichen (see Open
> questions). Nothing is on main yet. Verified at `c1a02ce976`: `read_pty`
> in `src/kiro_crew/dashboard/handlers/terminal.py` sends no frame when the
> shell exits, so the browser sees only a dropped socket, redials, and the dial
> spawns a new shell. The dead PTY is reaped by the 15-minute orphan sweep. The
> implementation is [#14509](https://github.com/kirodotdev/KiroCrew/pull/14509),
> with the Windows exit code in
> [#14511](https://github.com/kirodotdev/KiroCrew/pull/14511).

## Summary

When the shell in a dashboard terminal tab exits on its own (`exit`, `Ctrl+D`,
a crash), the tab closes. An abnormal exit posts one bell note. This changes a
default for every user, with no setting.

## Motivation

`exit` and `Ctrl+D` are how a user says they are finished with a shell. Today
the dashboard answers that with a dead tab: the socket drops, the client treats
it as a network failure, redials, and silently hands the user a new shell they
did not ask for. The user has to close the tab by hand, and does not learn
whether the old shell ended cleanly or crashed.

## Goals

- A shell that exits on its own closes its tab, wherever the tab lives.
- The user learns when a shell ended abnormally.
- The client never redials a session whose shell has exited.

## Non-goals

- Keeping a crashed shell's last output on screen. Closing the terminal when
  its shell exits, and its output with it, is common behaviour in editor
  terminals, including VS Code and Kiro IDE, where `exit` or `Ctrl+D` closes
  the terminal.
- A setting to keep exited tabs. It can be added later as a client-only choice
  (Alternatives).
- Restarting a shell inside an exited tab.
- Terminal ownership, reservation and popout transfer. Those belong to the
  terminal session ownership RFC
  ([rfc-terminal-session-ownership.md](rfc-terminal-session-ownership.md)); the
  two RFCs' scopes are disjoint.

## Design

### 1. Protocol

When the shell exits on its own, the server sends the attached window
`{"type": "exit", "status": N | null}`, closes the socket with code `4001`, and
reaps the session at once instead of waiting for the orphan sweep. `status` is
the exit code, negative for a death by signal (asyncio's convention), or `null`
when it cannot be read. A window that reconnects in the gap, or loses the exit
frame, still gets the `4001` close.

Deliberate teardowns are not exits and send neither: closing the tab, the
orphan sweep, or a dial replacing a dead session.

### 2. Client

On `exit` or `4001` the client stops retrying and closes the tab:

- **Docked panel:** the tab closes; the panel hides when it was the last tab,
  as a manual close of the last tab does.
- **Side-panel strip:** the tab closes in every chat's strip that holds it. The
  strip stays open because it also holds the pinned views. Focus moves only if
  it was on the closed tab.

### 3. Bell note

A non-zero status posts one note to the notification feed: title `Terminal
exited abnormally`, body `The shell exited with code N.` or `The shell was
killed by SIGNAME.` A clean exit posts nothing. The feed reaches every
dashboard session, not only the terminal's owner, so the note carries nothing
about the session: no shell, no directory, no session id; `meta` holds only the
status.

## Migration plan

### Phase 1: exit signalling, tab close and bell note

[#14509](https://github.com/kirodotdev/KiroCrew/pull/14509). The server sends
the `exit` frame and the `4001` close and reaps the session; the client closes
the tab in the docked panel and in every side-panel strip; a non-zero status
posts the bell note. On Windows the tab closes, but ConPTY reports no status,
so a failed Windows shell closes without a note.

**Exit criteria:**

- `exit` in a docked tab closes it, and closes the panel when it was the last
  tab.
- `exit` in a side-panel tab closes that tab in every strip that holds it and
  leaves the strip open.
- `exit 3` posts one note with code 3; `exit` posts none; a shell killed by a
  signal posts one note naming the signal.
- The note's title, body and `meta` carry no shell, directory or session id.
- Closing a tab by hand, the orphan sweep, or a dial replacing a dead session
  posts nothing and sends no `exit` frame.
- After an exit, no request re-spawns a shell for that session id, including a
  reconnect that was waiting when the shell exited.

### Phase 2: Windows exit status

[#14511](https://github.com/kirodotdev/KiroCrew/pull/14511). The ConPTY wrapper
reads the exited child's code, so the reap reports it and a non-zero code posts
the same note as on POSIX.

**Exit criteria:**

- On Windows, `exit 3` in the dashboard terminal closes the tab and posts one
  note with code 3; `exit` posts none.
- A pywinpty binding that exposes no exit status closes the tab without a note
  instead of raising or inventing a code.
- The live `cmd.exe /c exit N` test runs on the Windows CI shard and asserts
  the code.

## Backward compatibility

Compatible. An older client ignores the `exit` frame and treats `4001` as a
transport close, so it redials as it does today, and the dial spawns a fresh
shell because the session is already reaped.

## Security considerations

The bell note goes to every dashboard session, which is why it carries only the
status. The terminal routes' own access rules are unchanged.

## Alternatives considered

- **Keep the exited pane.** Keeps a crashed shell's last output, which is the real cost of closing. It needs scrollback and exit
  state held after the PTY is gone, plus a way to restore them; an earlier
  attempt ([#12395](https://github.com/kirodotdev/KiroCrew/pull/12395)) spent
  most of its size there.
- **Opt-in close-on-exit setting.** Avoids the default change, but leaves every
  user with the dead tab unless they find the setting. #12395 took this route.
- **Close by default, with an opt-in "keep exited tabs" setting later.**
  Compatible with this design and needs no protocol change; left for a
  follow-up if users ask for it.

## Open questions

None.

**Decided 2026-09-30 by bolichen (maintainer): this design is accepted.** A
shell that exits on its own closes its tab for every user, with no setting; a
known non-zero status posts one `system.terminal` bell note carrying no session
detail; and the Windows exit code follows in Phase 2. The implementations are
[#14509](https://github.com/kirodotdev/KiroCrew/pull/14509) and
[#14511](https://github.com/kirodotdev/KiroCrew/pull/14511).
