# Connecting a remote / OAuth MCP server

How to add and authenticate a **remote HTTP / OAuth** MCP server on the Kiro
Crew desktop app — the class of server you reach over `https://`, whose tools
you unlock by signing in through a browser, as opposed to a **local stdio**
server you launch with a `command`. The worked example is Miro's remote MCP
([`https://mcp.miro.com/`](https://mcp.miro.com/)), the flow that motivated this
guide, but the same steps apply to any OAuth-backed remote server (Linear,
Notion, Atlassian, a self-hosted OIDC-fronted service, …).

The short version is five moves, and every one of them has a trap that reads
like a dead end:

1. Add the server to the **agent's own** config, not just the Kiro global.
2. Mount it by adding its `@ref` to `tools`.
3. Start a session — nothing useful is *printed*.
4. Click **Authorize** on the banner Kiro Crew raises in chat.
5. Complete the browser flow, then **drain the warm pool** and start a fresh
   session — a new chat alone can reuse a pre-authentication process.

If the browser step fails closed with a "credential or exfiltration pattern"
error, the provider's authorization host is not on Kiro Crew's recognized set
and you need the [`oauth_endpoints.json`](#if-the-host-is-not-recognized-the-oauth-endpoint-allowlist)
keystone. Each section below is one of those moves and its trap.

## Add it to the right config for your agent

Kiro Crew renders one agent file it fully owns —
`~/.kiro/agents/kirocrew.json` — and `kiro-cli` loads MCP servers with this
precedence (highest wins for a same-named server; different names are additive):

| Priority | Source | Owner |
|---|---|---|
| 1 (highest) | `mcpServers` in the agent JSON (`~/.kiro/agents/kirocrew.json`) | Kiro Crew gateway (`agent.rebuild_agent_config`) |
| 2 | workspace `.kiro/settings/mcp.json` | you |
| 3 (lowest) | global `~/.kiro/settings/mcp.json` | you |

At render time the gateway **merges the Kiro global into the agent file**, so
the agent file is the superset kiro-cli actually loads — see
[../architecture/mcp.md](../architecture/mcp.md) ("Config file hierarchy" and
"Merge order"). That merge is why the naive "just add it to the global
`mcp.json`" instinct fails for the `kirocrew` agent: the agent pins
`includeMcpJson: false` (next section), so a server you hand-add to the global
*after* a render — or one the agent's `tools` never mounts — is
invisible to that agent.

The remedy is to put the entry where the agent will see it and grant it. A
remote server's shape is a `url` plus optional `headers` (see
[../reference/kiro-cli/mcp/configuration.md](../reference/kiro-cli/mcp/configuration.md),
"Remote server properties"):

```jsonc
// ~/.kiro/agents/kirocrew.json
{
  "includeMcpJson": false,
  "mcpServers": {
    "kirocrew-core":     { "command": "…", "args": ["mcp-core"] },
    "miro": { "url": "https://mcp.miro.com/" }
  },
  "tools":        ["@miro", "…"]
}
```

Two things make that entry usable, and both are required:

- The `"miro"` entry lives in the **agent's own** `mcpServers` (not only the
  global).
- The `@miro` ref is added to **`tools`**. A `@server` ref resolves against the
  agent's own `mcpServers` plus the global `mcp.json` — with no entry in either,
  the ref names nothing and mounts nothing
  ([../architecture/mcp.md](../architecture/mcp.md), "Managed servers").

> **Do not add the ref to `allowedTools` to make this work — it is not required,
> and it waives your governance ceiling.** `allowedTools` is kiro-cli's blanket
> auto-approve list: an auto-approved MCP tool is approved *locally* by
> kiro-cli, emits no permission request, and therefore never reaches
> `hooks.on_tool_call` — the PreToolUse plane carrying the always-on deny floor,
> the sensitive-path check and the governance ceiling. `tools` alone makes the
> server's tools **reachable**, which is all OAuth and normal use need; calls
> then go through the approval path like any other. Kiro Crew treats this list
> as governed state, not preference: every in-product writer of it must clear
> `platform.governance.may_skip_gate_now()` (which fails closed), and
> `kirocrew doctor` will **revoke** a `@ref` the ceiling disallows and log an
> `mcp_auto_approve_withheld` SEL event. A hand-edit is the one writer that
> clears no predicate, so grant it only as a deliberate, separate decision.

> The dashboard MCP / Integrations panel can add a remote server for you and
> write it into the scope files it manages. Hand-editing `kirocrew.json` is the
> equivalent when you would rather work in the file directly; either way the
> rebuild preserves servers you customized there.

## The `includeMcpJson` gotcha

This is the first round-trip and the one with the most misleading symptom. The
`kirocrew` agent ships with:

```json
{ "includeMcpJson": false }
```

The gateway already merges the Kiro global into the agent file, so with
`includeMcpJson: true` kiro-cli would merge that global a **second** time at
session start — producing duplicate entries and letting a stale path in the
global shadow the fresh path the gateway just resolved. Kiro Crew forces
`false` on every agent it manages to avoid that double-merge
([../architecture/mcp.md](../architecture/mcp.md), "`includeMcpJson` is pinned
false").

The consequence to internalize: **a server declared only in the global
`~/.kiro/settings/mcp.json` after a render is silently NOT loaded by the
`kirocrew` agent.** The symptom is that no session ever offers the server's
tools and no authentication banner is raised for it — the agent never connects,
so there is no `401` to trigger OAuth.

What makes that hard to read is the dashboard: the MCP panel's `kirocrew` badge
reads **green** for such a server anyway. That badge does not mean "loaded now"
— `mcp_discovery.list_servers()` computes it as *"will this load in Kiro Crew
sessions after the next rebuild"*, and since the rebuild inherits from the Kiro
global, membership in the global alone is enough to turn it green. So the panel
is reporting the future and the session is reporting the present, which is
exactly the gap `includeMcpJson: false` opens.

Together those read like a broken install: the dashboard says the server is
configured, and chat behaves as though it does not exist. It is neither broken
nor a typo — it is `includeMcpJson: false` doing exactly what it is pinned to
do. The fix is the previous section: put the server in the **agent file**
(`kirocrew.json`) and add the `@miro` ref to `tools`.
Once the agent can see the entry, the next session start runs OAuth for it.

## Authenticate it

With the server configured and granted, **start a session** — that is the whole
trigger. kiro-cli connects to the server, is met with a `401`, and runs OAuth
itself
([../architecture/design-notes/mcp-oauth-ownership.md](../architecture/design-notes/mcp-oauth-ownership.md),
"Path A"). There is no command to type: kiro-cli owns the OAuth chain end to
end, and Kiro Crew's only view of it is the one-directional `_kiro.dev/*`
notifications.

**The authorization URL is never printed into the transcript as text**, so a
session that comes up with no visible mention of the server is not a silent
failure. kiro-cli hands the URL to Kiro Crew over the ACP notification
`_kiro.dev/mcp/oauth_request`
([../reference/kiro-cli/acp.md](../reference/kiro-cli/acp.md), "MCP server
events"), and Kiro Crew renders it as an **inline banner** in chat — a lock
icon, `miro requires authentication.`, and an **Authorize** link that opens the
provider (`dashboard/chat_runner._emit_mcp_oauth_request`, rendered by
`McpOAuthBanner`). Requests kiro-cli buffered while bringing MCP servers up are
flushed into the transcript at session init, so the banner also appears on a
fresh session with nothing typed.

**For a server like Miro, the chat banner is the only place you can start the
sign-in** — and that is the case this guide is about. Two dashboard surfaces
offer the same action, but both are limited to **curated Connections
providers**, i.e. the providers listed in `connections/registry.json`:

- The **MCP** table's per-row **Sign in** control (`McpRowSignIn`) renders only
  when the row resolves to a registry provider *and* the Connections UI is
  unlocked. Minting is deliberately fenced to registry providers; arbitrary URLs
  are never minted. A user-added or self-hosted row — Miro included — instead
  gets a sentence linking you to chat, which is the accurate routing.
- A Connections provider card drives its own consent flow; a banner for one of
  those is tagged so chat does not repeat a prompt the card already shows.

So there is no authorization URL to hunt for and copy out of a status panel —
clicking **Authorize** in chat *is* step 4. On success the banner flips in place
to `miro authenticated.`.

What "signed in" eventually looks like is visible in the dashboard MCP /
Integrations panel, whose probe renders one of three badges for a remote server
that answers with a `401` (or a `403` carrying `WWW-Authenticate`) and has no
static `Authorization` header ([../architecture/mcp.md](../architecture/mcp.md),
"Discovery and probing"):

| Badge | Meaning |
|---|---|
| **Sign-in required** | A challenge was seen and no grant artifact exists yet — nobody has signed in. |
| **Signed in** (muted) | A grant artifact exists on disk. It reports that a sign-in happened, not that the token is still valid — the probe holds no token, so it cannot check validity. |
| **Not verified** | A bare `401`, or an older gateway: the probe observed nothing it could name. |

Reaching **Signed in** is what gives the flow an ending. Because the panel is
served from the probe cache for its TTL, a row you just authenticated can still
read "Sign-in required" until you re-probe it (the panel names that step).

## Complete the browser flow and select a team/tenant

Follow the **Authorize** link into a browser and complete the provider's consent
screen:

1. Sign in to the provider if you are not already.
2. **Select the right team / workspace / tenant.** Many providers scope a grant
   to one workspace, and the consent screen asks which one *before* you approve.
   Miro (and Slack, Notion, Atlassian, and most workspace products) work this
   way — approving under the wrong workspace yields a token that authenticates
   but sees none of the boards or projects you expected, which is easy to
   mistake for a broken connection. Watch for this selector and pick
   deliberately.
3. Approve the requested scopes.

The exact wording and layout of Miro's consent screen are not verified in this
repo (see the [UNVERIFIED note](#where-the-credentials-live-and-what-kiro-crew-can-see)
at the end), so treat the team/tenant selection as a step to *watch for* rather
than a pixel-exact walkthrough. On success the provider redirects to kiro-cli's
own local callback — which listens on the machine running the gateway — and the
token is written to disk.

That last detail bites when the browser is not on the gateway's machine. Driving
a **remote** gateway, the `localhost` callback is unreachable from your browser
and the tab ends on a connection error even though the code was minted. The
banner carries the recovery: expand *"Browser showed a connection error after
authorizing?"*, paste the full address the browser landed on, and **Complete
connection** relays it to the gateway, which replays it locally to finish the
exchange. It only delivers a code that was already minted.

## If the host is not recognized: the OAuth endpoint allowlist

If the browser step fails **before** you ever reach the provider — the chat
banner reports a prose warning like:

```text
🚫 miro sent an authentication URL containing a credential pattern
(rejected). If this is a self-hosted or otherwise unlisted identity
provider, its authorization endpoint may need adding to oauth_endpoints.json
in the Kiro Crew data home; otherwise ask the server owner to fix the URL.
```

(The terse string `URL contained credential or exfiltration pattern` is the
message's `error` metadata field, not on-screen text — you will not find it by
grepping the transcript for a visible line.) The authorization URL was rejected
by Kiro Crew's gate, not by the provider.
Kiro Crew scans consent URLs for credential-exfiltration patterns, and it
relaxes the base64-blob / query-length heuristics **only** for authorization
endpoints it recognizes by exact `(host, path)`. That recognized set is
code-owned — read its current contents from
`security._OAUTH_AUTHORIZATION_ENDPOINTS`; it covers the Connections launch
providers' MCP authorization servers plus the classic web-OAuth hosts.
**Miro is not in it**, so its real consent URL — which routinely exceeds the
query-length heuristic — fails closed. The chat banner that reports the
rejection (`dashboard/chat_runner.py`) names the remedy inline, because the fix
is agent-fenced with no dashboard writer and that banner is the only place a
user learns it exists.

The remedy is the operator keystone **`oauth_endpoints.json`**, which extends
the recognized set without weakening the gate. Create or edit it in the Kiro
Crew data home — `config_dir()/oauth_endpoints.json`, which respects
`KIROCREW_HOME` (typically `~/.kiro/crew/oauth_endpoints.json`):

```json
{
  "additional_authorization_endpoints": [
    { "host": "<auth-host>", "path": "/<exact-authorize-path>" }
  ]
}
```

### Validation rules

Every entry is validated strictly (`security._validate_operator_oauth_entries`);
an invalid entry is skipped with a warning rather than failing the whole file.

| Field | Rule |
|---|---|
| `host` | Lowercase LDH (letter/digit/hyphen) dot-labels ending in a letter TLD. **No** IP literal, wildcard, port, scheme, userinfo, or percent-escapes. Matched against the lowercased host. |
| `path` | **Exact and case-sensitive.** Must start with `/`, be `<= 512` chars, and must not contain `;` `?` `#` `%` `\` `..` or any whitespace. |
| whole file | At most **50** entries (extras beyond the cap are ignored). |

Two constraints are enforced by the **gate**, not the file, and cannot be
relaxed through it: the endpoint must be **HTTPS** and must carry **no explicit
port**. Listing a host here only exempts it from the base64-blob / query-length
heuristics on known OAuth params; fixed-credential patterns, heavy
percent-encoding, userinfo, fragments, and backslashes stay rejected
unconditionally.

### Behavior and posture

- **Fail-soft.** A missing, unreadable, corrupt, or non-object file yields the
  **empty** extension set. A mangled file never widens trust.
- **Agent-fenced keystone.** The file sits on Kiro Crew's protected keystone
  set: the agent can neither read nor write it, so a prompt-injected agent
  cannot author its own trust widening. The operator **hand-edits it
  out-of-band** — there is deliberately no dashboard writer.
- **No restart needed.** The loader is keyed on the file's stat (path, mtime,
  size), so a hand-edit takes effect on the **next check** with no gateway
  restart.

You must supply the provider's **actual** authorization host and path. Obtain
them from the provider's authorization-server metadata (RFC 8414 —
`/.well-known/oauth-authorization-server`), reached from the server's own
RFC 9728 protected-resource document — the same route the code-owned pairs were
derived from. Neither the rejected banner nor the log echoes the URL that was
refused (logging a URL that tripped the credential scanner would defeat the
scanner), so there is nothing to copy out of Kiro Crew here.
**Miro's exact authorize
host and path are not verified in this repo** — do not copy a guessed value;
read the real one and enter it exactly (the `path` comparison is byte-exact).

## Static header vs OAuth (and scopes)

OAuth is not the only way to authorize a remote entry. You can instead put a
static bearer token directly on the server config:

```json
{ "url": "https://mcp.example.com/", "headers": { "Authorization": "Bearer ${TOKEN}" } }
```

This **bypasses OAuth entirely**: the probe reads and sends the header on the
handshake (`mcp_discovery.py`), so no browser flow happens. The failure modes
differ accordingly — a `401` on an entry that **carries** a static
`Authorization` header stays a hard `error` (a supplied credential was
rejected), whereas a `401` on an entry with **no** static header is the
`needs_auth` / "Sign-in required" path described above.

For an OAuth server, an optional internal `scopes` list maps to the wire
`oauthScopes` field (`mcp_utils._wire_scopes`). It is all-or-nothing: the field
is **dropped whole** when it is absent, empty, or malformed. When it is dropped,
the entry authorizes with the **provider's default grant, which can be WIDER
than the list you wrote on disk**. A malformed value logs a warning so the swap
is diagnosable from the log rather than only from the consent screen.

On client type: kiro-cli registers as a **public client** via Dynamic Client
Registration (DCR) — `client_secret: null`, PKCE-protected — recorded in the
paired `.registration.json` alongside its `client_id`, callback `redirect_uri`,
and `scopes` (see
[../reference/kiro-cli/mcp/oauth-token-storage.md](../reference/kiro-cli/mcp/oauth-token-storage.md),
"`.registration.json`").

**Only a public `clientId` is configurable, and there is no confidential-client
option.** To skip DCR against a provider that pre-registers clients, set
`clientId` on the entry: Kiro Crew maps it to the wire field `oauth.clientId`
(`mcp_utils.kiro_oauth_wire_entry`, and `kiro_entry_client_id` reads it in
either spelling). That is the whole supported OAuth surface alongside `scopes` —
there is **no `clientSecret` and no `redirectUri`** on an MCP server entry.
Do not invent them: kiro-cli **ignores unknown keys silently**, so a secret
written there buys you nothing, does not make a confidential-client flow work,
and leaves a real credential sitting in plaintext in a file the agent itself can
read — the same exposure that rules out putting a bearer token in `headers`
([../architecture/design-notes/mcp-oauth-ownership.md](../architecture/design-notes/mcp-oauth-ownership.md),
"Path B"). A provider that cannot accept a public PKCE client is not
configurable here.

## Tools register only in a fresh session

A newly authenticated MCP server's tools become available only in a **new
session**. Kiro Crew keeps a **warm session pool**, and a running `kiro-cli acp`
subprocess caches its tokens and tool list **in memory** — the file on disk
changing does not reach a session that is already alive
([../reference/kiro-cli/mcp/oauth-token-storage.md](../reference/kiro-cli/mcp/oauth-token-storage.md),
caveats 2 and 5). So after authenticating:

1. **Drain the warm pool, or restart the gateway.** Do this *first*.
2. **Then** start a fresh chat / session.

**A new chat on its own is not enough, and this is the step that looks like the
sign-in failed.** "New session" does not imply "new process": a new session
claims a pre-spawned provider from the warm pool whenever one is available
(`session_pool._claim_from_pool` / `_drain_and_claim`), and the only things that
disqualify a pooled process are a mismatched agent name and the pool TTL —
never its MCP authentication state. So a chat opened straight after
authenticating can be served by a subprocess that started *before* the token
landed and cached its tool list without the server. Draining first is what makes
the fresh session actually cold.

Until then the tools will look absent even though the sign-in succeeded.

## Where the credentials live (and what Kiro Crew can see)

`kiro-cli` owns the OAuth chain end to end. On a successful sign-in it writes a
**paired** set of files, keyed by `sha256(origin + path)` of the server URL, in
`~/.aws/sso/cache/`:

```text
<sha256(origin+path)>.token.json          ← the OAuth bearer + refresh token
<sha256(origin+path)>.registration.json   ← the DCR client metadata
```

Both **survive a restart**. Full layout, contents, and the "sign out" file
recipes are in
[../reference/kiro-cli/mcp/oauth-token-storage.md](../reference/kiro-cli/mcp/oauth-token-storage.md).
A consequence worth knowing: removing only the MCP config entry leaves a usable
refresh token on disk, so a later reconnect silently resumes the old grant
(`mcp_grant.py`).

Kiro Crew **stats these files for presence** (`mcp_grant.grant_presence`) to
render the "Signed in" badge, but **nothing in that module opens a token file**
(`mcp_grant.py`): it reads no token value. So Kiro Crew can report neither the
**granted scope** nor the **expiry** of a Miro token. The curated-provider
reconnect short-circuit is bound to the providers listed in
`connections/registry.json`, and **Miro is not one of them**, so Kiro Crew
observes nothing about Miro's token reuse.

> **UNVERIFIED.** Miro's exact authorization host and path, the scopes it
> grants, and its token expiry are **not** verified in this repo, and no
> specific value for them is stated above. Confirm them on a machine running
> the desktop app: connect [`https://mcp.miro.com/`](https://mcp.miro.com/),
> then `stat ~/.aws/sso/cache/` for the `sha256(origin+path)` pair and read
> `.registration.json`'s `scopes` and `.token.json`'s `expires_in`.

## Related

- [../architecture/mcp.md](../architecture/mcp.md) — config hierarchy,
  `includeMcpJson` pinning, `@server` ref resolution, and the discovery /
  probing badges.
- [../architecture/design-notes/mcp-oauth-ownership.md](../architecture/design-notes/mcp-oauth-ownership.md)
  — why kiro-cli owns token custody and what Kiro Crew can and cannot see.
- [../reference/kiro-cli/mcp/oauth-token-storage.md](../reference/kiro-cli/mcp/oauth-token-storage.md)
  — where the paired token files live, their contents, and the file-level
  "sign out" recipes.
- [../reference/kiro-cli/mcp/configuration.md](../reference/kiro-cli/mcp/configuration.md)
  — the `mcpServers` schema, remote-server `url` / `headers`, and loading
  priority.
- [enterprise-mcp-governance.md](enterprise-mcp-governance.md) — when an
  enterprise MCP registry, rather than OAuth, is why a server will not connect.
- [../../src/kiro_crew/docs/troubleshooting.md](../../src/kiro_crew/docs/troubleshooting.md)
  — the user-facing "MCP tools not working" checklist.
