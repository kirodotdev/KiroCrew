# kiro-cli MCP OAuth Token Storage

Where kiro-cli writes MCP OAuth credentials on disk, how Kiro Crew identifies
the exact pair without reading token bytes, and how to disconnect safely. The file
layout is derived from `aws/amazon-q-developer-cli`'s
`crates/chat-cli/src/mcp_client/oauth_util.rs`; Kiro Crew's current behavior is in
`src/kiro_crew/mcp_grant.py` and `src/kiro_crew/connections/ownership.py`.

> Current supported paths: interactive kiro-cli provides `/mcp logout <server>`,
> while Kiro Crew's **Settings > Connections > Disconnect** removes configuration
> it owns and, only after a sharer/ownership census, unlinks the server's paired
> local grant artifacts. Manual file operations are recovery tools, not the normal
> product workflow.

## Storage location

```
~/.aws/sso/cache/
```

Yes — the same directory `aws sso login` writes to. kiro-cli
reuses the path for historical reasons (it's a fork of
`amazon-q-developer-cli`). There is no XDG override.

## File naming

For each remote MCP server with an `https://...` URL, kiro-cli writes
**two paired files** keyed by SHA-256 of the URL:

```
{sha256(origin+path)}.token.json          ← the OAuth bearer + refresh token
{sha256(origin+path)}.registration.json   ← the DCR client metadata
```

Both files use lowercase + dot-suffixed filenames. The hash is computed by
`mcp_client::oauth_util::compute_key`:

```rust
let input = format!("{}{}", url.origin().ascii_serialization(), url.path());
sha256(input)
```

So the input is exactly `<scheme>://<host><port>/<path>` — the URL string
from the agent config's `mcpServers["<name>"].url`, normalized by
`url::Url::ascii_serialization()`. That serialization lowercases the host,
IDNA-encodes Unicode domain names, keeps brackets around IPv6 literals, and
omits the default port.

Compute it from Python:

```python
import hashlib
hashlib.sha256(b"https://mcp.linear.app/mcp").hexdigest()
# → fb39103c7d2edac291c92d23247e0a7d90470b1b349c07b146aba4ee2c81591f
```

## Telling kiro-cli MCP files apart from AWS SSO files

`~/.aws/sso/cache/` mixes three unrelated credential systems. Distinguish by
filename pattern AND JSON shape:

| Files in dir | Owner | Distinguisher |
|---|---|---|
| `{sha256}.token.json` + `{sha256}.registration.json` (paired) | kiro-cli MCP | Two files with same prefix; snake_case keys |
| `{sha256}.json` (single, no `.token.` infix) | AWS SSO | One file; camelCase keys (`clientId`, `expiresAt`) |
| `kiro-auth-token*.json` | kiro-cli identity (Builder ID) | Literal "kiro-auth-token" prefix |

A safe MCP-specific operation must check for the **paired** `.token.json` +
`.registration.json` files at the computed sha256 prefix. The single-file
pattern is AWS SSO and must never be touched by MCP-related code.

## File contents

### `.token.json`

```json
{
  "access_token": "...",
  "token_type": "bearer",
  "expires_in": 86100,
  "refresh_token": "...",
  "scope": ""
}
```

Standard RFC 6749 §5.1 token-response shape. `expires_in` is seconds from
issuance — real expiry is `file_mtime + expires_in`. Linear issues ~24h
tokens; Notion/Atlassian/Anthropic typically 1h; GitHub Apps 1h.

The bearer in `access_token` is what kiro-cli sends as
`Authorization: Bearer <token>` on every MCP request to that server.

### `.registration.json`

```json
{
  "client_id": "wJRUqKFnnxPmEGpu",
  "client_secret": null,
  "scopes": ["openid", "email", "profile", "offline_access"],
  "redirect_uri": "http://127.0.0.1:49521"
}
```

Result of Dynamic Client Registration (RFC 7591). Pins kiro-cli's identity
to the auth server (`client_id`), the OAuth callback port, and the scopes.
`client_secret: null` because kiro-cli is a public client (PKCE-protected).

kiro-cli's DCR sends `client_name: "Q DEV CLI"` — a hardcoded constant in
the binary that **must not be changed** (some servers use it for identity).

## Three independent lifetimes

| Object | Where | Lifetime | Recreated when |
|---|---|---|---|
| `client_id` (in `.registration.json`) | Auth server's DB | Until DCR re-run | `.registration.json` deleted |
| `access_token` | `.token.json` | Hours (provider-dependent) | Refresh exchange, no user action |
| `refresh_token` | `.token.json` | Days/weeks/months | Full consent flow re-run (browser, user click) |

This is why two files exist: deleting only `.token.json` forces re-consent
without re-registration, while deleting both forces full DCR. Different
operations want different scopes.

## How to sign out or disconnect

### From Kiro Crew

For a registry-backed provider, open **Settings > Connections** and choose
**Disconnect**. The endpoint cancels an in-flight mint, removes the MCP entries
Kiro Crew owns, and considers the stored grant in the same locked transaction.
It unlinks both the token and registration artifacts only when its configuration
census can prove no other server entry shares that artifact key. If a source is
unreadable, a URL cannot be compared safely, another entry shares the grant, or an
unlink fails, the grant is kept or reported as surviving instead of being declared
gone.

Disconnect is local. The card continues to point at the provider's own revoke page
because only the provider can invalidate a token already issued upstream. An agent
session that already holds credentials or a tool list in memory may also need to be
recycled; starting a fresh session after the disconnect is the reliable boundary.

### From interactive kiro-cli

Use the current slash command with the configured server name:

```text
/mcp logout my-server
```

This removes the locally cached OAuth credential for that server and causes a fresh
OAuth flow when authentication is next required. `/mcp auth my-server` forces a
reauthentication, and `/mcp cancel-auth my-server` cancels a stuck browser flow.
These are interactive-chat commands, not top-level `kiro-cli mcp` subcommands.

### Manual recovery

Prefer the two supported paths above. If recovery requires touching the cache by
hand, compute the normalized `origin + path` key exactly as described above and
operate only on that prefix's `.token.json` and `.registration.json` pair. Rename
the exact files to a backup suffix before deleting anything, then recycle the
relevant agent sessions. Never use a wildcard in this directory: the sibling
single-file entries belong to AWS SSO and Kiro identity.

## Important caveats

1. **Local deletion ≠ provider revocation.** Deleting the file only
   invalidates kiro-cli's local copy. The token may still be accepted by
   the provider until natural expiry. For genuine sign-out, also revoke
   at the provider's UI (e.g. `https://linear.app/settings/account/security`)
   or call the provider's RFC 7009 `/revoke` endpoint. Without provider-side
   revocation, anyone who exfiltrated the token bytes (e.g. an agent that
   read the file) can keep using them.

2. **Running sessions may cache credentials and tools.** Removing files on disk
   does not rewrite an already-running agent process. Kiro Crew's Disconnect
   removes the durable pair and rebuilds owned configuration, but recycle the
   affected session (or restart the gateway when several pooled sessions are
   involved) when the change must take effect immediately.

3. **Path is shared with AWS SSO.** Never write code that does
   `rm ~/.aws/sso/cache/*.json` — that nukes legitimate AWS SSO sessions.
   Always target the `{sha256}.token.json` + `{sha256}.registration.json`
   pair by exact name.

4. **Race against in-flight reads.** Deleting mid-flight could race with
   kiro-cli reading the file. Not a correctness problem (kiro-cli falls
   back to "no creds, do OAuth"), but atomic-rename-into-place is safer
   than `rm` for production code.

5. **Interactive kiro-cli can reauthenticate in place.** `/mcp auth` and
   `/mcp logout` provide the supported current-session controls. Kiro Crew's
   Connections surface instead manages its owned config and durable artifact
   pair; use a fresh agent session after that operation so its tool inventory is
   rebuilt.

## How Kiro Crew exposes this

`mcp_grant.py` is the single implementation of the normalized key and paired
artifact paths. It stats artifacts for presence without opening token bytes and
unlinks the exact pair on a proven-owned Disconnect. `connections/ownership.py`
performs the configuration census first, accounts for slash-sensitive artifact
keys and endpoint aliases, preserves a grant when another entry shares it, and
reports an incomplete census rather than guessing. The dashboard handler returns
separate `grantRemoved`, `grantSurviving`, `grantSharedWith`, and census fields so
its UI never collapses a partial operation into "disconnected".

This is deliberately one **Disconnect** operation, not separate "sign out" and
"forget" buttons. Removing only configuration leaves a refresh token that silently
reauthorizes later; removing only one artifact leaves an incomplete pair. The
supported operation handles configuration and the pair together, while provider
revocation remains a separate provider-side action.

## Long-term direction

The design doc at `docs/architecture/design-notes/mcp-oauth-ownership.md` argues that
Kiro Crew should eventually own the OAuth chain end-to-end (token store,
refresh, sign-out, per-agent identity) once a Kiro SDK exists or we move
to the Claude Agent SDK. At that point this whole file becomes legacy —
the question of "where does kiro-cli put its tokens" stops mattering
because we'd inject pre-authenticated `Authorization` headers into the
agent config at session-spawn time, and kiro-cli's OAuth path would be
dead code in the Kiro Crew use case.

Until then, keep all cache-key derivation, presence checks, and exact-pair deletion
in `mcp_grant.py`; callers should use the supported CLI or Connections surfaces
rather than reproducing the undocumented cache layout. That keeps the compatibility
code small and gives it one removal point when kiro-cli no longer owns the OAuth
chain.
