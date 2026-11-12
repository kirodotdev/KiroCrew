# Set up Google for Kiro Crew (Gmail, Drive, Calendar, Sheets, Docs, Slides)

*Who this is for: you, connecting your own Google account to your own Kiro Crew. Private use only. About 10 minutes of clicking, plus a wait of a few days the first time while Google accepts your project into the Developer Preview.*

One Google app serves all six Google cards. You create it once, paste its Client ID and secret once, and every Google card can connect.

## The short path

### 1. Run the setup script

You need the [gcloud CLI](https://cloud.google.com/sdk/docs/install). Then, from a Kiro Crew checkout:

```bash
scripts/google-oauth-setup.sh            # macOS / Linux
```

```powershell
powershell -ExecutionPolicy Bypass -File scripts\google-oauth-setup.ps1   # Windows
```

The script signs you in, picks or creates a Google Cloud project named **Kiro Crew**, enables the twelve APIs the six cards need, and prints your **Project number** plus direct links for the three steps below. Pass a project ID (`scripts/google-oauth-setup.sh my-project`) to use a project you already have. Running it again is safe; it changes nothing that is already set and writes nothing to disk.

No checkout? Run the same thing by hand: create a project in the [Cloud console](https://console.cloud.google.com/projectcreate), then

```bash
gcloud services enable gmail.googleapis.com drive.googleapis.com calendar-json.googleapis.com \
  sheets.googleapis.com docs.googleapis.com slides.googleapis.com --project=PROJECT_ID
gcloud services enable gmailmcp.googleapis.com drivemcp.googleapis.com calendarmcp.googleapis.com \
  sheetsmcp.googleapis.com docsmcp.googleapis.com slidesmcp.googleapis.com --project=PROJECT_ID
gcloud projects describe PROJECT_ID --format='value(projectNumber)'
```

### 2. Three clicks in Google's console

Google has no API for these. The script prints each link with your project already selected.

1. **Join the Developer Preview.** Open [developers.google.com/workspace/preview](https://developers.google.com/workspace/preview), click **Apply to join**, and paste the Project number the script printed. Google's MCP servers are a Developer Preview, so they refuse calls until Google accepts the project, usually within a few days. If the script said the MCP APIs did not enable yet, run it again after Google's email.
2. **Consent screen.** Open the **Branding** link and click **Get Started**. App name `Kiro Crew`, your own email for support and contact, agree to the policy, **Create**.
   - **Audience:** pick **Internal** if it is offered (a Google Workspace account). Otherwise pick **External**, stay in **Testing**, and add yourself under **Audience → Test users**.
   - **Data Access → Add or Remove Scopes → Manually add scopes:** paste the one comma-separated line the script printed, click **Add to Table**, **Update**, **Save**.
3. **OAuth client.** Open the **Create client** link.
   - **Application type:** **Web application**.
   - **Authorized redirect URIs:** add the six URIs the script printed (listed below too). You may add only the cards you will use.
   - Tick **Use this client for an AI-powered agent**. Google's MCP servers reject clients without it.
   - Click **Create** and copy the **Client ID** and **Client secret**. Google shows the full secret only once.

| Card | Redirect URI |
|---|---|
| Google Drive | `http://127.0.0.1:48103/callback` |
| Gmail | `http://127.0.0.1:48104/callback` |
| Google Calendar | `http://127.0.0.1:48105/callback` |
| Google Sheets | `http://127.0.0.1:48109/callback` |
| Google Docs | `http://127.0.0.1:48110/callback` |
| Google Slides | `http://127.0.0.1:48111/callback` |

### 3. Paste once

In Kiro Crew open **Settings → OAuth Apps**, pick any Google card, paste the Client ID and Client secret, leave **Also save on Google Drive, …** on, and click **Save**. All six Google cards now show **Configured**. (Changing only the Client ID later saves on that card alone; enter the secret again to update all six.) Then click **Connect** on each card you want under **Customize → Connections**.

On **External / Testing** Google shows "Google hasn't verified this app" at sign-in; click **Continue**. That is expected for a private app.

## What each card asks for

Kiro Crew requests only these read-only scopes, so write tools (drafts, edits, new files) fail with a permission error. The per-card pages list each server's tools.

| Card | Scopes Kiro Crew requests | Card notes |
|---|---|---|
| Gmail | `gmail.readonly` | [gmail.md](gmail.md) |
| Google Drive | `drive.readonly` | [google-drive.md](google-drive.md) |
| Google Calendar | `calendar.calendarlist.readonly`, `calendar.events.readonly`, `calendar.events.freebusy` | [google-calendar.md](google-calendar.md) |
| Google Sheets | `spreadsheets.readonly`, `drive.readonly` | [google-sheets.md](google-sheets.md) |
| Google Docs | `documents.readonly`, `drive.readonly` | [google-docs.md](google-docs.md) |
| Google Slides | `presentations.readonly`, `drive.readonly` | [google-slides.md](google-slides.md) |

All scopes start with `https://www.googleapis.com/auth/`.

## If something fails

- `redirect_uri_mismatch`: that card's URI is missing from the client or differs by a character (no trailing slash, `127.0.0.1` not `localhost`). Client edits can take a few minutes to apply.
- `access_denied` or "app is being tested": your account is not under **Audience → Test users**.
- `403` or "API has not been used in project": the project is not yet accepted into the Developer Preview, or an MCP API is off. Run the script again.
- `admin_policy_enforced`: a Workspace admin blocks the scopes. The admin can trust the Client ID under **Security → API controls**.
- Sign-in works but every tool call is rejected: the client is missing **Use this client for an AI-powered agent**. Older clients cannot be changed; create a new one and paste it again. If calls still return a permission error, grant yourself **MCP Tool User** (`roles/mcp.toolUser`) under **IAM & Admin → IAM**.
- On **External / Testing** the grant expires seven days after you consent. Click **Connect** again. **Internal** does not expire this way.
- Remote, tailnet or Docker install: after consent the browser lands on `http://127.0.0.1:<port>/callback?...` and the page fails to load. Copy the whole URL from the address bar into the field Kiro Crew shows during Connect.

Containers and CI can set the client through environment variables instead, one pair per card, which override the dashboard: `KIROCREW_CONNECTIONS_<CARD>_CLIENT_ID` and `KIROCREW_CONNECTIONS_<CARD>_CLIENT_SECRET`, where `<CARD>` is `GMAIL`, `GOOGLE_DRIVE`, `GOOGLE_CALENDAR`, `GOOGLE_SHEETS`, `GOOGLE_DOCS` or `GOOGLE_SLIDES`.

To revoke, remove the app at [myaccount.google.com/permissions](https://myaccount.google.com/permissions). That ends every Google card connected through the app at once.

## Appendix: publishing to other people

Skip this for private use. It only matters if people outside your own account or Workspace domain would sign in to your app.

- **Internal** (Workspace project): no verification, no 100-user cap, no seven-day expiry. Limited to your organization.
- **External, Testing**: no verification, up to 100 test users, an unverified-app warning on each consent, and grants that expire after seven days.
- **External, In production**: `gmail.readonly`, `gmail.compose` and `drive.readonly` are **restricted** scopes. Publishing needs brand verification (verified domain, privacy policy, demo video), restricted-scope verification, and a yearly security assessment by a Google-approved assessor (CASA). Google's Developer Preview terms also forbid shipping preview features to users outside your domain or company before general availability.
- Google deletes OAuth clients unused for six months and emails 30 days before.

## Sources

- https://developers.google.com/workspace/guides/configure-mcp-servers (APIs, MCP services, consent screen, AI-powered agent client; accessed 2026-11-10)
- https://developers.google.com/workspace/preview (accessed 2026-11-10)
- https://developers.google.com/workspace/chat/release-notes (2026-10-12 entry on AI-powered agent clients)
- https://developers.google.com/identity/protocols/oauth2/web-server (redirect URI rules)
- https://support.google.com/cloud/answer/15549945 (Testing users and seven-day expiry)
- https://support.google.com/cloud/answer/13464325 (restricted scope verification)
- The per-card pages linked above carry each product's own sources.
