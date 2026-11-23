# Register a Slack OAuth app for Kiro Crew Connections

*Who this is for: the person who owns (or can create apps in) the Slack workspace that Kiro Crew should read. You need a browser and a Slack account with permission to install apps in that workspace. Expected time: 10 minutes, plus any wait for a workspace admin to approve the app.*

Slack is a pre-registered Connections entry (see the [index](README.md)): its
card reads "needs configuration" until the Client ID and Client Secret from
section 5 are saved, and offers **Connect** after, once the Crew-owned sign-in
preview is on and your chats run on Kiro Agent.

This runbook covers the **Slack MCP server** (`https://mcp.slack.com/mcp`), which lets Kiro Crew search and read Slack on behalf of the signed-in user. It is not the Slack channel bot described in `docs/guides/slack-setup.md`; that is a separate feature with separate tokens. One Slack app can serve both: section 1 creates it from the manifest Kiro Crew generates for the bot, with the MCP settings on.

## Before you start

- A Slack workspace where you can create and install apps. If app approval is turned on for the workspace, an admin must approve the app before it can be installed (see section 4).
- Your Slack workspace must allow the app to use the MCP server. Slack states: "Only directory-published apps or internal apps may use MCP." An app you create in your own workspace and never distribute is an internal app, so a single-workspace install qualifies. Do **not** turn on public distribution: "unlisted apps are prohibited from using MCP."
- Kiro Crew installed and its dashboard reachable, so you can open **Settings → Connections**.
- Facts you will need to type exactly:
  - Redirect URI: `http://localhost:48106/callback`
  - MCP endpoint: `https://mcp.slack.com/mcp`
  - Slack does not support Dynamic Client Registration, so the Client ID must be created by hand and entered into Kiro Crew.
  - The app must have **PKCE** enabled (the manifest in section 1 turns it on), and Kiro Crew needs both the app's **Client ID** and its **Client Secret** (section 5).
  - The Slack card connects only when Kiro Crew does the sign-in itself: the Crew-owned sign-in preview is on and your chats run on Kiro Agent. Until then the card shows a link to that setting instead of **Connect**. Kiro Crew keeps the app's Client Secret in its vault and never writes it into an MCP entry or an agent file.
  - Already have a `slack` MCP server set up with its own Client ID? Kiro Crew does not rewrite that entry, so the Kiro CLI keeps signing in with it. A `slack` server you added from the dashboard also keeps its own **Sign in** on the MCP Servers page, and a sign-in made there keeps working, for as long as no Slack app is saved under **Settings → Connections**. Once you save one there (section 5), the Slack card owns the entry: connect on the card, and a sign-in made from the MCP Servers page is not used any more. Kiro Crew does not read the secret from your MCP entry.

## 1. Create the app from the Slack card

1. In the Kiro Crew dashboard open **Settings → Connections** and find the **Slack** card.
2. Point at the warning triangle beside the card's action (**Before you connect**) and click **Create the Slack app**. Slack's create page opens in a new tab with the app filled in.
3. Pick the workspace Kiro Crew should read and click **Create**. You land on **Basic Information**.

The buttons show while the card still needs its app, and only to the dashboard owner. If the browser that opens Slack cannot use the link, click **Copy manifest** instead, open [https://api.slack.com/apps](https://api.slack.com/apps), click **Create New App → From a manifest**, pick the workspace and paste it.

The app comes from the same manifest Kiro Crew generates for its Slack channel bot (`docs/guides/slack-setup.md`), with **Slack tools for your agents** on. Section 3 lists what it sets. Two of those settings matter before you click **Create**:

- **PKCE is on, and that is permanent.** Slack: "Enabling PKCE marks your app as a public client, which is a one-way operation. It cannot be disabled without contacting Slack support." Slack needs it to accept the `localhost` redirect. Slack accepted PKCE and that redirect in one manifest when this was tested on 2026-11-19.
- **The app is also a Kiro Crew bot app.** It carries the bot user, the slash command and the bot scopes, so you can use the same app for the Slack channel bot. If you do not, the bot settings are unused: the Slack card signs in with user scopes only.

Already run the Kiro Crew bot on a Slack app? Do not make a second one: turn on **Slack tools for your agents** (Settings → Messaging Channels → Slack → Slack Code) and save the manifest Kiro Crew generates on your bot app, then continue at section 2.

Do not click anything under **Manage Distribution** / **Distribute App**. Activating public distribution turns the app into an "unlisted" distributed app, which Slack bars from MCP.

## 2. Copy the Client ID and Client Secret

On **Basic Information**, scroll to **App Credentials**. Note the **Client ID**, then click **Show** next to **Client Secret** and note it too. Kiro Crew needs both (section 5). Treat the secret like a password.

If your workspace needs an admin to approve apps, request that approval now (section 4).

## 3. What the manifest sets

You do not type any of this; it is here so you can check the app, or set it by hand on an app you already have.

| Where in Slack's app settings | Value |
|---|---|
| **Agents → Slack Model Context Protocol (MCP) Server** | On (`settings.is_mcp_enabled: true`) |
| **OAuth & Permissions → PKCE** | On (`oauth_config.pkce_enabled: true`) |
| **OAuth & Permissions → Redirect URLs** | `http://localhost:48106/callback` |
| **OAuth & Permissions → User Token Scopes** | the 18 scopes below |

Slack exempts only `localhost` from its HTTPS rule, and only "if the app has opted into PKCE", which is why the redirect is plain `http` on `localhost` rather than the `127.0.0.1` literal most other Kiro Crew providers use. Slack's MCP server also asks for the secret ("Slack supports confidential OAuth for MCP clients"), so Kiro Crew sends the Client Secret on every token request and keeps PKCE on only for the redirect.

The Slack MCP server acts as the signed-in user, so its scopes are **User Token Scopes**, a read-mostly set taken from Slack's per-tool scope table:

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

No write scope is requested (`chat:write`, `reactions:write`, `canvases:write`, `lists:write`, `files:write`, the `*:write` channel scopes); add one only if you want the agent to post or create things. Keep the app's **Bot Token Scopes** if the Kiro Crew bot uses the app: removing them stops the bot. Slack's rule that "Desktop redirects are not allowed to request bot scopes" applies to what the sign-in asks for, and the Slack card asks for user scopes only (at `https://slack.com/oauth/v2_user/authorize`).

Kiro Crew's OAuth callback listener runs on the gateway's loopback interface over plain HTTP, on port 48106. Remote, tailnet or Docker installs need no second redirect URL: after you approve, copy the full landing address from the browser bar and paste it into the dashboard when prompted, and the gateway replays it to its own listener.

Scopes are additive per installation and cannot be removed from an existing token; if you later add a scope, reinstall the app so the new user token carries it.

## 4. Review and publishing requirements

- **Slack Marketplace review is not required** for a single-workspace install. Slack's rule is "Only apps published in the Slack Marketplace and internal apps can use MCP at this time; unlisted apps are prohibited from using MCP." An app created in your workspace and installed only there is an internal app. Marketplace review becomes necessary only if you want to install the same app into other organisations' workspaces.
- **Workspace admin approval** may be required. Slack's harness guide lists as a prerequisite "Access to a Slack workspace with the MCP integration approved by your workspace admin", and its troubleshooting says to "Verify that your workspace admin has approved the MCP integration for your app." If your workspace uses **Only allow pre-approved apps** (Slack help: *Manage app approval for your workspace*), submit the app for approval from the OAuth & Permissions page's install prompt and wait for an admin.
- **Enterprise Grid**: Slack's MCP documentation does not state any Grid-specific requirement beyond normal org-level app approval. If your workspace belongs to a Grid org, expect the approval to happen at the org level; Slack's help article *Manage access to the Slack MCP server through your identity provider* describes additional IdP-based controls an org admin may have turned on.
- **IP allowlist**: if the app has **Allowed IP Address Ranges** configured, Kiro Crew's egress IP must be in it or MCP requests are rejected.

## 5. Enter the credentials in Kiro Crew

1. Open the Kiro Crew dashboard and go to **Settings → Connections**.
2. Find the **Slack** card.
3. Paste the **Client ID** from Basic Information → App Credentials into **Client ID**.
4. Paste the **Client Secret** from the same App Credentials section into **Client secret**. It is required: Kiro Crew refuses to start a Slack sign-in without it, because Slack's MCP server asks for `client_id` and `client_secret` ("Slack supports confidential OAuth for MCP clients"). Kiro Crew stores it encrypted in the secrets vault and never shows it again. It still sends the PKCE parameters on every Slack flow.
5. Use the card's copy button for the redirect URI to confirm it reads `http://localhost:48106/callback`, matching the redirect in section 3.
6. Save.

Container or CI alternative: set the environment variables `KIROCREW_CONNECTIONS_SLACK_CLIENT_ID` and `KIROCREW_CONNECTIONS_SLACK_CLIENT_SECRET` on the gateway process. Environment variables override dashboard values.

## 6. Verify the connection

1. In the dashboard open **Settings → Connections**. The Slack card should have changed from "Needs configuration" to **Connect**.
2. Click **Connect**. A browser tab opens Slack's consent page at `https://slack.com/oauth/v2_user/authorize` listing the user scopes from section 3. Pick the workspace if asked and click **Allow**.
3. If the gateway runs on the same machine as the browser, the tab lands on the loopback callback and the card shows **Connected**. If the gateway is remote, copy the landing URL from the address bar and paste it into the dashboard prompt.
4. Ask the agent something read-only, for example "search Slack for the last message in #general". A `missing_scope` error means a scope from section 3 was not added; add it and reinstall.

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Slack shows `bad_redirect_uri` | The URI Kiro Crew sent does not match a saved Redirect URL, or PKCE is not enabled on the app so Slack treats `http://localhost` as a server redirect and demands https | Check the redirect and the PKCE setting on OAuth & Permissions against section 3 |
| Slack rejects the authorization because of bot scopes | The sign-in asked for a bot scope; desktop redirects cannot request bot scopes. The Slack card asks for user scopes only, so the sign-in did not come from the card (for example a hand-made MCP entry that lists bot scopes) | Connect from the Slack card under **Settings → Connections**. Don't remove the app's Bot Token Scopes if the Kiro Crew bot uses it: the bot needs them, and the card's sign-in does not ask for them |
| Slack shows `invalid_team_for_non_distributed_app` | You authorised against a different workspace than the one the app was created in | Sign in to the correct workspace and retry; do not enable distribution |
| Consent page says the app needs admin approval | Workspace has app approval turned on | Ask an admin to approve the request (section 4) |
| Connected, but tools return `missing_scope` | A user scope from section 3 is missing | Add the scope under User Token Scopes, then reconnect |
| Connected, but MCP calls are rejected | App has **Allowed IP Address Ranges** set | Add the gateway's egress IP to the allowlist |
| Connected, then asked to reconnect about a month later | Refresh token expired (30-day limit for PKCE apps, see Known limits) | Click **Connect** again |

To revoke the grant later: in Slack, click your profile picture → **Preferences** → **Connected accounts** and disconnect the app, or open [https://my.slack.com/apps/manage](https://my.slack.com/apps/manage), select the app, open the **Configuration** tab and click **Revoke** under **Your authorization**. Removing the app from the workspace (Slack help: *Remove apps and custom integrations from your workspace*) revokes every user's token at once.

## Known limits

- Slack requires https redirect URLs; the only plain-http exception is `localhost` on an app with PKCE enabled. Kiro Crew therefore registers `http://localhost:48106/callback` and the app must stay a PKCE (public) client; PKCE cannot be turned off again without Slack support.
- Tokens issued through a desktop-style (PKCE + `localhost`) redirect are rotating tokens whose refresh tokens expire after 30 days ("if PKCE is enabled, all refresh tokens issued to your app will expire in 30 days instead of lasting indefinitely"). Users must re-consent monthly.
- Bot scopes cannot be requested through this redirect type, so the sign-in asks for user scopes only. The app's Bot Token Scopes are not affected: an app that also runs the Kiro Crew bot keeps them, and its bot token comes from **Install to Workspace**.
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
