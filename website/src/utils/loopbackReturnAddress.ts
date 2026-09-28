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

/** RFC 3986 scheme followed by `://`. Requires the `//` on purpose: a bare
 * `host:port/...` must not count as having a scheme, so it gets the default. */
const SCHEME_RE = /^[a-z][a-z0-9+.-]*:\/\//i

/** Default a scheme-less paste to `http://` (#7406).
 *
 * Mobile browsers — iOS Safari in particular — copy address-bar URLs without
 * the scheme, so the paste-back flow otherwise rejects exactly the text the
 * browser gave the user. The loopback callback listener is always plain HTTP,
 * and every containment check in `isValidLoopbackReturnAddress` (loopback
 * host, port floor, single code) applies to the normalized value, so the
 * default cannot admit anything the strict form would refuse. Callers must
 * validate AND submit this normalized value, mirroring the backend's own
 * normalization in `_validated_loopback_return_address`.
 */
export function normalizeLoopbackReturnAddress(value: string): string {
  const trimmed = value.trim()
  if (!trimmed || SCHEME_RE.test(trimmed)) return trimmed
  return `http://${trimmed}`
}

export function isValidLoopbackReturnAddress(value: string): boolean {
  try {
    const url = new URL(normalizeLoopbackReturnAddress(value))
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
