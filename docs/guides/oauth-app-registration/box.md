# Register a Box OAuth app for Kiro Crew Connections

*Who this is for: a Box Admin or Co-Admin of the Box enterprise whose files Kiro Crew should read, or the person who can ask one to click through the Admin Console with them. You need a browser and a Box account that can open the Admin Console. Expected time: 15 minutes if you are the admin; longer if you have to wait for one.*

This runbook covers the **Box MCP server** (`https://mcp.box.com`), Box's hosted Model Context Protocol endpoint. It lets Kiro Crew search, read and (if you allow it) write Box files as the signed-in Box user. Box's own documentation says the credentials for a custom client come from the **Admin Console**, not from the Developer Console app you may already know; the difference matters and is explained in section 1.

## Before you start

- A Box account that is an **Admin or Co-Admin** of the enterprise. Box's setup guide for custom MCP clients starts with "Sign in to the Box Admin Console" and every step after that is an admin action. A plain managed user cannot complete this runbook alone.
- Box states the MCP server is "available on all Box plans", but two of its tool groups need extra enablement or licences: AI tools need Box AI turned on for the enterprise (**Admin Console → Box AI → Settings → Enable AI API**), and Doc Gen tools need an Enterprise Advanced licence. Box's documentation does not state whether a free developer account exposes the Box MCP server in its Admin Console; if you only have a developer account, expect to check for the entry in step 2 of section 1 before going further.
- Kiro Crew installed and its dashboard reachable, so you can open **Settings → OAuth Apps**.
- Facts you will need to type exactly:
  - Redirect URI: `http://127.0.0.1:48108/callback`
  - MCP endpoint: `https://mcp.box.com`
  - Box does not support Dynamic Client Registration. Box's support article says: "Note that the Box MCP Server currently does not support Dynamic Client Registration." The Client ID and Client secret must therefore be created by hand and pasted into Kiro Crew.

## 1. Create the app

Box has two places that mint OAuth client credentials. The **Developer Console** (`https://app.box.com/developers/console` → **New App** → **User** app type; older Box documentation calls this a "Custom App" with "User Authentication (OAuth 2.0)") creates a Platform App for the Box REST API. The **Admin Console** creates **Integration Credentials** for the Box MCP server. Box's MCP documentation describes only the Admin Console path for connecting a custom client to `https://mcp.box.com`, and it does not state whether a token minted by a Developer Console Platform App is accepted by the MCP server. Follow the Admin Console path.

1. Sign in to Box at [https://app.box.com](https://app.box.com) as an Admin or Co-Admin and open the **Admin Console** from the left navigation (the shield icon; it is only shown to admin roles).
2. In the left sidebar click **Integrations**.
3. Find **Custom Box MCP Server**. Either open the **Categories** filter and choose **MCP**, or type `Box MCP` in the search field at the top of the page. If the entry is not there, your enterprise or plan does not show the MCP server yet; stop and contact Box support.
4. Hover over **Custom Box MCP Server** and click **Configure**.
5. Scroll to the **Additional Configuration** section and click **+ Add Integration Credentials**.
6. Enter a name for the credentials, for example `Kiro Crew Connections`, and click **Save**.
7. Expand the details of the entry you just created. Box shows a generated **Client ID** and **Client Secret**. Copy both now; you will paste them into Kiro Crew in section 5. Treat the client secret like a password.
8. Stay on this screen for sections 2 and 3, which fill in the remaining fields of the same entry.

Box adds the entry as a platform app under **Platform → Platform Apps** in the Admin Console, which is where you later disable or delete it. Box notes: "the Integration's availability status does not affect the availability status of its Integration Credentials platform apps", so the credentials have their own on/off switch there.

If you cannot reach the Admin Console and want to try the generic path anyway, the Developer Console steps are: open `https://app.box.com/developers/console`, click **New App**, select **User** as the app type, click **Create**, then on the **Configuration** tab copy the **Client ID** and **Client Secret** from **OAuth 2.0 Credentials**, add the redirect URI under **OAuth 2.0 Redirect URI**, tick scopes under **Application Scopes**, and click **Save Changes**. Box warns: "Once you create an OAuth 2.0 app, you cannot change it to Server Authentication." Whether `https://mcp.box.com` accepts a token from such an app is not stated in Box's documentation; if the Connect step in section 6 fails with an authorization error at the MCP endpoint, that is the likely reason, and you need the Admin Console path after all.

## 2. Redirect URI

1. In the Integration Credentials entry from section 1, find the **Redirect URI** (some Box pages label it **Redirect URIs**) field.
2. Enter exactly:

   ```
   http://127.0.0.1:48108/callback
   ```

3. Click **Save**.

Box accepts a plain-HTTP loopback redirect. Its OAuth setup guide states: "These must be valid URIs that are HTTPS, or a less secure HTTP for localhost or loopback address." The same guide says matching is exact except for the port: "Localhost and loopback address redirect URIs will be permitted to redirect to any port, but the scheme, domain, path and query parameters must match one of the configured URIs." That sentence is written for the Developer Console field; Box's MCP documentation does not separately state the validation rules for the Admin Console **Redirect URI** field, but the credentials are stored as a platform app, so expect the same rule. If the field rejects the value, record the exact error in the Known limits section and stop.

Kiro Crew's OAuth callback listener runs inside kiro-cli on the loopback interface over plain HTTP only; an https loopback is not possible, and the port is fixed per provider (Box is 48108). Remote, tailnet or Docker installs do not need a second redirect URI. After you approve the app, the browser is sent to that loopback URL, which is unreachable from your laptop when the gateway runs elsewhere; copy the full landing URL from the browser's address bar, paste it into the Kiro Crew dashboard when prompted, and the gateway replays it to its own loopback listener.

## 3. Scopes and permissions

Still in the Integration Credentials entry, tick the boxes under **Access Scopes**. Box's guidance: "Scopes define the maximum set of actions. Users can only access content they already have permission to view or edit in Box."

| Admin Console label | OAuth scope | Why |
|---------------------|-------------|-----|
| **Read and write all files and folders stored in Box** | `root_readwrite` | Box's client-side setup lists `root_readwrite` as the first scope for the MCP server. Tick **Read all files and folders stored in Box** (`root_readonly`) instead if you want a read-only connection; Box's documentation does not state whether the MCP server works with `root_readonly` alone. |
| **Manage AI** | `ai.readwrite` | Box's support article says "Under Scopes, ensure that Manage AI is selected." Needed for the Ask / Extract tools. Also requires Box AI to be enabled for the enterprise (see Before you start). |
| (Doc Gen) | `docgen.readwrite` | Optional. Box: "The `docgen.readwrite` scope requires an Enterprise Advanced license." Skip it unless you have that licence. |

Click **Save** after ticking the scopes. Individual MCP tools can additionally be switched on or off per enterprise under the Box MCP server's tool configuration; Box's FAQ notes that "Certain tools are disabled by default and must be enabled by an admin."

## 4. Review and publishing requirements

- **No Box review or marketplace listing** is needed. The Integration Credentials you created are private to your enterprise.
- **Admin approval is built in**: only an Admin or Co-Admin can create the credentials, so there is no separate "submit for approval" step. If you took the Developer Console route instead, Box's rule applies: "Unpublished applications using [OAuth 2.0] authentication may require enablement by a Box Admin or Co-Admin", and the admin enables the app by Client ID under **Platform → Platform Apps**.
- **Pricing**: Box states API calls are free only for "an app published in the Box Integrations Center" used with your own Box login. Kiro Crew's credentials are "additional integration credentials from the Box MCP Server", which Box lists among the charged cases (1 API call per tool invocation, plus AI Units for AI tools). Check your plan's API allowance.
- **Re-authorization**: if you change scopes later, Box requires the app to be re-authorized before the change takes effect; users will be asked to click Connect again.

## 5. Enter the credentials in Kiro Crew

1. Open the Kiro Crew dashboard and go to **Settings → OAuth Apps**.
2. Find the **Box** card.
3. Paste the **Client ID** from section 1 into the **Client ID** field.
4. Paste the **Client Secret** into the **Client secret** field. Box requires the secret at token exchange: its authorization-server metadata lists only `client_secret_basic` and `client_secret_post` as token endpoint authentication methods, and its token endpoint documentation sends `client_secret` on every `authorization_code` request. A public (secret-less) PKCE client is not documented.
5. Use the copy button next to the redirect URI on the card to confirm it reads `http://127.0.0.1:48108/callback`, the same value you saved in section 2.
6. Save.

Container or CI alternative: set the environment variables `KIROCREW_CONNECTIONS_BOX_CLIENT_ID` and `KIROCREW_CONNECTIONS_BOX_CLIENT_SECRET` on the gateway process. Environment variables override dashboard values.

## 6. Verify the connection

1. Open **Capabilities → Connections** in the Kiro Crew dashboard. The Box card should now read **Connect** instead of **Needs configuration**.
2. Click **Connect**. A browser tab opens on `https://account.box.com/api/oauth2/authorize` showing Box's consent page for the app name you chose in section 1, with the scopes from section 3.
3. Sign in with your Box account if asked, then click **Grant access to Box**.
4. The browser lands on `http://127.0.0.1:48108/callback?...`. On a local install this completes automatically. On a remote install, copy the landing URL from the address bar and paste it into the dashboard when prompted.
5. The Box card should now read **Connected**. Ask an agent to search Box for a file you know exists; a result proves the token works and the `root_readwrite` (or `root_readonly`) scope was granted.
6. If the consent page shows `redirect_uri_missing` or an "invalid redirect" error, the value in section 2 does not match exactly (scheme, host `127.0.0.1`, path `/callback`). If the token exchange fails with `invalid_client`, the Client ID or secret was pasted wrong or the credentials entry was disabled under **Platform → Platform Apps**.
7. If Connected shows but a search returns nothing, check the Box user's own access first. Box: "If a file is missing from search or Ask, check collaborations first, not the model." The MCP server never sees content the signed-in user cannot open.
8. If AI tools (Ask, Extract) are missing from the agent's tool list, Box AI is not enabled for the enterprise or **Manage AI** was not ticked in section 3. Fix the setting, then disconnect and reconnect so the tool list refreshes.

## Known limits

- **Admin-gated**: creating the credentials requires a Box Admin or Co-Admin. A per-install app cannot be created by an ordinary user, and Box's documentation does not state whether a free developer account can do it.
- **Confidential client only**: the client secret is mandatory at token exchange. Box's metadata advertises PKCE (`code_challenge_methods_supported: ["S256"]`), but no secret-less `none` method, so PKCE is an addition, not a replacement.
- **No Dynamic Client Registration**: quoted in Before you start. `POST https://api.box.com/oauth2/register` does not exist.
- **Issuer string discrepancy**: the MCP server's protected-resource metadata (`https://mcp.box.com/.well-known/oauth-protected-resource`) lists `"authorization_servers": ["https://api.box.com/"]` with a trailing slash, while the authorization-server metadata it points to (`https://api.box.com/.well-known/oauth-authorization-server`, also mirrored at `https://account.box.com/.well-known/oauth-authorization-server`) declares `"issuer": "https://api.box.com"` without one. A client that compares issuer strings strictly will see a mismatch. `https://mcp.box.com/.well-known/oauth-authorization-server` itself answers 401.
- **Endpoints**: authorize `https://account.box.com/api/oauth2/authorize`; token `https://api.box.com/oauth2/token`; revoke `https://api.box.com/oauth2/revoke` (POST with `client_id`, `client_secret`, `token`). To cut off Kiro Crew entirely, disable or delete the Integration Credentials entry under **Admin Console → Platform → Platform Apps**.
- **Tool availability depends on plan and admin toggles**: AI tools need Box AI enabled and an AI-enabled plan; Doc Gen needs Enterprise Advanced; some tools are off by default. Clients cache the tool list, so after an admin change disconnect and reconnect.
- **Metered**: custom Integration Credentials are a charged API-call case in Box's pricing table.
- **Token lifetimes**: Box access tokens expire after 60 minutes, refresh tokens "after 60 days or 1 use", and authorization codes after 30 seconds. Kiro Crew must refresh on schedule; a connection left idle for more than 60 days needs a fresh Connect.
- **Narrowing at authorize time**: Box lets the authorization URL carry a `scope` parameter that further restricts the token below the scopes ticked in section 3 ("When the scope parameter is omitted the application will use the scopes that were set when the application was created"). If Kiro Crew sends no `scope` parameter, the ticked scopes are what the token gets.
- **Legacy self-hosted server**: Box marks the open-source self-hosted Box MCP server "deprecated. Do not start new work on it." Kiro Crew uses the hosted endpoint only.

## Sources

- https://developer.box.com/guides/box-mcp/ (accessed 2026-09-10)
- https://developer.box.com/guides/box-mcp/setup/ (accessed 2026-09-10)
- https://developer.box.com/guides/box-mcp/permission-aware-access/ (accessed 2026-09-10)
- https://developer.box.com/guides/box-mcp/integrations/cursor/ (accessed 2026-09-10)
- https://support.box.com/hc/en-us/articles/43847256139923-Managing-Box-MCP-Servers (accessed 2026-09-10)
- https://support.box.com/hc/en-us/articles/30900136778259-Adding-Integration-Credentials-for-Customer-Instance-Integrations (accessed 2026-09-10)
- https://developer.box.com/guides/authentication/oauth2/oauth2-setup/ (accessed 2026-09-10)
- https://developer.box.com/guides/authentication/oauth2/without-sdk/ (accessed 2026-09-10)
- https://developer.box.com/reference/post-oauth2-token/ (accessed 2026-09-10)
- https://developer.box.com/reference/post-oauth2-revoke/ (accessed 2026-09-10)
- https://developer.box.com/guides/api-calls/permissions-and-errors/scopes/ (accessed 2026-09-10)
- https://developer.box.com/guides/api-calls/permissions-and-errors/expiration/ (accessed 2026-09-10)
- https://developer.box.com/guides/authorization/platform-app-approval/ (accessed 2026-09-10)
- https://docs.box.com/en/box-mcp/faq (accessed 2026-09-10)
- https://docs.box.com/en/box-mcp/pricing (accessed 2026-09-10)
- https://mcp.box.com/.well-known/oauth-protected-resource (accessed 2026-09-10)
- https://api.box.com/.well-known/oauth-authorization-server (accessed 2026-09-10)
