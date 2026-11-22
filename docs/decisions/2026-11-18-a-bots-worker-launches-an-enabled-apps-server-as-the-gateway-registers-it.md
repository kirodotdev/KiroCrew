# A Bot's worker launches an enabled app's MCP server with the launch line the gateway registers for it now

Decided by: Bolin Chen (bolichen97), Kiro Crew maintainer, 2026-11-18
Date: 2026-11-18

## Decision

A Bot's worker launches an enabled app's MCP server with the launch line the gateway registers for that app now; every other server in the Bot's file (the user's own, a registry install, one copied from an old agent) keeps the launch line the file recorded; the file's tool selection, approvals and switch-offs apply to both.

## Why

- A Bot's file copied each app server's launch line when the server was added, and that line names the install the app ran from then. An upgrade that removes that install left the Bot's worker unable to start the server while ordinary chats started it, and the only fix was a hand edit of the file.
- The file still says which of an app's servers the Bot has; only how to start one follows the app, which is already how a custom agent's copy treats an app server's launch line. Nothing else in the file becomes a live link, so `docs/request-for-change/rfc-mate-owned-agent-config.md` §3 ("the file is a stamp, not a parent") stands.
- A launch line set by hand in a Bot's file for an enabled app's server is not used; the gateway's is.
- Options not chosen: take every server's launch line from a live source of the same name, the user's own MCP config included (it changes the recorded stamp rule; the question of following a source live is open on #31219); rewrite every Bot's file at each rebuild (writes files the person owns, stale between rebuilds); a stable alias for app install folders (cannot repair files already written).
- Not decided here: a server added from the user's own MCP config whose recorded command later moved, and an app server whose declared name has a `/`, which keeps its recorded line because the alias it is mounted under can be another server's; #37400 stays open for both.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/37400#issuecomment-6970481244 -- the decision, as a maintainer listed in `MAINTAINERS.md`.
- https://github.com/kirodotdev/KiroCrew/issues/37400 -- the report.
