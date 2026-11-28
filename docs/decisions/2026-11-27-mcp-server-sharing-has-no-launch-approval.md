# MCP server sharing has no launch approval; every stdio server is shared by default

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-27
Supersedes: 2026-11-20-the-gateways-own-path-is-not-a-launch-change.md (in part), 2026-11-20-agents-ask-before-changing-gateway-trusted-files.md (in part: the crewmate folder only), 2026-11-23-the-owner-can-trust-a-shared-mcp-servers-changes.md, 2026-11-26-an-edition-folded-version-folder-keeps-a-launch-approval.md (in part)

## Decision

Sharing an MCP server across chats has no approval step. The gateway shares
every local (stdio) server nobody decided about, including servers declared in
crewmate files, and admits its launch the first time it sees it and whenever its
command, arguments or env change; the launch store stays the audit record. A
person controls sharing only by turning one server off, or sharing off for the
whole Kiro Crew, or by limiting a server to some sessions, and an off survives
every restart. There is no "Always trust changes to this server" choice, because
every change is admitted. In place of the person's look at the launch, a shared
backend runs inside the same global `standard` sandbox each chat's agent runs in
(Linux and macOS), with no exemption for a server that starts a browser; Kiro
Crew's own managed servers are the one exception.

Unchanged: a server whose arguments or env name the chat's folder, a disabled
server, a never-pooled server and a remote server stay per chat. A session
confined beyond the default `standard` sandbox tier keeps its servers per chat
and never attaches to a shared backend. Per-tool permission prompts are not part
of this decision and still ask.

## Why

- Asking a person to approve each server, and again on each update, put a step
  in front of an optimization most people never knew to turn on.
- The sandbox gives a wrapped shared backend the fence a per-chat copy already
  has, so for those servers removing the look at the launch does not widen what
  the backend can reach.
- Where the sandbox does not fit, a session whose own fence is tighter than
  `standard` is not given a weaker shared one.
- The crewmate folder `~/.kiro/mates/agents` gets no new seal: the maintainer
  accepts that an agent in any session can edit a crewmate file and get a
  server started by the shared daemon, inside the `standard` sandbox on Linux
  and macOS and unsandboxed on Windows. The gateway therefore launches from a
  folder an agent can write, which is why this entry supersedes the 2026-11-20
  agents-ask entry for that folder; the rest of that entry stands.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/43744#issuecomment-7233433230 --
  Bolin Chen restating this decision on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/43744#issuecomment-7233582328 --
  Bolin Chen's addendum: the crewmate folder part supersedes the 2026-11-20
  agents-ask entry in part.
- `docs/request-for-change/rfc-mcp-zero-approval-sharing.md` -- the design this
  decision settles (decisions A to E in its §5).
