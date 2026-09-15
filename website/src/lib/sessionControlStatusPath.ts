/**
 * The allowlist for an app session control's `statusPath`.
 *
 * @module lib/sessionControlStatusPath
 */

/**
 * Allowlist for a control's `statusPath`, mirroring the backend's
 * `_SESSION_CONTROL_STATUS_PATH_RE` exactly.
 *
 * The manifest is validated at install time, so a conforming install cannot
 * reach the frontend with anything else — but the value is interpolated into a
 * fetch URL, and the contract is to survive a stale or hand-edited manifest.
 * Revalidating is the difference between that being true and merely claimed.
 *
 * `.` is deliberately outside the character class, which makes `..` — and so
 * path traversal into another app's routes — unrepresentable rather than merely
 * rejected. `?` and `#` are excluded for the same reason: either would corrupt
 * the query string appended to the route.
 *
 * It lives in its own module because BOTH frontend layers that touch a
 * `statusPath` must agree on it — `hooks/useSessionControls` when it resolves a
 * declared path, and `api/client` when it interpolates one — and the two cannot
 * share it directly:
 *
 * - the hook imports `api/client`, so exporting from the hook would be an
 *   import cycle; and
 * - exporting from `api/client` would make the constant `undefined` inside the
 *   ~490 test files that replace that module with a `vi.mock` factory, which is
 *   a failure the type checker cannot see.
 *
 * A module neither side mocks is the only home where one spelling of this
 * boundary stays one spelling.
 */
export const SESSION_CONTROL_STATUS_PATH_RE = /^[a-z0-9][a-z0-9/_-]{0,63}$/
