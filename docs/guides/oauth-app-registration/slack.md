# Register a Slack OAuth app for Kiro Crew Connections

*Who this is for: the person who owns (or can create apps in) the Slack workspace that Kiro Crew should read. You need a browser and a Slack account with permission to install apps in that workspace. Expected time: 20 minutes, plus any wait for a workspace admin to approve the app.*

This runbook covers the **Slack MCP server** (`https://mcp.slack.com/mcp`), which lets Kiro Crew search and read Slack on behalf of the signed-in user. It is not the Slack channel bot described in `docs/guides/slack-setup.md`; that is a separate feature with separate tokens. You can reuse one Slack app for both, but this document only covers the MCP side.

## Before you start

- A Slack workspace where you can create and install apps. If app approval is turned on for the workspace, an admin must approve the app before it can be installed (see section 4).
- Your Slack workspace must allow the app to use the MCP server. Slack states: "Only directory-published apps or internal apps may use MCP." An app you create in your own workspace and never distribute is an internal app, so a single-workspace install qualifies. Do **not** turn on public distribution: "unlisted apps are prohibited from using MCP."
- Kiro Crew installed and its dashboard reachable, so you can open **Settings → OAuth Apps**.
- Facts you will need to type exactly:
  - Redirect URI: `http://localhost:48106/callback`
  - MCP endpoint: `https://mcp.slack.com/mcp`
  - Slack does not support Dynamic Client Registration, so the Client ID must be created by hand and entered into Kiro Crew.
  - The app must have **PKCE** enabled (section 1). Kiro Crew then authenticates as a public client, and the Client secret field in Kiro Crew can stay empty (section 5).

## 1. Create the app

1. Open [https://api.slack.com/apps](https://api.slack.com/apps) and sign in to the workspace.
2. Click **Create New App**.
3. Choose **From scratch**. (A manifest also works; Slack's own MCP sample uses **From a manifest**. From scratch is simpler because you only need OAuth settings, no bot user, events or interactivity.)
4. **App Name**: something recognisable, for example `Kiro Crew Connections`. Names must be unique within the workspace.
5. **Pick a workspace to develop your app in**: select the workspace Kiro Crew should read.
6. Click **Create App**. You land on **Basic Information**.
7. Enable MCP for the app: in the left sidebar open the **Agents** section and switch the **Slack Model Context Protocol (MCP) Server** toggle to **On**. Slack's sample-app guide names this exact toggle; if you do not see the Agents section, refresh the page after the app is created.
8. Enable PKCE: in the left sidebar under **Features**, click **OAuth & Permissions** and turn on the **PKCE** setting. Slack: "For standard (non-directory) apps, you can find the PKCE setting in the app settings under the OAuth & Permissions sidebar section." Do this **before** adding the redirect URL in section 2, because Slack only treats a `localhost` redirect as a desktop redirect "if the app has opted into PKCE". Be aware that this is permanent: "Enabling PKCE marks your app as a public client, which is a one-way operation. It cannot be disabled without contacting Slack support." If you would rather not convert an existing app, create a new one for Kiro Crew.
9. Back on **Basic Information**, scroll to **App Credentials** and note the **Client ID**. You will paste it into Kiro Crew in section 5. You do not need the **Client Secret** for a PKCE app (section 5 explains why); if you do reveal it, treat it like a password.

Do not click anything under **Manage Distribution** / **Distribute App**. Activating public distribution turns the app into an "unlisted" distributed app, which Slack bars from MCP.

## 2. Redirect URI

1. In the left sidebar under **Features**, click **OAuth & Permissions**. Confirm the **PKCE** setting from section 1 step 8 is on.
2. In **Redirect URLs**, click **Add New Redirect URL**.
3. Enter exactly:

   ```
   http://localhost:48106/callback
   ```

4. Click **Add**, then **Save URLs**.

Kiro Crew registers this provider's callback on the host name `localhost` because that is the only host name Slack exempts from its HTTPS rule. Slack's OAuth guide says: "The `redirect_uri` must use HTTPS. Alternatively, you can configure a Redirect URL in the App Management page under OAuth & Permissions. A Redirect URL must also use HTTPS." Its PKCE guide adds the one carve-out: "Redirects to `localhost` (e.g. `http://localhost:8080/auth`) are treated as desktop redirects if the app has opted into PKCE. If the app has never enabled PKCE, they will be treated like a server redirect." That is why the app must have PKCE enabled, and why the redirect uses `localhost` rather than the `127.0.0.1` literal most other Kiro Crew providers use.

Slack also documents confidential OAuth for MCP clients ("Slack supports confidential OAuth for MCP clients. You'll need to use your app's `client_id` and `client_secret`"), but a confidential client would need an https redirect, which Kiro Crew's loopback listener cannot serve. The PKCE + `localhost` route is the documented alternative.

Kiro Crew's OAuth callback listener runs inside kiro-cli on the loopback interface over plain HTTP only; an https loopback is not possible, and the port is fixed per provider (Slack is 48106). Remote, tailnet or Docker installs do not need a second redirect URI. After you approve the app, the browser is sent to that loopback URL, which is unreachable from your laptop when the gateway runs elsewhere; copy the full landing URL from the browser's address bar, paste it into the Kiro Crew dashboard when prompted, and the gateway replays it to its own loopback listener.

## 3. Scopes and permissions

The Slack MCP server acts as the signed-in user, so all scopes go under **User Token Scopes**, not Bot Token Scopes. On **OAuth & Permissions**, scroll to **Scopes → User Token Scopes** and click **Add an OAuth Scope** for each of the following. This is a read-mostly set taken from Slack's per-tool scope table:

| Scope | MCP tool it unlocks |
|-------|---------------------|
| `search:read.public`, `search:read.private`, `search:read.mpim`, `search:read.im` | Search messages and channels |
| `search:read.files` | Search files |
| `search:read.users` | Search users |
| `channels:history`, `groups:history`, `mpim:history`, `im:history` | Read a channel or thread |
| `channels:read`, `groups:read`, `im:read`, `mpim:read` | List the user's channels and channel members |
| `users:read` | Read a user profile (add `users:read.email` only if you need email addresses) |
| `files:read` | Read files |
| `canvases:read` | Read a canvas |
| `lists:read` | Read lists |

Leave out `chat:write`, `reactions:write`, `canvases:write`, `lists:write`, `files:write` and the `*:write` channel scopes unless you want the agent to post or create things. Leave **Bot Token Scopes** empty. This is not only tidiness: with PKCE on and a `localhost` redirect, Slack treats the flow as a desktop redirect, and "Desktop redirects are not allowed to request bot scopes." Any bot scope you add here would make Slack reject the authorization; the MCP server does not use a bot token anyway.

Scopes are additive per installation and cannot be removed from an existing token; if you later add a scope, reinstall the app so the new user token carries it.

## 4. Review and publishing requirements

- **Slack Marketplace review is not required** for a single-workspace install. Slack's rule is "Only apps published in the Slack Marketplace and internal apps can use MCP at this time; unlisted apps are prohibited from using MCP." An app created in your workspace and installed only there is an internal app. Marketplace review becomes necessary only if you want to install the same app into other organisations' workspaces.
- **Workspace admin approval** may be required. Slack's harness guide lists as a prerequisite "Access to a Slack workspace with the MCP integration approved by your workspace admin", and its troubleshooting says to "Verify that your workspace admin has approved the MCP integration for your app." If your workspace uses **Only allow pre-approved apps** (Slack help: *Manage app approval for your workspace*), submit the app for approval from the OAuth & Permissions page's install prompt and wait for an admin.
- **Enterprise Grid**: Slack's MCP documentation does not state any Grid-specific requirement beyond normal org-level app approval. If your workspace belongs to a Grid org, expect the approval to happen at the org level; Slack's help article *Manage access to the Slack MCP server through your identity provider* describes additional IdP-based controls an org admin may have turned on.
- **IP allowlist**: if the app has **Allowed IP Address Ranges** configured, Kiro Crew's egress IP must be in it or MCP requests are rejected.

## 5. Enter the credentials in Kiro Crew

1. Open the Kiro Crew dashboard and go to **Settings → OAuth Apps**.
2. Find the **Slack** card.
3. Paste the **Client ID** from Basic Information → App Credentials into **Client ID**.
4. Leave **Client secret** empty. Slack's PKCE guide says the client "should call the `oauth.v2.access` API method, but should not include `client_secret` in the parameters. Instead, the client should provide the code and the `code_verifier` secret it created earlier", and that for desktop redirects "Refreshes for those tokens do not require a `client_secret` because they are intended to be used on a public client." Kiro Crew sends the PKCE parameters on every Slack flow. If you do enter a secret, Kiro Crew includes it in the token exchange as well; Slack does not document how it treats a request carrying both, so this runbook recommends the documented shape and leaving the field empty.
5. Use the card's copy button for the redirect URI to confirm it reads `http://localhost:48106/callback`, matching what you saved in section 2.
6. Save.

Container or CI alternative: set the environment variable `KIROCREW_CONNECTIONS_SLACK_CLIENT_ID` on the gateway process (`KIROCREW_CONNECTIONS_SLACK_CLIENT_SECRET` exists too but is not needed for a PKCE app). Environment variables override dashboard values.

## 6. Verify the connection

1. In the dashboard open **Capabilities → Connections**. The Slack card should have changed from "Needs configuration" to **Connect**.
2. Click **Connect**. A browser tab opens Slack's consent page at `https://slack.com/oauth/v2_user/authorize` listing the user scopes from section 3. Pick the workspace if asked and click **Allow**.
3. If the gateway runs on the same machine as the browser, the tab lands on the loopback callback and the card shows **Connected**. If the gateway is remote, copy the landing URL from the address bar and paste it into the dashboard prompt.
4. Ask the agent something read-only, for example "search Slack for the last message in #general". A `missing_scope` error means a scope from section 3 was not added; add it and reinstall.

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Slack shows `bad_redirect_uri` | The URI Kiro Crew sent does not match a saved Redirect URL, or PKCE is not enabled on the app so Slack treats `http://localhost` as a server redirect and demands https | Re-check section 2, and confirm the PKCE setting on OAuth & Permissions is on (section 1 step 8) |
| Slack rejects the authorization because of bot scopes | A Bot Token Scope was added; desktop redirects cannot request bot scopes | Remove all Bot Token Scopes (section 3) and retry |
| Slack shows `invalid_team_for_non_distributed_app` | You authorised against a different workspace than the one the app was created in | Sign in to the correct workspace and retry; do not enable distribution |
| Consent page says the app needs admin approval | Workspace has app approval turned on | Ask an admin to approve the request (section 4) |
| Connected, but tools return `missing_scope` | A user scope from section 3 is missing | Add the scope under User Token Scopes, then reconnect |
| Connected, but MCP calls are rejected | App has **Allowed IP Address Ranges** set | Add the gateway's egress IP to the allowlist |
| Connected, then asked to reconnect about a month later | Refresh token expired (30-day limit for PKCE apps, see Known limits) | Click **Connect** again |

To revoke the grant later: in Slack, click your profile picture → **Preferences** → **Connected accounts** and disconnect the app, or open [https://my.slack.com/apps/manage](https://my.slack.com/apps/manage), select the app, open the **Configuration** tab and click **Revoke** under **Your authorization**. Removing the app from the workspace (Slack help: *Remove apps and custom integrations from your workspace*) revokes every user's token at once.

## Known limits

- Slack requires https redirect URLs; the only plain-http exception is `localhost` on an app with PKCE enabled. Kiro Crew therefore registers `http://localhost:48106/callback` and the app must stay a PKCE (public) client; PKCE cannot be turned off again without Slack support.
- Tokens issued through a desktop-style (PKCE + `localhost`) redirect are rotating tokens whose refresh tokens expire after 30 days ("if PKCE is enabled, all refresh tokens issued to your app will expire in 30 days instead of lasting indefinitely"). Users must re-consent monthly.
- Bot scopes cannot be requested through this redirect type; only user scopes work.
- No Dynamic Client Registration and no SSE transport: "We do not support SSE-based connections or Dynamic Client Registration at this time."
- MCP requests are subject to the same Web API rate limits as the underlying methods (search is Tier 2, channel history Tier 3).
- The MCP server exposes only what the signed-in user can already see; it cannot read channels the user is not a member of.
- Slack's authorization-server metadata is published at `https://mcp.slack.com/.well-known/oauth-authorization-server`; this runbook could not read the JSON body, so the issuer string is not quoted here.

## Sources

- https://docs.slack.dev/ai/slack-mcp-server/ (accessed 2026-09-10)
- https://docs.slack.dev/ai/slack-mcp-server/developing/ (accessed 2026-09-10)
- https://docs.slack.dev/ai/slack-mcp-server/connect-to-harnesses (accessed 2026-09-10)
- https://docs.slack.dev/authentication/installing-with-oauth (accessed 2026-09-10)
- https://docs.slack.dev/authentication/using-pkce (accessed 2026-09-10)
- https://docs.slack.dev/changelog/2026/03/30/pkce (accessed 2026-09-10)
- https://slack.com/help/articles/48855576908307-Guide-to-Model-Context-Protocol-in-Slack (accessed 2026-09-10)
- https://slack.com/help/articles/222386767-Manage-app-approval-for-your-workspace (accessed 2026-09-10)
- https://slack.com/help/articles/218891278-Connect-to-other-services-using-your-Slack-account (accessed 2026-09-10)
- https://slack.com/help/articles/360003125231-Remove-apps-and-custom-integrations-from-your-workspace (accessed 2026-09-10)
