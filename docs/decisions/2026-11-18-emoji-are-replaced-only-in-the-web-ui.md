# Emoji are replaced with icons only where a person sees the web UI

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-11-18

## Decision

Emoji are replaced with lucide icons only in text a person sees in the web UI:
the dashboard itself, and the backend text the dashboard shows (the dashboard
server's messages and tool replies, the tool-row, notice and sub-agent marks,
cron and heartbeat notification titles and chat rows, and the rest of the
backend text the web UI shows). The command line, `kirocrew doctor`, CI output,
Slack and the other chat apps (Telegram, Discord and the rest) may keep emoji,
and no CI check blocks an emoji there. The web UI rule against emoji as icons,
and its CI check, are unchanged.

## Why

- Emoji read fine in a terminal and in a chat app.
- In the web UI they clash with the theme and the icon set, so the UI uses
  lucide icons.

## Evidence

- https://github.com/kirodotdev/KiroCrew/pull/39210 -- Zezhen's pull request
  that states the decision and removes the backend emoji CI check added in
  https://github.com/kirodotdev/KiroCrew/pull/38278.
- https://github.com/kirodotdev/KiroCrew/pull/38097 -- the web UI change and
  its CI check, which stand.
- https://github.com/kirodotdev/KiroCrew/pull/38842,
  https://github.com/kirodotdev/KiroCrew/pull/38861 and
  https://github.com/kirodotdev/KiroCrew/pull/38821 -- the backend text the web
  UI shows, which stays without emoji.
- https://github.com/kirodotdev/KiroCrew/pull/39228 (command line),
  https://github.com/kirodotdev/KiroCrew/pull/39226 (`kirocrew doctor`),
  https://github.com/kirodotdev/KiroCrew/pull/39230 (Slack, keeping plain the
  lines the web UI also shows) and
  https://github.com/kirodotdev/KiroCrew/pull/39222 (other chat apps) -- the
  pull requests that bring the emoji back.
- https://github.com/kirodotdev/KiroCrew/issues/39269 -- the issue for this
  entry.
