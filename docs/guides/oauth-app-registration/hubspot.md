# Register a HubSpot OAuth app for Kiro Crew Connections

*Who this is for: a HubSpot Super Admin (or a user with App Marketplace Access permission) of the HubSpot account whose CRM data Kiro Crew should read. You need a browser and access to that account's Development area. Expected time: 15 minutes.*

This runbook covers the **remote HubSpot MCP server** at `https://mcp.hubspot.com`, which lets Kiro Crew query CRM records, activities, conversations and marketing content as the signed-in HubSpot user. It is not the local "Developer MCP server" used for building HubSpot apps with the HubSpot CLI.

## Before you start

- A HubSpot account on the current Developer Platform. The remote MCP server is "available across all hubs and tiers" (HubSpot changelog, January 2026); no paid tier or developer test account is required. A free CRM account works.
- Permission to create apps: the **Development** item in HubSpot's main navigation. HubSpot's OAuth guide says installers "must either be a Super Admin or have HubSpot Marketplace Access permissions"; assume the same for creating the app.
- Kiro Crew installed and its dashboard reachable, so you can open **Settings → OAuth Apps**.
- Facts you will need to type exactly:
  - Redirect URI: `http://localhost:48107/callback`
  - MCP endpoint: `https://mcp.hubspot.com` (HubSpot's inspector walkthrough writes it with a trailing slash, `https://mcp.hubspot.com/`; Kiro Crew handles this, you do not type it anywhere)
  - HubSpot does not support Dynamic Client Registration for this server; the Client ID and Client secret must be created by hand and entered into Kiro Crew.
  - HubSpot requires PKCE on every MCP OAuth flow. Kiro Crew's OAuth client sends the PKCE parameters; nothing for you to configure.

## 1. Create the app

HubSpot has a dedicated app type for this, the **MCP auth app**. Do not create a "public app", "private app" or a Projects-based app; those exist for other purposes and would need scopes configured by hand.

1. Sign in to HubSpot and open the account Kiro Crew should read. If you belong to several accounts, pick the right one from the account switcher first; the app is created per account.
2. In the main navigation bar, click **Development**.
3. In the left sidebar, click **MCP Auth Apps**. (Direct link: [https://app.hubspot.com/l/mcp-auth-apps/](https://app.hubspot.com/l/mcp-auth-apps/).)
4. In the upper right, click **Create MCP auth app**.
5. Fill in the dialog:
   - **App name**: something recognisable, for example `Kiro Crew Connections`.
   - **Description**: optional.
   - **Redirect URL**: `http://localhost:48107/callback` (see section 2 for why this host name).
   - **Icon**: optional.
6. Click **Create**.
7. HubSpot opens the app's details page. Under the client credentials section, copy the **Client ID** and reveal and copy the **Client secret**. You will paste both into Kiro Crew in section 5. Treat the client secret like a password.

You can change the name, description, icon and redirect URLs later with **Edit info** in the upper right of the details page.

## 2. Redirect URI

The redirect URL is entered in the **Create MCP auth app** dialog (step 5 above) or later via **Edit info**. The value must be exactly:

```
http://localhost:48107/callback
```

Kiro Crew registers this provider's callback on the host name `localhost` because that is the only host name HubSpot exempts from its HTTPS rule. HubSpot's OAuth documentation for redirect URLs says: "For security reasons, this URL must use `https` in production. (When testing using `localhost`, `http` can be used.) You also must use a domain, as IP addresses are not supported." A `127.0.0.1` literal, which most other Kiro Crew providers use, would fall under the IP-address refusal, so HubSpot is one of the providers whose callback is registered on `localhost` instead. HubSpot's own MCP page documents `http://localhost:6274/oauth/callback/debug` as a valid redirect for the MCP Inspector, which confirms http on `localhost` is accepted.

If you add more than one redirect URL, HubSpot uses the first one as the default. Keep the Kiro Crew URI first.

Kiro Crew's OAuth callback listener runs inside kiro-cli on the loopback interface over plain HTTP only; an https loopback is not possible, and the port is fixed per provider (HubSpot is 48107). Remote, tailnet or Docker installs do not need a second redirect URI. After you approve the app, the browser is sent to that loopback URL, which is unreachable from your laptop when the gateway runs on another host; copy the full landing URL from the browser's address bar, paste it into the Kiro Crew dashboard when prompted, and the gateway replays it to its own loopback listener.

## 3. Scopes and permissions

There is nothing to configure. HubSpot: "When creating an MCP auth app, it's important to note that you don't explicitly define the app's scopes. Instead, available scopes are automatically determined by two factors: The tools available in the MCP server at the time of installation. The permissions that the user chooses to grant during installation."

What this means in practice:

- The consent screen (section 6) lists the data the MCP server can reach; the connecting user grants some or all of it. To keep Kiro Crew read-mostly, grant the read items and decline write items on that screen.
- Every action is further limited by the connecting user's own HubSpot permissions: "All actions respect your existing HubSpot user permissions."
- Read access covers CRM records (contacts, companies, deals, tickets, leads, users, carts, invoices, orders, line items, products, quotes, subscriptions, custom objects, lists), activities (calls, emails, meetings, notes, tasks), content and marketing (blog posts, landing pages, site pages, campaigns, marketing events, marketing emails and their analytics), conversations, and user/account details.
- Write access, if granted, covers CRM records, activities, content, campaigns and marketing email drafts.
- If your account has **Sensitive Data** turned on, activities and conversations are blocked through the MCP server regardless of what was granted.
- When HubSpot adds tools, "users who have already installed the app will need to re-install to grant any new scopes." Expect an occasional re-consent.

Because the classic CRM scope names (`crm.objects.contacts.read` and friends) are not entered anywhere for an MCP auth app, this runbook does not list them.

## 4. Review and publishing requirements

- **No Marketplace listing and no HubSpot review** is required. An MCP auth app is created inside your own account and installed into that account by its own users. HubSpot's changelog describes the feature as "fully self-service" for "ecosystem partners and customers".
- **No developer account** (the separate "app developer account" type) is required; the app lives under **Development → MCP Auth Apps** in the regular CRM account.
- **Installer permissions**: the user who clicks Connect must be a Super Admin or have HubSpot Marketplace Access, per HubSpot's general OAuth rule. HubSpot's MCP FAQ adds: "The admin of the HubSpot account needs to connect first, to allow other users in the account to connect thereafter." Have an admin do the first connection.
- **Distribution limits**: "Apps built with the HubSpot MCP server follow the same distribution limits as other HubSpot apps." For a single-account install this does not matter; only multi-account distribution would run into HubSpot's install-count limits or a Marketplace listing.

## 5. Enter the credentials in Kiro Crew

1. Open the Kiro Crew dashboard and go to **Settings → OAuth Apps**.
2. Find the **HubSpot** card.
3. Paste the **Client ID** from the MCP auth app details page into **Client ID**.
4. Paste the **Client secret** into **Client secret**.
5. Use the card's copy button for the redirect URI to confirm it reads `http://localhost:48107/callback`, matching what you entered in section 2.
6. Save.

Container or CI alternative: set the environment variables `KIROCREW_CONNECTIONS_HUBSPOT_CLIENT_ID` and `KIROCREW_CONNECTIONS_HUBSPOT_CLIENT_SECRET` on the gateway process. Environment variables override dashboard values.

## 6. Verify the connection

1. In the dashboard open **Capabilities → Connections**. The HubSpot card should have changed from "Needs configuration" to **Connect**.
2. Click **Connect**. A browser tab opens HubSpot's authorization page. HubSpot describes the three steps you will see: "Select the HubSpot account to connect. Grant permissions to the app. Authorize the connection."
3. Choose the account, review the permission list (untick write items if you want read-only), and authorize.
4. If the gateway runs on the same machine as the browser, the tab lands on the loopback callback and the card shows **Connected**. If the gateway is remote, copy the landing URL from the address bar and paste it into the dashboard prompt.
5. Ask the agent something read-only, for example "how many open deals are in HubSpot?". HubSpot's own smoke test is the `get_user_details` tool, which returns the connected user, account and available tools.
6. Confirm the install landed: in HubSpot click the **settings icon** → **Integrations → Connected Apps**; the app appears under **My apps** with your name as installer.

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Authorization page shows a redirect URL error | The URI saved on the app does not exactly match `http://localhost:48107/callback` (scheme, host name, port and path all count) | Re-check section 2 and fix the value with **Edit info** |
| Authorization fails before the consent screen | Connecting user lacks Super Admin / App Marketplace Access, or no admin has connected yet | Have an admin connect first (section 4) |
| PKCE error during token exchange | Client did not send `code_challenge` / `code_verifier` | Kiro Crew sends them; report the exact error text to the Kiro Crew team |
| Worked, then stopped after a period of idleness | Refresh token invalidated | Click **Connect** again to re-run the flow |
| Activities or conversations missing | Account has Sensitive Data turned on | Expected; HubSpot blocks these through MCP |

To revoke the grant later: **settings icon → Integrations → Connected Apps**, click the app, then use its **Actions → Uninstall** menu. Uninstalling invalidates the refresh token; Kiro Crew's card returns to **Connect**. Deleting the MCP auth app itself under **Development → MCP Auth Apps** revokes every user's grant at once.

## Known limits

- Plain-http redirects are accepted only on the host name `localhost` ("IP addresses are not supported"), which is why Kiro Crew registers `http://localhost:48107/callback` rather than a `127.0.0.1` literal for this provider.
- PKCE is mandatory: "HubSpot's MCP server requires PKCE (Proof Key for Code Exchange) for all OAuth authentication flows."
- Access tokens expire and must be refreshed with the refresh token; if the refresh token is invalidated the user must reconnect. HubSpot has announced single-use (rotating) refresh tokens for MCP clients.
- No Dynamic Client Registration; credentials come from the MCP auth app.
- Scopes cannot be pinned by the operator; they follow HubSpot's tool set and the user's grant, and may require re-consent when HubSpot adds tools.
- Accounts with Sensitive Data enabled lose activity and conversation access through MCP.
- Search is built on the CRM search API, which has no vector search.
- HubSpot's authorization-server metadata for `https://mcp.hubspot.com` was not readable by this runbook's tooling, so the issuer string is not quoted here; HubSpot documents the server URL both as `https://mcp.hubspot.com` and `https://mcp.hubspot.com/`.

## Sources

- https://developers.hubspot.com/docs/apps/developer-platform/build-apps/integrate-with-the-remote-hubspot-mcp-server (accessed 2026-09-10)
- https://developers.hubspot.com/docs/apps/developer-platform/build-apps/authentication/oauth/working-with-oauth (accessed 2026-09-10)
- https://developers.hubspot.com/changelog/public-beta-self-service-mcp-auth-apps-for-the-hubspot-remote-mcp-server (accessed 2026-09-10)
- https://developers.hubspot.com/changelog/remote-hubspot-mcp-server-is-now-generally-available (accessed 2026-09-10)
- https://developers.hubspot.com/ai-tools/mcp (accessed 2026-09-10)
- https://knowledge.hubspot.com/integrations/manage-your-connected-apps (accessed 2026-09-10)
