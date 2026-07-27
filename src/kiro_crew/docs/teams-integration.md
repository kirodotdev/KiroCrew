# Microsoft Teams Integration

KiroCrew can run as a Microsoft Teams bot, so you can DM your agent from Teams
the same way you would from Slack, Telegram, Discord, Webex, or WeCom.

> **This release is DM-only and self-hosted.** You chat with the bot in a **1:1
> personal chat**. Messages sent in team channels or group chats are refused
> (fail closed) so tool output is never exposed to unauthorized members.
> Because Teams uses the Bot Framework model, KiroCrew exposes an **inbound
> HTTPS webhook** and you must give your gateway a **public HTTPS URL** (unlike
> the other channels, which open an outbound connection and need no public
> URL).

## How it works

Unlike Slack (Socket Mode) or Webex (device WebSocket), the Microsoft Bot
Framework **pushes** activities to a messaging endpoint you host. KiroCrew:

1. Registers a single inbound route on the gateway's existing HTTP server:
   `POST /api/messaging/teams`.
2. Validates every inbound request's `Authorization: Bearer <jwt>` against the
   Bot Framework signing keys (issuer, `aud == your App ID`, signature,
   expiry) **before** processing it — unauthenticated requests get `401`.
3. Fast-acks with `200` and runs the agent turn in the background, then sends
   the reply back via the Bot Connector REST API using an app-credential token.
4. Enforces a **deny-by-default allow-list** of Azure AD UPNs/emails, and
   **direct/personal chat only**.

## Prerequisites

- A public HTTPS URL that reaches your gateway. Options:
  - A reverse proxy (nginx/Caddy) terminating TLS in front of the gateway.
  - Hosting the gateway on a VM/App Service with a public hostname.
  - A dev tunnel for local testing: `devtunnel host -p 5476` (Microsoft Dev
    Tunnels) or `ngrok http 5476`.
- The Teams extra installed (for inbound JWT validation):

  ```bash
  pip install "kirocrew[teams]"
  ```

  If the channel is enabled without this extra, KiroCrew logs an actionable
  error and skips Teams (the rest of the gateway still starts).

## 1. Register an Azure Bot

1. In the [Azure Portal](https://portal.azure.com), create an **Azure Bot**
   resource (or an App Registration + Bot Channels Registration).
2. Note the **Microsoft App ID** (Client ID). Create a **client secret** and
   note its value (the **App Password**).
3. For a **single-tenant** bot, also note the **Tenant ID**. For a
   **multi-tenant** bot, leave the tenant blank.
4. Under the bot's **Channels**, add the **Microsoft Teams** channel.
5. Set the bot's **Messaging endpoint** to your public URL plus the KiroCrew
   route:

   ```
   https://<your-public-host>/api/messaging/teams
   ```

## 2. Configure KiroCrew

Provide the credentials via environment variables (preferred) in
`~/.kiro/crew/.env`:

```
MICROSOFT_APP_ID=<your app id>
MICROSOFT_APP_PASSWORD=<your client secret>
MICROSOFT_APP_TENANT_ID=<your tenant id or leave unset for multi-tenant>
```

Then enable the channel and add your allow-list in `~/.kiro/crew/config.json`:

```json
{
  "teams": {
    "enabled": true,
    "allowed_emails": ["you@yourcompany.com"]
  }
}
```

- `allowed_emails` — Azure AD UPNs/emails **or AAD object ids** permitted to
  DM the bot. Teams activities reliably carry the sender's object id (email is
  often absent), so listing object ids works out of the box; emails are matched
  when Teams supplies them. **Empty = deny everyone** (fail closed); a Teams bot
  is reachable by anyone in the org, so this list is what gates access.
- `app_id` / `app_password` / `tenant_id` may also be set here instead of the
  environment, but the credential env vars take precedence and keep secrets out
  of the config file.
- `soft_threshold_pct` / `hard_threshold_pct` — context-window nudges
  (defaults 80 / 95), same as the other channels.

## 3. Package the Teams app and start a chat

1. Build a Teams app manifest that references your bot's App ID (Teams
   Developer Portal → **Apps** → **New app**, add a **Bot** with your App ID,
   scope **Personal**).
2. Side-load the app package into Teams (**Apps → Manage your apps → Upload a
   custom app**), or have a tenant admin publish it to your org.
3. Open a 1:1 chat with the bot and send a message.

## Verifying

- `kirocrew doctor` and the dashboard **Settings → Teams** badge report whether
  the channel connected (the badge turns green once the outbound app
  credentials validate).
- The status endpoint is `GET /api/teams/config`.
- Slash commands in the chat: `/new` (fresh conversation), `/compact` (compress
  context), `/help`.

## Security notes

- The webhook is **exempt from the dashboard cookie gate** because it performs
  its own Bot Framework JWT validation — a request with a missing or invalid
  token is rejected with `401` before any processing.
- Authorization is **deny-by-default** on both the email allow-list and the
  conversation scope (personal only). All denials are recorded in the security
  event log.
- The App Password and all bearer tokens are treated as secrets: the
  `app_password` config field is marked sensitive and never logged.

## Limitations (this release)

- 1:1 personal chat only — no team channels, group chats, or @mention handling.
- No Adaptive Cards / tappable buttons (a `[OPTIONS: …]` prompt is delivered as
  plain text; just reply normally).
- No token-by-token streaming — the bot shows a typing indicator and delivers
  the final answer in one message (chunked if very long).
