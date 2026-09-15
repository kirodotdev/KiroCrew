# Register an Asana OAuth app for Kiro Crew Connections

*Who this is for: the person who operates a Kiro Crew install and wants agents to read and update Asana work through Asana's hosted MCP server. Expected time: 10 minutes; longer if your Asana domain is in app "approval mode" and a super admin must approve the app.*

Kiro Crew connects to the Asana V2 MCP server at `https://mcp.asana.com/v2/mcp`. Asana requires every MCP client to be pre-registered: "The V2 MCP server (`https://mcp.asana.com/v2/mcp`) requires OAuth 2.0 authentication with a registered client ID and client secret", and "Dynamic client registration is not supported with Asana's V2 MCP Server." You create that registration in Asana's developer console as an app of type **MCP app**. The older V1 beta server at `https://mcp.asana.com/sse` is deprecated and shuts down on 5 August 2026; do not use it.

## Before you start

- An Asana account that is a member of the workspace or organization the agents should work in. The developer console is available to any Asana user; no paid developer program is needed.
- Know whether your Asana domain is on the Enterprise+ or Legacy Enterprise tier with app management turned on. Asana's MCP page says the MCP client app "must not be blocked in your workspace via Asana app management. If it is, you will be prompted to send a request for your admin to unblock the app for your domain when trying to authorize." If that is your situation, find out who your super admin is before you start.
- A running Kiro Crew gateway with the dashboard open.
- Note the exact redirect URI you will register: `http://127.0.0.1:48102/callback`. It is plain HTTP on the loopback interface. Kiro Crew's callback listener runs inside kiro-cli on the loopback interface over plain HTTP only; an https loopback is not possible.
- Tokens from an MCP app work only against the MCP server: "Tokens issued for MCP apps only work with the MCP server ... If you need to make standard Asana API requests, create a separate API app." Do not reuse an existing Asana API app for this.

## 1. Create the app

1. Go to `https://app.asana.com/0/my-apps` (the developer console, labelled **My apps**) and sign in.
2. Click **Create new app**.
3. In **App name**, type a name your teammates will recognize on the consent screen, for example `Kiro Crew Connections`. Asana shows this name "when your application requests permission to access their account as well as when they review the list of apps they have authorized."
4. Under app type, select **MCP app**. Do not pick the standard API app type; its tokens are rejected by the MCP server.
5. Click **Create app**.
6. The app opens on a page showing the **Client ID** and **Client secret**. Copy both now. To rotate the secret later: select the app, click **OAuth** in the sidebar, then **Reset** next to the client secret.
7. In the left sidebar, click **OAuth**. Under **Redirect URLs**, click **Add redirect URL**, enter exactly `http://127.0.0.1:48102/callback`, and save. Section 2 explains the value.
8. In the left sidebar, click **Manage distribution**. Under **Distribution method** choose either **Specific workspaces** (then **+ Add workspace** and pick every workspace your users will connect from) or **Any workspace**. Click **Save changes**. Asana warns: "If you choose 'Specific workspaces' but don't select any workspaces, users will see an error saying 'This app is not available to your Asana workspace or organization.'"
9. Optionally fill in **App listing details** (icon, short and long description, company name, support URL, privacy policy URL). Asana shows these to admins who decide whether to approve or block an app, so fill them in if your domain uses app management.

## 2. Redirect URI

Register exactly `http://127.0.0.1:48102/callback`. The Kiro Crew card shows the same string with a copy button.

Asana's general OAuth guide says redirect URLs for "non-native applications *must* supply a 'https' URL" and that Asana "enforce[s] the use of `https` redirect endpoints for application registrations". For MCP apps, however, Asana's own client guide registers plain HTTP loopback URLs: it instructs operators to set the redirect URL to `http://localhost:8080/callback` for Claude Code, `http://127.0.0.1:33418/` for VS Code, and `http://localhost:3334/oauth/callback` for "most other clients (Windsurf, Kiro, Codex)". The loopback HTTP form is therefore accepted for MCP apps, and Kiro Crew's `http://127.0.0.1:48102/callback` follows the same pattern.

The port is part of the match. Asana states: "The redirect URL **must match exactly** between your Asana app settings and your client configuration", and, in the OAuth reference, `redirect_uri` "must match the redirect URL specified in the application settings". Asana's documentation does not describe any loopback exception that ignores the port, so register the port `48102` exactly as shown, including the `/callback` path and no trailing slash. If Kiro Crew ever changes the port, the Asana registration must change too.

Remote, tailnet and Docker installs do not need a second redirect URI. After the user clicks **Allow** on app.asana.com, the browser is sent to `http://127.0.0.1:48102/callback?code=...`, which is unreachable from the user's laptop when the gateway runs elsewhere. The user copies that landing URL from the browser's address bar and pastes it into the Kiro Crew dashboard; the gateway replays it to its own loopback listener and finishes the token exchange.

## 3. Scopes and permissions

MCP apps do not use Asana's granular `<resource>:<action>` scopes. Asana's integration guide says: "`scope` (optional): Use `default` or omit this parameter—MCP apps don't require specific scopes", and its troubleshooting entry adds: "This error ['Invalid scope(s) requested'] appears if you include a `scope` parameter in your authorization request. MCP apps don't require scopes—remove the `scope` parameter entirely." The server's protected-resource metadata lists exactly one supported scope, `default`.

What the token can do is bounded by the user, not by the app: "Asana MCP access is currently user-based. All actions taken over MCP will appear as the user who authorized them. All authorizations may access any available MCP tool (at the time of authorization or any tools added in the future). Access is determined by the authenticated user's Asana permissions."

Consequences for a read-mostly deployment:

- There is no way to register an Asana MCP app as read-only. Every authorized token can call every MCP tool, including task creation and edits.
- Limit exposure by connecting with an Asana user whose workspace permissions are already narrow (for example a guest or a member of only the projects the agent needs), and by keeping Kiro Crew's own tool-approval rules on for write actions.
- The **Permission scopes** section under the **OAuth** tab applies to standard API apps; for an MCP app the console does not gate anything the server enforces.

Two protocol details worth knowing when reading Kiro Crew logs:

- Asana's MCP authorization request may carry `resource=https://mcp.asana.com/v2` (no `/mcp` suffix), which Asana documents as "optional" and describes as "the one key difference" from its standard OAuth flow. The token is then used against `https://mcp.asana.com/v2/mcp`.
- Discovery works but does not replace registration. The server answers an unauthenticated request with `WWW-Authenticate: Bearer realm="Asana MCP", resource_metadata="https://mcp.asana.com/.well-known/oauth-protected-resource/v2"`; that document names `https://app.asana.com` as the authorization server, whose metadata at `https://app.asana.com/.well-known/oauth-authorization-server` lists the authorize, token and revoke endpoints and no registration endpoint. Asana: "Although these documents can be used to dynamically discover endpoints, clients must still pre-register to get a client id and client secret."

## 4. Review and publishing requirements

- No Asana review, App Directory listing or publication is required. An unpublished app is usable as soon as **Manage distribution** includes the user's workspace.
- Workspace membership: **Specific workspaces** can only list workspaces the app creator belongs to. If the operator is not a member of a target workspace, either add a member as the app owner or choose **Any workspace**.
- Admin app management (Enterprise+ and Legacy Enterprise tiers): super admins can allow or block apps from the admin console's **Apps** tab, and can place a domain in "approval mode" where no apps are allowed unless explicitly approved by the super admin. For V2 MCP clients "this will be a per-client configuration by blocking or allowing the associated app", meaning your Kiro Crew registration is approved or blocked on its own. Asana's help-center pages for this feature could not be read by the author at time of writing (the site requires a browser); the wording above is quoted from Asana's own summary of those pages.
- Other tiers have no per-app admin control; Asana says such customers may contact Asana Support (the request must come from a super admin) to block the beta V1 app only.
- Approval interruption: when the app is blocked, the authorizing user sees a prompt to request that the admin unblock it. Until the admin acts, **Connect** in Kiro Crew cannot complete.

## 5. Enter the credentials in Kiro Crew

1. Open the Kiro Crew dashboard and go to **Settings → OAuth Apps → Asana card**.
2. Paste the value from step 1.6 into **Client ID**.
3. Paste the client secret into **Client secret**.
4. Use the card's copy button for the redirect URI to double-check that the Asana app holds exactly `http://127.0.0.1:48102/callback`.
5. Save.

Container or CI alternative: set the environment variables `KIROCREW_CONNECTIONS_ASANA_CLIENT_ID` and `KIROCREW_CONNECTIONS_ASANA_CLIENT_SECRET` on the gateway process. Environment variables override dashboard values.

Asana requires the secret at token exchange (`client_secret` is marked "required" and the authorization server advertises only `client_secret_post` and `client_secret_basic`), so the app is a confidential client and the secret must live on the gateway. Asana's guidance: "Use unique credentials per developer (don't share)", "Rotate credentials regularly", "Never commit your credentials to version control".

## 6. Verify the connection

1. Open **Capabilities → Connections**. The Asana card should read **Connect** instead of **Needs configuration**.
2. Click **Connect**. An app.asana.com page opens. Sign in if prompted, review the request, and click **Allow**. The page shows the **App name** from step 1.3 and the account being authorized; if the wrong Asana account is shown, sign out of Asana in that browser first and start again from the card.
3. Local installs return to the dashboard automatically; remote installs show the paste box described in section 2. If the browser instead shows a plain-text error before any consent page, Asana says that happens when "either the `client_id` or `redirect_uri` do not match": compare the Client ID on the card with the console, and the redirect URL with `http://127.0.0.1:48102/callback` character by character.
4. The card should now read **Connected**.
5. Ask an agent something read-only, for example "list my incomplete Asana tasks due this week". A successful answer confirms the token reaches `https://mcp.asana.com/v2/mcp`.
6. If you see "This app is not available to your Asana workspace or organization", go back to **Manage distribution** and add the user's workspace (section 1, step 8).
7. If you see "Invalid scope(s) requested", the client is sending a `scope` parameter; Asana says to remove it. Report this against Kiro Crew rather than changing the app.
8. If authentication fails without a specific message, Asana's checklist is: verify the redirect URL matches exactly, verify client ID and secret, check **Manage distribution**, confirm the endpoints are `https://app.asana.com/-/oauth_authorize` and `https://app.asana.com/-/oauth_token`.

Revoking: the user opens their Asana **Settings**, then the **Apps** tab, which "allows you to manage the integrations you've authorized to access your account"; Asana states "You can revoke an app's access at any time from this tab using the Deauthorize button next to that app's name." An operator can also revoke programmatically with `POST https://app.asana.com/-/oauth_revoke` using the refresh token. Super admins on eligible tiers can block the app for the whole domain from the admin console.

## Known limits

- No read-only mode: every Asana MCP token can use every tool. Restrict the connecting user's Asana permissions instead.
- Confidential client only: PKCE is supported (`S256`) but does not replace the secret; `client_secret` is still required at `https://app.asana.com/-/oauth_token`.
- Exact redirect match including port: Asana documents no port-ignoring loopback rule, so the registration and Kiro Crew's port must stay in sync.
- Access tokens last one hour; Kiro Crew must refresh with the refresh token, and the refresh call also requires the client secret.
- MCP-app tokens cannot call the standard Asana REST API; a second, standard API app is needed for that.
- Distribution is per workspace; adding a new workspace later requires editing **Manage distribution**.
- V1 beta compatibility ends on 5 August 2026: the `https://mcp.asana.com/sse` endpoint and the single shared "Asana MCP" app disappear. Any older Kiro Crew configuration pointing at `/sse` must be replaced by this registration.
- Admin control exists only on Enterprise+ and Legacy Enterprise; on other tiers, the only ways to stop the integration are the user's **Deauthorize** button, the operator's `oauth_revoke` call, or deleting the app in the developer console.
- Asana treats tokens as opaque and says their format "may change without notice"; do not build any checks on token shape.
- Asana's documentation does not state a limit on redirect URLs per app or on the number of apps a user may create.
- Asana's help-center articles on app management render only in a browser; the quotes in section 4 come from Asana's developer documentation and Asana's own search summaries of those articles.

## Sources

- https://developers.asana.com/docs/integrating-with-asanas-mcp-server (accessed 2026-09-10)
- https://developers.asana.com/docs/connecting-mcp-clients-to-asanas-v2-server (accessed 2026-09-10)
- https://developers.asana.com/docs/using-asanas-mcp-server (accessed 2026-09-10)
- https://developers.asana.com/docs/oauth (accessed 2026-09-10)
- https://developers.asana.com/docs/share-your-app (accessed 2026-09-10)
- https://mcp.asana.com/.well-known/oauth-protected-resource/v2 (accessed 2026-09-10)
- https://app.asana.com/.well-known/oauth-authorization-server (accessed 2026-09-10)
- https://help.asana.com/s/article/app-management-and-integrations (accessed 2026-09-10)
- https://help.asana.com/s/article/apps-settings (accessed 2026-09-10)
- https://help.asana.com/s/article/api (accessed 2026-09-10)
