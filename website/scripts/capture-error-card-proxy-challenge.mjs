/**
 * Screenshot runner for capture/error-card-proxy-challenge.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6844 --strictPort
 *   node scripts/capture-error-card-proxy-challenge.mjs http://127.0.0.1:6844 <outdir>
 *
 * Captures the sheet in both themes, element-scoped to the capture root. Every
 * episode is asserted before the frame is taken, so a screenshot cannot photograph
 * the wrong state: BEFORE must really be the bare status, CHALLENGED and FRAMED must
 * each offer the lapse as a condition rather than assert it, CHALLENGED must lead
 * with the reload and keep the refusal arm, FRAMED must name an openable address, and
 * no episode may leak an unresolved catalog key.
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { EPISODES } from '../capture/error-card-proxy-challenge.episodes.mjs'

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

    for (const ep of EPISODES) {
      await page.locator(`[data-episode="${ep}"] [data-testid="error-card"]`).waitFor({ timeout: 10000 })
    }

    const before = await text('before')
    const challenged = await text('challenged')
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

    // Both messages lead with the ACTION and offer the lapse as a CONDITION. Nothing
    // reads the body's contents any more, so asserting a lapse would state as fact
    // something no signal establishes.
    for (const [ep, body] of [['CHALLENGED', challenged], ['FRAMED', framed]]) {
      if (!/if a sign-in page appears/i.test(body)) {
        throw new Error(`${ep} does not offer the lapse as a condition: ${body}`)
      }
      if (/^your access proxy/i.test(body.trim())) {
        throw new Error(`${ep} opens by asserting a lapse it cannot confirm: ${body}`)
      }
      if (body.includes('HTTP 403')) throw new Error(`${ep} still shows the bare status: ${body}`)
      // The negative control: neither may carry the gateway remedy, which the REJECTED
      // episode shows and which cannot fix an interposed gate's refusal.
      if (/terminal/i.test(body)) throw new Error(`${ep} carries the gateway remedy: ${body}`)
    }

    if (!/^reload this browser tab/i.test(challenged.trim())) {
      throw new Error(`CHALLENGED does not lead with the recovering action: ${challenged}`)
    }
    // The block arm must survive: this message still covers the refusal it cannot explain.
    if (!/proxy or firewall/i.test(challenged)) {
      throw new Error(`CHALLENGED drops the refusal explanation: ${challenged}`)
    }

    if (!/terminal/i.test(rejected)) {
      throw new Error(`REJECTED episode is not the gateway string, so the contrast is not shown: ${rejected}`)
    }
    if (challenged === rejected) throw new Error('CHALLENGED and REJECTED render the same string')

    // An unresolved key would render as the key itself; that must never ship as evidence.
    for (const [ep, body] of [['challenged', challenged], ['framed', framed], ['rejected', rejected]]) {
      if (body.includes('api.client.')) throw new Error(`${ep} leaked an unresolved catalog key: ${body}`)
    }

    if (errors.length) throw new Error(`page errors: ${errors.join(' | ')}`)
    await page.locator('[data-capture-root]').screenshot({
      path: `${OUT}/error-card-proxy-challenge-${theme}.png`,
    })

    // A text transcript beside the frame, because a PNG added by a patch is a binary
    // blob to any reviewer who cannot fetch it -- which is every fork review lane. The
    // strings and the link are the substance; this is the part of the evidence that
    // survives being read as a diff.
    if (theme === 'light') {
      const anchors = await page
        .locator('[data-episode="framed"] a')
        .evaluateAll(nodes => nodes.map(n => ({
          text: n.textContent, href: n.getAttribute('href'),
          target: n.getAttribute('target'), rel: n.getAttribute('rel'),
        })))
      const lines = [
        '# error-card-proxy-challenge — rendered text',
        '',
        'Written by scripts/capture-error-card-proxy-challenge.mjs in the same run that',
        'takes the PNGs, from the live DOM after every assertion below it passed.',
        '',
      ]
      for (const ep of EPISODES) {
        lines.push(`## ${ep}`, '', await text(ep), '')
      }
      // The address is offered as a control, because at 13px in an anywhere-wrap
      // container a bare origin breaks mid-URL and has to be retyped.
      lines.push('## framed episode — link elements', '')
      lines.push(anchors.length === 0
        ? '(none rendered)'
        : anchors.map(a => `- text=${a.text} href=${a.href} target=${a.target} rel=${a.rel}`).join('\n'))
      lines.push('')
      writeFileSync(`${OUT}/error-card-proxy-challenge.md`, lines.join('\n'), 'utf8')
    }
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
