# Behind a sub-path proxy the refresh cookie's path comes from the path of `dashboard.url`, not from a request header

Decided by: Bolin Chen (maintainer, @bolichen97)
Date: 2026-11-28

## Decision

The refresh cookie `mc_refresh_<port>` is scoped to the path of `dashboard.url`
plus `/api/auth` (`https://host/kirocrew` gives `/kirocrew/api/auth`), with one
helper for every writer and clearer; a `dashboard.url` with no usable path keeps
`/api/auth`, the path never widens to `/`, and no request header such as
`X-Forwarded-Prefix` decides it.

## Why

- Behind a proxy that serves the dashboard at a sub-path and strips it, the
  cookie at `/api/auth` was never sent to `<sub-path>/api/auth/refresh` or
  `/logout`, so renewal failed and sign-out could not revoke.
- The assignee asked a maintainer to choose between a loopback-trusted
  `X-Forwarded-Prefix` header and an operator setting. `dashboard.url` is
  already the dashboard's public URL, and the WakaTime sign-in callback already
  reads its path as the sub-path, so no new setting is needed.
- A configured value cannot be moved by a request, gives every response the
  same path, and also serves a proxy in a container or on another host, which a
  header honoured only from a loopback peer would not reach.

## Evidence

- Question: https://github.com/kirodotdev/KiroCrew/issues/39764#issuecomment-7152313702
- Decision: https://github.com/kirodotdev/KiroCrew/issues/39764#issuecomment-7235890844
- The header approach this does not take: https://github.com/kirodotdev/KiroCrew/pull/39931
