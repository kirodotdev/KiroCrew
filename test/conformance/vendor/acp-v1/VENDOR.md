# Vendored Agent Client Protocol (ACP) v1 wire schema

These files are copied **verbatim** from the upstream Agent Client Protocol
repository and are the *authoritative, MeshClaw-independent* oracle for the
Phase 3 black-box conformance gate (`test/test_acp_conformance_blackbox.py` via
`test/acp_bb_schema.py`). The gate validates every frame `meshclaw acp` emits
against these files — never against MeshClaw's own `kiro_crew.acp.types`
constants — so a drift between MeshClaw and the real protocol is caught.

## Provenance (immutable pin)

| Property | Value |
| --- | --- |
| Source repo | https://github.com/agentclientprotocol/agent-client-protocol |
| Git tag | `schema-v1.21.0` (annotated) |
| Tag object SHA | `fe2db5aa7c7f5565424515075c00a66f8f6715d8` |
| **Commit SHA** | `272bf799f35a258c6a4107a0410ed361e83683d3` |
| Tag date | 2026-08-20T19:43:10Z |
| Wire protocol version | `1` (`meta.json` `version: 1`) |
| Schema dialect | JSON Schema Draft 2020-12 |
| License | Apache-2.0 (see `LICENSE`) |
| Copyright | 2025 Zed Industries, Inc. and contributors |
| Retrieved | 2026-08-24 |

The tag→commit pin was verified via the GitHub refs API at vendoring time:
`schema-v1.21.0` (annotated tag `fe2db5a…`) dereferences to commit
`272bf799f35a258c6a4107a0410ed361e83683d3`.

## Files (copied verbatim, unmodified)

| File | Upstream path @ `schema-v1.21.0` | Role in the gate |
| --- | --- | --- |
| `schema.json` | `schema/v1/schema.json` | STABLE wire schema. Source of `$defs/StopReason`, `$defs/ContentBlock`, `$defs/SessionUpdate`, `$defs/PermissionOptionKind`. **Authoritative** oracle. |
| `meta.json` | `schema/v1/meta.json` | Method-name table (`agentMethods`, `clientMethods`, `protocolMethods`) + `version`. **Authoritative** method vocabulary. |
| `schema.unstable.json` | `schema/v1/schema.unstable.json` | Draft/unstable surface. **Informational only** — the gate never validates against it. |
| `meta.unstable.json` | `schema/v1/meta.unstable.json` | Unstable method additions. Informational only. |
| `LICENSE` | `LICENSE` | Apache-2.0 grant (retained per §4). |
| `SHA256SUMS` | (generated) | Integrity manifest; `verify_provenance()` checks it. |

## Raw URLs (immutable at the pinned tag)

```
https://raw.githubusercontent.com/agentclientprotocol/agent-client-protocol/schema-v1.21.0/schema/v1/schema.json
https://raw.githubusercontent.com/agentclientprotocol/agent-client-protocol/schema-v1.21.0/schema/v1/schema.unstable.json
https://raw.githubusercontent.com/agentclientprotocol/agent-client-protocol/schema-v1.21.0/schema/v1/meta.json
https://raw.githubusercontent.com/agentclientprotocol/agent-client-protocol/schema-v1.21.0/schema/v1/meta.unstable.json
https://raw.githubusercontent.com/agentclientprotocol/agent-client-protocol/schema-v1.21.0/LICENSE
```

## Known limitation

No JSON Schema Draft 2020-12 validator (`jsonschema`) is installed in this
offline Brazil workspace, and adding an unpinned dependency is disallowed. The
gate therefore does **not** run full Draft-2020-12 validation of every
`params`/`result` object against `schema.json`. Instead the oracle
(`test/acp_bb_schema.py`) derives its closed **vocabularies** — method names,
stop reasons, session-update kinds, content-block types, permission-option
kinds, JSON-RPC error codes, and the negotiated protocol version — directly from
these vendored files, and performs structural (shape) checks against them. That
is enough to catch: an out-of-schema stop reason, an unknown method, a
non-permitted JSON-RPC error code, a wrong protocol version, and malformed
envelope/update/permission shapes. Full schema-object validation and the
optional `agent-client-protocol` Pydantic cross-check become available if/when a
validator is vendored (see `UPDATING.md`); `test/acp_bb_schema.py` is the single
seam.
