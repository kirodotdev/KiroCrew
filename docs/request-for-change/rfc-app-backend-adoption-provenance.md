---
title: App-backend adoption is attribution-gated
status: draft
kind: decision
author: kirocrew-worker (drafted for maintainer decision)
created: 2026-09-25
last-audited: 2026-09-25
audited-at: 27dbd6db5f
doc-pr:
implementation-prs: [13650]
tracking-issues: [13403]
supersedes: []
superseded-by: []
---

# RFC: App-backend adoption is attribution-gated

Status: draft. This document exists to put a decision on the base branch, because
the change it covers REMOVES two capabilities the current specs describe. It asks
maintainers to accept the removals, or to reject them and say what should ship
instead. Nothing here is implemented by this document; the implementation is
[#13650](https://github.com/kirodotdev/KiroCrew/pull/13650).

- Author: drafted by the agent working #13403; the acceptance decision is a
  maintainer's, not the author's.
- Related: `docs/system-specs/modules/app-kit-platform.md` section 17 (backend health),
  `docs/app-kit/api-reference.md` (gateway origin freshness),
  `docs/request-for-change/rfc-app-sandbox-isolation.md` (the mask this relies on).

## Problem

Adoption admits a listener as an app's backend on two facts, and neither names the
process or the code it runs: the manifest-declared port is occupied, and something
on that port answers the declared health path. A backend that outlives its app's
uninstall and rebinds the port satisfies both, so the next install that happens to
use the same app name adopts it. The gateway then addresses a process the install
did not place as that app's backend, reports its health as the app's, and aims stop
at its PIDs. Adopted instances are excluded from the startup stale-reap by design,
so the misattribution renews itself for as long as the survivor lives.

That is #13403. Reaching it needs no misconfiguration: an app whose manifest
declares a fixed port, plus any detached child that survives the uninstall.

## The decision this asks for

Adopt only owners the gateway can attribute to a spawn it recorded for that app.
The record already exists (`config_dir()/app_backends.pids.json`: pid, start
instant, and a per-spawn instance token that also travels on the child's
environment where the process cannot rewrite it).

Accepting this removes two capabilities that are currently described as working.
They are stated here plainly because they are the reason this document exists.

### Removal 1 -- an externally-managed backend is no longer adoptable

`app-kit-platform.md` described an "**adopted** externally-managed backend, whose
contract is to survive gateway exit and be re-adopted on the next start", and the
recovery path was written for when "the EXTERNAL supervisor put something back,
possibly a different process". Under attribution, a listener no spawn of ours
recorded is refused, so an operator running an app's backend under their own
supervisor can no longer have it adopted; the app's start refuses while that
listener holds the port.

This is not separable from the fix. A clean uninstall drops the app's row, so the
survivor of a previous install meets an empty record -- which is exactly the state
an operator's own never-recorded backend is in. The two cases are
indistinguishable from inside the gateway, so admitting one admits the other.

### Removal 2 -- adoption refuses where the fence cannot be proven

The record is only evidence against a process that could not have written it. The
sandbox masks it, but a mask lives in a MOUNT NAMESPACE and a namespace is fixed
when a process is spawned: a backend started before the mask existed sees the
record as an ordinary writable file, and nothing the current gateway does reaches
an already-running namespace. So the implementation reads the mask back out of
each captured owner's own mount view (`/proc/<pid>/mountinfo`) and refuses where
it cannot.

That makes adoption refuse on every host with no per-process mount view: macOS,
Windows, and Linux with no sandbox backend or `agent.sandbox='off'`. The refusal
names which case it met, so an operator can tell a closed door from a blind one.

This adds no platform debt that was not already present: the `tree` attribution
route reads `/proc/<pid>/environ` and is Linux-only for the same reason.

## Known residual, and the stricter alternative

The owner-view fence is not a complete closure, and this document does not claim
it is. The gate constrains each captured OWNER, never the row's AUTHOR. A pre-mask
survivor can therefore write a row naming a DIFFERENT listener that is itself
currently fenced -- another app's live backend bound to the port the target app
declares -- and the `leader` route attributes that accomplice. Another process's pid
and start instant are readable same-UID, so the forgery needs no secret.

Closing that requires the trust root to leave the disk: attribute only against
spawn records the gateway process holds in memory. That closes it completely, and
costs a third capability -- adoption across a gateway restart ends, including the
case the stale-reap deliberately leaves standing (a dead leader whose group member
still holds the port).

So maintainers are choosing between:

| option | closes the forgery | also removes |
|---|---|---|
| owner-view fence (as implemented) | narrows it; an accomplice-naming row still passes | removals 1 and 2 |
| in-memory records only | yes | removals 1 and 2, plus adoption across a gateway restart |

## Alternatives considered

- **Sign the rows with a gateway-held secret.** Rejected on measurement: every
  candidate key on disk is equally readable to a namespace the mask never covered,
  so signing raises the bar for future spawns without closing the pre-mask case.
- **Constrain the row's author rather than its owners.** Unavailable: the record
  sits in a directory those namespaces can write, and an already-running namespace
  cannot be retro-masked.
- **Keep adoption unattributed and mitigate elsewhere** (for example a wider
  uninstall port probe). Rejected: the uninstall probe is a single instant and a
  survivor that rebinds after it is reported stopped, so this leaves the headline
  defect open.

## Questions for maintainers

1. Accept removal 1, or is the externally-managed contract one to keep? Keeping it
   requires a way to tell an operator's backend from an uninstall survivor that the
   gateway does not have today.
2. Accept removal 2, or should a host with no mount view keep adopting on the
   record's word alone? The second is a documented decision to trust a file that
   host's own children can write.
3. Owner-view fence, or in-memory records only? The second closes the residual and
   ends adoption across a gateway restart.
