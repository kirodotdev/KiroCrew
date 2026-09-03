# kiro-cli MCP OAuth Token Storage

Where kiro-cli writes MCP OAuth credentials on disk, how to find them, and
how to "sign out" of a remote MCP server. Not documented upstream — derived
from the `aws/amazon-q-developer-cli` source
(`crates/chat-cli/src/mcp_client/oauth_util.rs`).

> Why this matters: kiro-cli has **no `mcp logout` command**, no ACP method
> for sign-out, and the docs say nothing about where tokens live. The only
> in-CLI affordance is `/mcp` → reauthenticate inside an interactive
> `kiro-cli chat` REPL — unreachable from Kiro Crew's ACP-based gateway.
> Surgical file deletion is the only mechanism Kiro Crew can drive.

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

## How to "sign out" of a remote MCP server

There is no command. Three levels of action, choose by intent:

### 1. "Sign me out, keep kiro-cli registered" (most common intent)

```bash
KEY=$(python3 -c "import hashlib; print(hashlib.sha256(b'<server-url>').hexdigest())")
rm "$HOME/.aws/sso/cache/${KEY}.token.json"
pkill -f "kiro-cli acp"   # drop in-memory tokens in running sessions
```

Next MCP call → kiro-cli sees no token but has a registration → re-runs
the consent flow only → emits `_kiro.dev/mcp/oauth_request` → Kiro Crew's
banner fires → user authorizes → fresh token written.

### 2. "Forget this server entirely" (resets DCR too)

```bash
KEY=$(python3 -c "import hashlib; print(hashlib.sha256(b'<server-url>').hexdigest())")
rm "$HOME/.aws/sso/cache/${KEY}".{token,registration}.json
pkill -f "kiro-cli acp"
```

Next MCP call → kiro-cli does full DCR + consent → both files are
recreated. Use when changing OAuth scopes or doing a clean reset.

### 3. "Reversible test" — rename to `.bak`

```bash
KEY=...
DIR=$HOME/.aws/sso/cache
mv "$DIR/$KEY.token.json"        "$DIR/$KEY.token.json.bak"
mv "$DIR/$KEY.registration.json" "$DIR/$KEY.registration.json.bak"
pkill -f "kiro-cli acp"
```

kiro-cli looks for the exact `.token.json` / `.registration.json` filenames
(`oauth_util.rs:246`), so `.bak` is invisible to it. Restore by moving back.

## Important caveats

1. **Local deletion ≠ provider revocation.** Deleting the file only
   invalidates kiro-cli's local copy. The token may still be accepted by
   the provider until natural expiry. For genuine sign-out, also revoke
   at the provider's UI (e.g. `https://linear.app/settings/account/security`)
   or call the provider's RFC 7009 `/revoke` endpoint. Without provider-side
   revocation, anyone who exfiltrated the token bytes (e.g. an agent that
   read the file) can keep using them.

2. **kiro-cli sessions cache tokens in memory.** A running `kiro-cli acp`
   subprocess holds the bearer in process memory regardless of what the
   file says. Always `pkill -f "kiro-cli acp"` after deleting the file.
   Kiro Crew's warm session pool means this also requires draining the pool
   (or a gateway restart) to fully take effect.

3. **Path is shared with AWS SSO.** Never write code that does
   `rm ~/.aws/sso/cache/*.json` — that nukes legitimate AWS SSO sessions.
   Always target the `{sha256}.token.json` + `{sha256}.registration.json`
   pair by exact name.

4. **Race against in-flight reads.** Deleting mid-flight could race with
   kiro-cli reading the file. Not a correctness problem (kiro-cli falls
   back to "no creds, do OAuth"), but atomic-rename-into-place is safer
   than `rm` for production code.

5. **No re-emit of kiro-cli's `oauth_request` for the current session.**
   In-memory tokens in already-running sessions remain usable until the
   process exits. Sign-out only takes effect on next process spawn.

## How Kiro Crew should expose this

Two distinct dashboard affordances, mapping to actions 1 and 2 above:

- **"Sign out"** → delete only `.token.json`. Common case.
- **"Forget this connection"** → delete both. Edge case, separate UI.

Implementation outline (~50 LOC):

```python
import hashlib
from pathlib import Path

def _mcp_cache_paths(server_url: str) -> tuple[Path, Path]:
    key = hashlib.sha256(server_url.encode()).hexdigest()
    cache = Path.home() / ".aws/sso/cache"
    return cache / f"{key}.token.json", cache / f"{key}.registration.json"


def sign_out_mcp(server_url: str) -> bool:
    """Delete kiro-cli's cached OAuth token for this server.

    Returns True if a token file was removed.  Caller should also restart
    or recycle kiro-cli sessions so in-memory copies are dropped.

    Does NOT revoke at the provider — call the provider's /revoke endpoint
    separately for genuine sign-out, or instruct the user to revoke at the
    provider's UI.
    """
    token_path, _ = _mcp_cache_paths(server_url)
    if token_path.is_file():
        token_path.unlink()
        return True
    return False


def forget_mcp(server_url: str) -> bool:
    """Delete both token AND registration so the next call re-runs DCR."""
    token_path, reg_path = _mcp_cache_paths(server_url)
    deleted = False
    for p in (token_path, reg_path):
        if p.is_file():
            p.unlink()
            deleted = True
    return deleted
```

Pair with a `state.recycle_kiro_sessions()` call so the warm pool reloads
(or trigger a kill on currently active ACP processes for that slot).

## Long-term direction

The design doc at `docs/design/mcp-oauth-ownership-problem.md` argues that
Kiro Crew should eventually own the OAuth chain end-to-end (token store,
refresh, sign-out, per-agent identity) once a Kiro SDK exists or we move
to the Claude Agent SDK. At that point this whole file becomes legacy —
the question of "where does kiro-cli put its tokens" stops mattering
because we'd inject pre-authenticated `Authorization` headers into the
agent config at session-spawn time, and kiro-cli's OAuth path would be
dead code in the Kiro Crew use case.

Until then, the recipe in this doc is the only way to drive sign-out from
Kiro Crew, and it's worth keeping the helper code small, well-tested, and
clearly scoped to "kiro-cli's caching behavior" so it can be removed
cleanly later.
