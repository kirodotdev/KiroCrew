# Register a Google OAuth client for Kiro Crew Connections (Gmail)

*Who this is for: the person who owns (or can create) a Google Cloud project and wants Kiro Crew agents to search and read mail, and optionally create drafts, in a Gmail mailbox through Google's remote Gmail MCP server. Expected time: 30–45 minutes of clicking, plus a wait of "a couple of days" for Google to accept you into the Developer Preview Program if you are not already a member.*

Google's Gmail MCP server is a **Developer Preview** feature. Google accepts a plain-http loopback redirect URI, so Kiro Crew's callback works as-is. Every Gmail scope the server uses is in Google's **restricted** class, which matters only if you publish an External app; Internal or Testing installs are unaffected.

## Before you start

You need:

- A Google Account that can sign in to the [Google Cloud console](https://console.cloud.google.com/). To pick **Internal** audience (recommended, no verification) the project must belong to a Google Cloud Organization, which means a Google Workspace or Cloud Identity account. A consumer `@gmail.com` account can only pick **External**.
- Membership in the [Google Workspace Developer Preview Program](https://developers.google.com/workspace/preview). Google states "Access to Google Workspace MCP servers is part of the Public Developer Preview Program." The application form asks for your Google Workspace account and your Google Cloud project number; Google says "The whole process should be done within a couple of days." Google's page does not state whether a consumer `@gmail.com` account is accepted.
- A running Kiro Crew dashboard where you can open **Settings → OAuth Apps**.
- Optional: the `gcloud` CLI. Every step below has a console path too.

Facts about Kiro Crew's side you will need to type exactly:

- Redirect URI for Gmail: `http://127.0.0.1:48104/callback`
- Provider slug for environment variables: `GMAIL`
- MCP endpoint Kiro Crew connects to: `https://gmailmcp.googleapis.com/mcp/v1` (verified against Google's "Configure the Gmail MCP server" page and Google Cloud's MCP supported-products table).

## 1. Create the app

Google does not have an "app" object; you create a Google Cloud project, enable two APIs, configure the Google Auth Platform (consent screen), and then create an OAuth client inside it. If you already did this for Google Drive or Google Calendar in the same project, skip to 1.3 and add only the Gmail-specific items.

### 1.1 Join the Developer Preview Program

1. Open [https://developers.google.com/workspace/preview](https://developers.google.com/workspace/preview).
2. Read the Program Terms, click **Apply to join the Developer Preview Program**, and submit the form with your Google Workspace account and Google Cloud project number (Console → project picker → **Project number**).
3. Wait for Google's confirmation email. Until the project is registered, the MCP endpoint may refuse your calls.

### 1.2 Create or pick a Google Cloud project

1. In the [Google Cloud console](https://console.cloud.google.com/), click the project picker and choose **New project**, or select an existing project you control.
2. Note the **Project ID** and **Project number**.

### 1.3 Enable the Gmail API and the Gmail MCP API

Google requires two services: the ordinary **Gmail API** and the **Gmail MCP API**.

Console path: open [https://console.cloud.google.com/flows/enableapi?apiid=gmail.googleapis.com](https://console.cloud.google.com/flows/enableapi?apiid=gmail.googleapis.com), pick your project, click **Next**, then **Enable**. Repeat with [https://console.cloud.google.com/flows/enableapi?apiid=gmailmcp.googleapis.com](https://console.cloud.google.com/flows/enableapi?apiid=gmailmcp.googleapis.com).

CLI equivalent:

```bash
gcloud services enable gmail.googleapis.com gmailmcp.googleapis.com --project=PROJECT_ID
```

### 1.4 Configure the Google Auth Platform (OAuth consent screen)

1. Go to **Google Auth Platform → Branding**: [https://console.cloud.google.com/auth/branding](https://console.cloud.google.com/auth/branding). If you see **Google Auth Platform not configured yet**, click **Get Started**.
2. **App Information**: App name `Kiro Crew Gmail MCP` (Google's own example is `Gmail MCP Server`). **User support email**: pick your address. Click **Next**.
3. **Audience**: select **Internal** if offered; otherwise **External**. Click **Next**.
4. **Contact Information**: enter an email address. Click **Next**.
5. **Finish**: tick **I agree to the Google API Services: User Data Policy**, click **Continue**, then **Create**.
6. If you chose **External**: open **Audience** ([https://console.cloud.google.com/auth/audience](https://console.cloud.google.com/auth/audience)), under **Test users** click **Add users**, enter the Google Account(s) whose mailbox will be connected, click **Save**. Leave **Publishing status** at **Testing** (see section 4).
7. Open **Data Access** ([https://console.cloud.google.com/auth/scopes](https://console.cloud.google.com/auth/scopes)) → **Add or Remove Scopes**; under **Manually add scopes** paste the scopes from section 3, click **Add to Table**, **Update**, then **Save**.

### 1.5 Create the OAuth client

1. Go to **Google Auth Platform → Clients → Create Client**: [https://console.cloud.google.com/auth/clients/create](https://console.cloud.google.com/auth/clients/create).
2. **Application type**: **Web application** (see section 2 for why not "Desktop app").
3. **Name**: `Kiro Crew`.
4. Leave **Authorized JavaScript origins** empty.
5. Under **Authorized redirect URIs** click **+ Add URI** and paste `http://127.0.0.1:48104/callback` exactly. To share one client across Google Drive and Google Calendar as well, also add `http://127.0.0.1:48103/callback` and `http://127.0.0.1:48105/callback`; one client may carry all three URIs and the same Client ID and secret then go into all three Kiro Crew cards.
6. Click **Create**. Copy the **Client ID** (ends in `.apps.googleusercontent.com`) and the **Client secret** now. Google shows the full secret only once; afterwards only the last four characters are displayed. If you lose it, click **Add Secret** on the client page to rotate.

## 2. Redirect URI

Register exactly `http://127.0.0.1:48104/callback`. Google matches literally: "the `http` or `https` scheme, case, and trailing slash ('/') must all match". No trailing slash, no `localhost` substitution.

Plain http on loopback is allowed by Google's redirect URI validation rules: "Redirect URIs must use the HTTPS scheme, not plain HTTP. Localhost URIs (including localhost IP address URIs) are exempt from this rule." and "Hosts cannot be raw IP addresses. Localhost IP addresses are exempted from this rule." Google's own MCP client guide registers `http://localhost:8787/callback` on a Web application client for Cursor Desktop, the same shape as Kiro Crew's URI.

Why **Web application** rather than **Desktop app**: a Desktop-app client has no redirect URI list ("The console does not require any additional information to create OAuth 2.0 credentials for desktop applications"); Google's installed-app flow expects the app to "start an HTTP listener on a random available port" and documents the loopback form as `http://127.0.0.1:port` with no path. Google's documentation does not state whether a `/callback` path is accepted on a Desktop-app loopback redirect. Kiro Crew uses a fixed port and path, so use the Web application type, where the exact URI is registered and validated. It is also the type every Google MCP-server page uses for third-party clients.

Kiro Crew's OAuth callback listener runs inside kiro-cli on the loopback interface over plain HTTP only; an https loopback is not possible, and Google does not require one.

Remote, tailnet or Docker installs do not need a second redirect URI. After consent, the browser is sent to `http://127.0.0.1:48104/callback?...` on the laptop, where nothing is listening, so the page fails to load. Copy that full URL from the address bar and paste it into the field the Kiro Crew dashboard shows during Connect; the gateway replays it against its own loopback listener and completes the exchange.

## 3. Scopes and permissions

Google's Gmail MCP page tells you to declare exactly two scopes on **Data Access**:

| Scope | Google's class | What it unlocks on the Gmail MCP server |
|---|---|---|
| `https://www.googleapis.com/auth/gmail.readonly` | **Restricted** ("View your email messages and settings") | `search_threads`, `get_thread`, `get_message`, `list_labels`, `list_drafts` |
| `https://www.googleapis.com/auth/gmail.compose` | **Restricted** ("Manage drafts and send emails") | `create_draft`; Google's tool list also includes `label_message`, `label_thread`, `unlabel_message`, `unlabel_thread`, `create_label`, whose scope Google's page does not specify |

Google's page does not map tools to scopes; the mapping above follows the Gmail API scope descriptions. Declaring a scope on Data Access is a ceiling, not a request; what the user actually grants is what Kiro Crew requests at Connect time, and the consent screen lists it.

Recommendation: declare `gmail.readonly` only if agents should only read. Add `gmail.compose` only if you want `create_draft` (drafts are saved to the mailbox; the user sends from Gmail). Google's page says `gmail.compose` covers "Manage drafts and send emails", so understand that it is broader than drafting; there is no Gmail MCP tool that sends, but the scope permits sending through the Gmail API.

Both scopes are on Google's **Restricted Scopes** list. Google's rule: "If you store restricted scope data on servers (or transmit), then you must go through a security assessment." This applies only when an External app is published (section 4). There is no non-sensitive Gmail scope that can read mail; the non-sensitive Gmail scopes are add-on-only or `gmail.labels`.

The client secret: Google's token-exchange reference marks `client_secret` as **Optional**, and "Manage OAuth Clients" calls web server apps "Private Clients" that "can securely store the client secret". In practice a Web application client that omits or mis-types the secret receives `invalid_client`. Kiro Crew sends the secret at token exchange, so enter it.

## 4. Review and publishing requirements

- **Internal audience** (Organization project): no Google verification, no unverified-app warning, no 100-user cap. Google warns that "high-risk Gmail and Drive scopes might require additional configuration by your organization's administrators"; if Connect fails with `admin_policy_enforced`, a Workspace admin must trust the client ID under **Security → API controls** ("Let Internal apps access restricted Google Workspace APIs").
- **External audience, Testing**: no verification. Limits: 100 test users; "unverified app" warning on every consent; "Authorizations by a test user will expire seven days from the time of consent. If your OAuth client requests an `offline` access type and receives a refresh token, that token will also expire." Plan to click **Connect** again weekly.
- **External audience, In production**: both Gmail scopes are restricted, so publishing requires brand verification (verified homepage domain, privacy policy, demo video), restricted-scope verification, and an annual security assessment by a Google-empanelled assessor ("Security Assessment", often called CASA). Google's Developer Preview terms also forbid shipping preview features to users outside your domain or company before GA. For a personal or team Kiro Crew install, stay in Testing or use Internal.
- Google's generic MCP guide requires the IAM role **MCP Tool User** (`roles/mcp.toolUser`) on the project. The Gmail MCP page does not mention it. If Connect succeeds but tool calls return a permission error, grant it in **IAM & Admin → IAM**.
- Unused OAuth clients are deleted after six months of inactivity; Google emails 30 days before.

## 5. Enter the credentials in Kiro Crew

1. Open the Kiro Crew dashboard → **Settings → OAuth Apps** → the **Gmail** card.
2. Paste the **Client ID** into "Client ID" and the **Client secret** into "Client secret". The card's copy button gives the redirect URI to compare with section 2.
3. Click **Save**.

Container or CI alternative (environment variables override dashboard values):

```bash
KIROCREW_CONNECTIONS_GMAIL_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
KIROCREW_CONNECTIONS_GMAIL_CLIENT_SECRET=GOCSPX-...
```

After saving, the Gmail card on **Capabilities → Connections** switches from "Needs configuration" to "Connect".

## 6. Verify the connection

1. On **Capabilities → Connections**, click **Connect** on the Gmail card. Sign in with the mailbox owner's account. On External/Testing you first see "Google hasn't verified this app"; click **Continue**. The consent page should list "View your email messages and settings" (`gmail.readonly`) and, if declared and requested, "Manage drafts and send emails" (`gmail.compose`).
2. Approve. The browser lands on `http://127.0.0.1:48104/callback?...`. Locally the page closes itself; on a remote install paste the URL back into the dashboard.
3. The card shows **Connected**.
4. In a chat session, ask: **"What did Ariel say in her last email about our marketing plan?"** (use a real sender). Google's test description: the client "filters for emails from Ariel using `gmail.search_threads`, retrieves the latest thread's content with `gmail.get_thread`, and then summarizes it for you."
5. If you declared `gmail.compose`, ask: **"Draft an email to ariel@example.com saying that I approve the marketing plan."** Google: the client "uses `gmail.create_draft` to create an email in your drafts folder". Open Gmail → **Drafts** to confirm the draft exists and nothing was sent.
6. Expected tool list from `https://gmailmcp.googleapis.com/mcp/v1`: `create_draft`, `get_message`, `get_thread`, `label_message`, `label_thread`, `list_drafts`, `list_labels`, `search_threads`, `unlabel_message`, `unlabel_thread` (Google's reference sidebar also lists `create_label`).

If something fails:

- `redirect_uri_mismatch`: the client's URI is not `http://127.0.0.1:48104/callback` byte for byte; client edits "may take 5 minutes to a few hours" to apply.
- `access_denied` / "app is being tested": the account is not in **Test users**.
- `403` / "API has not been used in project": `gmailmcp.googleapis.com` or `gmail.googleapis.com` is not enabled, or the project is not yet registered in the Developer Preview.
- `admin_policy_enforced`: the Workspace admin blocks restricted Gmail scopes for third-party or internal apps.
- Consent works but tools fail: ask the admin to check **OAuth log events** in the security investigation tool (Google's troubleshooting tip), and consider `roles/mcp.toolUser`.

To revoke on the Google side, the user opens [https://myaccount.google.com/permissions](https://myaccount.google.com/permissions) and removes the app. Revocation "removes all OAuth 2.0 scopes previously granted to a project", so if Gmail shares a project with Drive and Calendar, all three connections are revoked together.

## Known limits

- Developer Preview: features stay 3–6 months in the program, may change, and may not be shipped to users outside your domain or company before GA.
- Google does not support Dynamic Client Registration or OAuth Client ID Metadata Documents for its MCP servers, so the client ID and secret must always be entered by hand.
- Google's documentation does not state the contents of the MCP server's protected-resource metadata, so the exact `authorization_servers` string (with or without trailing slash) is not documented. Google's OpenID Connect issuer is documented as `https://accounts.google.com` (no trailing slash).
- Testing-mode refresh tokens expire after 7 days; Internal audience avoids this.
- Both usable Gmail scopes are restricted; there is no read-only path that avoids that class.
- Google recommends screening prompts and responses for prompt injection ("you must screen prompts and responses for malicious content"); mail is untrusted input, so review agent actions.

## Sources

- https://developers.google.com/workspace/gmail/api/guides/configure-mcp-server (accessed 2026-09-10)
- https://developers.google.com/workspace/guides/configure-mcp-servers (accessed 2026-09-10)
- https://docs.cloud.google.com/mcp/supported-products (accessed 2026-09-10)
- https://docs.cloud.google.com/mcp/set-up-authentication-mcp-servers (accessed 2026-09-10)
- https://docs.cloud.google.com/mcp/configure-mcp-ai-application (accessed 2026-09-10)
- https://developers.google.com/workspace/preview (accessed 2026-09-10)
- https://developers.google.com/identity/protocols/oauth2/web-server (accessed 2026-09-10)
- https://developers.google.com/identity/protocols/oauth2/native-app (accessed 2026-09-10)
- https://support.google.com/cloud/answer/15549257 (accessed 2026-09-10)
- https://support.google.com/cloud/answer/15549945 (accessed 2026-09-10)
- https://developers.google.com/workspace/gmail/api/auth/scopes (accessed 2026-09-10)
- https://support.google.com/cloud/answer/13464325 (accessed 2026-09-10)
- https://support.google.com/cloud/answer/13464321 (accessed 2026-09-10)
- https://support.google.com/cloud/answer/13464323 (accessed 2026-09-10)
- https://developers.google.com/workspace/guides/configure-mcp-security (accessed 2026-09-10)
- https://myaccount.google.com/permissions (accessed 2026-09-10)
