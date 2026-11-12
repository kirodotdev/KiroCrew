# A launch added beside an approved one reads as a new launch, not a changed command

Decided by: Bolin Chen (bolichen97), Kiro Crew maintainer
Date: 2026-11-07

## Decision

On MCP Management, a `changed_needs_reapproval` row whose approved launches all
still run beside one that is not approved reads **Paused · new launch to
approve**, and `kirocrew doctor` names it `new launch not approved yet`; a row
whose approved launch changed keeps **Paused · command changed**. Both read the
refusal's own displays, with no new reason code, store field or status field.

## Why

- A second agent declaring an approved server with its own arguments paused it
  as "command changed", although nothing the operator approved had changed.
- "Command changed" stays where an agent-writable spec really changed an
  approved launch, which is the case the refusal exists for.
- Not chosen: neutral wording for every changed row (it drops "command changed"
  from that case), recording who approved in the store, approving every launch
  an edition finds at first start, a partly shared row state, and stopping an
  edition's own first-start approvals.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/33346 -- the maintainer's issue
  stating the outcome: a launch added beside an unchanged approved one reads as
  a new launch waiting for approval, and an approved launch that changed keeps
  "command changed".
- The maintainer's choice of this option on 2026-11-07, restated on the pull
  request that adds this entry.
- https://github.com/kirodotdev/KiroCrew/pull/29585 -- the open pull request
  that edits the same paused-row paragraph of `docs/architecture/mcp.md`; this
  change lands after it or together with it.
