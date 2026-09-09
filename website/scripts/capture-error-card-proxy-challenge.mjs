/**
 * Screenshot runner for capture/error-card-proxy-challenge.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6844 --strictPort
 *   node scripts/capture-error-card-proxy-challenge.mjs http://127.0.0.1:6844 <outdir>
 *
 * Captures the before/after sheet in both themes, element-scoped to the capture
 * root. Every episode is asserted before the frame is taken, so a screenshot
 * cannot photograph the wrong state: BEFORE must really be the bare status, AFTER
 * must name the proxy and must NOT carry the gateway remedy, BLOCKED must name a
 * refusal without calling it a lapse, and no episode may
 * leak an unresolved catalog key.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6844'
const OUT = process.argv[3] || '../temp-screenshots/error-card-proxy-challenge'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({
    viewport: { width: 820, height: 1300 },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  try {
    await page.goto(`${BASE}/capture/error-card-proxy-challenge.html?theme=${theme}`, {
      waitUntil: 'networkidle',
    })
    const text = async ep =>
      (await page.locator(`[data-episode="${ep}"] [data-testid="error-card"]`).innerText()) ?? ''

    for (const ep of ['before', 'after', 'blocked', 'framed', 'rejected']) {
      await page.locator(`[data-episode="${ep}"] [data-testid="error-card"]`).waitFor({ timeout: 10000 })
    }

    const before = await text('before')
    const after = await text('after')
    const blocked = await text('blocked')
    const framed = await text('framed')
    const rejected = await text('rejected')


    // An embedded pane's own URL is never surfaced, so the wording has to CARRY it;
    // "this page" named something the reader cannot see.
    if (!/new tab/i.test(framed) || /reload this browser tab/i.test(framed)) {
      throw new Error(`FRAMED does not name an action a frame can complete: ${framed}`)
    }
    if (!/https?:\/\/\S+/i.test(framed)) {
      throw new Error(`FRAMED names no openable address: ${framed}`)
    }

    // BEFORE is the state this PR removes: a bare status and nothing else.
    if (!before.includes('HTTP 403')) throw new Error(`BEFORE is not the bare status: ${before}`)

    // A block page leads with what was OBSERVED. Detection cannot confirm a proxy
    // exists, so asserting one in the opening clause states a guess as a fact.
    if (!/^this request was refused/i.test(blocked.trim())) {
      throw new Error(`BLOCKED does not lead with the observed refusal: ${blocked}`)
    }
    if (/^your access proxy/i.test(blocked.trim())) {
      throw new Error(`BLOCKED opens by asserting a proxy it cannot confirm: ${blocked}`)
    }
    if (/expired|sign in again/i.test(blocked)) {
      throw new Error(`BLOCKED wrongly diagnoses a proxy lapse: ${blocked}`)
    }

    // AFTER must name the proxy and the recovering action.
    if (!/access proxy/i.test(after)) throw new Error(`AFTER does not name the proxy: ${after}`)
    if (!/reload/i.test(after)) throw new Error(`AFTER does not name the recovering action: ${after}`)
    if (after.includes('HTTP 403')) throw new Error(`AFTER still shows the bare status: ${after}`)


    // The negative control: AFTER must not carry the gateway remedy, which the
    // REJECTED episode shows and which cannot fix a proxy lapse.
    if (/terminal/i.test(after)) throw new Error(`AFTER carries the gateway remedy: ${after}`)
    if (!/terminal/i.test(rejected)) {
      throw new Error(`REJECTED episode is not the gateway string, so the contrast is not shown: ${rejected}`)
    }
    if (after === rejected) throw new Error('AFTER and REJECTED render the same string')

    // An unresolved key would render as the key itself; that must never ship as evidence.
    for (const [ep, body] of [['after', after], ['blocked', blocked], ['framed', framed], ['rejected', rejected]]) {
      if (body.includes('api.client.')) throw new Error(`${ep} leaked an unresolved catalog key: ${body}`)
    }

    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    await page.locator('[data-capture-root]').screenshot({
      path: `${OUT}/error-card-proxy-challenge-${theme}.png`,
    })
    console.log(`${theme}: OK`)
  } catch (e) {
    console.error(`${theme}: FAILED — ${e}`)
    failed++
  } finally {
    await ctx.close()
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
