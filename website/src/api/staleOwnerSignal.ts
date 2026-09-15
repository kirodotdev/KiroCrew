/**
 * Stale pre-owner session signal — shared, dependency-free detection.
 *
 * The backend labels exactly one owner-gate denial with a machine-readable
 * code: a session whose token was minted before `KIROCREW_OWNER_ID` was
 * configured (the subject is fixed at mint time and survives refresh, so only
 * a fresh sign-in recovers). The blessed transport (`api/client`) detects it
 * inside its `j`/`jNullable` pipeline, but several owner-gated surfaces fetch
 * directly — the app-sdk scoped API, the MCP-app tool relay, Mochi's approval
 * bridge — and would otherwise swallow the signal into a generic error.
 *
 * This module is a LEAF on purpose: the direct-fetch call sites import only
 * this file, never `api/client`, so the vendored app-sdk bundle and the Mochi
 * panel page do not grow a dependency on the whole dashboard client graph.
 * `api/client` installs the actual prompt (its re-auth banner) at its own
 * module load; in a document where `api/client` never loads, detection still
 * returns true and the caller keeps its own error path — the prompt is simply
 * absent, which is the correct degraded behavior for a non-dashboard document.
 */

/** Matches `STALE_OWNER_SESSION_CODE` in `dashboard/handlers/source_providers.py`. */
export const STALE_OWNER_SESSION_CODE = 'stale_session_reauth'

type StaleOwnerHandler = () => void

let _handler: StaleOwnerHandler | null = null

/** Installed once by `api/client` (the module that owns the re-auth banner). */
export function installStaleOwnerHandler(handler: StaleOwnerHandler): void {
  _handler = handler
}

/** Test-only: detach the handler so cases can assert the uninstalled no-op. */
export function __resetStaleOwnerHandlerForTests(): void {
  _handler = null
}

/** The backend `code` field, from a raw body string or already-parsed JSON. */
function errorCode(body: unknown): string | undefined {
  let parsed: unknown = body
  if (typeof body === 'string') {
    const trimmed = body.trim()
    if (!trimmed.startsWith('{')) return undefined
    try {
      parsed = JSON.parse(trimmed)
    } catch {
      return undefined
    }
  }
  const code = (parsed as { code?: unknown } | null)?.code
  return typeof code === 'string' && code ? code : undefined
}

/**
 * Detect the stale pre-owner denial on a response that did not travel through
 * the blessed transport, and raise the installed re-auth prompt. Accepts the
 * body as raw text or as parsed JSON because the call sites hold different
 * shapes at their failure points. Returns whether the signal matched — the
 * caller keeps (and should keep) its own error handling either way; this only
 * ADDS the prompt, it never swallows the failure.
 */
export function noteStaleOwnerResponse(status: number, body: unknown): boolean {
  if (status !== 401) return false
  if (errorCode(body) !== STALE_OWNER_SESSION_CODE) return false
  _handler?.()
  return true
}
