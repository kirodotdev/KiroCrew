# WakaTime read auth moves to OAuth; the API key keeps only the write path

## Context

WakaTime announced that on 1 November 2026 an API key becomes write-only. A key keeps working inside a plugin to send coding activity, and stops working for every other endpoint. Reading stats, summaries, durations, or the current user over an API key returns 401 after that date. To keep reading a user's own data an app must use an OAuth 2.0 access token.

Kiro Crew's WakaTime integration authenticates every call with the API key, through a single `Authorization: Basic base64(<api_key>:)` header built in `WakaTimeClient._headers()`. Four of the five verbs are reads and break on 1 November: `verify` (`GET /users/current`), `get_summaries`/`fetch_summaries`, `get_stats`/`fetch_stats`, and `get_durations`. Only `send_heartbeats` (`POST /users/current/heartbeats.bulk`) is a write and keeps working on the key.

The reads feed real surfaces: the Overview tab's stats panel and language bars (`WakaTimeTab.tsx`, served by `GET /api/wakatime/stats`) and the billable-hours export (`GET /api/wakatime/export`). Those go dark on 1 November unless read auth moves to OAuth.

## Decision

Read calls use an OAuth 2.0 bearer token when one is configured. The API key stays as the auth for `send_heartbeats`, and as the fallback for reads before 1 November and for self-hosted backends that have no OAuth provider.

`_headers()` chooses per request: a bearer header when an OAuth access token is present, else the existing Basic-key header. The five verbs do not change. One auth seam moves; the REST core, the timeout and 429 handling, the degrade-to-empty read contract, and the strict-verb failure contract all stay as they are.

The two auth modes coexist on purpose. Forcing OAuth-only would break two groups at once: anyone still on a key before the cutoff, and self-hosted Wakapi or Hackatime users. The integration already supports a self-hosted backend through `config.wakatime.api_base_url`, and those backends expose the v1 REST surface over an API key with no OAuth provider of their own. OAuth is for reads against wakatime.com; the key remains valid everywhere it still works.

### Auth selection per call

| Verb | Method, path | Auth after 1 Nov |
|---|---|---|
| `verify` | `GET /users/current` | OAuth bearer (key is 401) |
| `get_summaries` / `fetch_summaries` | `GET /users/current/summaries` | OAuth bearer |
| `get_stats` / `fetch_stats` | `GET /users/current/stats/{range}` | OAuth bearer |
| `get_durations` | `GET /users/current/durations` | OAuth bearer |
| `send_heartbeats` | `POST /users/current/heartbeats.bulk` | API key (write path keeps the key) |

Against a self-hosted backend (`api_base_url` set, no OAuth configured) every verb stays on the API key.

## The OAuth flow

The provider endpoints, verified against WakaTime's developer docs:

- `https://wakatime.com/oauth/authorize`: redirect the user here. Required args `client_id`, `response_type=code`, `redirect_uri`. Optional `scope`, `state`, `force_approve`.
- `https://wakatime.com/oauth/token`: a server-side POST to exchange the code. Required `client_id`, `client_secret`, `redirect_uri` (same as the authorize step), `grant_type=authorization_code`, `code`. Returns an access token, a refresh token, and an expiry.
- `https://wakatime.com/oauth/revoke`: a server-side POST to invalidate a token on disconnect. Required `client_id`, `client_secret`, `token`.

Refresh: when the access token has expired, POST to `/oauth/token` with `grant_type=refresh_token`, `client_id`, `client_secret`, `redirect_uri`, and the stored `refresh_token`, which returns a fresh access token without prompting the user again. The client refreshes before a read rather than after a 401, and treats a 401 on a read as a signal to refresh once and retry before surfacing an auth error.

Scopes requested are the minimum the reads need, per the security standard's least-privilege rule. `read_stats` and `read_summaries` cover the stats panel, the language bars, and the export; `read_summaries` alone covers the summaries-based export, so the stats panel is what pulls in `read_stats`. Durations need `read_heartbeats`, which is broader, so that scope is requested only if the durations read is kept in scope. No `write_*` or `email` scope is requested: heartbeats keep the key, and the integration has no use for the user's email.

## Credentials and storage

The client secret and the user's tokens are credentials and live in the encrypted vault, next to the existing `WAKATIME_API_KEY`, never in `.env`. The service note on `resolve_api_key` already records why: a key in `.env` is loaded into the process environment, and the agent-spawn scrub only strips credential names it knows, so an unknown key can reach a subprocess. The same reasoning holds for an access token and a refresh token, so they take the same vault-only path.

New vault entries, following the existing `WAKATIME_API_KEY` constant:

- `WAKATIME_OAUTH_CLIENT_SECRET`: the app's client secret.
- `WAKATIME_OAUTH_ACCESS_TOKEN`: the current access token.
- `WAKATIME_OAUTH_REFRESH_TOKEN`: the refresh token.

New non-secret config on `WakaTimeConfig` (the client ID and expiry are not secrets):

- `oauth_client_id`: the app's client ID, from wakatime.com/apps.
- `oauth_token_expires_at`: epoch seconds the access token expires, so the client can refresh before a call rather than discover expiry through a 401.

The `app_secret` revoke-all behavior and the state-parameter check against CSRF follow WakaTime's documented flow; the `state` value is generated per authorize request and verified on the callback.

## Rollout

Three scoped changes, each its own pull request, in dependency order:

1. Client auth seam. `_headers()` chooses bearer-or-Basic; a token accessor and a bounded refuse-then-refresh path on a read 401. No new config surface beyond reading the tokens. Covered by unit tests that pin the header choice for each mode and the refresh-on-expiry path.
2. OAuth flow and token storage. The authorize redirect, the callback handler that exchanges the code, encrypted token storage, the refresh call, and revoke-on-disconnect. New config fields and vault entries land here.
3. Settings and onboarding UX. A Connect with WakaTime button for reads, the connected or disconnected state, and the key field kept for the heartbeat-send path. A dashboard surface change, so the screenshot gate applies.

The deadline is 1 November 2026. Changes 1 and 2 are the ones that must land before then for reads to keep working; change 3 can follow if the flow is reachable another way in the interim.

## Why not

- OAuth-only, drop the key. Breaks pre-cutoff key users and every self-hosted backend, which have no OAuth provider. The write path also still wants the key.
- Refresh only after a 401. Costs a failed read and a retry on every expiry. The token carries its own expiry, so refreshing ahead of a call is cheaper and the 401 path stays as a backstop for a token revoked out of band.
- A broad scope grant. `read_logged_time` or a blanket read scope would cover everything in one request, but the standard requires least privilege, and the reads need only stats and summaries, plus heartbeats read for durations if that verb stays.
