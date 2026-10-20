# WakaTime read auth moves to OAuth; the API key keeps only the write path

## Context

WakaTime announced that on 1 November 2026 an API key becomes write-only. A key keeps working inside a plugin to send coding activity, and stops working for every other endpoint. Reading stats, summaries, durations, or the current user over an API key returns 401 after that date. To keep reading a user's own data an app must use an OAuth 2.0 access token.

Kiro Crew's WakaTime integration has two callers, and each builds its own `WakaTimeClient`:

- The gateway's read path. `wakatime/service.py` `build_client()` builds the client the dashboard handlers use: `fetch_stats` behind `GET /api/wakatime/stats` (the Overview tab's stats panel and language bars in `WakaTimeTab.tsx`) and `fetch_summaries` behind `GET /api/wakatime/export` (the billable-hours export). Those go dark on 1 November unless read auth moves to OAuth.
- The heartbeat sender. `wakatime/heartbeats.py` builds its own key-only client for `send_heartbeats` (`POST /users/current/heartbeats.bulk`), which is a write and keeps working on the key.

The client's other read verbs, `verify` (`GET /users/current`) and `get_durations` (`GET /users/current/durations`), have no product caller today.

## Decision

Read calls use an OAuth 2.0 bearer token when one is configured. The API key stays as the auth for `send_heartbeats`, and as the fallback for reads before 1 November and for self-hosted backends that have no OAuth provider.

The auth mode is chosen per client, not per request. This is what #19810 shipped: `WakaTimeClient` takes an optional `oauth_access_token` at construction, and `_headers()` signs every call that client makes with `Authorization: Bearer <token>` when a token is set, else with the existing Basic-key header. A client built with a token signs every verb with it, `send_heartbeats` included, and the client tests pin exactly that.

So the split between reads and writes is made by which client a caller builds:

- `build_client()` passes the OAuth access token when one is stored, so the dashboard reads go over OAuth.
- The heartbeat sender keeps building its client without a token, so heartbeats stay on the key. It must not be handed the OAuth token: a token signing heartbeats would need the `write_heartbeats` scope, which this integration does not request.

The verb names and signatures do not change. The REST core, the timeout and 429 handling, and the degrade-to-empty contract of the plain `get_*` reads stay as they are. One part of the strict-verb failure contract changes in change 2, described under "Telling an auth failure from no data" below.

The two auth modes coexist on purpose. Forcing OAuth-only would break two groups at once: anyone still on a key before the cutoff, and self-hosted Wakapi or Hackatime users. The integration already supports a self-hosted backend through `config.wakatime.api_base_url`, and those backends expose the v1 REST surface over an API key with no OAuth provider of their own. OAuth is for reads against wakatime.com; the key remains valid everywhere it still works.

### Auth by caller

| Caller | Client built with | Verbs it calls | Auth after 1 Nov |
|---|---|---|---|
| Dashboard reads (`build_client()`) | OAuth access token, when stored | `fetch_stats`, `fetch_summaries` | OAuth bearer |
| Heartbeat sender (`heartbeats.py`) | API key only, never a token | `send_heartbeats` | API key |

Against a self-hosted backend (`api_base_url` set, no OAuth configured) `build_client()` passes no token, so every call stays on the API key.

## The OAuth flow

The provider endpoints, verified against WakaTime's developer docs:

- `https://wakatime.com/oauth/authorize`: redirect the user here. Required args `client_id`, `response_type=code`, `redirect_uri`. Optional `scope`, `state`, `force_approve`.
- `https://wakatime.com/oauth/token`: a server-side POST to exchange the code. Required `client_id`, `client_secret`, `redirect_uri` (same as the authorize step), `grant_type=authorization_code`, `code`. Returns an access token, a refresh token, and an expiry.
- `https://wakatime.com/oauth/revoke`: a server-side POST to invalidate a token on disconnect. Required `client_id`, `client_secret`, `token`.

Refresh: when the access token has expired, POST to `/oauth/token` with `grant_type=refresh_token`, `client_id`, `client_secret`, `redirect_uri`, and the stored `refresh_token`, which returns a fresh access token without prompting the user again.

Refresh lives in the service layer, not in the client. #19810 deliberately left refresh out of `WakaTimeClient`: a client holds one fixed token for its lifetime. `build_client()` reads the stored expiry and refreshes before it builds the client when the token has expired or is about to. A 401 on a read is the backstop for a token revoked out of band: the read handler surfaces it as an auth error that asks the user to reconnect, and the service does not retry it in a loop.

### Telling an auth failure from no data

The dashboard reads already use the strict verbs, so a failed read is never shown as an empty week. `fetch_stats` and `fetch_summaries` raise `WakaTimeUnavailableError` when `_api` returns nothing, and the handlers answer 502 `upstream_unavailable`. What they cannot do today is tell a 401 from an outage: `_api` logs the status and returns `None` for any non-2xx, so both raise the same error.

Change 2 adds that split in the strict verbs only. `_api` hands the strict verbs the failing status, and `fetch_stats` and `fetch_summaries` raise `WakaTimeAuthError`, the class `verify` already raises, on a 401 or 403. Every other failure still raises `WakaTimeUnavailableError`. The handlers map `WakaTimeAuthError` to a distinct reconnect response. The plain `get_*` reads keep degrading to empty, since their callers want a quiet result and show no auth state.

Scopes requested are the minimum the reads need, per the security standard's least-privilege rule. `read_stats` covers `fetch_stats`, and `read_summaries` covers `fetch_summaries` and the summaries-backed today and yesterday ranges of `fetch_stats`. `read_heartbeats` is not requested: it is the broad scope that covers durations and raw heartbeats, and `get_durations` has no product caller. A later change that adds a durations caller must add that scope and a fresh consent with it. No `write_*` or `email` scope is requested: heartbeats keep the key, and the integration has no use for the user's email.

## Credentials and storage

The client secret and the user's tokens are credentials and live in the encrypted vault, next to the existing `WAKATIME_API_KEY`, never in `.env`. The service note on `resolve_api_key` already records why: a key in `.env` is loaded into the process environment, and the agent-spawn scrub only strips credential names it knows, so an unknown key can reach a subprocess. The same reasoning holds for an access token and a refresh token, so they take the same vault-only path.

New vault entries, following the existing `WAKATIME_API_KEY` constant:

- `WAKATIME_OAUTH_CLIENT_SECRET`: the app's client secret.
- `WAKATIME_OAUTH_TOKENS`: one entry holding the access token, the refresh token, and the access token's expiry (epoch seconds) together, as a JSON object.

The expiry is stored with the tokens, not in `config.json`. It changes on every refresh, and keeping it in `WakaTimeConfig` would rewrite the config file on every refresh and could fall out of step with the vault after a crash between the two writes. One vault entry commits all three values in a single atomic replace, so a reader never sees a new access token with an old expiry. Two dashboard reads can reach the refresh step at the same time, so change 2 adds a single-flight lock around it in `build_client()`. The handlers call `build_client()` through `asyncio.to_thread`, so the lock is a module-level `threading.Lock`. The thread that takes it re-reads `WAKATIME_OAUTH_TOKENS` first and refreshes only if the stored token is still expired. A thread that waited then finds the fresh token and builds its client with it. Without the lock, two refreshes could both write, and a stale refresh token could win. The vault has no compare-and-swap write for this: `set_with_revision` overwrites and returns the new revision. One lock in one process is enough, because only the gateway refreshes; the heartbeat sender never reads the tokens. A second process that refreshes would need a new conditional vault write, and that is out of scope here.

New non-secret config on `WakaTimeConfig`:

- `oauth_client_id`: the app's client ID, from wakatime.com/apps. It is public, since it appears in every consent URL.

The `state` parameter protects the callback against CSRF. A fresh random `state` is generated per authorize request, held server-side for that one request with a short expiry, and checked and consumed on the callback; a callback whose `state` is missing, unknown, or already used is refused before the code is exchanged.

## Rollout

Three scoped changes, each its own pull request, in dependency order:

1. Client auth seam. Landed in #19810. `WakaTimeClient` takes an optional `oauth_access_token`, and `_headers()` signs every call that client makes with a bearer header when it is set, else with Basic. No refresh and no new config surface. Unit tests pin the header for each mode, the empty-token fallback to Basic, and that a token client signs `send_heartbeats` with the bearer too.
2. OAuth flow and token storage. The authorize redirect with its `state`, the callback handler that checks `state` and exchanges the code, the `WAKATIME_OAUTH_TOKENS` and client-secret vault entries, the `oauth_client_id` config field, the refresh-before-build step in `build_client()` with its single-flight lock, the `WakaTimeAuthError` split in the strict reads, and revoke-on-disconnect. The heartbeat sender stays key-only.
3. Settings and onboarding UX. A Connect with WakaTime button for reads, the connected or disconnected state, and the key field kept for the heartbeat-send path. A dashboard surface change, so the screenshot gate applies.

The deadline is 1 November 2026. Change 2 is the one that must land before then for reads to keep working; change 3 can follow if the flow is reachable another way in the interim.

If change 2 has not shipped by 1 November, nothing breaks silently, and nothing extra ships to cover the gap. Heartbeats keep sending on the key. Each dashboard read gets a 401, the strict verb raises, and the handler answers 502 `upstream_unavailable`. The Overview tab shows its error state instead of stats, and the billable-hours export fails with that error instead of writing a file with zero hours. That error text already names a rejected key as a cause. Reads come back when change 2 ships and the user connects.

## Why not

- OAuth-only, drop the key. Breaks pre-cutoff key users and every self-hosted backend, which have no OAuth provider. The write path also still wants the key.
- Choose auth per verb inside the client. It would keep a token client from signing heartbeats, but it puts the read-or-write policy inside the REST client and needs both credentials in one object. Choosing per client keeps that policy with the caller, where the two callers are already separate, and is what #19810 shipped.
- Refresh inside the client, after a 401. Costs a failed read and a retry on every expiry, and needs the client to hold the refresh token and the client secret. The token carries its own expiry, so `build_client()` refreshes ahead of a call; the 401 stays a backstop for a token revoked out of band.
- Reuse the Connections OAuth path (`connections/oauth_clients.py`). Connections grants are kiro-cli's: kiro-cli runs the consent, holds the grant, and uses it to call a remote MCP server for an agent. Kiro Crew only checks that the grant file exists, and kiro-cli hands it no token. `oauth_clients.py` registers no callback and does no code exchange or refresh; it only writes an operator's client ID and secret into the agent spec kiro-cli reads. The WakaTime reads are not an MCP connection. The gateway itself calls WakaTime's REST API from the dashboard handlers, so there is no kiro-cli grant it could borrow, and the gateway has to hold the token it sends.
- Treat the Crew-held tokens as a break of the Connections rule. The rule that Kiro Crew never holds a connection's credential covers the grants kiro-cli owns for Connections. WakaTime is not a connection. It is a gateway integration that already holds its own credential, `WAKATIME_API_KEY`, in the vault. The OAuth tokens replace that key for reads and sit on the same footing: vault-only, read at the request site, never logged. The design copies two parts of `oauth_clients.py`: the client ID in plain config, and the client secret in the vault only.
- A broad scope grant. `read_logged_time` or a blanket read scope would cover everything in one request, but the standard requires least privilege, and the reads need only stats and summaries.
