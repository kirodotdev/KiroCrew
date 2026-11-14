/**
 * Spot a declared frame this document's CSP refuses because the policy predates it.
 *
 * The gateway admits an app's declared origins in the dashboard `frame-src` while
 * the app is enabled and trusted, but a document keeps the policy it was served
 * with. An app enabled (or trusted, or approved) after this document loaded has its
 * declared iframe refused until the page is reloaded. AppHost watches for that
 * refusal and asks the owner to refresh the page; it never reloads by itself.
 */

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
  const spaced = ['\t', '\n', '\r', '\f'].reduce((s, ws) => s.split(ws).join(' '), csp || '')
  const directive = spaced.split(';').map(d => d.trim()).find(d => d.startsWith('frame-src '))
  return !!directive && directive.split(' ').filter(Boolean).slice(1).includes(origin)
}
