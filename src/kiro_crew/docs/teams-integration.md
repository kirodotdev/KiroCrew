# Microsoft Teams Integration

Kiro Crew can run as a Microsoft Teams bot, so you can DM your agent from Teams
the same way you would from Slack, Telegram, Discord, Webex, or WeCom.

> **This release is DM-only and self-hosted.** You chat with the bot in a **1:1
> personal chat**. Messages sent in team channels or group chats are refused
> (fail closed) so tool output is never exposed to unauthorized members.
> Because Teams uses the Bot Framework model, Kiro Crew exposes an **inbound
> HTTPS webhook** and you must give your gateway a **public HTTPS URL** (unlike
> the other channels, which open an outbound connection and need no public
> URL).

## How it works

Unlike Slack (Socket Mode) or Webex (device WebSocket), the Microsoft Bot
Framework **pushes** activities to a messaging endpoint you host. Kiro Crew:

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
  - Hosting the gateway on a VM/App Service with a public hostname. Set
    `dashboard.host` (or `dashboard_url`) to that hostname — the gateway rejects a
    `Host` header it does not serve, as a DNS-rebinding barrier.
  - A dev tunnel for local testing: `devtunnel host -p 5476` (Microsoft Dev
    Tunnels) or `ngrok http 5476`.

  The gateway itself speaks HTTP, so something must terminate TLS in front of it;
  Teams will not deliver to a non-HTTPS endpoint.
- The JWT dependency installed (for inbound token validation), in the
  environment that runs the gateway:

  ```bash
  pip install "PyJWT[crypto]==2.13.0"
  ```

  This is the whole of the `teams` extra. Installing the dependency directly is
  the form that works on every layout: Kiro Crew is not published on PyPI, so
  `pip install "kirocrew[teams]"` cannot resolve, and the direct-URL form below
  reinstalls Kiro Crew itself rather than just adding the dependency.

  If the channel is enabled without it, Kiro Crew logs an actionable error and
  skips Teams (the rest of the gateway still starts).

 > [!NOTE]
 > The error `invalid-egg-fragment` occurs because modern `pip` (v22+) deprecated and removed support for using `#egg=package_name[extra]` in direct Git URLs.
 >
 > Use PIP's **Direct URL requirement syntax** (`package[extra] @ git+https://...`) to install the package along with extras:
 >
 > ```bash
 > pip install "kirocrew[teams] @ git+https://github.com/kirodotdev/KiroCrew.git"
 > ```
 > ### Alternative Solutions
 >
 > #### Option 1: Install Subdirectory / Local Clone (Editable Mode)
 >
 > If you have cloned the [Kiro Crew GitHub repository](https://github.com/kirodotdev/KiroCrew) locally, navigate to the repo root and run:
 >
 > ```bash
 > pip install -e ".[teams]"
 > ```
 >
 > #### Option 2: Fallback to Two Steps
 >
 > If you run into issues with Git dependency syntax, you can install the repository root first and then install the required `teams` dependencies directly (such as `pyjwt` and `cryptography` required for Azure Bot Framework JWT validation):
 >
 > ```bash
 > pip install "git+https://github.com/kirodotdev/KiroCrew.git"
 > pip install pyjwt cryptography
 > ```

## 1. Register an Azure Bot

1. In the [Azure Portal](https://portal.azure.com), create an **Azure Bot**
   resource (or an App Registration + Bot Channels Registration).
2. Note the **Microsoft App ID** (Client ID). Create a **client secret** and
   note its value (the **App Password**).
3. For a **single-tenant** bot, also note the **Tenant ID**. For a
   **multi-tenant** bot, leave the tenant blank.
4. Under the bot's **Channels**, add the **Microsoft Teams** channel.
5. Set the bot's **Messaging endpoint** to your public URL plus the Kiro Crew
   route:

   ```
   https://<your-public-host>/api/messaging/teams
   ```

## 2. Configure Kiro Crew

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
- `app_id` and `tenant_id` may also be set here instead of the environment; the
  matching env vars take precedence.
- **`app_password` is env-only.** It is read from `MICROSOFT_APP_PASSWORD`
  (env or `.env`) and is deliberately **not** loaded from `config.json`, which the
  agent can read — putting it there has no effect and the channel will not start.
- `soft_threshold_pct` / `hard_threshold_pct` — context-window nudges (defaults `80` / `95`), same as the other channels.
- `session_folder` — optional dashboard sidebar folder for Teams conversations (default `""`, which leaves them unfiled).

Defaults: `enabled` is `false`; `app_id` and `tenant_id` are `""`; `allowed_emails` is `[]`; and `app_password` is `""` until `MICROSOFT_APP_PASSWORD` supplies it. `app_password` remains env-only even though the in-memory config field has that default.

Transport capabilities: streaming and reactions are disabled; editing, inbound and outbound files, rich blocks, and proactive sends are enabled; threads are disabled. Text chunks are capped at 16,000 characters and interactive prompts at five buttons.

## 3. Package the Teams app and start a chat

1. Build a Teams app manifest that references your bot's App ID (Teams
   Developer Portal → **Apps** → **New app**, add a **Bot** with your App ID,
   scope **Personal**).
2. **Set the bot's `supportsFiles` property to `true`.** This is required, not
   optional: Microsoft states that without it the file features do not work, and
   receiving files in a personal chat is one of them. Miss it and the bot never
   sees a PDF, Word document or text file you send — with no error and no refusal
   line, while pasted inline images keep working, so the failure looks
   intermittent. (Microsoft does not support Teams file send/receive in GCC High,
   DoD or 21Vianet deployments at all.)
3. Side-load the app package into Teams (**Apps → Manage your apps → Upload a
   custom app**), or have a tenant admin publish it to your org.
4. Open a 1:1 chat with the bot and send a message.

## Verifying

- The dashboard **Settings → Teams** badge reports whether the channel connected:
  it turns green once the outbound app credentials validate, and turns red with a
  reason if a later send fails (a delivered message clears it again).
- The status endpoint is `GET /api/teams/config`.
- Gateway logs record `Teams channel started` on success, and an actionable error
  when the channel is enabled but a prerequisite is missing.

## Commands

Send `/help` in the chat for the current list. Today:

| Command | Effect |
|---|---|
| `/new` (`/start`) | Start a fresh conversation, dropping anything still queued |
| `/compact` | Compress the conversation context |
| `/stop` (`/cancel`) | Stop the reply in progress and clear the queue |
| `/yolo on\|off\|renew` | Auto-approve tools **everywhere** until it expires — see below |
| `/link` / `/unlink` | Resume / stop mirroring dashboard replies into this chat |
| `/sessions [search words]` | Continue a recent dashboard chat here (owner only) |
| `/dashboard [<N>h\|<N>m]` | Get a **dashboard login link** — see Security notes |
| `/help` | Show the command list |

Right-click → **Reply** works: Teams prepends the quoted message to what it sends,
so the quoted text is stripped and only your own words reach the agent — which is
also why a quote-replied `/stop` still stops the reply.

While a reply is running, prefix a message to control it: `/queue <message>`
answers it after the current reply, `/steer <message>` folds it into the reply in
progress. A message sent mid-turn is never lost — if it cannot be folded in, it is
held and shown in a single "⏳ Queued (N)" receipt that is edited in place, then
answered as one combined turn.

### Continuing a dashboard conversation

`/sessions` lists your recent dashboard chats as buttons; press one and it continues in
this Teams chat, with the last few messages replayed so you can see where you left off.
`/sessions <words>` searches them — over message content as well as titles, so a phrase
you remember from the conversation finds it. `/unlink` (or `/new`) comes back to your own
Teams conversation.

It is **owner-only**: a dashboard session is your whole working transcript, so the list
is available only when `teams.allowed_emails` holds exactly one address. With more than
one configured, `/sessions` refuses for everybody rather than exposing one person's chats
to the rest of the list.

Two things are deliberate and worth knowing:

- A session already open in another channel, or another session already attached to this
  chat, is refused with the reason rather than silently taken over — run `/unlink` first.
- If the link is broken out of band (you closed the dashboard tab, or the session was
  deleted), your next message is **refused rather than quietly answered by a different
  conversation**, and it tells you which session went away. `/sessions` and `/new` keep
  working so you always have a way out.

### Approving tools

When the agent wants to run a tool, you get a card with **Approve**, **Approve +
auto-approve** and **Deny**. Approve allows that one tool; Deny refuses it. If
nobody answers within five minutes the tool is **denied** — the safe default.

Buttons from an older conversation stop working on purpose: each prompt carries a
one-time token, so a card you scroll back to and tap tells you it is no longer
waiting instead of approving something you did not mean to.

**Approve + auto-approve** stops the asking — and it is the same switch as `/yolo
on` and the dashboard's YOLO toggle, which is why the button says so rather than
calling itself "Trust session". There is deliberately only ONE auto-approve grant in
Kiro Crew: turning it on here turns it on for your dashboard chats and scheduled
jobs too, until it expires. Two grants with two lifetimes would mean two answers to
"is auto-approve on?", and the wrong answer to that question is the expensive one.
So treat it accordingly — and note that everyone on `allowed_emails` can press it.

`/yolo` reports the state and how long is left, `/yolo on` and `/yolo renew` arm or
extend it, and `/yolo off` turns it off everywhere.

None of this disables the security gate: the sensitive-path keystone, the
enterprise governance ceiling, and the destructive-command deny-list all run ahead
of auto-approval, so anything denied by policy stays denied.

## Security notes

- The webhook is **exempt from the dashboard cookie gate and from the CSRF origin
  check, for `POST` only**, because it performs its own Bot Framework JWT
  validation — signature against the Bot Framework signing keys, `aud` equal to
  your App ID, issuer, and expiry (with the standard five-minute clock skew). A
  request with a missing or invalid token is rejected with `401` before the
  activity is acted on — the body is read and parsed first, under a size cap, so
  that the route can bound it. The exemption is required, not a convenience: the
  Connector is a server-to-server caller that sends no `Origin` header, and the
  CSRF check has no setting that would admit it.
- The activity's `channelId` is checked **positively** against `msteams`. An Azure
  Bot resource has Web Chat enabled by default and can carry Direct Line; both
  reach the same endpoint with a token this validator accepts, so anything that is
  not Teams is refused and audited.
- The reply target is bound to the `serviceUrl` **inside the validated token**, so
  a replayed activity cannot redirect your bot's credential at another host.
- The request body is size-capped and a redelivered activity is dropped as a
  duplicate. Repeated failed authentications are throttled **when the gateway is
  reached directly**; the throttle is skipped for a proxied request (it would
  otherwise key on the proxy), so the JWKS refetch an unknown signing key triggers
  is rate-limited separately, next to the fetch itself.
- Authorization is **deny-by-default** on both the allow-list and the conversation
  scope (personal only). All denials are recorded in the security event log.
- **`/dashboard` issues a login credential.** It mints a presigned dashboard
  session URL for the asking user (default 1 hour, capped at 24), and issuance is
  recorded in the security event log. Everyone on `allowed_emails` can do this, so
  treat that list as the set of people you would hand a dashboard session to.
- The App Password and all bearer tokens are treated as secrets: the
  `app_password` field is env-only and never logged, the `MICROSOFT_APP_*`
  variables are hidden from agent subprocesses, and a preview pod force-disables
  Teams so it can never drive your real bot.
- An inbound attachment is fetched only after the scope and allow-list gates pass,
  the bot's Connector token is offered only to recognized Microsoft hosts, and a URL
  that resolves into a private, loopback or link-local range is refused — so an
  activity cannot turn the gateway into a proxy for your internal network.

## Limitations (this release)

- 1:1 personal chat only — no team channels, group chats, or @mention handling.
  A reply in a channel would expose tool output to people who are not on your
  allow-list, so non-personal scopes are refused.
- **No token-by-token streaming.** Teams' native streaming is 1:1-only, throttled
  to one update per second, and cut off after two minutes, which an agent turn
  routinely exceeds — a stream that dies mid-answer is worse than one complete
  message. Instead you get a typing indicator, a progress message that updates
  while tools run, and then the final answer in that same message.
- **Only images can be sent as files.** A PNG or JPEG the agent produces arrives as
  an inline attachment, up to 1 MB each and four per reply. Any other file type —
  including GIF, WebP and BMP, which Teams either will not render inline or does not
  list — is refused with the reason shown and its path left visible in the reply.
  Sending anything else needs Teams' file-consent round trip, which this channel's
  webhook does not handle.
- **Files you send are read**, in a 1:1 chat: an image, a plain-text file or a
  document (PDF, Word, and the rest of the parseable set) is downloaded and handed
  to the turn, and a voice message is transcribed when speech-to-text is available.
  Video is refused with a visible note, as is anything too large or of an
  unsupported type — nothing is dropped silently.
- **A plain Teams message renders only part of Markdown** — bold, italic,
  inline/preformatted code, blockquotes and links work; headings, lists, tables
  and images do not. Long replies are split without breaking code fences.
- No emoji reaction on a steered message (a bot cannot add reactions in Teams), so
  a folded-in message is acknowledged with a short reply instead.
- **No model picker yet.** Telegram can switch models from chat; Teams cannot. Use the
  dashboard. (`/sessions` — continuing a dashboard chat here — IS supported; see below.)
- **The `send_message` agent tool does not reach Teams.** A cron result DOES arrive
  here when its originating dashboard chat is mirrored into this conversation (that is
  what `/link` binds). What is Slack-only is the explicit `send_message` tool call
  with a channel or user target — its addressing, allow-list and threading are Slack
  concepts — so a Teams-only install should rely on the mirror rather than on that
  tool's own delivery.
