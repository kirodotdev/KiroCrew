# WeChat (Weixin) integration

Connect Kiro Crew to **personal WeChat** (微信) through Tencent's **iLink bot API**.
Sign-in is a QR scan from the dashboard; messages arrive over HTTP long-poll, so
no public endpoint, webhook, or tunnel is required.

> **This is not WeCom.** For enterprise WeChat (企业微信) use the
> [WeCom channel](./wecom-integration.md) instead — it uses an official
> WebSocket bot gateway. This page covers *personal* WeChat via iLink.

## Risks — read before enabling

- **Unofficial surface.** The iLink bot API is not a published Tencent developer
  product. Behaviour can change without notice, and using a personal account as
  a bot may violate WeChat's terms of service. Accounts can be limited or banned.
  Prefer WeCom for anything business-critical.
- **DM only.** QR login binds an *iLink bot identity* (e.g. `a5ace6fd482e@im.bot`),
  which generally does **not** receive ordinary WeChat group events. Group chat is
  therefore unsupported — there is no policy you can set to enable it.
- **One poller per credential.** Only a single gateway may long-poll a given bot
  credential at a time.

## Setup

1. Open **Settings → Channels → WeChat**.
2. Click **Connect via QR**.
3. Scan the code with the WeChat mobile app and confirm on your phone.
4. Restart the gateway. The status pill turns **Connected**.

The bot credential is written server-side (into the credential store, never
returned to the browser); only the non-secret account id and connection status
are exposed to the dashboard.

## Access policy

`Who can message the bot` maps to `weixin.dm_policy`:

| Value | Behaviour |
|---|---|
| `allowlist` | Only user IDs in `weixin.allowed_user_ids` (**default**) |
| `open` | Anyone who messages the bot |
| `disabled` | All messages ignored |

The default is **`allowlist` with an empty list, which authorizes nobody** — a
freshly connected bot ignores every DM until you add an id. This matches the
other channels (Telegram/Discord are fail-closed the same way). An unrecognized
value also denies. To collect a user ID, have the person DM the bot once and read
the id from the gateway log.

## Commands

| Command | Effect |
|---|---|
| `/new`, `新对话`, `清空` | Start a fresh session |
| `/compact` | Compact the session context in place |

Context length is managed automatically: at `weixin.soft_threshold_pct` (80%) the
bot suggests `/compact` or `/new`; at `weixin.hard_threshold_pct` (95%) it
compacts on its own.

## Behaviour notes

- **One reply per turn.** iLink cannot edit a sent message, so the answer is
  buffered and delivered when the turn completes rather than streamed. The native
  "typing…" indicator runs for the duration.
- **Markdown is preserved** — iLink clients render it. Replies longer than 4000
  characters split at paragraph/code-fence boundaries.
- **Tool calls and reasoning are not echoed** to the chat (they would each become
  a separate bubble).
- **No approval buttons.** iLink has no interactive controls, so the channel runs
  the approval ladder without a decider: `interactive` mode is deny-by-default,
  while `auto` / `trust` behave normally.

## Configuration reference

Set under `weixin` in `config.json` (or via the Settings panel):

| Key | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable the channel |
| `account_id` | — | iLink bot account id (from QR login) |
| `base_url` | `https://ilinkai.weixin.qq.com` | iLink API base URL |
| `dm_policy` | `allowlist` | `allowlist` / `open` / `disabled` (default denies until an id is added) |
| `allowed_user_ids` | `[]` | Permitted user IDs when `allowlist` |
| `soft_threshold_pct` | `80` | Suggest compaction past this usage |
| `hard_threshold_pct` | `95` | Force compaction past this usage |

The credential is read from the `WEIXIN_TOKEN` credential (env / `.env` /
credential store) and overrides `weixin.token`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Status stuck on "Signed in — restart to connect" | Restart the gateway; credentials are read once at boot |
| `errcode -14` in the log | Session expired — re-run the QR sign-in |
| `errcode -2` | iLink rate limit; the poller backs off automatically |
| Bot ignores DMs | Check `dm_policy` — under `allowlist` the sender must be listed |
| Group messages ignored | Expected: iLink bot identities do not receive group events |
| Images / voice / files ignored | Expected: media is not supported yet (text only) |
| Startup error about `aiohttp` | Install the messaging extra |

## Attribution

The iLink protocol layer (endpoints, auth headers, QR login, long-poll cursor)
was ported from Nous Research's Hermes Agent `weixin` adapter, MIT licensed —
<https://github.com/NousResearch/hermes-agent>.
