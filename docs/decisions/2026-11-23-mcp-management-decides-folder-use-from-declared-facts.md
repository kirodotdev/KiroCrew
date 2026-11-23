# MCP Management never asks whether a server needs the project folder; it decides from declared facts and keeps unknown servers in each chat

Decided by: Ray Xu (maintainer, @buluoray)
Date: 2026-11-23

## Decision

MCP Management no longer asks "does this server need to start in your project
folder?" before sharing it: a server is shared without asking only when a
declared fact says it does not use the project folder (Kiro Crew's own servers,
a server an edition lists as folder-free, or an app server whose manifest says
so), a server the gateway sees ask for the chat's roots (`roots/list`) stays in
each chat whatever was declared, every other server stays in each chat with no
question, the person can still turn Share on for any server on its Sharing row
(that switch is their answer), and answers already stored in
`mcp_gateway.cwd_use` are kept. This replaces the "Ask in plain words" and
"No automatic detection" lines of
`docs/request-for-change/rfc-mcp-routed-server-working-dir.md` (sections 3 and
4) for this question; the RFC's rule that nothing is shared on a guess still
holds, because config hints such as a `.` argument only ever keep a server in
each chat.

## Why

- Users read the question as a file-access question and answered Yes for
  everything, so nothing got shared.
- Kiro Crew should not ask what it can work out, and should fall back to the safe
  answer when it cannot.
- Keeping an unknown server in each chat costs memory, never correctness, and one
  switch on its row still shares it.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/41655 -- the change this decision governs; Ray Xu restates the decision on the pull request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/41646 -- the wording fix this replaces.
