# Register a Google OAuth client for Kiro Crew Connections (Google Drive)

*Who this is for: the person who owns (or can create) a Google Cloud project and wants Kiro Crew agents to search and read files in a Google Drive account through Google's remote Drive MCP server. Expected time: 30–45 minutes of clicking, plus a wait of "a couple of days" for Google to accept you into the Developer Preview Program if you are not already a member.*

Google's Drive MCP server is a **Developer Preview** feature. Google accepts a plain-http loopback redirect URI, so Kiro Crew's callback works as-is; the gating items are program enrollment and OAuth scope class, both covered below.

## Before you start

You need:

- A Google Account that can sign in to the [Google Cloud console](https://console.cloud.google.com/). To pick **Internal** audience (recommended, no verification) the project must belong to a Google Cloud Organization, which means a Google Workspace or Cloud Identity account. A consumer `@gmail.com` account can only pick **External**.
- Membership in the [Google Workspace Developer Preview Program](https://developers.google.com/workspace/preview). Google states "Access to Google Workspace MCP servers is part of the Public Developer Preview Program." The application form asks for your Google Workspace account and your Google Cloud project number; Google registers that project and says "The whole process should be done within a couple of days." Google's page does not state whether a consumer `@gmail.com` account is accepted.
- A running Kiro Crew dashboard where you can open **Settings → OAuth Apps**.
- Optional: the `gcloud` CLI, if you prefer commands to console clicks. Every step below has a console path too.

Facts about Kiro Crew's side you will need to type exactly:

- Redirect URI for Google Drive: `http://127.0.0.1:48103/callback`
- Provider slug for environment variables: `GOOGLE_DRIVE`
- MCP endpoint Kiro Crew connects to: `https://drivemcp.googleapis.com/mcp/v1` (verified against Google's "Configure the Drive MCP server" page and Google Cloud's MCP supported-products table).

## 1. Create the app

Google does not have an "app" object; you create a Google Cloud project, enable two APIs, configure the Google Auth Platform (consent screen), and then create an OAuth client inside it.

### 1.1 Join the Developer Preview Program

1. Open [https://developers.google.com/workspace/preview](https://developers.google.com/workspace/preview).
2. Read the Program Terms, then click **Apply to join the Developer Preview Program** and submit the form with your Google Workspace account and Google Cloud project number (Console → project picker → the numeric **Project number**).
3. Wait for Google's confirmation email. Until the project is registered, the MCP endpoint may refuse your calls even if everything below is configured.

### 1.2 Create or pick a Google Cloud project

1. In the [Google Cloud console](https://console.cloud.google.com/), click the project picker at the top and choose **New project**, or select an existing project you control.
2. Note the **Project ID** (a string) and **Project number** (numeric); you need both later.

### 1.3 Enable the Drive API and the Drive MCP API

Google requires two services for the Drive MCP server: the ordinary **Google Drive API** and the **Google Drive MCP API**.

Console path: open [https://console.cloud.google.com/flows/enableapi?apiid=drive.googleapis.com](https://console.cloud.google.com/flows/enableapi?apiid=drive.googleapis.com), pick your project, click **Next**, then **Enable**. Repeat with [https://console.cloud.google.com/flows/enableapi?apiid=drivemcp.googleapis.com](https://console.cloud.google.com/flows/enableapi?apiid=drivemcp.googleapis.com).

CLI equivalent:

```bash
gcloud services enable drive.googleapis.com drivemcp.googleapis.com --project=PROJECT_ID
```

### 1.4 Configure the Google Auth Platform (OAuth consent screen)

1. Go to **Google Auth Platform → Branding**: [https://console.cloud.google.com/auth/branding](https://console.cloud.google.com/auth/branding). If you see **Google Auth Platform not configured yet**, click **Get Started**.
2. **App Information**: App name `Kiro Crew Drive MCP` (any name is fine; Google's own example is `Drive MCP Server`). **User support email**: pick your address. Click **Next**.
3. **Audience**: select **Internal** if the option is offered. If it is greyed out you are not in an Organization; select **External**. Click **Next**.
4. **Contact Information**: enter an email address. Click **Next**.
5. **Finish**: tick **I agree to the Google API Services: User Data Policy**, click **Continue**, then **Create**.
6. If you chose **External**: open **Audience** ([https://console.cloud.google.com/auth/audience](https://console.cloud.google.com/auth/audience)), under **Test users** click **Add users**, enter the Google Account(s) that will click **Connect** in Kiro Crew, click **Save**. Leave **Publishing status** at **Testing** for now (see section 4).
7. Open **Data Access** ([https://console.cloud.google.com/auth/scopes](https://console.cloud.google.com/auth/scopes)) → **Add or Remove Scopes**, and under **Manually add scopes** paste the scopes from section 3, click **Add to Table**, **Update**, then **Save**.

### 1.5 Create the OAuth client

1. Go to **Google Auth Platform → Clients → Create Client**: [https://console.cloud.google.com/auth/clients/create](https://console.cloud.google.com/auth/clients/create).
2. **Application type**: **Web application**. (Why not "Desktop app": see section 2.)
3. **Name**: `Kiro Crew` (or anything).
4. Leave **Authorized JavaScript origins** empty.
5. Under **Authorized redirect URIs** click **+ Add URI** and paste `http://127.0.0.1:48103/callback` exactly. If you also plan to register Gmail and Google Calendar, add `http://127.0.0.1:48104/callback` and `http://127.0.0.1:48105/callback` to the same client now; one client may carry all three URIs and you then paste the same Client ID and secret into all three Kiro Crew cards.
6. Click **Create**. A dialog shows the **Client ID** (ends in `.apps.googleusercontent.com`) and the **Client secret**. Copy both immediately. Google states the secret "will only be shown after you create the client" and later displays only its last four characters; if you lose it, use **Add Secret** on the client page to rotate.

## 2. Redirect URI

Register exactly `http://127.0.0.1:48103/callback`. Google matches redirect URIs literally: "the `http` or `https` scheme, case, and trailing slash ('/') must all match", so do not add a trailing slash and do not substitute `localhost`.

Plain http on loopback is allowed. Google's redirect URI validation rules state: "Redirect URIs must use the HTTPS scheme, not plain HTTP. Localhost URIs (including localhost IP address URIs) are exempt from this rule." and "Hosts cannot be raw IP addresses. Localhost IP addresses are exempted from this rule." Google's own MCP client guide registers `http://localhost:8787/callback` on a Web application client for Cursor Desktop, which is the same shape as Kiro Crew's URI.

Why **Web application** rather than **Desktop app**: a Desktop-app client has no redirect URI list at all ("The console does not require any additional information to create OAuth 2.0 credentials for desktop applications"); Google's installed-app flow expects the app to "start an HTTP listener on a random available port" and documents the loopback form as `http://127.0.0.1:port` with no path. Google's documentation does not state whether a `/callback` path is accepted on a Desktop-app loopback redirect. Kiro Crew uses a fixed port and a fixed path, so the Web application type, where you register the exact URI and Google validates against it, is the type whose behaviour is documented. It is also the type every Google MCP-server page uses for third-party clients (Antigravity, Claude, Cursor, ChatGPT).

Kiro Crew's OAuth callback listener runs inside kiro-cli on the loopback interface over plain HTTP only; an https loopback is not possible, and none is needed for Google.

Remote, tailnet or Docker installs do not need a second redirect URI. After you approve consent, the browser is sent to `http://127.0.0.1:48103/callback?...` on the laptop, where nothing is listening, so the page fails to load. Copy that full URL from the address bar and paste it into the field the Kiro Crew dashboard shows during Connect; the gateway replays it against its own loopback listener and completes the exchange.

## 3. Scopes and permissions

Google's Drive MCP page tells you to declare exactly two scopes on the **Data Access** page:

| Scope | Google's class | What it unlocks on the Drive MCP server |
|---|---|---|
| `https://www.googleapis.com/auth/drive.readonly` | **Restricted** ("View and download all your Drive files") | `search_files`, `list_recent_files`, `get_file_metadata`, `get_file_permissions`, `read_file_content`, `download_file_content` |
| `https://www.googleapis.com/auth/drive.file` | **Non-sensitive** (per-file access to files the user opened or created with the app) | `create_file`, `copy_file` |

Google's page does not map tools to scopes; the mapping above follows the Drive API scope descriptions and may be narrower or wider than the server enforces. Declaring a scope on Data Access is a ceiling, not a request; the scopes actually granted are the ones Kiro Crew asks for at Connect time, and the consent screen lists them.

Recommendation: declare `drive.readonly` only if you want agents limited to reading. Add `drive.file` only when you want agents to create or copy files. Google recommends `drive.file` as the non-sensitive default for most Drive apps, but on its own it cannot search or read files the app did not create.

Because `drive.readonly` is **restricted**, it drives the review requirements in section 4. Google also limits restricted Drive scopes to specific app categories ("Backup and sync", "Productivity and education", "Reporting and security") when an app is submitted for verification.

The client secret: Google's token-exchange reference marks `client_secret` as **Optional** for both Web and Desktop clients, and its "Manage OAuth Clients" page says web server applications are "Private Clients" that "can securely store the client secret". In practice a Web application client that omits or mis-types the secret receives `invalid_client` ("The OAuth client secret is incorrect"). Kiro Crew sends the secret at token exchange, so enter it.

## 4. Review and publishing requirements

- **Internal audience** (Organization project): no Google verification, no unverified-app warning, no 100-user cap. Google notes that "high-risk Gmail and Drive scopes might require additional configuration by your organization's administrators" (Admin console → "Let Internal apps access restricted Google Workspace APIs"). If Connect fails with `admin_policy_enforced`, ask your Workspace admin to trust the client ID.
- **External audience, publishing status Testing**: no verification needed. Limits: at most 100 test users; every test user sees an "unverified app" warning; "Authorizations by a test user will expire seven days from the time of consent. If your OAuth client requests an `offline` access type and receives a refresh token, that token will also expire." Expect to click **Connect** again in Kiro Crew weekly.
- **External audience, In production**: because `drive.readonly` is restricted, publishing requires brand verification (verified domain homepage, privacy policy, demo video) plus restricted-scope verification and an annual third-party security assessment (Google's "Security Assessment", sometimes called CASA). Google's Developer Preview terms additionally say preview features "may not be included in public applications prior to the General Availability (GA) announcement". For a personal or team Kiro Crew install, stay in Testing or use Internal.
- Google's generic MCP guide says callers need the IAM role **MCP Tool User** (`roles/mcp.toolUser`) on the project. The Workspace MCP pages do not mention it. If Connect succeeds but tool calls return a permission error, grant that role to the connecting user in **IAM & Admin → IAM**.
- Unused OAuth clients are deleted after six months of inactivity; Google emails 30 days before.

## 5. Enter the credentials in Kiro Crew

1. Open the Kiro Crew dashboard → **Settings → OAuth Apps** → the **Google Drive** card.
2. Paste the **Client ID** into "Client ID" and the **Client secret** into "Client secret". The card's copy button gives you the redirect URI to double-check against section 2.
3. Click **Save**.

Container or CI alternative (environment variables override dashboard values):

```bash
KIROCREW_CONNECTIONS_GOOGLE_DRIVE_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
KIROCREW_CONNECTIONS_GOOGLE_DRIVE_CLIENT_SECRET=GOCSPX-...
```

After saving, the Google Drive card on **Capabilities → Connections** switches from "Needs configuration" to "Connect".

## 6. Verify the connection

1. On **Capabilities → Connections**, click **Connect** on the Google Drive card. A Google sign-in and consent page opens. If the project is External/Testing you first see "Google hasn't verified this app"; click **Continue**. The consent page should list "View and download all your Drive files" (that is `drive.readonly`).
2. Approve. The browser lands on `http://127.0.0.1:48103/callback?...`. On a local install the page closes itself; on a remote install paste the URL back into the dashboard.
3. The card shows **Connected**.
4. In a chat session, ask the agent: **"Summarize the file Marketing Plan."** (substitute a real file name). Google's test description says the client "calls `drive.search_files` to locate 'Marketing Plan', then uses `drive.read_file_content` to retrieve and summarize its content." A successful tool call from `https://drivemcp.googleapis.com/mcp/v1` confirms the end-to-end path.
5. Expected tool list from the server: `copy_file`, `create_file`, `download_file_content`, `get_file_metadata`, `get_file_permissions`, `list_recent_files`, `read_file_content`, `search_files`.

If something fails:

- `redirect_uri_mismatch`: the URI on the client does not match `http://127.0.0.1:48103/callback` byte for byte. Google notes changes "may take 5 minutes to a few hours" to take effect.
- `access_denied` / "app is being tested": the signing-in account is not in **Test users**.
- `403` or "API has not been used in project": `drivemcp.googleapis.com` or `drive.googleapis.com` is not enabled, or the project is not yet registered in the Developer Preview.
- Consent works but tools fail: check the OAuth log events in the Workspace admin security investigation tool (Google's troubleshooting tip), and consider `roles/mcp.toolUser`.

To disconnect on the Google side, the user opens [https://myaccount.google.com/permissions](https://myaccount.google.com/permissions) and removes the app. Google notes revocation "removes all OAuth 2.0 scopes previously granted to a project", so if Drive, Gmail and Calendar share one project, all three connections are revoked together.

## Known limits

- Developer Preview: Google's terms say preview features stay 3–6 months in the program, may change, and may not be shipped to users outside your domain or company before GA.
- Only files that are eligible for the Drive MCP server are returned; see Google's "Drive MCP file eligibility" page.
- Google does not support Dynamic Client Registration or OAuth Client ID Metadata Documents for its MCP servers ("Google and Google Cloud remote MCP servers don't support Dynamic Client Registration"), so the client ID and secret must always be entered by hand.
- Google's documentation does not state the contents of the MCP server's protected-resource metadata, so the exact `authorization_servers` string (with or without trailing slash) is not documented. Google's OpenID Connect issuer is documented as `https://accounts.google.com` (no trailing slash).
- Testing-mode refresh tokens expire after 7 days; Internal audience avoids this.
- Google's Workspace admin can block third-party access to Drive scopes for the whole organization.

## Sources

- https://developers.google.com/workspace/drive/api/guides/configure-mcp-server (accessed 2026-09-10)
- https://developers.google.com/workspace/guides/configure-mcp-servers (accessed 2026-09-10)
- https://docs.cloud.google.com/mcp/supported-products (accessed 2026-09-10)
- https://docs.cloud.google.com/mcp/set-up-authentication-mcp-servers (accessed 2026-09-10)
- https://docs.cloud.google.com/mcp/configure-mcp-ai-application (accessed 2026-09-10)
- https://developers.google.com/workspace/preview (accessed 2026-09-10)
- https://developers.google.com/identity/protocols/oauth2/web-server (accessed 2026-09-10)
- https://developers.google.com/identity/protocols/oauth2/native-app (accessed 2026-09-10)
- https://support.google.com/cloud/answer/15549257 (accessed 2026-09-10)
- https://support.google.com/cloud/answer/15549945 (accessed 2026-09-10)
- https://developers.google.com/workspace/drive/api/guides/api-specific-auth (accessed 2026-09-10)
- https://support.google.com/cloud/answer/13464325 (accessed 2026-09-10)
- https://support.google.com/cloud/answer/13464321 (accessed 2026-09-10)
- https://support.google.com/cloud/answer/13464323 (accessed 2026-09-10)
- https://developers.google.com/workspace/drive/api/guides/drive-mcp-server-file-eligibility (accessed 2026-09-10)
- https://myaccount.google.com/permissions (accessed 2026-09-10)
