import { useCallback, useEffect, useState, type RefObject } from 'react'

/**
 * The origin a framed page tried to move to and the dashboard's own CSP
 * `frame-src` refused, or null while nothing was refused. An empty string means
 * a refusal whose target the engine did not name. The second value clears it.
 *
 * The preview frame loads a local dev server, which the policy admits. A later
 * navigation INSIDE that frame (an OAuth redirect to `accounts.google.com`, a
 * link to any public site) is checked against the same `frame-src`, refused,
 * and the frame is left on the engine's blank error page. The framed app never
 * learns why, so a sign-in button just spins. Widening the policy would not
 * help: Google and GitHub sign-in pages refuse to be framed anywhere, and an
 * OAuth flow needs a top-level browsing context.
 *
 * The refusal is visible to the dashboard, though: the engine fires a
 * `securitypolicyviolation` event at the document that owns the policy, which is
 * this one. Chromium targets that event at the document, not at the iframe
 * element, and its `blockedURI` carries only the origin, so a document-targeted
 * report cannot be tied to one frame: another dashboard frame refused the same
 * way reports identically. The caller therefore only ANNOUNCES the refusal and
 * never tears the frame down on it, so a misattributed report costs a
 * dismissable notice, not the preview's state. The hook listens only while the
 * caller is framing (`enabled`), drops reports aimed at some OTHER element, and
 * keeps the refused origin until `resetKey` changes (a new URL or a reload) or
 * the reader dismisses it.
 */
export function useFrameNavigationBlock(
  enabled: boolean,
  frameRef: RefObject<HTMLIFrameElement | null>,
  resetKey: string,
  /** The framed address's own origin. A refusal of it is the frame's first
   *  load being refused, not a navigation away from a page that loaded, so it
   *  is left to the panel's existing embed explanations. */
  ownOrigin: string,
): [string | null, () => void] {
  const [blocked, setBlocked] = useState<string | null>(null)
  // Cleared by a new URL or a reload only. `enabled` follows the panel's tab
  // being active, and switching side-panel tabs keeps the frame (blank page
  // included) mounted, so it must not clear what the frame still shows.
  useEffect(() => { setBlocked(null) }, [resetKey])
  useEffect(() => {
    if (!enabled) return
    const onViolation = (e: SecurityPolicyViolationEvent) => {
      if (e.effectiveDirective !== 'frame-src' && e.effectiveDirective !== 'child-src') return
      const target = e.target
      if (target instanceof Element && target !== frameRef.current) return
      const raw = e.blockedURI || ''
      let origin = raw
      try { origin = new URL(raw).origin } catch { /* keep the engine's text */ }
      if (origin && origin === ownOrigin) return
      setBlocked(origin)
    }
    document.addEventListener('securitypolicyviolation', onViolation)
    return () => document.removeEventListener('securitypolicyviolation', onViolation)
  }, [enabled, frameRef, ownOrigin])
  const dismiss = useCallback(() => setBlocked(null), [])
  return [blocked, dismiss]
}
