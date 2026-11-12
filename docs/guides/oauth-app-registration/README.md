# OAuth app registration runbooks

Some Connections providers do not support Dynamic Client Registration, so Kiro Crew cannot obtain an OAuth client from them on its own. For each of those providers an operator has to create an OAuth app in the vendor's developer console once, register the redirect URI Kiro Crew reserves for that provider, and paste the resulting Client ID (and, for confidential clients, the Client secret) into the Kiro Crew dashboard under **Settings → OAuth Apps**. Each runbook below walks one provider from an empty vendor console to a working **Connect** button, and states the vendor's review, approval and scope requirements as documented on the date in its Sources section. Environment variables `KIROCREW_CONNECTIONS_<PROVIDER>_CLIENT_ID` / `_CLIENT_SECRET` are the container and CI alternative to the dashboard fields.

The redirect URI is fixed per provider (host, port and path) and is shown with a copy button on the provider's card in **Settings → OAuth Apps**. Most providers use the `127.0.0.1` loopback literal; Slack and HubSpot use the host name `localhost` because it is the only plain-http host those vendors accept. GitHub, Asana, Google Drive, Gmail, Google Calendar, Google Sheets, Google Docs and Google Slides are the pre-registered providers on this build. The Slack, HubSpot and Box runbooks are written ahead of their Connections entry, so each card appears once that entry ships; Microsoft 365 is a different case, explained in its own runbook. For the Google runbooks, the OAuth client must be created with **Use this client for an AI-powered agent** ticked: Google's Workspace MCP servers reject other clients from 2026-11-11, and an existing client cannot be marked after 2026-11-12 ([Workspace release notes, 2026-10-12](https://developers.google.com/workspace/chat/release-notes)).

| Runbook | Redirect URI to register |
|---|---|
| [github.md](github.md) | `http://127.0.0.1:48101/callback` |
| [asana.md](asana.md) | `http://127.0.0.1:48102/callback` |
| [google.md](google.md) | **One setup for every Google card** (Gmail, Drive, Calendar, Sheets, Docs, Slides): a script, three clicks, paste once |
| [google-drive.md](google-drive.md) | `http://127.0.0.1:48103/callback` |
| [gmail.md](gmail.md) | `http://127.0.0.1:48104/callback` |
| [google-calendar.md](google-calendar.md) | `http://127.0.0.1:48105/callback` |
| [google-sheets.md](google-sheets.md) | `http://127.0.0.1:48109/callback` |
| [google-docs.md](google-docs.md) | `http://127.0.0.1:48110/callback` |
| [google-slides.md](google-slides.md) | `http://127.0.0.1:48111/callback` |
| [slack.md](slack.md) | `http://localhost:48106/callback` |
| [hubspot.md](hubspot.md) | `http://localhost:48107/callback` |
| [box.md](box.md) | `http://127.0.0.1:48108/callback` |
| [microsoft-365.md](microsoft-365.md) | none — Microsoft 365 is not a Connections entry; the runbook explains the status and an optional Entra registration to prepare |

## One Google app for every Google card

All six Google cards share one Google OAuth client. Follow [google.md](google.md): `scripts/google-oauth-setup.sh` (or `scripts/google-oauth-setup.ps1`) creates the project and enables the APIs, then three console steps make the client. Saving its Client ID and secret on any Google card in **Settings → OAuth Apps**, with **Also save on …** on, fills the other five. The per-product pages keep only that product's scopes, endpoint and gotchas.
