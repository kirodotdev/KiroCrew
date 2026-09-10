# Register a GitHub OAuth app for Kiro Crew Connections

*Who this is for: the person who operates a Kiro Crew install and wants agents to reach GitHub through GitHub's hosted MCP server. Expected time: 15 minutes, plus any wait for an organization owner's approval.*

Kiro Crew connects to the remote GitHub MCP server at `https://api.githubcopilot.com/mcp/`. That server does not issue credentials itself and does not support Dynamic Client Registration, so every MCP client needs its own GitHub app registration. GitHub's host-integration guide states it directly: "Dynamic Client Registration is NOT supported by Remote GitHub MCP Server at this time" and "Each MCP host application needs to configure a GitHub App or OAuth App to support remote access via OAuth." This runbook registers a plain **OAuth App** (not a GitHub App), because that is the registration type whose credentials are exactly a client ID and a client secret, which is what the Kiro Crew Connections card asks for.

## Before you start

- A GitHub account. If the repositories you want to reach belong to an organization, you also need to know whether that organization has "OAuth app access restrictions" turned on (it is on by default for new organizations). If it is, an organization owner must approve the app before it can read organization data; plan for that hand-off.
- A running Kiro Crew gateway with the dashboard open, so you can copy the redirect URI from the card and paste credentials back in.
- Decide who owns the registration. An OAuth app can be created under your personal account or under any organization where you have admin rights. A registration owned by the organization is automatically trusted when the organization enables OAuth app access restrictions.
- Note the exact redirect URI you will register: `http://127.0.0.1:48101/callback`. It is plain HTTP on the loopback interface. Kiro Crew's callback listener runs inside kiro-cli on the loopback interface over plain HTTP only; an https loopback is not possible.
- Optional alternative: GitHub's MCP documentation also accepts a fine-grained Personal Access Token (PAT) in the `Authorization: Bearer` header for any host that supports remote MCP servers. This runbook covers the OAuth path; a PAT is not entered through the Connections card.

## 1. Create the app

1. Sign in to GitHub. In the upper-right corner, click your profile picture, then **Settings**. To register under an organization instead, open the organization's **Settings** page.
2. In the left sidebar, click **Developer settings**.
3. In the left sidebar, click **OAuth apps**.
4. Click **New OAuth App** (GitHub labels the button **Register a new application** if you have never created one).
5. In **Application name**, type a name your teammates will recognize on the consent screen, for example `Kiro Crew Connections`.
6. In **Homepage URL**, type any public URL that describes the install, for example your team's Kiro Crew documentation page. GitHub requires a value here.
7. Optionally fill in **Application description**. Users see it on the authorization page.
8. In **Authorization callback URL**, type exactly `http://127.0.0.1:48101/callback`. Section 2 explains why.
9. Leave **Enable Device Flow** unchecked. Kiro Crew uses the browser redirect flow.
10. **Expire user access tokens** is checked by default. Leave it checked: GitHub then issues an 8-hour access token plus a refresh token that expires after six months without use. If your Kiro Crew build predates refresh-token support for this provider, uncheck it; the runbook does not know your build, so check the release notes.
11. Click **Register application**.
12. On the app page, copy the **Client ID**. Then click **Generate a new client secret**, and copy the secret immediately; GitHub shows it only once.

Why not a GitHub App? GitHub's host guide marks GitHub Apps as the recommended registration type because their tokens expire and use fine-grained permissions. A GitHub App also yields a client ID and client secret, and its user-authorization flow accepts the same callback URL, so it could be entered on the same card. It comes with two costs that matter for a per-install tool: a GitHub App "must be installed on a GitHub Organization before they can be used", with installation normally approved by an organization admin, and it ignores the `scope` parameter, using the permissions fixed in the registration instead. GitHub notes that "OAuth Apps don't require installation and, typically, can be used immediately." Kiro Crew's documentation does not state whether its GitHub connector has been exercised against a GitHub App registration; stay with an OAuth App unless your organization policy forbids OAuth Apps entirely.

## 2. Redirect URI

Register exactly `http://127.0.0.1:48101/callback`. The Kiro Crew card shows the same string with a copy button.

GitHub accepts a loopback HTTP redirect. Its "Loopback redirect urls" passage says: "The optional `redirect_uri` parameter can also be used for loopback URLs, which is useful for native applications running on a desktop computer. If the application specifies a loopback URL and a port, then after authorizing the application users will be redirected to the provided URL and port. The `redirect_uri` does not need to match the port specified in the callback URL for the app." GitHub also notes that the OAuth RFC "recommends not to use `localhost`, but instead to use loopback literal `127.0.0.1`", which is what Kiro Crew uses. Because the port is not compared for loopback URLs, registering the port explicitly is harmless and keeps the entry readable.

GitHub added a per-callback **wildcard matching** toggle on August 3, 2026. Leave it disabled for this app: the registered value and the value Kiro Crew sends are identical, so exact matching is sufficient and safer.

Remote, tailnet and Docker installs do not need a second redirect URI. After the user approves on github.com, the browser is sent to `http://127.0.0.1:48101/callback?code=...`, which is unreachable from the user's laptop when the gateway runs elsewhere. The user copies that landing URL from the browser's address bar and pastes it into the Kiro Crew dashboard; the gateway replays it to its own loopback listener and completes the exchange.

## 3. Scopes and permissions

A GitHub OAuth App has no scope settings in the console. Scopes are requested at authorization time and shown to the user on the consent screen, and the user may grant fewer than requested.

The remote MCP server publishes the scopes it understands in its protected-resource metadata (`https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/`): `repo`, `read:org`, `read:user`, `user:email`, `read:packages`, `write:packages`, `read:project`, `project`, `gist`, `notifications`. The tool reference in the server's README lists `repo` as the "OAuth Challenge Scope" for almost every tool, including read-only ones such as `get_file_contents`.

For read-mostly use, the practical minimum is:

| Scope | Why |
|-------|-----|
| `repo` | Any access to private repositories, including reading files, issues and pull requests. GitHub's OAuth scopes are coarse: `repo` also grants write. There is no read-only OAuth scope for private repositories. |
| `read:org` | Organization and team membership lookups. |
| `read:user` | Resolving the signed-in user (the `context` toolset). |

To keep the agent read-only despite the write-capable `repo` scope, point Kiro Crew at the read-only server URL `https://api.githubcopilot.com/mcp/readonly` (or send the `X-MCP-Readonly: true` header). GitHub documents: "Add `/readonly` to the end of any URL to restrict the tools in the toolset to only those that enable read access." Single toolsets have the same shape, for example `https://api.githubcopilot.com/mcp/x/issues/readonly`. Kiro Crew's documentation does not state which scopes its GitHub connector requests; check the consent screen the first time you connect.

## 4. Review and publishing requirements

- No GitHub review, listing or Marketplace submission is needed. An OAuth App works as soon as it is registered.
- Organization approval: when an organization has "OAuth app access restrictions" enabled (the default for new organizations), "organization members and outside collaborators cannot authorize OAuth app access to organization resources." Apps owned by the organization itself are trusted automatically. For an app owned by a personal account, the hand-off is:
  1. The connecting user clicks **Connect** in Kiro Crew and, on GitHub's consent page, clicks **Request** next to the restricted organization before clicking **Authorize**. GitHub notifies the owners.
  2. An organization owner opens **Organization Settings → Third-party Access → OAuth app policy** (`https://github.com/organizations/<ORG>/settings/oauth_application_policy`), finds the pending request, and clicks **Grant**.
  3. GitHub's documentation does not state whether a token issued before the approval gains organization access afterwards. If the card already shows **Connected** but organization data is still missing after the owner grants access, disconnect in Kiro Crew and connect once more.
  Owners can also pre-approve the app before anyone connects, and can later revoke it from the same page ("Denying access to a previously approved OAuth app").
- Enterprise Cloud with SAML/SSO: the user must have an active SSO session for the organization when authorizing, or the token cannot reach SSO-protected resources.
- Copilot policies: the "MCP servers in Copilot" enterprise/organization policy governs first-party Copilot editors only; GitHub's governance document says it does not affect third-party hosts, whose access is controlled by the OAuth App, GitHub App and PAT policies above.
- GitHub Enterprise Server is not supported by the remote server. GitHub Enterprise Cloud with data residency uses `https://copilot-api.<SUBDOMAIN>.ghe.com/mcp` instead.

## 5. Enter the credentials in Kiro Crew

1. Open the Kiro Crew dashboard and go to **Settings → OAuth Apps → GitHub card**.
2. Paste the value from step 1.12 into **Client ID**.
3. Paste the generated secret into **Client secret**.
4. Use the card's copy button for the redirect URI to double-check that GitHub holds exactly `http://127.0.0.1:48101/callback`.
5. Save.

Container or CI alternative: set the environment variables `KIROCREW_CONNECTIONS_GITHUB_CLIENT_ID` and `KIROCREW_CONNECTIONS_GITHUB_CLIENT_SECRET` on the gateway process. Environment variables override dashboard values.

GitHub's host guide warns that "end users will be able to discover your 'client secret'" when a client runs on customer hardware, and recommends a registration "exclusively dedicated" to that client. Keep this app for Kiro Crew only, and rotate the secret with **Generate a new client secret** if it leaks.

## 6. Verify the connection

1. Open **Capabilities → Connections**. The GitHub card should read **Connect** instead of **Needs configuration**.
2. Click **Connect**. A github.com page opens listing the requested scopes and, if you belong to restricted organizations, a **Request** or **Grant** control per organization.
3. Click **Authorize**. Local installs return to the dashboard automatically; remote installs show the paste box described in section 2.
4. The card should now read **Connected**.
5. Ask an agent something read-only, such as listing open issues in a repository you can see. If organization repositories are missing, the organization has not approved the app yet (section 4).
6. To confirm the grant on the GitHub side, open **Settings → Applications → Authorized OAuth Apps**; your app should be listed with the granted scopes.

Revoking: the user opens **Settings → Applications → Authorized OAuth Apps**, clicks the **⋯** menu next to the app, then **Revoke**. An organization owner can also remove access under **Third-party Access**. Deleting the OAuth App in Developer settings revokes every token it issued.

## Known limits

- No read-only scope for private repositories: `repo` is read and write. Use the `/readonly` server URL to constrain tools.
- `client_secret` is required at token exchange ("Required" in GitHub's table) even when PKCE is used; the app is a confidential client, so the secret must live on the gateway.
- Expiring tokens: with the default setting, access tokens last 8 hours and refresh tokens 6 months without use. If Kiro Crew is not run for six months, the user must reconnect.
- GitHub limits a user/app/scope combination to ten live tokens and ten new tokens per hour; repeated reconnects can hit this.
- The GitHub discovery documents are in public preview and "subject to change". Kiro Crew should follow the `WWW-Authenticate` `resource_metadata` URL rather than hard-coding endpoints, as GitHub's host guide recommends.
- Organization approval is per organization; in a multi-organization enterprise an OAuth App cannot be approved at the enterprise level (only GitHub Apps can).
- SSO overlay: on GitHub Enterprise Cloud, an OAuth token only reaches SSO-protected organizations if the user had a valid SSO session when authorizing. A user who authorized while signed out of SSO must revoke and reconnect.
- GitHub Enterprise Server has no hosted MCP server; this runbook applies to github.com and to `ghe.com` data-residency tenants only.
- Users can edit granted scopes on the consent page and grant less than requested; if tools fail with permission errors after connecting, compare the granted scopes shown under **Authorized OAuth Apps** with the table in section 3.
- The Homepage URL, description and app name are public. GitHub's guidance: "Only use information in your OAuth app that you consider public."

## Sources

- https://github.com/github/github-mcp-server/blob/main/README.md (accessed 2026-09-10)
- https://github.com/github/github-mcp-server/blob/main/docs/remote-server.md (accessed 2026-09-10)
- https://github.com/github/github-mcp-server/blob/main/docs/host-integration.md (accessed 2026-09-10)
- https://github.com/github/github-mcp-server/blob/main/docs/policies-and-governance.md (accessed 2026-09-10)
- https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app (accessed 2026-09-10)
- https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps (accessed 2026-09-10)
- https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps (accessed 2026-09-10)
- https://docs.github.com/en/apps/github-authentication-discovery-endpoints (accessed 2026-09-10)
- https://github.com/.well-known/oauth-authorization-server/login/oauth (accessed 2026-09-10)
- https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/ (accessed 2026-09-10)
- https://docs.github.com/en/organizations/managing-oauth-access-to-your-organizations-data/about-oauth-app-access-restrictions (accessed 2026-09-10)
- https://docs.github.com/en/apps/oauth-apps/using-oauth-apps/reviewing-your-authorized-oauth-apps (accessed 2026-09-10)
