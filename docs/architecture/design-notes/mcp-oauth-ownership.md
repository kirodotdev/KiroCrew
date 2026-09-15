# The kiro-cli credential boundary for OAuth MCP servers

## Use cases

**Use case 1 — Agent-driven install:** A user asks an agent to "look at my GitHub repo." The agent doesn't have a GitHub MCP server available, so it should: (a) install one, (b) ask the user to authenticate, (c) call the tool with the resulting credentials.

**Use case 2 — User-driven install:** A user clicks "Install GitHub integration" in the Kiro Crew dashboard. The dashboard walks them through GitHub auth and the integration is ready for the next session.

Both flows end at the same place: a remote MCP server that needs an OAuth bearer token in `Authorization: Bearer …` on every request. Three things have to happen end-to-end:

1. **Authorization** — the user grants consent in a browser; the OAuth provider hits a callback URL with an authorization code; the callback handler exchanges it for an access token (and refresh token).
2. **Storage** — the token is persisted somewhere durable, scoped per-user / per-agent / per-server, and looked up on every subsequent MCP call.
3. **Use** — when the MCP client opens an HTTP/SSE connection to the server, the bearer is injected into the request header. When the access token expires, it's refreshed (or re-prompted) without losing the session.

## How this works today (kiro-cli backend)

kiro-cli reads its agent definition from `agent.json` at session start. MCP servers are declared inline in that file. From there, two paths:

**Path A — no token in the agent config.** kiro-cli connects to the MCP server, gets a 401, runs OAuth itself, and surfaces the consent URL via the `_kiro.dev/mcp/oauth_request` ACP notification. Kiro Crew renders that URL as a dashboard banner; the user clicks through; the OAuth provider eventually calls back to **kiro-cli's own local callback server**; kiro-cli stores the token in **its own on-disk grant store** — a pair of files under `~/.aws/sso/cache/`, keyed by `sha256(origin+path)` of the server URL: `<sha256>.token.json` holds the bearer + refresh token and `<sha256>.registration.json` holds the DCR client metadata (see [kiro-cli MCP OAuth token storage](../../reference/kiro-cli/mcp/oauth-token-storage.md) for the source-derived layout). These files survive a restart. The token **values** and their lifecycle — expiry, refresh, sign-out — are effectively opaque and unownable to Kiro Crew: it cannot read the bearer, enumerate authenticated servers from the token contents, or drive a refresh. What it *can* do is **stat** the paired files for presence (`mcp_grant.grant_presence`, which never opens them), which is how the dashboard renders "Signed in" vs "Sign-in required". All subsequent calls "just work" because the bearer is injected internally by kiro-cli.

**Path B — token already written into the agent config's `headers`.** kiro-cli sees `Authorization: Bearer …` on the MCP server entry and connects without running OAuth at all.

Path B has two showstoppers:

- **Expiration is invisible.** The token in `agent.json` is static. When it expires, the next MCP call returns 401 mid-turn, and there's no refresh story.
- **Plaintext on disk.** The token sits in a JSON file the agent itself can read. An agent doing legitimate filesystem work — `cat ~/.kiro/agents/kirocrew.json`, `grep -r Bearer ~`, anything — pulls the credential into its own context. Same risk class as `.env` files and `.aws/credentials`, except agents are LLM-driven, with a "curiosity gradient" much higher than a human's.

So in practice we live on Path A. The cost: **kiro-cli owns the entire OAuth chain** — config reading, browser flow, callback server, token storage, refresh, sign-out — and Kiro Crew's only observation surface is one-directional `_kiro.dev/*` notifications. Concretely:

- We can't see **which** MCP servers are authenticated for the current user.
- We can't proactively refresh tokens or check expiry.
- We can't sign out of one MCP server without nuking kiro-cli's whole identity.
- We can't have two Kiro Crew users (or two agents in the same workspace) authenticated to the same MCP server with different accounts — kiro-cli's store is one-per-machine.
- We can't show "GitHub: connected as octocat" in the dashboard, because that data lives in kiro-cli's store and we can't read it.

The ACP-level workarounds we've built (the OAuth banner, dedup, completion patching, role-aware redaction, `chat_message_update`) are all symptoms of the same thing: **we're rendering UI for a flow we don't own.**

## The boundary in one sentence

kiro-cli owns the OAuth chain end to end and Kiro Crew never holds the credential:
Kiro Crew can observe that a grant exists, by `stat` on the paired token and
registration artifacts, and nothing more. A design that would move custody into Kiro
Crew is a proposal, not current behaviour, and is argued in
[`../../request-for-change/rfc-crew-agent-sdk-boundary.md`](../../request-for-change/rfc-crew-agent-sdk-boundary.md).
