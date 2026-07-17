# Mobile Dashboard Access (Tunnels)

Access the KiroCrew dashboard from your phone by exposing the local
dashboard port (5476) through a secure named tunnel and pointing the
Slack bot's presigned links at the tunnel URL.

## How It Works

The dashboard binds to `localhost:5476` on whatever host runs KiroCrew.
To reach it from a phone, you put a tunnel in front of that local port.
A tunneling service (e.g. [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/),
[ngrok](https://ngrok.com/), or [Tailscale Funnel](https://tailscale.com/kb/1223/funnel))
gives you a stable public HTTPS URL that proxies to your local port. You
then set that URL as `dashboard.url` so the Slack bot generates links
your phone can open.

> **Security note:** A tunnel exposes your dashboard to the public
> internet. KiroCrew protects the dashboard with a per-session token
> (the presigned `?token=...` link), but you should still prefer a tunnel
> provider that adds its own authentication layer (e.g. Cloudflare Access,
> Tailscale's private network) when possible, and keep token lifetimes
> short.

## Prerequisites

- KiroCrew running on a host (local machine or a remote host — see
  [REMOTE_DESKTOP_SETUP.md](REMOTE_DESKTOP_SETUP.md))
- A tunneling tool installed on that host. This guide uses
  [cloudflared](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/)
  as the example, but any tool that produces a stable HTTPS URL works.

## Setup

### 1. Create a named tunnel

A *named* tunnel produces a stable URL that doesn't change between
restarts. With cloudflared, for example:

```bash
# One-time: authenticate and create the named tunnel
cloudflared tunnel login
cloudflared tunnel create kirocrew

# Route a hostname you control to the tunnel, then run it pointed at port 5476
cloudflared tunnel route dns kirocrew kirocrew.example.com
cloudflared tunnel --url http://localhost:5476 run kirocrew
# → https://kirocrew.example.com
```

With ngrok the equivalent is `ngrok http --domain=kirocrew.example.com 5476`.
Whatever tool you use, the goal is the same: a stable HTTPS URL that
proxies to your local KiroCrew dashboard port (5476).

### 2. Configure the dashboard URL

Set `dashboard.url` in `~/.kirocrew/config.json` to the tunnel URL:

```json
{
  "dashboard": {
    "url": "https://kirocrew.example.com"
  }
}
```

This tells the Slack bot to generate presigned links using the tunnel URL instead of `localhost`.

### 3. Keep the tunnel running

The tunnel must stay connected for mobile access to work. Run it in a
tmux session, as a background process, or as a systemd service:

```bash
tmux new -s tunnel
cloudflared tunnel --url http://localhost:5476 run kirocrew
# Ctrl+B, D to detach
```

Most tunnel providers also ship a service installer (e.g. `cloudflared
service install`) so the tunnel auto-starts on boot — recommended for
24/7 setups.

### 4. Restart the gateway

```bash
kirocrew restart   # or: kirocrew gateway
```

## Usage

1. Type `/kirocrew dashboard` (or `/kirocrew dashboard 6h` for a longer session) in your KiroCrew Slack DM
2. The bot DMs you a presigned link: `https://<tunnel-url>/?token=...`
3. Tap the link on your phone — it opens the dashboard in your mobile browser

## Session Duration

| Layer | Duration | Notes |
|-------|----------|-------|
| **`/kirocrew dashboard` token** | **1 hour (default)** | **Practical limit** — configurable: `/kirocrew dashboard 6h`, max `/kirocrew dashboard 20h` |
| Presigned link click window | 5 minutes | Must click before it expires |

The **dashboard presigned token** is the session limit. Use
`/kirocrew dashboard 6h` or `/kirocrew dashboard 20h` for longer sessions.
When the token expires, generate a new link with `/kirocrew dashboard`.

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `/kirocrew dashboard` link still shows `localhost` | Verify `dashboard.url` is set in config.json and restart the gateway |
| Link opens but shows "token expired" | Click within 5 minutes of generating. Use `/kirocrew dashboard` again for a fresh link |
| Tunnel disconnects | Reconnect the tunnel — a *named* tunnel reuses the same URL on restart |
| Dashboard loads but layout is broken | Ensure you're on the latest KiroCrew; the dashboard ships a mobile-responsive layout |
| Phone can't reach the URL | Verify the tunnel process is running and connected on the host serving KiroCrew |

## References

- [REMOTE_DESKTOP_SETUP.md](REMOTE_DESKTOP_SETUP.md) — remote host setup guide
- [cloudflared tunnels](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) — install, usage, architecture
- [ngrok](https://ngrok.com/docs) — alternative tunneling tool
- [Tailscale Funnel](https://tailscale.com/kb/1223/funnel) — expose a local service over your tailnet
