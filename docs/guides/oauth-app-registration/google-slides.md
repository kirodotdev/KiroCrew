# Register a Google OAuth client for Kiro Crew Connections (Google Slides)

*Who this is for: the person who owns (or can create) a Google Cloud project and wants Kiro Crew agents to read presentations through Google's remote Google Slides MCP server. Expected time: 30–45 minutes of clicking, plus a wait of "a couple of days" for Google to accept you into the Developer Preview Program if you are not already a member. If you already made a Google client for another Google card, this takes five minutes: see [One Google client for every Google card](README.md#one-google-client-for-every-google-card).*

Google Slides is a pre-registered Connections entry. Its card shows **Needs configuration** until a client is saved in **Settings → OAuth Apps**, and the entry stays launch-gated until a person walks the manual launch-gate check with a real client.

Google's Google Slides MCP server is a **Developer Preview** feature. Google accepts a plain-http loopback redirect URI, so Kiro Crew's callback works as-is.

## Before you start

You need:

- A Google Account that can sign in to the [Google Cloud console](https://console.cloud.google.com/). To pick **Internal** audience (recommended, no verification) the project must belong to a Google Cloud Organization, which means a Google Workspace or Cloud Identity account. A consumer `@gmail.com` account can only pick **External**.
- Membership in the [Google Workspace Developer Preview Program](https://developers.google.com/workspace/preview). Google states "Access to Google Workspace MCP servers is part of the Public Developer Preview Program." The form asks for your Google Workspace account and your Google Cloud project number.
- A running Kiro Crew dashboard where you can open **Settings → OAuth Apps**.
- Optional: the `gcloud` CLI. Every step below has a console path too.

Facts about Kiro Crew's side you will need to type exactly:

- Redirect URI for Google Slides: `http://127.0.0.1:48111/callback`
- Provider slug for environment variables: `GOOGLE_SLIDES`
- MCP endpoint Kiro Crew connects to: `https://slidesmcp.googleapis.com/mcp/v1` (verified against Google's "Configure the Google Slides MCP server" page and an unauthenticated probe of its protected-resource metadata, which names `https://accounts.google.com/` as the authorization server).

## 1. Create the app

Google does not have an "app" object; you create a Google Cloud project, enable two APIs, configure the Google Auth Platform (consent screen), and then create an OAuth client inside it. If you already did this for another Google card, use the same project and client: do 1.3, add the scopes from section 3 on **Data Access**, and add this card's redirect URI to the client (1.5 step 5).

### 1.1 Join the Developer Preview Program

1. Open [https://developers.google.com/workspace/preview](https://developers.google.com/workspace/preview).
2. Read the Program Terms, click **Apply to join the Developer Preview Program**, and submit the form with your Google Workspace account and Google Cloud project number (Console → project picker → **Project number**).
3. Wait for Google's confirmation email. Until the project is registered, the MCP endpoint may refuse your calls.

### 1.2 Create or pick a Google Cloud project

1. In the [Google Cloud console](https://console.cloud.google.com/), click the project picker and choose **New project**, or select an existing project you control.
2. Note the **Project ID** and **Project number**.

### 1.3 Enable the Google Slides API and the Google Slides MCP API

Console path: open [https://console.cloud.google.com/flows/enableapi?apiid=slides.googleapis.com](https://console.cloud.google.com/flows/enableapi?apiid=slides.googleapis.com), pick your project, click **Next**, then **Enable**. Repeat with [https://console.cloud.google.com/flows/enableapi?apiid=slidesmcp.googleapis.com](https://console.cloud.google.com/flows/enableapi?apiid=slidesmcp.googleapis.com).

CLI equivalent:

```bash
gcloud services enable slides.googleapis.com slidesmcp.googleapis.com --project=PROJECT_ID
```

### 1.4 Configure the Google Auth Platform (OAuth consent screen)

1. Go to **Google Auth Platform → Branding**: [https://console.cloud.google.com/auth/branding](https://console.cloud.google.com/auth/branding). If you see **Google Auth Platform not configured yet**, click **Get Started**.
2. **App Information**: App name `Kiro Crew` (Google's own example is `Slides MCP Server`). **User support email**: pick your address. Click **Next**.
3. **Audience**: select **Internal** if offered; otherwise **External**. Click **Next**.
4. **Contact Information**: enter an email address. Click **Next**.
5. **Finish**: tick **I agree to the Google API Services: User Data Policy**, click **Continue**, then **Create**.
6. If you chose **External**: open **Audience** ([https://console.cloud.google.com/auth/audience](https://console.cloud.google.com/auth/audience)), under **Test users** click **Add users**, enter the Google Account(s) that will click **Connect**, click **Save**. Leave **Publishing status** at **Testing** (see section 4).
7. Open **Data Access** ([https://console.cloud.google.com/auth/scopes](https://console.cloud.google.com/auth/scopes)) → **Add or Remove Scopes**; under **Manually add scopes** paste the scopes from section 3, click **Add to Table**, **Update**, then **Save**.

### 1.5 Create the OAuth client

1. Go to **Google Auth Platform → Clients → Create Client**: [https://console.cloud.google.com/auth/clients/create](https://console.cloud.google.com/auth/clients/create).
2. **Application type**: **Web application** (the type every Google MCP-server page uses; see the [Google Drive runbook](google-drive.md#2-redirect-uri) for why not "Desktop app").
3. **Name**: `Kiro Crew`.
4. Leave **Authorized JavaScript origins** empty.
5. Under **Authorized redirect URIs** click **+ Add URI** and paste `http://127.0.0.1:48111/callback` exactly. If you will use other Google cards too, add every URI in [One Google client for every Google card](README.md#one-google-client-for-every-google-card) to this same client now.
6. Tick **Use this client for an AI-powered agent**. From 2026-11-11 Google's Workspace MCP servers refuse a client without it, and from 2026-11-12 an existing client can no longer be switched to it, so a client made without it must be replaced by a new one.
7. Click **Create**. Copy the **Client ID** (ends in `.apps.googleusercontent.com`) and the **Client secret** now. Google shows the full secret only once; afterwards only the last four characters are displayed. If you lose it, click **Add Secret** on the client page to rotate.

## 2. Redirect URI

Register exactly `http://127.0.0.1:48111/callback`. Google matches literally: "the `http` or `https` scheme, case, and trailing slash ('/') must all match". No trailing slash, no `localhost` substitution. Plain http on loopback is allowed by Google's redirect URI validation rules ("Localhost URIs (including localhost IP address URIs) are exempt from this rule.").

Remote, tailnet or Docker installs do not need a second redirect URI. After consent, the browser is sent to `http://127.0.0.1:48111/callback?...` on the laptop, where nothing is listening, so the page fails to load. Copy that full URL from the address bar and paste it into the field the Kiro Crew dashboard shows during Connect; the gateway replays it against its own loopback listener and completes the exchange.

## 3. Scopes and permissions

Google's Google Slides MCP page tells you to declare four scopes on **Data Access**: `drive.readonly`, `drive.file`, `presentations.readonly` and `presentations`. Kiro Crew requests only the two read-only ones at Connect:

| Scope | Requested by Kiro Crew | What it allows | Tools it serves |
|---|---|---|---|
| `https://www.googleapis.com/auth/presentations.readonly` | yes | See all your Google Slides presentations | `read_presentation`, `read_slide_page`, `read_slide_page_thumbnail` |
| `https://www.googleapis.com/auth/drive.readonly` | yes | **Restricted**: view and download all Drive files | Google lists it for this server |
| `https://www.googleapis.com/auth/presentations` | no | See, edit, create, and delete all your Google Slides presentations | `update_presentation` |
| `https://www.googleapis.com/auth/drive.file` | no | Non-sensitive: per-file access to files the app opened or created | creating files |

Google's page does not map tools to scopes; the mapping above follows the API scope descriptions. Declaring a scope on Data Access is a ceiling; what the user grants is what Kiro Crew requests at Connect, and the consent screen lists it. With only the read-only scopes granted, the write tools fail with a permission error.

`drive.readonly` is **restricted**, which drives the review rules in section 4.

The client secret: a Web application client that omits or mis-types the secret receives `invalid_client`. Kiro Crew sends the secret at token exchange, so enter it.

## 4. Review and publishing requirements

- **Internal audience** (Organization project): no Google verification, no unverified-app warning, no 100-user cap. If Connect fails with `admin_policy_enforced`, a Workspace admin must trust the client ID under **Security → API controls**.
- **External audience, Testing**: no verification. Limits: 100 test users; "unverified app" warning on every consent; "Authorizations by a test user will expire seven days from the time of consent." Plan to click **Connect** again weekly.
- **External audience, In production**: `drive.readonly` is restricted, so publishing requires brand verification, restricted-scope verification and an annual security assessment. Google's Developer Preview terms also forbid shipping preview features to users outside your domain or company before GA. For a personal or team install, stay in Testing or use Internal.
- Google's generic MCP guide requires the IAM role **MCP Tool User** (`roles/mcp.toolUser`) on the project. If Connect succeeds but tool calls return a permission error, grant it in **IAM & Admin → IAM**.

## 5. Enter the credentials in Kiro Crew

1. Open the Kiro Crew dashboard → **Settings → OAuth Apps** → the **Google Slides** card.
2. Paste the **Client ID** into "Client ID" and the **Client secret** into "Client secret". If you reuse a client from another Google card, paste the same two values. The card's copy button gives the redirect URI to compare with section 2.
3. Click **Save**.

Container or CI alternative (environment variables override dashboard values):

```bash
KIROCREW_CONNECTIONS_GOOGLE_SLIDES_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
KIROCREW_CONNECTIONS_GOOGLE_SLIDES_CLIENT_SECRET=GOCSPX-...
```

After saving, the Google Slides card on **Customize → Connections** switches from "Needs configuration" to "Connect".

## 6. Verify the connection

1. On **Customize → Connections**, click **Connect** on the Google Slides card. Sign in. On External/Testing you first see "Google hasn't verified this app"; click **Continue**. Approve the read-only scopes the consent page lists.
2. The browser lands on `http://127.0.0.1:48111/callback?...`. Locally the page closes itself; on a remote install paste the URL back into the dashboard.
3. The card shows **Connected**.
4. In a chat session, ask: **"List the slide titles in the presentation Q3 Review."** (substitute a real presentation name or paste its link).
5. Expected tool list from `https://slidesmcp.googleapis.com/mcp/v1`: `read_presentation`, `read_slide_page`, `read_slide_page_thumbnail`, `update_presentation`.

If something fails:

- `redirect_uri_mismatch`: the client's URI is not `http://127.0.0.1:48111/callback` byte for byte; client edits "may take 5 minutes to a few hours" to apply.
- `access_denied` / "app is being tested": the account is not in **Test users**.
- `403` / "API has not been used in project": `slidesmcp.googleapis.com` or `slides.googleapis.com` is not enabled, or the project is not yet registered in the Developer Preview.
- Sign-in works but every tool call is refused: the client was made without **Use this client for an AI-powered agent**. Make a new client with it ticked.

To revoke on the Google side, the user opens [https://myaccount.google.com/permissions](https://myaccount.google.com/permissions) and removes the app. Revocation "removes all OAuth 2.0 scopes previously granted to a project", so every Google card connected through the same project is revoked together.

## Known limits

- Developer Preview: features may change, and may not be shipped to users outside your domain or company before GA.
- Google does not support Dynamic Client Registration for its MCP servers, so the client ID and secret must always be entered by hand.
- Testing-mode grants expire after 7 days; Internal audience avoids this.

## Sources

- https://developers.google.com/workspace/slides/api/guides/configure-mcp-server (accessed 2026-11-02)
- https://developers.google.com/workspace/chat/release-notes (2026-10-12 entry on the AI-powered-agent client flag; accessed 2026-11-02)
- https://developers.google.com/workspace/guides/configure-mcp-servers (accessed 2026-11-02)
- https://developers.google.com/workspace/preview (accessed 2026-11-02)
- https://developers.google.com/identity/protocols/oauth2/web-server (accessed 2026-11-02)
- https://support.google.com/cloud/answer/15549257 (accessed 2026-11-02)
- https://myaccount.google.com/permissions (accessed 2026-11-02)
- `https://slidesmcp.googleapis.com/.well-known/oauth-protected-resource/mcp/v1` (unauthenticated probe, 2026-11-02)
