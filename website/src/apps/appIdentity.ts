/**
 * appId validation — the one place an app id enters the frontend as a key.
 *
 * An appId is not a display string. The platform namespaces an app's persisted
 * view state and its cached data to it, so the id becomes a `localStorage` key
 * segment and a react-query key prefix. An unvalidated id is therefore an
 * injection surface, not a cosmetic problem.
 *
 * Kept as a pure module with no React import, for the same reason
 * `overlaySlots.ts` is: the rule a storage key depends on must be testable
 * without mounting a tree. The identity CONTEXT and its hooks live in
 * `app-sdk/identity.ts` — apps read identity from there; this module is
 * host-side registration machinery and is deliberately not published to
 * third-party apps.
 */

/**
 * appId charset: non-empty, lowercase alphanumerics and `-` only.
 *
 * Following the shape of Codex's `validate_plugin_segment`. Three refusals are
 * load-bearing rather than stylistic:
 *
 * - `.` and `..` are refused BY THE CHARSET rather than by a special case. An
 *   appId is a path/key segment, so `..` traverses out of the namespace it was
 *   supposed to name and `.` collapses to it.
 * - `/` and `\` are refused for the same reason: they would let one app's id
 *   address a second namespace.
 * - Uppercase is refused because keys are compared byte-wise. `Aws-Control` and
 *   `aws-control` would be two namespaces for one app, and which one a user's
 *   state landed in would depend on which caller wrote it first.
 */
const APP_ID_RE = /^[a-z0-9-]+$/

/**
 * Whether `appId` may be used as a host-owned key segment.
 *
 * Takes `unknown` on purpose. The runtime registration seam
 * (`registerBuiltinComponents`) accepts an object authored outside this file, so
 * a non-string id is reachable at runtime even where TypeScript says it is not,
 * and a bad type must be refused rather than coerced into a key.
 */
export function isValidAppId(appId: unknown): appId is string {
  return typeof appId === 'string' && APP_ID_RE.test(appId)
}
