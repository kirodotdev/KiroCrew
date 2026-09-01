/** Client-side pre-check for a pasted OAuth loopback return address.
 *
 * Mirrors the backend's `_validated_loopback_return_address`
 * (`src/kiro_crew/dashboard/handlers/connections.py`): plain HTTP, a loopback
 * host from the SAME set the backend admits — `127.0.0.1`, `::1`, or
 * `localhost` (kiro-cli's callback URL can be localhost-shaped, so a stricter
 * client check would reject the exact paste the recovery flow solicits) — an
 * explicit port, no userinfo, no fragment, and exactly one non-empty `code`.
 * Shared by the Connections card and the chat banner's relay affordance so the
 * two surfaces cannot drift apart again (PR #4796 Design review).
 */
export function isValidLoopbackReturnAddress(value: string): boolean {
  try {
    let normalized = value.trim()
    // iOS Safari omits http:// when copying loopback URLs (e.g. 127.0.0.1:8976/callback?code=...).
    // Default to http:// so the paste validates — other constraints still apply.
    if (normalized && !normalized.includes('://')) normalized = `http://${normalized}`
    const url = new URL(normalized)
    const loopback =
      url.hostname === '127.0.0.1'
      || url.hostname === '[::1]'
      || url.hostname === '::1'
      || url.hostname === 'localhost'
    const codes = url.searchParams.getAll('code')
    return url.protocol === 'http:'
      && loopback
      && url.port !== ''
      // The runtime callback binds an unprivileged port; the backend refuses
      // anything below 1024, so mirror that rather than round-tripping it.
      && Number(url.port) >= 1024
      && url.username === ''
      && url.password === ''
      && url.hash === ''
      && codes.length === 1
      && codes[0] !== ''
  } catch {
    return false
  }
}
