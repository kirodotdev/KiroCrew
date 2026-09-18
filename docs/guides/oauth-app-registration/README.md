# OAuth app registration runbooks

Some Connections providers do not support Dynamic Client Registration, so Kiro Crew cannot obtain an OAuth client from them on its own. For each of those providers an operator has to create an OAuth app in the vendor's developer console once, register the redirect URI Kiro Crew reserves for that provider, and paste the resulting Client ID (and, for confidential clients, the Client secret) into the Kiro Crew dashboard under **Settings → OAuth Apps**. Each runbook below walks one provider from an empty vendor console to a working **Connect** button, and states the vendor's review, approval and scope requirements as documented on the date in its Sources section. Environment variables `KIROCREW_CONNECTIONS_<PROVIDER>_CLIENT_ID` / `_CLIENT_SECRET` are the container and CI alternative to the dashboard fields.

The redirect URI is fixed per provider (host, port and path) and is shown with a copy button on the provider's card in **Settings → OAuth Apps**. Most providers use the `127.0.0.1` loopback literal; Slack and HubSpot use the host name `localhost` because it is the only plain-http host those vendors accept.

| Runbook | Redirect URI to register |
|---|---|
| [github.md](github.md) | `http://127.0.0.1:48101/callback` |
| [asana.md](asana.md) | `http://127.0.0.1:48102/callback` |
| [google-drive.md](google-drive.md) | `http://127.0.0.1:48103/callback` |
| [gmail.md](gmail.md) | `http://127.0.0.1:48104/callback` |
| [google-calendar.md](google-calendar.md) | `http://127.0.0.1:48105/callback` |
| [slack.md](slack.md) | `http://localhost:48106/callback` |
| [hubspot.md](hubspot.md) | `http://localhost:48107/callback` |
| [box.md](box.md) | `http://127.0.0.1:48108/callback` |
| [microsoft-365.md](microsoft-365.md) | none — Microsoft 365 is not a Connections entry; the runbook explains the status and an optional Entra registration to prepare |
