# Every completed Kiro Crew sign-in counts as an account change, a same-account re-sign-in too

Decided by: Zezhen Xu (maintainer, @CrysisDeu)
Date: 2026-10-27

## Decision
Every sign-in that `KasLoginService` saves moves the vault's sign-in marker, so the identity fingerprint changes and the next turn's sweep retires idle kiro-backed sessions once, even when the person signed in to the same account again.

## Why
- Two accounts of one kind (Google A, then Google B) can come back with one `profileArn`, and then every fingerprinted claim matches. Without the marker, B inherits A's model catalog and A's sessions are not retired.
- The sign-in responses carry nothing else that names the account. The social device poll (`src/kiro_crew/auth/login/device.py`, `_token_from_poll`) reads `accessToken`, `refreshToken`, `profileArn`, `expiresIn` and `identityProvider`; the social portal exchange (`src/kiro_crew/auth/login/portal.py`, `exchange_code`) reads the same fields less `identityProvider`. The external IdP exchange (`src/kiro_crew/auth/login/external_idp.py`, `exchange_code`) reads `access_token`, `refresh_token` and `expires_in`. Decoding the access token was ruled out as a non-goal.
- `replaces` does not tell the two cases apart: a Settings re-sign-in always names the current vault slot (`website/src/components/KasLoginGate.tsx`, `replaces`), whether the next account is the same one or another.
- The cost lands on social and external IdP sign-ins, which now pay one idle-session sweep per re-sign-in. Builder ID and Identity Center already paid it: each of their sign-ins registers a fresh SSO-OIDC client (`src/kiro_crew/auth/service.py`), and `client_id` is a fingerprinted claim (`src/kiro_crew/auth/bridge.py`, `_VAULT_IDENTITY_CLAIMS`).
- The account e-mail that GetUsageLimits returns with `isEmailRequired` (`src/kiro_crew/dashboard/handlers/kiro_usage_api.py`) is not used as a substitute: it is a separate network call after the sign-in, and the code already retries without it when the service answers HTTP 400, so which account kinds return it is not known.
- A sign-in response that carried a stable per-account id would give the fingerprint another way to tell the two cases apart; that is the change that would reopen this.

## Evidence
- https://github.com/kirodotdev/KiroCrew/issues/24942 -- the maintainer's proposal: a same-account re-sign-in also bumps the marker, "which is the safe answer".
- https://github.com/kirodotdev/KiroCrew/pull/25608 -- the change that ships it, by the same maintainer.
- https://github.com/kirodotdev/KiroCrew/issues/25964 -- the report of the cost, which asks for this record if the trade is kept.
