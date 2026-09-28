/**
 * Generated webhook request examples — the snippets must actually run.
 *
 * `csrf_middleware` applies `check_origin` to every non-safe method with no path
 * exemption, and a missing Origin is trusted only for loopback callers. The
 * documented callers for /api/hooks/agent are the opposite of loopback — CI
 * runners, bots, deploy pipelines — so a snippet without an Origin header is
 * rejected with 403 before bearer/HMAC auth ever runs. A copy-pasteable example
 * that always 403s is worse than no example, so the header is pinned here.
 */
import { describe, it, expect } from 'vitest'

import { exampleFor } from '../pages/webhooks/requestExamples'

const URL_ = 'https://crew.example.com/api/hooks/agent'

describe('webhook request examples carry an accepted Origin', () => {
  it('sends the gateway origin on the bearer-only form', () => {
    const snippet = exampleFor('bearer', URL_, 'sess-1', 'hello', 300)
    expect(snippet).toContain("-H 'Origin: https://crew.example.com'")
    // The origin is the scheme://host only — never the full endpoint path,
    // which is not a valid Origin and would not match the allowed set.
    expect(snippet).not.toContain('Origin: https://crew.example.com/api')
  })

  it('sends the gateway origin on the signed form too', () => {
    const snippet = exampleFor('signed', URL_, 'sess-1', 'hello', 300)
    expect(snippet).toContain("-H 'Origin: https://crew.example.com'")
  })

  it('keeps a placeholder origin when the endpoint URL is not known yet', () => {
    // The page renders before it has resolved its own URL; the snippet must
    // still be coherent rather than emitting a bare "://" fragment.
    for (const mode of ['bearer', 'signed'] as const) {
      expect(exampleFor(mode, '', 'sess-1', 'hello', 300))
        .toContain("-H 'Origin: <gateway-url>'")
    }
  })

  it('carries a port when the gateway runs on one', () => {
    const snippet = exampleFor('bearer', 'http://10.0.0.4:5476/api/hooks/agent', 's', 'm', 300)
    expect(snippet).toContain("-H 'Origin: http://10.0.0.4:5476'")
  })
})
