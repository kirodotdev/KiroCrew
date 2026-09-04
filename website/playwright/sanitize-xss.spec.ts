import { test, expect } from '@playwright/test'
import * as path from 'path'
import * as fs from 'fs'
import { fileURLToPath } from 'url'

// ESM scope: no `__dirname` (package is "type": "module"); derive it.
const __dirname = path.dirname(fileURLToPath(import.meta.url))

// Real-BROWSER XSS gate for `sanitize()` (src/api/helpers.ts), the DOMPurify
// wrapper that scrubs LLM/agent-generated (attacker-influenceable) HTML before
// it reaches the DOM. The vitest suite validates this under happy-dom, whose
// HTML parser is measurably less browser-faithful than a real engine; this spec
// closes that gap by exercising the SAME DOMPurify build the app ships
// (dompurify 3.3.3, default config — `sanitize()` is literally
// `DOMPurify.sanitize(html)`) inside real Chromium, so a browser-parser
// divergence (mutation-XSS especially) that happy-dom would miss fails HERE.
//
// It injects the app's own vendored purify.js into the page rather than reaching
// into a hashed prod chunk: the config is DOMPurify's default, so the injected
// call reproduces production sanitization exactly, and the assertion is what
// matters — no live script / event handler / dangerous-scheme URL survives in
// the browser's parsed DOM. Runs in the default (credential-less) gate; needs no
// agent turn.
const PURIFY_UMD = path.resolve(__dirname, '../node_modules/dompurify/dist/purify.js')

// Attacker-influenceable payloads DOMPurify must neutralize.
const VECTORS: Array<{ name: string; html: string }> = [
  { name: 'script element', html: '<div>ok</div><script>window.__xss=1;alert(1)</script>' },
  { name: 'img onerror', html: '<img src=x onerror="window.__xss=1">' },
  { name: 'svg onload', html: '<svg onload="window.__xss=1"></svg>' },
  { name: 'div onclick', html: '<div onclick="window.__xss=1">x</div>' },
  { name: 'anchor javascript: URL', html: '<a href="javascript:window.__xss=1">x</a>' },
  { name: 'anchor mixed-case javascript: URL', html: '<a href="jAvAsCrIpT:window.__xss=1">x</a>' },
  { name: 'iframe data: html', html: '<iframe src="data:text/html,<script>window.__xss=1</scr' + 'ipt>"></iframe>' },
  { name: 'svg-wrapped script', html: '<svg><script>window.__xss=1</scr' + 'ipt></svg>' },
  { name: 'mathml-wrapped script', html: '<math><mtext><script>window.__xss=1</scr' + 'ipt></mtext></math>' },
  // Mutation-XSS shape — the class most sensitive to parser differences, the
  // exact reason a real-browser gate matters beyond happy-dom.
  { name: 'mXSS noscript/title breakout', html: '<noscript><p title="</noscript><img src=x onerror=window.__xss=1>">' },
]

test.describe('DOMPurify sanitize() — real-browser XSS neutralization', () => {
  test('neutralizes classic + mutation XSS vectors in Chromium', async ({ page }) => {
    // Navigate to the app so we run inside the real dashboard document/origin.
    await page.goto('/chat', { waitUntil: 'domcontentloaded' })

    // Inject the app's own DOMPurify build (default config == what sanitize() uses).
    const purifySrc = fs.readFileSync(PURIFY_UMD, 'utf-8')
    await page.addScriptTag({ content: purifySrc })
    expect(await page.evaluate(() => typeof (window as any).DOMPurify?.sanitize)).toBe('function')

    for (const v of VECTORS) {
      const result = await page.evaluate((html: string) => {
        const w = window as any
        w.__xss = 0
        const clean: string = w.DOMPurify.sanitize(html)
        // Parse the sanitized output into a live DOM and inspect for dangerous
        // residue (a detached <script> won't execute, so we assert on structure
        // + the __xss flag rather than relying on execution).
        const doc = new DOMParser().parseFromString(clean, 'text/html')
        const hasScript = doc.querySelector('script') !== null
        const els = Array.from(doc.querySelectorAll('*'))
        const hasEventHandler = els.some((el) =>
          Array.from(el.attributes).some((a) => /^on/i.test(a.name)),
        )
        // Full dangerous-scheme set (javascript:, data:, vbscript:) — an
        // incomplete list here would be its own XSS blind spot. Browsers ignore
        // leading whitespace + C0 control chars when resolving a URL scheme, so
        // strip [\x00-\x20] before matching.
        const hasJsUrl = els.some((el) =>
          ['href', 'src', 'xlink:href'].some((attr) => {
            const val = (el.getAttribute(attr) || '').replace(/[\x00-\x20]+/g, '').toLowerCase()
            return /^(?:javascript|data|vbscript):/.test(val)
          }),
        )
        return { clean, hasScript, hasEventHandler, hasJsUrl, executed: w.__xss }
      }, v.html)

      // The security invariant, engine-independent: no live script, no inline
      // event handler, no dangerous-scheme URL survives; nothing executed.
      expect(result.hasScript, `${v.name}: <script> survived: ${result.clean}`).toBe(false)
      expect(result.hasEventHandler, `${v.name}: event handler survived: ${result.clean}`).toBe(false)
      expect(result.hasJsUrl, `${v.name}: dangerous URL survived: ${result.clean}`).toBe(false)
      expect(result.executed, `${v.name}: payload executed`).toBe(0)
    }
  })

  test('preserves benign markup (sanitizer is not over-stripping)', async ({ page }) => {
    await page.goto('/chat', { waitUntil: 'domcontentloaded' })
    await page.addScriptTag({ content: fs.readFileSync(PURIFY_UMD, 'utf-8') })
    const clean = await page.evaluate(() =>
      (window as any).DOMPurify.sanitize('<strong>bold</strong> <em>it</em> <a href="/ok">link</a>'),
    )
    expect(clean).toContain('bold')
    expect(clean).toContain('it')
    expect(clean).toContain('href="/ok"')
  })
})
