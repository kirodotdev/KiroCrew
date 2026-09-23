# Connections

**Connections** is the one page for everything outside Kiro Crew that an agent can
reach: the MCP servers this install runs, and the third-party services you have
signed into. A curated vendor connects in a click and its tools appear in your next
session; a server nobody curated is added by hand on the same page and behaves the
same way afterwards. The page also reports whether each one is actually answering,
which is usually the question you came with.

Open it from the sidebar: **Agent Capabilities → Connections**. The older
`/connections` address still works and redirects there.

The gallery ships on. To remove every Connections surface from an install, set
`connections_ui: false` in that instance's `config.json`. Config is read live, so
neither direction needs a gateway restart, and with the gallery off chat is again
the only place a sign-in is offered.

## The vendor catalogue

`src/kiro_crew/connections/registry.json` holds **28** curated providers. A catalogue
entry is what lets Kiro Crew offer a one-click **Connect** instead of asking you for
a URL: the entry carries the vendor's MCP endpoint, the scopes worth asking for, the
revoke page to send you to later, and a baseline of the vendor's published OAuth
metadata that a nightly job watches for drift.

Of those, **8** render a card today. Two conditions decide which:

| Entry state | Card | Why |
|---|---|---|
| Launch gate passed | **Connect** | A human has walked the whole flow on this provider and recorded its revoke surface. Six entries: Notion, Linear, Atlassian, Stripe, Vercel, GitLab |
| Needs an operator-registered OAuth app | **Configure OAuth app** | The vendor refuses runtime client registration, so the card is an instruction rather than an offer. Two entries: GitHub, Asana |
| Launch gate not yet walked | hidden | The entry exists so the nightly metadata check covers it; nothing about it is claimed to you yet |
| Vendor admits clients by allowlist or waitlist | hidden | No per-install app can satisfy it, which is a dead end rather than an instruction. Four entries: Figma, Canva, Dropbox, Square |

Every entry also carries a `category` — eight buckets, from `developer-tools` to
`payments-finance` — and a `tier`. Both are catalogue metadata. **Tier is not
latency and not quality**: tier 3 means the vendor gates clients by allowlist, which
is the same fact as the hidden row above.

A hidden entry is not a missing feature. If a vendor you want is in the catalogue but
has no card, you can still add its MCP endpoint by hand as an ordinary remote server
— you give up the curated scopes, the revoke link and the **Sign in** control, and
you keep the tools.

## Adding your own MCP server

Anything not in the catalogue is added on the same page. A server takes one of two
shapes, and exactly one:

**stdio** — Kiro Crew launches a local process and talks to it over its pipes:

```json
{ "command": "npx", "args": ["-y", "some-mcp-server"], "env": { "API_REGION": "us-east-1" } }
```

**remote** — an HTTP endpoint Kiro Crew calls over the network:

```json
{ "url": "https://mcp.example.com/" }
```

`command` and `url` are mutually exclusive; the URL must be `http://` or `https://`.
`args` and `env` belong only to the stdio shape. When you paste a block of several
servers, every entry is validated before any of them is written, so a single bad
entry fails the whole paste rather than half-applying it. A name already configured
anywhere is refused rather than overwritten.

### Remote servers that need a sign-in

A remote server usually answers the first request with a `401`, and that is the whole
trigger — there is no command to run. `kiro-cli` owns the OAuth exchange and runs it
itself, so **start a session** and watch chat: a banner appears with a lock icon, the
server's name, and an **Authorize** link. The authorization URL is never printed into
the transcript as text, so a session that mentions nothing is not a silent failure —
the banner is where it lives.

Follow the link, and on the consent screen **pick the right workspace or tenant
before approving**. Most workspace products scope a grant to one of them, and a grant
approved under the wrong one authenticates cleanly while seeing none of your data,
which reads exactly like a broken connection.

Curated catalogue rows get a second route: a per-row **Sign in** control, and the
provider's own card. Minting is deliberately fenced to catalogue providers, so a
hand-added row — even one pointing at a vendor that *is* in the catalogue — is sent
to chat instead. That is accurate routing, not a missing button.

Two other ways to authorize a remote server:

- **A static token instead of OAuth.** Put `"headers": {"Authorization": "Bearer ${TOKEN}"}`
  on the entry and no browser flow happens at all. A `${VAR}` reference is resolved
  when the handshake is sent; an unresolved one stays literal, which then reads as a
  server asking for a sign-in.
- **An operator-registered OAuth app**, for the two vendors that refuse runtime
  registration. See [credentials](#credentials-and-what-kiro-crew-can-see) below.

### The tools appear in the next *cold* session

A running `kiro-cli` process caches its token and its tool list in memory, so a file
on disk changing does not reach a session that is already alive. Kiro Crew also keeps
a warm pool of pre-spawned processes, and a new chat claims one of those whenever it
can — which means **a new chat alone is often not enough**, and this is the step that
looks like the sign-in failed.

After authorizing: drain the warm pool (or restart the gateway) **first**, then start
a fresh session.

## Credentials, and what Kiro Crew can see

`kiro-cli` owns the OAuth chain end to end. On a successful sign-in it writes two
files into its own cache directory, keyed by a hash of the server's URL — one holding
the bearer and refresh token, one holding the client registration. Both survive a
restart.

**Kiro Crew checks whether those files exist and never opens them.** That is what the
"Signed in" badge reports: a sign-in happened, not that the token is still valid. It
also means Kiro Crew can tell you neither the granted scope nor the expiry, and there
is no place in the product where a provider token can be printed.

One consequence worth knowing: **removing a server's config entry does not revoke its
grant.** The token files are keyed on the URL, not on the entry, so a later reconnect
silently resumes the old grant. Use the card's **Disconnect**, or the vendor's own
revoke page, when you mean to end access.

For the two vendors needing an operator-registered app, the split is:

| Half | Where it goes |
|---|---|
| Client ID | `config.json`, under `connections.oauth_clients.<slug>.client_id` |
| Client secret | the encrypted vault, as `CONNECTIONS_<SLUG>_CLIENT_SECRET` |

Enter both under **Settings → OAuth Apps**, which also shows the exact redirect URI
to register in the vendor console. The panel is write-only for the secret: it never
reads one back, so a screenshot of it leaks nothing. An environment pair
(`KIROCREW_CONNECTIONS_<SLUG>_CLIENT_ID` and `_CLIENT_SECRET`) overrides both, for
containers and CI. The registered redirect URI is fixed and cannot be renumbered —
every operator who followed the runbook registered that exact string.

Do not hand-write a client secret into a server entry instead. A secret there sits in
plaintext in a file the agent can read. See [Secrets vault](secrets-vault.md) for what
the vault refuses the agent, and [Blocked commands](blocked-commands.md) for what
happens when the agent tries to read one anyway.

## Health: what a row's badge means

Each row carries the result of a real handshake — Kiro Crew launches the stdio
command or calls the URL and sends an MCP `initialize`.

| Badge | Meaning | What to do |
|---|---|---|
| `ok` | The server answered and listed its tools | nothing |
| `needs_auth` | The server is working correctly and asking for a token | authorize it; this is **not** a failure |
| `error` | Reached and wrong: a bad command, a crash, a refused credential, a broken tool list | read the row's error text |
| `timeout` | No answer inside the budget | usually a cold package cache on first launch; re-probe once |
| `disabled` | You turned it off | nothing; it is never probed |
| `unknown` / `outdated` | No fresh reading yet | re-probe |

Results are cached, so a row you just fixed can keep reading stale until you re-probe
it. The panel names that step.

A remote server that answers with a challenge gets a second, separate badge about the
sign-in: **Sign-in required** (a challenge was seen and no grant exists), **Signed
in** (the grant files are there), or **Not verified** (nothing the check could name).

### Test

A curated provider's card has an owner-only **Test** action, which is the only path
that asks the live runtime what the agent can actually see. It starts a promptless
session under the real agent, reads the MCP manager's status and the final tool
inventory, and answers one of three verdicts: **usable** (tools are exposed),
**no tools** (the server is up and the agent sees nothing from it — usually a server
that is configured but not mounted), or **failed**. It never invokes a tool, so it
cannot have a side effect on your account.

### The launch gates behind a curated card, L0 and L1

Two scheduled jobs are why a curated card claims to work. Neither runs on your
machine; both are why an entry got a card in the first place.

| Gate | Needs an account? | Asserts |
|---|---|---|
| **L0** | no | the vendor's published OAuth metadata still matches the baseline committed with the entry |
| **L1** | yes, one human consent click per provider, once | a grant that exists still works against the live endpoint |

L1's vocabulary is shaped by Kiro Crew holding no token: a tokenless request to a
healthy authorized server *correctly* gets a challenge, so that is its own verdict
rather than a failure. `PASS` and `GRANT_HELD` are green, `NEEDS_RECONSENT` means a
credential was actually refused and a human must re-approve, `FAIL` means reached and
wrong or unreachable, and `SKIPPED` means there was nothing to exercise — L1 never
starts a consent flow of its own. It also stops at listing tools and never calls one,
so no provider dispatch happens outside the governed, audited agent path.

## Tool aliases: why a tool's name may differ

Two MCP servers can ship the same tool name. Linear and Vercel both expose
`list_projects`; Linear, GitHub and GitLab all expose `list_issues`. An agent calls a
tool by bare name, so the second server to mount would shadow the first — one
provider's tool becomes unreachable and the agent silently calls the other one's.

Kiro Crew renames the collision instead. The pattern is always
`<provider>_<tool>`, and **every** claimant is renamed — none keeps the bare name, so
there is no "first one wins" to reason about:

```text
list_projects  →  linear_list_projects   and   vercel_list_projects
list_issues    →  github_list_issues, gitlab_list_issues, linear_list_issues
```

Renames are declared in the catalogue for four providers today — GitHub, Linear,
Vercel and GitLab — and only for the tools that actually collide. A server that is
not the catalogue provider gets no renames, whatever it calls itself.

**If you are an agent:** when a tool you expected is absent, check for the
`<provider>_<tool>` spelling before reporting the server broken, and call the tool
the inventory actually lists rather than the name the vendor's own documentation uses.

## Quarantine: a server that keeps failing

A single failed probe is routine — a cold package cache, a laptop that just woke, a
registry blip. So Kiro Crew counts **consecutive** failures instead, durably, and the
row reports the count. After **3** in a row the server is marked quarantined; change
the threshold with `agent.mcp_quarantine_after_failures`, and set it to `0` to turn
the feature off entirely.

Only `error` and `timeout` count. A status carrying no verdict is ignored rather than
counted either way — including `needs_auth`, because counting that would quarantine
every OAuth server you have not signed into yet.

**Quarantine reports; it does not unmount.** The server stays configured and stays
mounted, and nothing writes `disabled` on your behalf — that key is your choice, and
a counter that flipped it would be indistinguishable from you having switched the
server off by hand.

To clear one: fix the underlying problem, then use the row's reset control. It clears
the counter as well as the flag, so a reset server does not sit one failure away from
crossing again. Resetting a server with nothing on file reports that rather than
claiming success.

**If you are an agent:** a quarantined server means the tool you want has been
unreachable for several consecutive checks, so retrying the same call is unlikely to
help. Say which server is quarantined and what its last error was, and offer the
reset control — do not edit the user's MCP config to work around it.

## Behind enterprise MCP governance

If your organization runs an enterprise MCP registry, a server can be configured
correctly here and still refuse to connect, because the registry — not Kiro Crew —
decides which servers a host may launch. That needs two halves, and both are
required: declaring registry mode on the Kiro Crew side, and having an administrator
allow-list the servers. The walkthrough, including central policy distribution across
hosts, is in
[Kiro Crew behind enterprise MCP governance](https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/enterprise-mcp-governance.md)
— a contributor document in the repository, not part of this installed package.

The longer walkthrough for one stubborn remote OAuth server, including the operator
keystone for an identity provider Kiro Crew does not recognize, is in
[Connecting a remote / OAuth MCP server](https://github.com/kirodotdev/KiroCrew/blob/main/docs/guides/connecting-remote-oauth-mcp-server.md).

## Related docs

- [MCP Apps](mcp-apps.md): rendering an MCP tool's interactive output in chat instead
  of a wall of JSON
- [Secrets vault](secrets-vault.md): credentials encrypted on disk and refused to the
  agent
- [Blocked commands](blocked-commands.md): why a read or a command was refused, and
  what the agent is told to do instead
- [Troubleshooting](troubleshooting.md): the shorter "MCP tools not working" checklist
- [Configuration](configuration.md): the config file, the warm pool, and environment
  variables
