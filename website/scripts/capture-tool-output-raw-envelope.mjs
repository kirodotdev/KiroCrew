/**
 * Screenshot + assertions for capture/tool-output-raw-envelope.html (issue #7799).
 *
 * From website/:
 *   KIROCREW_PORT=1 npx vite --host 127.0.0.1 --port 6831 --strictPort &
 *   node scripts/capture-tool-output-raw-envelope.mjs http://127.0.0.1:6831 \
 *     ../temp-screenshots/tool-output-raw-envelope
 *
 * (KIROCREW_PORT is pinned to an unused port on purpose: vite.config.ts proxies
 * /api to localhost:${KIROCREW_PORT||5476}, and 5476 is a real gateway. This
 * fixture makes no API calls, so any request that appears is a bug to see fail,
 * not to route at someone's live instance.)
 *
 * The assertions carry the proof. "An Output tab appeared" is only meaningful
 * against the state that preceded it, so the run pins the pair: with an empty
 * `meta.output` the panel offers NO section control and NO payload box, and with
 * the serialised rawOutput it offers an Output section whose box contains the
 * payload. A regression fails the run rather than producing a plausible picture.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/tool-output-raw-envelope'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0
const check = (name, ok, detail) => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? ` — ${detail}` : ''}`)
  if (!ok) failed++
}

for (const theme of ['dark', 'light']) {
  const ctx = await browser.newContext({
    viewport: { width: 820, height: 900 },
    deviceScaleFactor: 1,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  const apiHits = []
  page.on('pageerror', e => errors.push(String(e)))
  page.on('request', r => {
    if (new URL(r.url()).pathname.startsWith('/api/')) apiHits.push(r.url())
  })

  await page.goto(`${BASE}/capture/tool-output-raw-envelope.html?theme=${theme}`, {
    waitUntil: 'networkidle',
  })
  await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
  // framer-motion's height:auto entrance and the segmented pill spring settle
  // within a few hundred ms; measuring mid-animation reads a transient height.
  await page.waitForTimeout(900)

  const probe = await page.evaluate(() => {
    const out = {}
    for (const state of ['before', 'after', 'after-both']) {
      const host = document.querySelector(`[data-state="${state}"]`)
      if (!host) continue
      const labels = [...host.querySelectorAll('button')].map(b => (b.textContent || '').trim())
      const box = host.querySelector('pre, table')
      out[state] = {
        sectionButtons: labels.filter(l => l === 'Input' || l === 'Output'),
        modeButtons: labels.filter(l => l === 'Formatted' || l === 'Raw'),
        // A one-section panel names the section with a plain label instead.
        sectionLabel:
          [...host.querySelectorAll('span')]
            .map(s => (s.textContent || '').trim())
            .find(t => t === 'Input' || t === 'Output') || null,
        payloadText: box ? (box.textContent || '').replace(/\s+/g, ' ').trim() : null,
        hasPayloadBox: !!box,
      }
    }
    return out
  })

  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${theme}.png` })

  console.log(`\n--- ${theme} ---`)
  check(`${theme}: no page errors`, errors.length === 0, errors[0])
  check(`${theme}: fixture made no /api calls`, apiHits.length === 0, apiHits[0])

  const b = probe.before
  check(`${theme}/before: no section control`, b.sectionButtons.length === 0, b.sectionButtons.join(','))
  check(`${theme}/before: no section label either`, b.sectionLabel === null, `${b.sectionLabel}`)
  check(`${theme}/before: no payload box`, b.hasPayloadBox === false)

  const a = probe.after
  check(`${theme}/after: the Output section is named`, a.sectionLabel === 'Output', `${a.sectionLabel}`)
  check(`${theme}/after: a payload box is rendered`, a.hasPayloadBox === true)
  check(
    `${theme}/after: the box carries the captured rawOutput`,
    !!a.payloadText && a.payloadText.includes('notEnabled') && a.payloadText.includes('retracted'),
    a.payloadText,
  )
  // One section only: an Input/Output toggle here would offer an empty side.
  check(`${theme}/after: no two-way toggle for an output-only call`, a.sectionButtons.length === 0)

  const ab = probe['after-both']
  check(
    `${theme}/after-both: a real Input/Output toggle`,
    ab.sectionButtons.length === 2,
    ab.sectionButtons.join(','),
  )
  check(
    `${theme}/after-both: Output is the section shown`,
    !!ab.payloadText && ab.payloadText.includes('exitCode'),
    ab.payloadText,
  )
  check(`${theme}/after-both: JSON payload offers Raw/Formatted`, ab.modeButtons.length === 2)

  const dim = await page.locator('[data-capture-root]').boundingBox()
  check(
    `${theme}: frame within the 2000px capture cap`,
    dim.width <= 2000 && dim.height <= 2000,
    `${Math.round(dim.width)}x${Math.round(dim.height)}`,
  )

  await ctx.close()
}

await browser.close()
console.log(failed ? `\n${failed} check(s) FAILED` : '\nall checks passed')
process.exit(failed ? 1 : 0)
