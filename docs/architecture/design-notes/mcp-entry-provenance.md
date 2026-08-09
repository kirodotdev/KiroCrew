# MCP entry provenance: who may rewrite a shared config entry

Kiro Crew writes MCP server entries into two config files it does not own — the
kiro-global `~/.kiro/settings/mcp.json` and the Claude Code sidecar `~/.mcp.json`.
Users hand-edit both, and other tools write the sidecar. Any sync that can
*update* entries (not just add them) must answer one question per entry:
**did we write this?**

Name presence in the dashboard store cannot answer it. A minimal `{"url": ...}`
entry is byte-identical whether our emitter produced it or a user typed it, so
"our managed server's url moved" and "a different server the user happened to
name the same" reach the write as the same input. Provenance is recorded at
write time instead of inferred at read time.

## The invariant

> An entry in a shared config file is Kiro Crew–managed **iff** it carries our
> marker. Name presence in the store is necessary, never sufficient. An
> unmarked entry is the user's and is never rewritten.

## The marker

Entries Kiro Crew writes into shared files carry one reserved key:

    "x-kirocrew": { "managed": true }

The `x-` form cannot collide with a kiro-cli field (its config structs derive
`rename_all = "camelCase"`, which never produces a hyphen), and kiro-cli
tolerates unknown keys (`McpServerConfig` is an untagged enum with no
`deny_unknown_fields`; schema validation runs against the re-serialized struct,
after deserialization has dropped anything unknown). Anything other than the
exact marker shape reads as unmarked — the predicate fails safe in the
direction of NOT writing.

The marker is a declaration of write authorship, **not a security boundary**.
It defends a shared file against our own writer, not against the file's owner:
the user can strip it (reclaiming the entry — we stop rewriting it, permanently,
see *Reclamation beats migration*) or add it (volunteering the entry for
management). Both directions are fail-safe.

## Write resolution

Every sync write to a shared file resolves to one of three outcomes
(`mcp_provenance.resolve_write`); store-side management remains a necessary
precondition — the marker narrows who may be rewritten, never widens it:

| on disk | store manages name | outcome |
|---|---|---|
| no entry at all | yes | **create** — written stamped |
| any entry present | no | **leave alone** — add-only for a name we do not manage |
| marked entry | yes | **rewrite** — propagation, gated on proof rather than a name |
| unmarked entry present — including when its bytes already equal our emit | yes | **decline** — treated as the user's, preserved, divergence logged |
| present but unparseable — a string, `null`, a list | yes | **decline** — it occupies the name and cannot carry a marker, so it is unmarked, i.e. the user's |

There is deliberately no fourth outcome that stamps an unmarked entry whose
content already matches our emit. See *Reclamation beats migration* below.

Only true absence is a create, and it is signalled explicitly (`ABSENT`) rather
than as `None`: a hand-edited file can hold `"notion": null`, which
`mapping.get(name)` reports identically to a missing key. Treating the two the
same would let the one shape that occupies a name while carrying no marker read
as a free slot, and the create branch would write over it.

Resolution is per **entry**, not per transport or per file. A `command` makes
authorship no more knowable than a `url` does, so a stdio entry in the sidecar
resolves the same way a remote one does. (The kiro-global file's stdio branch is
create-only, so it has no rewrite to gate.)

## Reclamation beats migration

Entries written before the marker existed carry no marker, so they now **decline
permanently** rather than being migrated into management. That is a deliberate
trade, not an oversight.

The tempting migration is to stamp an unmarked entry when the sync would change
nothing about it — its bytes already match our emit, so the write adds the marker
and nothing else. The problem is what else produces those exact bytes: stripping
the marker off one of *our* entries leaves precisely our emit behind. So

- "written before the marker existed", and
- "the user stripped the marker to reclaim this entry"

are **the same disk state**, and nothing in the file distinguishes them. This is
the same undecidability the marker was introduced to escape — the exact-name
collision, one level in. A migration branch would therefore re-stamp reclaimed
entries, and the sync after that is free to rewrite them, which breaks the
promise the marker makes above.

No content test can separate the two, so the choice is which one to serve. This
picks reclamation: it is a live, repeatable user action with no recovery if we
get it wrong, whereas the un-migrated case is a one-time state with a two-click
fix.

**Re-establishing management on a legacy entry: Disconnect, then Connect.** The
delete path removes the name from the shared file outright
(`handlers/mcp.py`, `api_mcp_remove` / `api_mcp_server_detail`, both
`data["mcpServers"].pop(name)` then `_write_mcp_json`). The name is then absent,
so the next sync reaches the create branch and writes the entry **stamped**
(`resolve_write`: `if on_disk is ABSENT: return stamp(candidate) ...`). Nothing
special-cases legacy entries; they rejoin management through the ordinary
authoring path, which is the only path that can honestly claim authorship.

## Interaction with the existing MCP management panel

The dashboard's MCP panel and the marker govern two different decisions, by
design:

- **The panel is the admission authority.** Which servers exist in the store,
  which are enabled, and which are prompted into kiro's own config file is
  decided exactly where it is today. Nothing about that flow changes.
- **The marker is the rewrite authority.** It only decides whether a *later
  sync* may update an entry already sitting in a shared file.

Concretely:

- **Entries added or edited through the panel never carry the marker.** The
  editor strips the reserved key structurally on save, so a hand-added marker
  cannot ride a save back out and volunteer an entry the user did not intend to
  hand over. A panel-authored entry in a shared file is therefore the user's:
  sync will never rewrite it.
- **The dashboard's own store stays unmarked entirely.** The store is ours by
  definition; the marker only ever appears in files we do not own.
- **The emitted agent spec is stripped of the marker.** kiro-cli never sees it;
  the spec is rendered output, not an owned file.
- **Connections sync is the only writer that stamps**, and only for names the
  store manages.

## What this is not

- Not token custody or OAuth grant ownership — see `mcp-oauth-ownership.md`.
- Not the consent-endpoint allowlist (`oauth_endpoints.json`), which governs
  where a user may be *sent* for consent, not which entries may be rewritten.
