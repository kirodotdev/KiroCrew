# Register a Google OAuth client for Kiro Crew Connections (Google Calendar)

*Who this is for: the person who owns (or can create) a Google Cloud project and wants Kiro Crew agents to list calendars, read events and check free/busy time in a Google Calendar account through Google's remote Calendar MCP server. Expected time: 30–45 minutes of clicking, plus a wait of "a couple of days" for Google to accept you into the Developer Preview Program if you are not already a member.*

Google's Calendar MCP server is a **Developer Preview** feature. Google accepts a plain-http loopback redirect URI, so Kiro Crew's callback works as-is. The Calendar scopes Google lists for the server are all read-only and are not on Google's restricted-scope list, which makes this the lightest of the three Google runbooks on the review side.

## Before you start

You need:

- A Google Account that can sign in to the [Google Cloud console](https://console.cloud.google.com/). To pick **Internal** audience (recommended, no verification) the project must belong to a Google Cloud Organization, which means a Google Workspace or Cloud Identity account. A consumer `@gmail.com` account can only pick **External**.
- Membership in the [Google Workspace Developer Preview Program](https://developers.google.com/workspace/preview). Google states "Access to Google Workspace MCP servers is part of the Public Developer Preview Program." The application form asks for your Google Workspace account and your Google Cloud project number; Google says "The whole process should be done within a couple of days." Google's page does not state whether a consumer `@gmail.com` account is accepted.
- A running Kiro Crew dashboard where you can open **Settings → OAuth Apps**.
- Optional: the `gcloud` CLI. Every step below has a console path too.

Facts about Kiro Crew's side you will need to type exactly:

- Redirect URI for Google Calendar: `http://127.0.0.1:48105/callback`
- Provider slug for environment variables: `GOOGLE_CALENDAR`
- MCP endpoint Kiro Crew connects to: `https://calendarmcp.googleapis.com/mcp/v1` (verified against Google's "Configure the Calendar MCP server" page and Google Cloud's MCP supported-products table).

## 1. Create the app

Google does not have an "app" object; you create a Google Cloud project, enable two APIs, configure the Google Auth Platform (consent screen), and then create an OAuth client inside it. If you already did this for Google Drive or Gmail in the same project, skip to 1.3 and add only the Calendar-specific items.

### 1.1 Join the Developer Preview Program

1. Open [https://developers.google.com/workspace/preview](https://developers.google.com/workspace/preview).
2. Read the Program Terms, click **Apply to join the Developer Preview Program**, and submit the form with your Google Workspace account and Google Cloud project number (Console → project picker → **Project number**).
3. Wait for Google's confirmation email. Until the project is registered, the MCP endpoint may refuse your calls.

### 1.2 Create or pick a Google Cloud project

1. In the [Google Cloud console](https://console.cloud.google.com/), click the project picker and choose **New project**, or select an existing project you control.
2. Note the **Project ID** and **Project number**.

### 1.3 Enable the Calendar API and the Calendar MCP API

Google requires two services: the ordinary **Google Calendar API** (service name `calendar-json.googleapis.com`) and the **Google Calendar MCP API** (`calendarmcp.googleapis.com`).

Console path: open [https://console.cloud.google.com/flows/enableapi?apiid=calendar-json.googleapis.com](https://console.cloud.google.com/flows/enableapi?apiid=calendar-json.googleapis.com), pick your project, click **Next**, then **Enable**. Repeat with [https://console.cloud.google.com/flows/enableapi?apiid=calendarmcp.googleapis.com](https://console.cloud.google.com/flows/enableapi?apiid=calendarmcp.googleapis.com).

CLI equivalent:

```bash
gcloud services enable calendar-json.googleapis.com calendarmcp.googleapis.com --project=PROJECT_ID
```

### 1.4 Configure the Google Auth Platform (OAuth consent screen)

1. Go to **Google Auth Platform → Branding**: [https://console.cloud.google.com/auth/branding](https://console.cloud.google.com/auth/branding). If you see **Google Auth Platform not configured yet**, click **Get Started**.
2. **App Information**: App name `Kiro Crew Calendar MCP` (Google's own example is `Calendar MCP Server`). **User support email**: pick your address. Click **Next**.
3. **Audience**: select **Internal** if offered; otherwise **External**. Click **Next**.
4. **Contact Information**: enter an email address. Click **Next**.
5. **Finish**: tick **I agree to the Google API Services: User Data Policy**, click **Continue**, then **Create**.
6. If you chose **External**: open **Audience** ([https://console.cloud.google.com/auth/audience](https://console.cloud.google.com/auth/audience)), under **Test users** click **Add users**, enter the Google Account(s) whose calendar will be connected, click **Save**. Leave **Publishing status** at **Testing** (see section 4).
7. Open **Data Access** ([https://console.cloud.google.com/auth/scopes](https://console.cloud.google.com/auth/scopes)) → **Add or Remove Scopes**; under **Manually add scopes** paste the three scopes from section 3, click **Add to Table**, **Update**, then **Save**.

### 1.5 Create the OAuth client

1. Go to **Google Auth Platform → Clients → Create Client**: [https://console.cloud.google.com/auth/clients/create](https://console.cloud.google.com/auth/clients/create).
2. **Application type**: **Web application** (see section 2 for why not "Desktop app").
3. **Name**: `Kiro Crew`.
4. Leave **Authorized JavaScript origins** empty.
5. Under **Authorized redirect URIs** click **+ Add URI** and paste `http://127.0.0.1:48105/callback` exactly. To share one client across Google Drive and Gmail as well, also add `http://127.0.0.1:48103/callback` and `http://127.0.0.1:48104/callback`; one client may carry all three URIs and the same Client ID and secret then go into all three Kiro Crew cards.
6. Click **Create**. Copy the **Client ID** (ends in `.apps.googleusercontent.com`) and the **Client secret** now. Google shows the full secret only once; afterwards only the last four characters are displayed. If you lose it, click **Add Secret** on the client page to rotate.

## 2. Redirect URI

Register exactly `http://127.0.0.1:48105/callback`. Google matches literally: "the `http` or `https` scheme, case, and trailing slash ('/') must all match". No trailing slash, no `localhost` substitution.

Plain http on loopback is allowed by Google's redirect URI validation rules: "Redirect URIs must use the HTTPS scheme, not plain HTTP. Localhost URIs (including localhost IP address URIs) are exempt from this rule." and "Hosts cannot be raw IP addresses. Localhost IP addresses are exempted from this rule." Google's own MCP client guide registers `http://localhost:8787/callback` on a Web application client for Cursor Desktop, the same shape as Kiro Crew's URI.

Why **Web application** rather than **Desktop app**: a Desktop-app client has no redirect URI list ("The console does not require any additional information to create OAuth 2.0 credentials for desktop applications"); Google's installed-app flow expects the app to "start an HTTP listener on a random available port" and documents the loopback form as `http://127.0.0.1:port` with no path. Google's documentation does not state whether a `/callback` path is accepted on a Desktop-app loopback redirect. Kiro Crew uses a fixed port and path, so use the Web application type, where the exact URI is registered and validated. It is also the type every Google MCP-server page uses for third-party clients.

Kiro Crew's OAuth callback listener runs inside kiro-cli on the loopback interface over plain HTTP only; an https loopback is not possible, and Google does not require one.

Remote, tailnet or Docker installs do not need a second redirect URI. After consent, the browser is sent to `http://127.0.0.1:48105/callback?...` on the laptop, where nothing is listening, so the page fails to load. Copy that full URL from the address bar and paste it into the field the Kiro Crew dashboard shows during Connect; the gateway replays it against its own loopback listener and completes the exchange.

## 3. Scopes and permissions

Google's Calendar MCP page tells you to declare exactly three scopes on **Data Access**, all read-only:

| Scope | Google's class | What it unlocks on the Calendar MCP server |
|---|---|---|
| `https://www.googleapis.com/auth/calendar.calendarlist.readonly` | Not restricted; Google's Calendar scopes page does not label sensitivity | `list_calendars` |
| `https://www.googleapis.com/auth/calendar.events.readonly` | Not restricted; label not stated | `list_events`, `get_event`, `search_events` |
| `https://www.googleapis.com/auth/calendar.events.freebusy` | Not restricted; label not stated | `suggest_time` (free/busy lookups) |

Google's page does not map tools to scopes; the mapping above follows the Calendar API scope descriptions. Declaring a scope on Data Access is a ceiling, not a request; what the user actually grants is what Kiro Crew requests at Connect time, and the consent screen lists it.

Two things Google's Calendar MCP page does **not** say:

- It does not list `https://www.googleapis.com/auth/calendar.readonly`. That broader scope would also work for reading, but the three narrower scopes above are what Google documents for the server, so declare those.
- It lists write tools (`create_event`, `update_event`, `delete_event`, `respond_to_event`) but declares no write scope for them. Google's documentation does not state which scope those tools need. If you want them to work you would have to add a Calendar write scope such as `https://www.googleapis.com/auth/calendar.events` on Data Access and confirm behaviour yourself; this runbook recommends staying read-only.

Class: none of the Calendar scopes appear on Google's "Restricted Scopes" list, so no security assessment is ever required. Calendar scopes that read user data are treated by Google as **sensitive** for verification purposes (Google's own verification example uses a Calendar scope), which means an External app that is published to production needs sensitive-scope verification but not the restricted-scope security assessment.

The client secret: Google's token-exchange reference marks `client_secret` as **Optional**, and "Manage OAuth Clients" calls web server apps "Private Clients" that "can securely store the client secret". In practice a Web application client that omits or mis-types the secret receives `invalid_client`. Kiro Crew sends the secret at token exchange, so enter it.

## 4. Review and publishing requirements

- **Internal audience** (Organization project): no Google verification, no unverified-app warning, no 100-user cap. Calendar scopes are not "high-risk" in Google's wording, but a Workspace admin can still restrict third-party or internal app access to Calendar under **Security → API controls**; `admin_policy_enforced` at consent means that restriction is on.
- **External audience, Testing**: no verification. Limits: 100 test users; "unverified app" warning on every consent; "Authorizations by a test user will expire seven days from the time of consent. If your OAuth client requests an `offline` access type and receives a refresh token, that token will also expire." Plan to click **Connect** again weekly.
- **External audience, In production**: brand verification (verified homepage domain, privacy policy, demo video showing the consent screen) plus sensitive-scope verification with a scope justification. No security assessment, because no Calendar scope is restricted. Google's Developer Preview terms also forbid shipping preview features to users outside your domain or company before GA. For a personal or team Kiro Crew install, stay in Testing or use Internal.
- Google's generic MCP guide requires the IAM role **MCP Tool User** (`roles/mcp.toolUser`) on the project. The Calendar MCP page does not mention it. If Connect succeeds but tool calls return a permission error, grant it in **IAM & Admin → IAM**.
- Unused OAuth clients are deleted after six months of inactivity; Google emails 30 days before.

## 5. Enter the credentials in Kiro Crew

1. Open the Kiro Crew dashboard → **Settings → OAuth Apps** → the **Google Calendar** card.
2. Paste the **Client ID** into "Client ID" and the **Client secret** into "Client secret". The card's copy button gives the redirect URI to compare with section 2.
3. Click **Save**.

Container or CI alternative (environment variables override dashboard values):

```bash
KIROCREW_CONNECTIONS_GOOGLE_CALENDAR_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
KIROCREW_CONNECTIONS_GOOGLE_CALENDAR_CLIENT_SECRET=GOCSPX-...
```

After saving, the Google Calendar card on **Capabilities → Connections** switches from "Needs configuration" to "Connect".

## 6. Verify the connection

1. On **Capabilities → Connections**, click **Connect** on the Google Calendar card. Sign in with the calendar owner's account. On External/Testing you first see "Google hasn't verified this app"; click **Continue**. The consent page should list the three read-only Calendar permissions (calendar list, events, free/busy) and nothing that says "edit" or "delete".
2. Approve. The browser lands on `http://127.0.0.1:48105/callback?...`. Locally the page closes itself; on a remote install paste the URL back into the dashboard.
3. The card shows **Connected**.
4. In a chat session, ask: **"When is my next meeting with Ariel?"** (use a real attendee). Google's test description: "The client checks your schedule using `calendar.list_events` and details your next meeting with Ariel."
5. Ask: **"Which calendars do I have?"** and expect `list_calendars` to run. Ask: **"Find a free 30-minute slot for me and Ariel tomorrow"** and expect `suggest_time`.
6. Expected tool list from `https://calendarmcp.googleapis.com/mcp/v1`: `create_event`, `delete_event`, `get_event`, `list_calendars`, `list_events`, `respond_to_event`, `suggest_time`, `update_event` (Google's reference sidebar also lists `search_events`). With only the three read-only scopes granted, the write tools should fail with an insufficient-permission error; that is the expected outcome, not a misconfiguration.

If something fails:

- `redirect_uri_mismatch`: the client's URI is not `http://127.0.0.1:48105/callback` byte for byte; client edits "may take 5 minutes to a few hours" to apply.
- `access_denied` / "app is being tested": the account is not in **Test users**.
- `403` / "API has not been used in project": `calendarmcp.googleapis.com` or `calendar-json.googleapis.com` is not enabled, or the project is not yet registered in the Developer Preview.
- `admin_policy_enforced`: the Workspace admin restricts Calendar access for third-party or internal apps.
- Consent works but tools fail: ask the admin to check **OAuth log events** in the security investigation tool (Google's troubleshooting tip), and consider `roles/mcp.toolUser`.

To revoke on the Google side, the user opens [https://myaccount.google.com/permissions](https://myaccount.google.com/permissions) and removes the app. Revocation "removes all OAuth 2.0 scopes previously granted to a project", so if Calendar shares a project with Drive and Gmail, all three connections are revoked together.

## Known limits

- Developer Preview: features stay 3–6 months in the program, may change, and may not be shipped to users outside your domain or company before GA.
- Google does not support Dynamic Client Registration or OAuth Client ID Metadata Documents for its MCP servers, so the client ID and secret must always be entered by hand.
- Google's documentation does not state the contents of the MCP server's protected-resource metadata, so the exact `authorization_servers` string (with or without trailing slash) is not documented. Google's OpenID Connect issuer is documented as `https://accounts.google.com` (no trailing slash).
- Testing-mode refresh tokens expire after 7 days; Internal audience avoids this.
- Google documents only read-only scopes for this server while listing write tools; the write path is undocumented on the scope side.
- Google recommends screening prompts and responses for prompt injection; event descriptions and invitations are untrusted input, so review agent actions.

## Sources

- https://developers.google.com/workspace/calendar/api/guides/configure-mcp-server (accessed 2026-09-10)
- https://developers.google.com/workspace/guides/configure-mcp-servers (accessed 2026-09-10)
- https://docs.cloud.google.com/mcp/supported-products (accessed 2026-09-10)
- https://docs.cloud.google.com/mcp/set-up-authentication-mcp-servers (accessed 2026-09-10)
- https://docs.cloud.google.com/mcp/configure-mcp-ai-application (accessed 2026-09-10)
- https://developers.google.com/workspace/preview (accessed 2026-09-10)
- https://developers.google.com/identity/protocols/oauth2/web-server (accessed 2026-09-10)
- https://developers.google.com/identity/protocols/oauth2/native-app (accessed 2026-09-10)
- https://support.google.com/cloud/answer/15549257 (accessed 2026-09-10)
- https://support.google.com/cloud/answer/15549945 (accessed 2026-09-10)
- https://developers.google.com/workspace/calendar/api/auth (accessed 2026-09-10)
- https://support.google.com/cloud/answer/13464325 (accessed 2026-09-10)
- https://support.google.com/cloud/answer/13464321 (accessed 2026-09-10)
- https://support.google.com/cloud/answer/13464323 (accessed 2026-09-10)
- https://developers.google.com/workspace/guides/configure-mcp-security (accessed 2026-09-10)
- https://myaccount.google.com/permissions (accessed 2026-09-10)
