/**
 * Screenshot + assertion runner for capture/tool-call-states.html.
 *
 * From website/:
 *   npx vite --host 127.0.0.1 --port 6817 --strictPort
 *   node scripts/capture-tool-call-states.mjs http://127.0.0.1:6817 \
 *     ../temp-screenshots/tool-call-states
 *
 * The assertion matters more than the image: each state is derived from store
 * shape, so a mis-seeded fixture silently renders every row as "done" and the
 * frame would look plausible while proving nothing. The probe reads each row's
 * icon colour and checks all five states are actually distinct.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6817'
const OUT = process.argv[3] || '../temp-screenshots/tool-call-states'

mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
let failed = 0

const variants = [
  { name: 'dark', theme: 'dark', width: 900, height: 700 },
  { name: 'light', theme: 'light', width: 900, height: 700 },
  { name: 'dark-narrow', theme: 'dark', width: 390, height: 760, narrow: true },
]

for (const { name, theme, width, height, narrow = false } of variants) {
  const ctx = await browser.newContext({
    viewport: { width, height },
    deviceScaleFactor: 2,
    colorScheme: theme,
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))

  try {
    await page.goto(`${BASE}/capture/tool-call-states.html?theme=${theme}&width=${narrow ? 'narrow' : 'wide'}`, { waitUntil: 'networkidle' })
    await page.waitForSelector('[data-capture-root]', { timeout: 15000 })
    // The pill reveal + shimmer animations need to settle, and the spinner must
    // be caught mid-rotation rather than at frame zero.
    await page.waitForTimeout(1200)

    const rows = await page.evaluate(() => {
      // Every pill is a button whose label starts with the tool name; read the
      // leading icon's computed colour, which is what encodes the state.
      const pills = [...document.querySelectorAll('button')]
        .filter(b => b.querySelector('svg') && b.textContent?.trim())
      return pills.map(b => {
        const svg = b.querySelector('svg')
        return {
          label: (b.textContent || '').trim().slice(0, 46),
          icon: svg ? getComputedStyle(svg).color : '',
          spinning: svg ? svg.className.baseVal?.includes('animate-spin') || false : false,
        }
      })
    })

    const overflowsViewport = await page.evaluate(() =>
      document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    )

    await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/${name}.png` })

    let frameFailed = 0
    if (!rows.some(r => r.label.includes('sed ×4 · index.js')) || rows.some(r => r.label.includes('/bin/zsh -lc'))) {
      frameFailed++
      console.error(`FAIL ${name}: oversized shell title did not collapse to "sed ×4 · index.js"`)
    }
    const colours = new Set(rows.map(r => r.icon))
    // done / running / pending / rejected / auto-denied must not collapse into
    // one another: ok, accent, warn and danger are four distinct tokens (pending
    // and auto-denied legitimately share warn).
    if (colours.size < 4) {
      frameFailed++
      console.error(`FAIL ${theme}: only ${colours.size} distinct icon colours across ${rows.length} rows — states collapsed, fixture is not driving them`)
    }
    if (!rows.some(r => r.spinning)) {
      frameFailed++
      console.error(`FAIL ${theme}: no spinning icon — the running state was not reached (is chat.slotRunning true?)`)
    }
    if (overflowsViewport) {
      frameFailed++
      console.error(`FAIL ${name}: capture root overflows the ${width}px viewport`)
    }
    if (errors.length) {
      frameFailed++
      console.error(`FAIL ${name}: ${errors.length} page error(s)\n  ${errors.join('\n  ')}`)
    }
    failed += frameFailed

    if (!frameFailed) {
      console.log(`\nok   ${name}.png — ${rows.length} pills, ${colours.size} distinct icon colours`)
      for (const r of rows) {
        console.log(`     ${r.spinning ? 'spin' : '    '} ${r.icon.padEnd(24)} ${r.label}`)
      }
    }
  } catch (err) {
    failed++
    console.error(`FAIL ${name}: ${err.message}`)
  }
  await ctx.close()
}

await browser.close()
if (failed) {
  console.error(`\n${failed} assertion(s) failed — the frames do not show the states they claim.`)
  process.exit(1)
}
