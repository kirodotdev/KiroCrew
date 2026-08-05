// @vitest-environment jsdom
//
// XSS regression guard for `src/api/helpers.ts`'s DOMPurify sanitizer.
//
// Runs under jsdom, NOT the suite-default happy-dom. The concern DOMPurify's own
// docs raise — that a non-browser DOM can parse a payload differently than a real
// browser — turned out to be REAL for happy-dom: since DOMPurify 3.4.10 (which
// changed its per-node traversal to read textContent before innerHTML), happy-dom
// mis-parses so badly that DOMPurify strips benign markup while LEAVING <script>
// and on* handlers through — the exact inversion of a sanitizer. That is an
// upstream happy-dom parser bug (happy-dom 20.11.1 is the latest and still hits
// it), not a DOMPurify vulnerability: the SAME build sanitizes correctly under
// jsdom and in real browsers. So a happy-dom run of these vectors is a false
// negative, and jsdom — DOMPurify's own reference DOM, browser-faithful here — is
// the correct fast guard. See vite.config.ts for why the rest of the suite stays
// on happy-dom.
//
// Authoritative real-browser fidelity — the concern that a test DOM could mutate
// a payload differently than Chromium (mutation-XSS especially) — is still
// covered by the companion Playwright spec `website/playwright/sanitize-xss.spec.ts`,
// which runs the SAME DOMPurify build against these vectors in real Chromium on
// the offline `setup.py test_e2e` gate. Keep the two in sync: a vector added here
// should also be covered there.
//
// Each case asserts the DANGEROUS artifact is gone (no live script/handler/
// javascript: URL), not an exact serialization — DOMPurify output can vary by
// version, but the security invariant must hold on every engine.
import { describe, it, expect } from 'vitest'
import { sanitize } from '../api/helpers'

describe('DOMPurify XSS neutralization under jsdom', () => {
  it('strips <script> element and its payload', () => {
    const out = sanitize('<div>ok</div><script>alert(1)</script>')
    expect(out).not.toMatch(/<script/i)
    expect(out).not.toContain('alert(1)')
    expect(out).toContain('ok')
  })

  it('strips inline event-handler attributes (onerror/onload/onclick)', () => {
    for (const payload of [
      '<img src=x onerror=alert(1)>',
      '<svg onload=alert(1)>',
      '<div onclick="alert(1)">x</div>',
      '<body onpageshow=alert(1)>',
    ]) {
      const out = sanitize(payload)
      expect(out.toLowerCase()).not.toContain('onerror')
      expect(out.toLowerCase()).not.toContain('onload')
      expect(out.toLowerCase()).not.toContain('onclick')
      expect(out.toLowerCase()).not.toContain('onpageshow')
      expect(out).not.toContain('alert(1)')
    }
  })

  it('drops javascript: and data: script URLs on href/src', () => {
    expect(sanitize('<a href="javascript:alert(1)">x</a>')).not.toContain('javascript:')
    expect(sanitize('<a href="jAvAsCrIpT:alert(1)">x</a>').toLowerCase()).not.toContain('javascript:')
    const iframe = sanitize('<iframe src="data:text/html,<script>alert(1)</script>"></iframe>')
    expect(iframe).not.toContain('alert(1)')
  })

  it('neutralizes SVG/MathML-wrapped script vectors', () => {
    const svg = sanitize('<svg><script>alert(1)</script></svg>')
    expect(svg).not.toMatch(/<script/i)
    expect(svg).not.toContain('alert(1)')
    const mathml = sanitize('<math><mtext><script>alert(1)</script></mtext></math>')
    expect(mathml).not.toContain('alert(1)')
  })

  it('handles the mXSS-style nested/malformed markup without leaking live markup', () => {
    // Classic mutation-XSS shapes: the sanitized output must not contain a live
    // <img>/<script> reconstructed from the malformed input.
    const out = sanitize('<noscript><p title="</noscript><img src=x onerror=alert(1)>">')
    expect(out.toLowerCase()).not.toContain('onerror')
    expect(out).not.toContain('alert(1)')
    const out2 = sanitize('<![CDATA[<img src=x onerror=alert(1)>]]>')
    expect(out2.toLowerCase()).not.toContain('onerror')
  })

  it('keeps benign markup intact (sanitizer is not over-stripping)', () => {
    const out = sanitize('<strong>bold</strong> <em>it</em> <a href="/ok">link</a>')
    expect(out).toContain('bold')
    expect(out).toContain('it')
    expect(out).toContain('href="/ok"')
  })
})
