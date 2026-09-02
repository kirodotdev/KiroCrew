# Connections Disconnect: what "disconnected" is allowed to mean

Disconnect used to remove the MCP entry and nothing else. That is not a
disconnection — it is hiding one. kiro-cli's stored grant artifacts stayed on
disk, so the next Connect found a live refresh token and resumed the old grant
without asking, while the card had already told the user this machine's
connection was gone.

## Three local things, and one thing that is not ours

`POST /api/connections/disconnect` disposes any in-flight mint (a grant arriving
moments after the user asked for the connection to be gone is not a race worth
keeping), then — in ONE locked transaction — removes the MCP entry from the
scopes that configure this provider and unlinks the stored grant artifacts.

What it deliberately does **not** do is revoke at the provider. Nothing in this
process can, only the provider can, so the response never claims the upstream
grant is dead and the card keeps offering the provider's revoke page. The copy
was already honest about this before the grant was actually being deleted; the
change here is that the behaviour finally matches it. **Cancel** still never
revokes: a cancelled *new* connect suppresses its own feedback, so a shared grant
would vanish silently. Only a deliberate Disconnect touches the credential.

## Why the response carries several answers

The artifacts are a **pair** (token + registration) and either half can fail to
unlink alone, so "the token went" is not the same fact as "the grant is gone",
and neither implies the config entry came out.

| Field | Established |
|---|---|
| `grantRemoved` | at least one artifact was unlinked by this call |
| `grantSurviving` | labels still on disk after an ATTEMPTED unlink, re-stat'd rather than inferred from what the delete loop believed it removed |
| `entryRemoved` | at least one scope configured this provider's endpoint under our slug, and that entry is gone |
| `grantSharedWith` | other entries pointing at the same endpoint, which is why the grant was deliberately kept |
| `grantCensusIncomplete` | a source the decision needed could not be read, so the grant was kept with no sharer to name |

A survivor is the one outcome that must not be rounded up. The card renders it
through `role="alert"` rather than `role="status"` — announced by a screen reader,
and pointing at the provider's revoke page — because a local grant outliving the
click is precisely the state this endpoint exists to prevent.

Only pairs this Disconnect actually **tried** to unlink are re-stat'd, which is
what makes that alert safe to fire on sight. A pair kept on purpose — for a named
sharer, or because the census had a gap — is still on disk *by design*, so
re-stat'ing it reported a correct refusal as a surviving artifact and forced a
precedence ladder to decide which survivors were real. Restricting the re-stat to
attempts deletes the ambiguity instead of ranking it: `grantSurviving` now means
failed unlink and nothing else, the audit needs no `or grant_shared_with` escape,
and the card's clause order carries no hidden claim.

## A grant is keyed by `grant_key`, so it is not always ours to delete

`grant_key` is a sha256 over origin + path, query dropped and path kept verbatim.
**One artifact pair therefore serves every entry whose URL hashes to that key**,
whatever those entries are called. That makes the revoke a wider act than the
purge beside it, and it needs its own ownership question, asked with the
credential's OWN identity function:

- Entry identity is `normalized_endpoint` on name **and** url — the pair the card
  matches on. It keeps the query (a query can select a different server) and
  strips a trailing slash.
- Grant identity is `grant_key` equality, because the artifacts are files *named
  by* `grant_key`. The two disagree in both directions: a `?workspace=` variant is
  a different endpoint but the same pair; a trailing-slash variant is the same
  endpoint but a different pair. Testing the credential with the endpoint
  comparator would delete a shared grant in the first case, and in the second
  strand a live one — reported as a deliberate keep.

The sweep reads the **raw specs**, disabled entries included (a switched-off
server still owns its grant), with the probe view unioned in, so neither
disabling an entry nor holding it only in agent config makes its grant deletable.

## One lock, one census

The destructive acts share one judgement, so they share one read and one lock
hold. Splitting them produced three data-loss paths of the same shape — ownership
decided over an incomplete census, or acted on outside the lock that judged it:

- **The census was incomplete.** Discovery merges exactly one agent spec
  (`kirocrew.json`), so a spec the user wrote by hand, or one an app materialized,
  was invisible — while kiro-cli authorizes from it and shares the pair the
  endpoint names. `spec_census` globs the whole agents directory alongside the
  `mcp.json` scopes, precisely because the specs that matter are the ones Kiro
  Crew does *not* own. An agent spec contributes sharers but is never a purge
  target: Kiro Crew does not own a user's agent file.
- **The purge was wider than the judgement.** Ownership was read from the merged
  winner while `_purge_server_config` removed the name from *every* scope, so a
  same-named entry in a lower-priority scope pointing elsewhere was deleted
  unseen. The purge now takes the scopes ownership matched
  (`_purge_server_config(scopes=...)`). Passing `None` still means every scope,
  which is what an uninstall wants — there the *name* is going away, not one
  endpoint under it.
- **The revoke ran after the lock released**, so an entry created at the same
  endpoint in that window lost a grant it had not yet used. Holding the lock
  across two `unlink` calls under the user's home is the accepted cost, and it is
  the exposure every other writer under this lock already has.

Both the purge and the revoke go through `_offload_config_write`, not a bare
`to_thread`: a cancelled request task would otherwise release the lock while the
worker is still working, letting a concurrent purge interleave with a stale
snapshot or reopening the revoke window this transaction closes.

**Fail closed, asymmetrically**, because the two acts need opposite evidence. The
revoke needs the *absence* of a sharer, which an unreadable source can hide, so
one unreadable source keeps the grant and says so — and "unreadable" covers every
shape that means *entries unknown*: unreadable bytes, malformed JSON, a directory
where a file should be (the unenumerable-agents-dir sentinel), and structurally
invalid documents (`[]`, `{"mcpServers": []}`). Only genuine absence and a
document that declares no entries read as empty. The purge acts only on
*positive* evidence — a scope it read and matched — so an unreadable scope is
simply not purged; refusing the whole request would leave the user unable to
disconnect because of a file with nothing to do with this provider. This is also
why `spec_census` does not call `_load_mcp_json_by_source`: that function warns
and skips an unreadable source, which is right for a view and wrong for a
destructive decision.

A sibling URL outside the provable set (non-ASCII or percent-carrying host,
dot-segment or non-ASCII path) fails closed as census-incomplete — see the
identity section. *Within* the provable set, a host Python's IDNA still refuses
(empty or >63-char label) is skipped rather than kept: kiro-cli serializes such
a host verbatim, it differs from the registry host, so its key provably is not
ours — and failing closed on it would let one junk line block every disconnect.
Enumeration of the agents dir goes through `os.listdir`, not `Path.glob`,
because glob suppresses scan errors: an executable-but-unlistable directory
yields zero entries with no raise, reading a hidden sharer as absent. A missing
directory (fresh machine) stays genuine absence.

Disconnect is **owner-only**, checked before the request is even parsed: this
endpoint deletes machine-global config and OAuth artifacts, the same server-side
boundary every mutating agents route enforces, and presigned dashboard links admit
non-owner subjects.

**The raw census is authoritative; the probe view does not get a second vote.**
`list_servers` is a merged read whose sources include the rendered agent config,
so a mirrored entry reappears there as a plain row with its provenance lost.
Judged again, a mirrored same-name query variant (same artifact key, different
endpoint) reads as an independent sharer and blocks the revoke while the purge
removes both entries — leaving the credential behind. So a configured row whose
`(name, url)` the raw census already carries is skipped: it has been judged once,
with provenance. A row the census does *not* carry still votes, so a source the
census misses can never lose a real sharer.

Residuals, stated rather than papered over: the handler awaits `cancel_mint`first, so a wedged teardown can keep a Disconnect busy for its shutdown timeout
(firing it as a task would let a grant arrive after the user disowned the
connection); the census reads the per-project `.kiro/agents` dirs of every OPEN
chat slot as well as the user-level one, so a project with no open slot is still
invisible (slot state is what the dashboard actually knows about the checkouts
kiro-cli runs in); and the transaction lock serializes
the gateway's own writers (the mcp.json writers and, as of this change, the
agent-config PUT) but cannot serialize *external* writers — kiro-cli and hand
edits mutate the same census sources from outside the process, and a write of
theirs landing between the census read and the unlink is judged by a snapshot
that never saw it. No gateway-side lock can close that; it is inherent to
config files the gateway does not own.

Two sibling paths are knowingly left alone: removing the same server through the
plain MCP surface (`api_mcp_apply` → `_purge_server_config`), and removing it
from the Kiro global config through the REST handler
(`DELETE /api/mcp/servers/{name}`, which rewrites only that one file), both take
the entry out with no grant census and leave the grant pair on disk, so an
OAuth'd server uninstalled either way keeps a usable refresh token. The deferral
is deliberate rather than an oversight — nothing on either surface claims the
authorization was revoked, so neither produces the dishonesty this note exists
to remove, and revoking there would need the same ownership census this endpoint
runs, on paths whose callers include bulk edits that were never a withdrawal of
consent.

The rebuild runs **inside** this transaction, through the shielded offload. A
post-lock rebuild snapshots the config before a concurrent Disconnect's purge and
can write last, resurrecting an entry whose grant that transaction just deleted —
a configured provider with a dead credential, reachable with two dashboard tabs.
Nesting it is safe for a reason worth recording, because the opposite was assumed
once and was wrong: this endpoint holds the `~/.kiro/settings/mcp.lock` sidecar,
while `rebuild_agent_config`'s internal lock defaults to
`~/.kiro/agents/kirocrew.lock` (`apps/bridges._mcp_lock`). They are different
flocks, so the non-reentrancy that would otherwise deadlock never applies.

## Identity is the endpoint, never the entry name

The card matches a provider to an entry on **name and url** together
(`connectionProviderForServer`), and the purge honours the same pair. Removing by
name alone would delete a user's own server that merely happens to be called
`notion` because they clicked Disconnect on the Notion card. This is the rule
`l1_smoke` already keeps: a registry slug is a label a caller can collide with,
while an endpoint is the thing being talked to. When no scope configures our slug
at this endpoint, `entryRemoved` is false and nothing is purged — but the grant is
still ours to revoke, because it is keyed on the registry URL rather than on
whatever `mcp.json` currently holds.

The census judges ownership *first* and sharer-tests everything that is not ours
— including entries carrying our own name. A `notion` entry at a query variant
holds the same artifact pair (`grant_key` drops the query) while failing the
endpoint test, and a same-named agent-spec entry is never a purge target but
holds a grant like any other; keying the branches on the name let both fall
through the census entirely, which is how a same-named survivor lost its grant.

Two implementations compute the artifact key — this module and kiro-cli's WHATWG
url parser, which percent-decodes hostnames, IDNA-maps Unicode hosts and
normalizes dot-segments before hashing. So key equality is asserted only inside
the **provable set** (lowercase-ASCII LDH hosts, printable-ASCII paths free of
`%`, backslashes and dot-segments), where both serializations are byte-identical
by construction; any URL outside it is *unprovable* and fails closed as a
census-incomplete keep. `%6dcp.notion.com`, `/a/../mcp` and `/a\..\mcp` all name
the registry pair under WHATWG while hashing elsewhere here — the last one
because WHATWG folds `\` to `/` *before* removing the dot segment, while
`path.split("/")` sees one opaque segment and the dot-segment guard never fires,
so the backslash has to be excluded on its own. Guessing on either side of that
divergence is how a live consent dies. And because ownership is
endpoint-keyed (slash-insensitive) while the pair is artifact-keyed
(slash-sensitive), **every owned key's pair is judged and revoked separately**: an
owned entry at `<url>/` holds its own pair, and revoking only the registry key
would purge the entry while its real credential survives behind "Disconnected
locally". Sharers are therefore tracked **per key** — a sharer of the registry
pair says nothing about an owned trailing-slash pair nobody else uses, and one
flat flag skipped that pair's revoke while the purge still removed its entry.

**A mirror is not a sharer.** `_purge_server_config` strips the entry from the
rendered `<agents>/kirocrew.json` and from every `scope.agent_mcp_file` itself, so
those entries are reflections of the scopes this transaction is purging rather
than independent grant holders — counting them made an ordinary Disconnect see its
own reflection as a sharer and skip the revoke *every time*, which is the failure
mode where the feature silently never fires and no census-mocking test can see it.
A hand-written agent spec is the opposite: Kiro Crew never writes it, so its entry
keeps its own grant and must be able to block a revoke. The exclusion is scoped to
the justification — with nothing owned the purge does not run, so a mirrored entry
is a real holder again and blocks normally.
The census-incomplete user copy says a source "could not be read";
for an unprovable entry URL that is a slight imprecision (the file was readable,
the entry could not be safely compared) accepted to avoid a 13-bundle string
change — the essential claim, that another user of the grant could not be ruled
out, is exact.

## The credential boundary

The artifacts are stat'd and unlinked, never opened. kiro-cli owns the OAuth
chain and its store ([mcp-oauth-ownership.md](mcp-oauth-ownership.md)); the
gateway may observe and delete, never read. A regression test pins it by making
`open`, `read_text` and `read_bytes` raise for the duration of a revoke. Every
one of those stats runs off the event loop, because they touch paths under the
user's home and stall as long as a network mount does. `surviving_grant_artifacts`
keeps `artifact_presence`'s three-valued answer rather than collapsing it: an
unreadable artifact counts as surviving, since reporting it gone would claim this
machine's connection is dead while a usable refresh token may still be there.

The single-file `{sha256}.json` form that shares the cache directory belongs to
AWS SSO and is deliberately never touched.

## Deferred: proving the grant still works

Upgrading **Test** from an MCP-level probe to an authenticated round-trip is not
here. It needs a provider HTTP probe and a real runtime activation (kiro-cli holds
the bearer), plus a verdict vocabulary reconciled against the status enum that
shipped with the tiers note. Test is not broken today; it performs a real probe,
just a shallower one than its name suggests. Splitting it keeps the
security-relevant fix from waiting on the expensive one.
