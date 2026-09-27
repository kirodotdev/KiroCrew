---
title: An owner-written MCP autoApprove is respected
status: accepted
author: Bolin Chen
created: 2026-09-27
last-audited: 2026-09-27
audited-at: 94bcda2f75
doc-pr: null
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---

# RFC: An owner-written MCP autoApprove is respected

- Changed default: `mcp.honour_auto_approve` goes from off to ON.
- The enterprise governance ceiling is unchanged and still wins.

## The decision

An `autoApprove` list the owner wrote themselves, in `~/.kiro/settings/mcp.json`
or directly in an agent file, is KEPT in the agent config Kiro Crew writes. The
setting that decides it, `mcp.honour_auto_approve`, defaults to on. Setting it to
a real `false` restores the strict floor, where every verb no server spec declares
is dropped and those tools go back through the approval gate.

## What it replaces, and why that shape was wrong

The floor shipped the other way round: any `autoApprove` no managed or edition
spec declared was dropped on an ungoverned host, and keeping one required finding
an undocumented config key. The reasoning was sound as far as it went. kiro-cli
approves an autoApproved MCP tool locally and emits no permission request, so
`hooks.on_tool_call` never runs and Kiro Crew's own tool gate never sees the call;
a verb hand-added to a shared MCP config therefore exempted itself from the gate
silently, and fail-closed is the right instinct for a gate bypass.

What that misses is whose decision it is. The list is not something that arrives
by accident or from a third party: the owner typed it into their own file, about
their own tools, on their own machine. Deleting it:

- removed the only way they had to express "stop asking me about this tool",
- did so SILENTLY, with a log line and an audit event neither of which a person
  reads, and
- offered as its remedy a single global boolean with no dashboard control, so the
  way back was to hand-edit `config.json` and restart.

A product may decline to honour a dangerous request, but it should not quietly
discard a deliberate one and leave nothing in its place. That is the defect this
RFC records, and respecting the list is the smaller of the two risks: the owner
already had to write it.

## Boundary: only the owner's own list, and only a usable one

"Owner-written" is a claim about PROVENANCE, so the filter tests it rather than
inferring it from "no spec declared this". Two sources are not the owner and are
excluded from the honoured path, which leaves them on the strict floor exactly as
before:

- An app's own server. `apps/bridges.py` keys the ones it registers into the
  shared config `<app>:<server>` and grants them from the manifest, not from any
  choice the owner made, so honouring one would let a third party exempt its own
  tools from the approval gate with no card. Any `:` in a server name is read as
  that shape, which means an owner who used one keeps getting cards: the ambiguous
  case fails closed.
- Everything a writer materializes for an APP. That writer declines the opt-in
  outright rather than naming the app's keys, because naming them is an enumeration
  that goes stale: the manifest's servers, the shipped agent spec's own
  un-namespaced keys and the per-agent policy's `servers` keys were each found only
  after the previous one was covered, and the policy is app-controlled state that
  may carry `autoApprove` (a shipped builtin already writes one). Nothing on that
  map is the owner stating a preference about their own tools; what a spec DECLARES
  still survives, and everything else routes through the approval gate. The
  shared config, whose map genuinely mixes the two, names the app's `<app>:`-prefixed
  entries instead, which is exact and closed.
- An entry Kiro Crew authored in a file it does not own, which carries the
  `x-kirocrew` provenance marker. That is our own emission.

A value that is not a `list[str]` is not honoured either. The floor this replaces
coerced any other shape to `[]` and popped the key, so a wrong-typed value never
reached disk; preserving one writes an agent spec kiro-cli's strict parsing
rejects, and nothing repairs it -- the self-heal covers a MISSING spec file, not
an invalid one, so every later rebuild re-preserves the same bad value. Falling
through to the floor keeps the recovery path that already exists.

## Boundary: the ceiling is not the owner's to widen

`may_skip_gate_now` is unchanged and remains authoritative. A ref a governance
ceiling has an opinion about keeps nothing, whatever `mcp.honour_auto_approve`
says, because the ceiling is the OPERATOR's policy loaded from a trust root the
agent process does not own, not a preference of whoever is using the machine. So
the enterprise path is untouched by this change, and the change is strictly about
the floor an ungoverned host applies to its own owner.

The same reasoning bounds the reverse direction: what a managed or edition spec
declares is Kiro Crew's own emission rather than the owner's choice, so it is kept
either way and is not reported as a grant.

## Visibility, which is the other half of respecting a choice

An exemption GRANTED is as much a permission decision as one revoked, and until
now only the revocation appeared in the security feed. So honouring an
owner-written list emits `mcp_auto_approve_honoured`, naming the server and the
undeclared verbs. An operator auditing a host can now see both why a tool started
prompting and which calls are skipping the gate, from the same feed.

## What is deliberately NOT in this change

- **No dashboard control for the setting.** It belongs in Settings and its absence
  is part of what made the old default hard to live with, but the key is now on by
  default, so the missing control no longer blocks anyone from the behaviour they
  asked for. It stays a config key until someone designs where it goes.
- **No per-server granularity.** One boolean covers every server. A per-server map
  would let an owner honour their own list on one server while keeping the strict
  floor everywhere else; nothing here forecloses that, and the single boolean is
  what the current shape already supports.
- **No change to `allowedTools`.** That is the other route around the gate and it
  keeps its own ceiling filter, unchanged.

## Consequences

- An install that carried a hand-added `autoApprove` and lost it gets it back on
  the next spec rebuild, because the rebuild preserves the entry it reads from
  disk. An install whose file was already rewritten without the key has nothing to
  restore and the owner writes it again.
- A host that wants every MCP call gated sets `mcp.honour_auto_approve` to
  `false`. That is a restart-scoped setting: the spec is rebuilt at startup, so
  turning it off does not retract a grant already in the file.
- Nothing about the change is visible to a user who never wrote an `autoApprove`,
  which is the common case.
