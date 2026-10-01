---
title: Discovery-registry admission in the public governance ceiling
status: draft
author: hkrishg-aws
created: 2026-10-01
last-audited: 2026-10-01
audited-at: 0d0b4598d
doc-pr: 16007
implementation-prs: []
tracking-issues: [12677]
supersedes: []
superseded-by: []
---

# RFC: Discovery-registry admission in the public governance ceiling

Status: `draft`. Nothing proposed here is implemented by this document.
All repository references are measured at the `audited-at` revision.
Related: [#12677](https://github.com/kirodotdev/KiroCrew/issues/12677).
The proposed design records the author's narrowed recommendation, not acceptance.

## Summary

Make external discovery-registry admission expressible in `security_policy.json`
with one proposed `registries` ruleset, shared by skill and MCP catalogs.
Keep the edition's policy as an additional restriction.
The single decision requested is whether this permission belongs in the public
governance ceiling at all, or should remain edition-only.

## Motivation

An operator of the public build cannot restrict which external registries the
gateway admits using its security-policy document. The built-in catalog has no
discovery-registry row (`SCOPE_CATALOG` in `src/kiro_crew/platform/governance.py`),
and the default registry policy returns True (`DefaultExternalAccessPolicy.admits_registry`
in `src/kiro_crew/platform/defaults.py`).
The existing seam allows an edition to restrict admission instead
(`ExternalAccessPolicy` in `src/kiro_crew/platform/interfaces.py`). Agent
`skill_discover` in `src/kiro_crew/mcp_tools/skills.py` also uses the gateway's
discovery route.

## Goals

- Let the existing policy document narrow external-registry admission by host.
- Preserve edition refusals and unchanged behavior when the scope is absent.
- State shared-catalog and restart costs before implementation.

## Non-goals

- Implementing this proposal, invalidating live catalogs, or adding per-kind rows.
- Governing all network traffic, manual/local installs, or MCP tool execution.
- Changing host-pattern validation. That is
  [#13966](https://github.com/kirodotdev/KiroCrew/issues/13966), with the separate
  [#14004](https://github.com/kirodotdev/KiroCrew/pull/14004) work.
- Adding a `policy validate` diagnostic for catalog coverage.

## Design

### One host-keyed row

Add `registries` as a `RULESET` with `matcher="host"`, without a new evaluator.
The catalog is append-only (the append-only contract comment above `SCOPE_CATALOG`
in `src/kiro_crew/platform/governance.py`);
ruleset composition is already defined (the "The four archetypes (one composition
algebra each)" section of `docs/system-specs/modules/governance.md`).
An example fragment admitting all three external built-ins is:

```json
{"registries":{"mode":"allow","allow":["skills.sh","api.github.com","registry.modelcontextprotocol.io"]}}
```

Match `_url_host(api_base)`, never the provider name or the raw URL.
The host matcher is case-insensitive fnmatch, not URL parsing
(`_match_host` in `src/kiro_crew/platform/governance.py`); `network.egress` already
extracts hosts this way (`classify_tool_args` in `src/kiro_crew/platform/governance.py`).

The built-in starting hosts are distinct (`_API_BASE` in
`src/kiro_crew/skill_providers/skillsh.py`, `_GITHUB_API_BASE` in
`src/kiro_crew/dashboard/handlers/discover.py`, and `_API_BASE` in
`src/kiro_crew/mcp_providers/official.py`). One row can distinguish them today.
**Cost:** allow-mode binds both catalogs. Listing only skill hosts also excludes
the official MCP registry. A host serving both kinds cannot be allowed for one
and denied for the other. The existing `kind` argument remains an edition-policy
input, not part of the host matcher (`ExternalAccessPolicy.admits_registry` in
`src/kiro_crew/platform/interfaces.py`).

### Admission at the shared gate

Require both the governance verdict for the extracted host and the composed
edition verdict in `_shared.admits_registry`, retaining its existing fail-closed
and critical-audit path (`admits_registry` and `_admits` in
`src/kiro_crew/dashboard/handlers/_shared.py`). Do not put the ceiling check
only in `DefaultExternalAccessPolicy`: editions replace that adapter slot
(`PlatformContext` and its `external_access` field in `src/kiro_crew/platform/context.py`).

All four existing external-registry gates already use this helper (`_build_registry`
in `src/kiro_crew/dashboard/handlers/discover.py` and `_build_registry` in
`src/kiro_crew/dashboard/handlers/mcp_discover.py`). The edition MCP capability
provider is outside these gates (`_build_registry` in
`src/kiro_crew/dashboard/handlers/mcp_discover.py`).

### Semantics and timing

Use `agent_backend`'s distinction between what a build can serve and what a
deployment may select (the `agent_backend` section of `docs/system-specs/modules/governance.md`). Unlike
its additive-over-a-protected-backend-floor rule
(the "Additive over a floor" semantics ruling in the `agent_backend` section of
`docs/system-specs/modules/governance.md`), this row has **no protected
external registry**. Allow-mode is exclusive across external registries; empty
allow excludes them all. Deny-mode permits unmatched hosts; absent scope adds
no restriction (`ScopedRuleset` and `_query_level` in
`src/kiro_crew/platform/governance.py`). Edition refusal still wins.
The existing ADD-only security floor is unchanged
(the "ADD-only security floor" section of `docs/system-specs/modules/platform-context.md`).

**Next-gateway-start guarantee:** policy changes do not invalidate an already
built catalog. Restarting rebuilds both under the then-current policy on first
use. Both registries are lazy process caches (`_get_registry` in
`src/kiro_crew/dashboard/handlers/discover.py` and `_get_registry` in
`src/kiro_crew/dashboard/handlers/mcp_discover.py`), so a change before a
catalog's first use can affect that build. This does not promise a boot-time
snapshot of unused catalogs. It follows the explicit restart-contract approach
of `agent_backend`, not live revocation (the "When a policy change binds: the next
gateway start" contract in the `agent_backend` section of
`docs/system-specs/modules/governance.md`).

## Migration plan

1. Record the maintainer's ceiling-versus-edition decision. Exit criterion: this
   document records the answer; acceptance is explicit, not inferred from merge.
2. **Blocked on public-ceiling acceptance:** implement the one row and shared
   check, update the owning governance spec and policy example. Exit criteria:
   tests cover absent scope, empty allow, both catalogs, extracted hosts,
   hostless inputs, edition refusal, policy/audit failure, and cached-versus-fresh
   catalog behavior. If edition-only is chosen, no implementation follows here.

## Backward compatibility

Absent scope leaves the default adapter's permissiveness unchanged. Configured
allow-mode can intentionally remove both external catalogs; no external-registry
floor is promised. The RFC changes no runtime behavior.

## Security considerations

This is admission of a declared starting host, not an egress allowlist for every
request. Provider redirect protections remain separate; the skills provider's
download allowlist includes content hosts beyond its API host
(`_ALLOWED_HOSTS` and its redirect-target comment in `src/kiro_crew/skill_providers/skillsh.py`).
An edition provider without `api_base` supplies empty input (`_build_registry` in
`src/kiro_crew/dashboard/handlers/discover.py`);
`_url_host` in `src/kiro_crew/platform/governance.py` returns empty for hostless inputs.
Ordinary ruleset matching applies, so allow-mode excludes empty unless a pattern
such as `*` matches it, while deny-mode can permit it. This adds no URL validator.
An admission audit records the gate verdict, not proof that a request occurred.

## Alternatives considered

- **Public ceiling:** registry admission is already a target-pinned security
  decision (`ExternalAccessPolicy` in `src/kiro_crew/platform/interfaces.py`). Crew's ceiling governs
  its own host actions (the opening scope statement in `docs/system-specs/modules/governance.md`), and public
  default composition already loads it (the "Boot sequence" section of `docs/system-specs/modules/platform-context.md`).
  Safety first supports making this permission available without an edition
  adapter (tenet 1, Safety first, in `TENETS.md`).
- **Edition-only:** the existing composition seam already owns this variation
  (the "Model" section of `docs/system-specs/modules/platform-context.md`,
  with the permissive public baseline in `DefaultExternalAccessPolicy.admits_registry`
  in `src/kiro_crew/platform/defaults.py`). Keep the public catalog baseline and
  avoid another coupled, restart-bound scope whose name could imply broader
  protection than it delivers. Easy to use supports restraint (tenet 3, Easy to use, in `TENETS.md`);
  governance has deliberate scope boundaries, not exhaustive egress coverage
  (the "Scope boundaries (documented, not gaps)" section of `docs/system-specs/modules/governance.md`).

## Open questions

**Does external discovery-registry admission belong in the public governance
ceiling, with the narrowed design above, or should it remain edition-only?**
Please record either choice and its reason. This document stays `draft` until a
maintainer explicitly accepts the proposed public-ceiling design.
