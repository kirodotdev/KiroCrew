/**
 * Reload when this document's CSP predates an app's `ui.frameSources`.
 *
 * The gateway admits an app's declared origins in the dashboard `frame-src` while
 * the app is enabled and trusted, but a document keeps the policy it was served
 * with. An app enabled (or trusted) after this document loaded would have its
 * declared iframe refused until a reload. AppHost watches for that refusal, asks
 * the gateway for its current policy, and reloads only when that policy now admits
 * the refused origin. A refusal a reload cannot cure (the app is not trusted)
 * reloads nothing.
 */

/** Backstop against a reload loop should the browser keep refusing an origin the
 *  served policy admits: at most one reload per app in this window. */
const RELOAD_GAP_MS = 30_000
const KEY_PREFIX = 'kc:frame-sources-reload:'

type Violation = Pick<SecurityPolicyViolationEvent, 'effectiveDirective' | 'blockedURI' | 'originalPolicy'>

/** The declared origin `e` refuses as a frame because this document's policy lacks
 *  it, or null. When the refusing policy already lists the origin, the refusal is
 *  of a redirect target it did not declare, which no reload can cure. */
export function refusedDeclaredOrigin(e: Violation, declared: readonly string[]): string | null {
  if (e.effectiveDirective !== 'frame-src') return null
  let origin: string
  try {
    origin = new URL(e.blockedURI).origin
  } catch {
    return null
  }
  if (!declared.includes(origin) || policyAdmitsFrame(e.originalPolicy, origin)) return null
  return origin
}

/** Whether a `Content-Security-Policy` value's `frame-src` lists `origin` exactly. */
export function policyAdmitsFrame(csp: string | null, origin: string): boolean {
  const directive = (csp || '').split(';').map(d => d.trim()).find(d => d.startsWith('frame-src '))
  return !!directive && directive.split(/\s+/).slice(1).includes(origin)
}

/** The policy the gateway serves a document at this URL now. */
async function currentPolicy(): Promise<string | null> {
  const resp = await fetch(window.location.href, { method: 'HEAD', cache: 'no-store', credentials: 'same-origin' })
  return resp.headers.get('Content-Security-Policy')
}

/** Reload the document for `app` when the gateway's policy now admits `origin`. */
export async function reloadIfFrameAdmitted(
  app: string,
  origin: string,
  deps: { policy?: () => Promise<string | null>; reload?: () => void; now?: number } = {},
): Promise<boolean> {
  const { policy = currentPolicy, reload = () => window.location.reload(), now = Date.now() } = deps
  let csp: string | null
  try {
    csp = await policy()
  } catch {
    return false
  }
  if (!policyAdmitsFrame(csp, origin)) return false
  const key = KEY_PREFIX + app
  try {
    if (now - Number(sessionStorage.getItem(key) || 0) < RELOAD_GAP_MS) return false
    sessionStorage.setItem(key, String(now))
  } catch {
    return false
  }
  reload()
  return true
}
