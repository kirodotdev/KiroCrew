/**
 * Contract test for the shared loopback-hostname helper (src/lib/paneEmbedding.ts).
 *
 * The pane-address AUTHORITY moved to `lib/paneChannel.ts` (its `resolvePaneMode`
 * owns the direct-loopback vs same-origin-relay decision, covered in
 * `paneChannel.test.ts`). What remains here is the single fact both transports
 * share: whether a hostname is a loopback name the SSH forward's bind is
 * reachable on.
 *
 * Observable contract this owns (test-audit authoring gate):
 *   1. Behaviour: the exact loopback allowlist — `127.0.0.1`, `localhost`, and
 *      single-label `*.localhost` — and nothing that merely looks like one.
 *   2. Credible regression: widening the regex to admit `localhost.evil.com` or
 *      `evil.localhost.com` would let a non-loopback host be treated as reachable
 *      loopback and drive `resolvePaneMode` onto the direct path for a host the
 *      browser cannot reach; these assertions turn red on that widening.
 *   3. Coverage gap: `paneChannel.test.ts` exercises the mode decision on
 *      canonical hosts but not the look-alike rejections; `tunnelOrigin.test.ts`
 *      covers origin parsing, not the bare hostname predicate. This is that owner.
 *   4. Production seam: `isLoopbackHostname` is imported by `paneChannel` in
 *      production; no test-only export.
 */
import { describe, it, expect } from 'vitest'
import { isLoopbackHostname } from '../lib/paneEmbedding'

describe('isLoopbackHostname', () => {
  it('accepts the loopback allowlist: 127.0.0.1, localhost, single-label *.localhost', () => {
    for (const hostname of ['127.0.0.1', 'localhost', 'kirocrew.localhost']) {
      expect(isLoopbackHostname(hostname)).toBe(true)
    }
  })

  it('rejects a look-alike loopback host (the security-relevant boundary)', () => {
    // A hostname that merely contains "localhost" is NOT loopback: routing a pane
    // to it on the direct path would aim at a host the forward never binds.
    for (const hostname of ['evil.localhost.com', 'localhost.evil.com', 'notlocalhost', '']) {
      expect(isLoopbackHostname(hostname)).toBe(false)
    }
  })

  it('rejects a published tunnel host', () => {
    expect(isLoopbackHostname('crew.example.ts.net')).toBe(false)
  })
})
