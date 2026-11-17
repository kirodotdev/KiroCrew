# A runtime scope with no agent runtime left is the operator's to stop

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-10

## Decision

The gateway does not stop a runtime scope that has no agent runtime left in it, or that holds a process Kiro Crew cannot attribute to this install; `kirocrew doctor` lists each such scope that no session tracks and that is older than ten minutes, with its memory, task count, age and the `systemctl --user stop <unit>` command that ends it, and the operator decides.

## Why

- What is left in such a scope can be a server an agent was asked to keep running. Nothing on the host tells that apart from an MCP server or helper its runtime left behind, so an automatic stop would end work a person still uses.
- The scope reaper already keeps these scopes on purpose: marker inheritance alone never authorizes a scope-wide stop. A report showed the cost of that residual (memory and tasks held for days by scopes from gateways that had exited), and it asked for either an automatic stop or a doctor listing with a cleanup command. The listing meets it, kills nothing, and leaves an automatic stop possible later on stronger ownership proof.
- Failed transient units hold no process or memory; they are not part of this decision.

## Evidence

- https://github.com/kirodotdev/KiroCrew/issues/33354 -- the report, and the reporter's reaper log showing every such scope kept for no agent-runtime anchor or an unattributable member.
- https://github.com/kirodotdev/KiroCrew/issues/33354#issuecomment-6757964091 -- the maintainer's decision.
- `docs/system-specs/modules/session.md` §Reaping abandoned agent scopes -- the reaper's rules and this residual.
