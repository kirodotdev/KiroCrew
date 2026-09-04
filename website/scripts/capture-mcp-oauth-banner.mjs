/**
 * Screenshot + assertion runner for capture/mcp-oauth-banner.html.
 *
 * The label is an ARGUMENT, not a scene, because the two states differ by which
 * version of `src/pages/chat/McpOAuthBanner.tsx` is on disk. Tailwind only scans
 * `src/`, so a before-state faked inside the capture file would render unstyled
 * regardless of its classes and prove nothing.
 *
 * From website/, with the dev server already up:
 *   npx vite --host 127.0.0.1 --port 6815 --strictPort
 *
 *   git stash push ../website/src/pages/chat/McpOAuthBanner.tsx   # broken source
 *   node scripts/capture-mcp-oauth-banner.mjs http://127.0.0.1:6815 OUT before
 *   git stash pop                                                 # fixed source
 *   node scripts/capture-mcp-oauth-banner.mjs http://127.0.0.1:6815 OUT after
 *
 * The assertion is what makes each frame trustworthy: `before` must show
 * Preflight's untokenised grey border and a transparent fill (i.e. no rule was
 * emitted), `after` must show a token-derived colour in both. Reading computed
 * style proves the RULE exists, which a screenshot alone cannot.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6815'
const OUT = process.argv[3] || '../temp-screenshots/mcp-oauth-banner'
const LABEL = process.argv[4] || 'after'

if (!['before', 'after'].includes(LABEL)) {
  console.error(`usage: … <baseUrl> <outDir> <before|after>  (got "${LABEL}")`)
  process.exit(2)
}

/** Tailwind Preflight's default border colour — what an un-emitted rule leaves. */
const PREFLIGHT = 'rgb(229, 231, 235)'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    viewport: { width: 620, height: 380 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))

  const name = `${theme}-${LABEL}.png`
  try {
    await page.goto(`${BASE}/capture/mcp-oauth-banner.html?theme=${theme}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
    await page.waitForSelector('[data-state="pending"]', { timeout: 10000 })
    await page.waitForTimeout(400)

    const states = await page.evaluate(() => ['pending', 'done', 'failed'].map(state => {
      const el = document.querySelector(`[data-state="${state}"]`).firstElementChild
      const cs = getComputedStyle(el)
      return { state, border: cs.borderTopColor, bg: cs.backgroundColor }
    }))

    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}` })

    let frameFailed = 0
    for (const s of states) {
      const isPreflight = s.border === PREFLIGHT
      const isTransparent = s.bg === 'rgba(0, 0, 0, 0)' || s.bg === 'transparent'
      if (LABEL === 'before' && !(isPreflight && isTransparent)) {
        frameFailed++
        console.error(`FAIL ${name}: ${s.state} does not show the un-emitted state — is the fixed source on disk? border=${s.border} bg=${s.bg}`)
      }
      if (LABEL === 'after' && (isPreflight || isTransparent)) {
        frameFailed++
        console.error(`FAIL ${name}: ${s.state} still un-styled, border=${s.border} bg=${s.bg}`)
      }
    }
    if (errors.length) {
      frameFailed++
      console.error(`FAIL ${name}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
    }
    failed += frameFailed
    if (!frameFailed) {
      console.log(`ok   ${name}`)
      for (const s of states) console.log(`       ${s.state.padEnd(8)} border=${s.border}  bg=${s.bg}`)
    }
  } catch (err) {
    failed++
    console.error(`FAIL ${name}: ${err.message}`)
  }
  await ctx.close()
}

await browser.close()
if (failed) {
  console.error(`\n${failed} assertion(s) failed — the frames do not show the state they claim.`)
  process.exit(1)
}
console.log(`\n${LABEL}: every state matched its expected colour source`)
