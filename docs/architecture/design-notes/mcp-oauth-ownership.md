# The OAuth ownership problem with kiro-cli as the backend

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

## How it would work with the Agent SDK

The Agent SDK takes `mcpServers` as an in-memory dict on every `query()` call:

```python
options = ClaudeAgentOptions(mcp_servers={
    "github": {
        "type": "http",
        "url": "https://api.githubcopilot.com/mcp/",
        "headers": {"Authorization": f"Bearer {token}"},
    }
})
```

The SDK doesn't run OAuth, doesn't handle callbacks, doesn't store anything. Whatever bearer we hand it is what it uses.

That inverts the ownership model: **Kiro Crew owns the OAuth chain end-to-end.**

- The dashboard runs the consent flow (open browser, receive callback, exchange code for token).
- Kiro Crew stores tokens in its own credential store — keychain on macOS, sealed SQLite on Linux, whatever fits the deployment's security posture.
- Token scoping is up to us: per-user × per-agent × per-server. Two agents in one workspace can hold tokens for two different GitHub accounts.
- Refresh is a Kiro Crew concern: a background task watches expiry, refreshes, hands the new bearer to the next `query()`.
- Sign-out is a single dashboard click — delete the row from our store and revoke upstream.
- Tokens never sit in `agent.json`. The file holds only the **shape** of the MCP server (URL, server-id, scope hints); the bearer is injected at runtime.
- The dashboard can show "GitHub: connected as octocat, expires in 47 min" because the data lives in Kiro Crew.

## The core problem in one sentence

**With kiro-cli, the entire chain — config → OAuth → token storage → header injection — lives inside the CLI process, and Kiro Crew can only observe it through opaque ACP notifications. With the Agent SDK, that chain is Kiro Crew's code, and we can shape it into whatever the product needs.**

---

## Things to tighten before presenting

1. **"An agent could grep and leak the token" understates it.** The instinct is right, but the real risk isn't malicious agents. It's a benign one. A GitHub MCP server with an OAuth token in `agent.json` plus a perfectly reasonable user prompt — "summarize what's in my home directory" — can leak the credential into chat output without anyone misbehaving. Lead with the **prompt-injection / accidental-leak** angle; it's more persuasive because it's harder to mitigate.

2. **The "different accounts per MCP" point isn't in the original write-up but is one of the strongest.** kiro-cli's grant store is process-global — the paired-file cache under `~/.aws/sso/cache/` holds one identity per server per machine, so one GitHub identity per machine. With Kiro Crew owning identity, a workspace running two agents (e.g. `personal-tasks` and `team-tasks`) can hold two different GitHub tokens against the same MCP server. That's a concrete product capability we can't deliver today.

3. **Use a comparison table instead of prose; reviewers process it faster:**

   | Concern | kiro-cli (today) | Agent SDK (proposed) |
   |---|---|---|
   | Where does the OAuth token live? | paired `~/.aws/sso/cache/<sha256>.token.json` + `.registration.json` files (values opaque; presence stat-able) | Kiro Crew's credential store |
   | Who runs the callback server? | kiro-cli, on a port it picks | Kiro Crew, on a port we control |
   | Can we list authenticated MCP servers? | No | Yes |
   | Can we refresh proactively? | No | Yes |
   | Per-agent identities? | No | Yes |
   | Sign-out per server? | No | Yes |
   | Plaintext token in `agent.json`? | Yes (Path B fallback) | Never |

4. **Acknowledge the cost.** The pitch is more credible if it admits the trade: we'd be writing ~500 LOC of OAuth (DCR + PKCE + callback + storage + refresh) that today comes for free from kiro-cli. But that cost is **already being paid** — in `_kiro.dev/mcp/*` parsing, banner state machines, redaction carve-outs — and what we get for paying it doesn't include the capabilities owning OAuth would deliver. Net it's a wash on lines, a clear win on capabilities.

5. **"We can't access it" is the thesis — don't bury it.** Open with that sentence: *"We're rendering UI for a flow whose config-reading, browser-driving, callback-handling, token-storage, and refresh logic all live inside another process, and our only API to it is read-only JSON-RPC notifications."* Then use cases, then today's flow, then the SDK alternative.

For an engineering-leadership / CR-description version, the structure I'd use is: **Problem (1 paragraph) → Use cases (concrete) → Today's flow + why it falls short → Proposed flow → Trade-offs table → Migration cost estimate.**
