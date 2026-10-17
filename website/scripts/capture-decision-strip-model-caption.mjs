/**
 * Screenshots + assertions for the routing receipt after the baseline caption goes.
 *
 * Drives website/capture/decision-strip-model-caption.html, which mounts the REAL
 * DecisionStrip over the REAL gateway record shape.
 *
 * The assertions are the point: the collapsed row must NOT carry the caption, and
 * the expanded panel MUST still name the baseline. A change that dropped the fact
 * entirely, rather than moving it, photographs identically and fails here.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6845 --strictPort   # in another shell
 *   node scripts/capture-decision-strip-model-caption.mjs http://127.0.0.1:6845 ../temp-screenshots/decision-strip-model-caption
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6845'
const OUT = process.argv[3] || '../temp-screenshots/decision-strip-model-caption'
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 900, height: 620 }

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, older than
// the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0

function check(label, ok, detail) {
  console.log(`${label.padEnd(46)}: ${ok ? 'ok' : 'FAIL'}${detail ? `  ${detail}` : ''}`)
  if (!ok) failures++
}

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: VIEWPORT })
  await page.goto(`${BASE}/capture/decision-strip-model-caption.html?theme=${theme}`, {
    waitUntil: 'networkidle',
  })
  await page.waitForSelector('[data-episode="expanded"] [data-testid="decision-strip-model-pick"]')

  const collapsed = page.locator('[data-episode="collapsed"]')
  const expanded = page.locator('[data-episode="expanded"]')

  const pick = (await collapsed.locator('[data-testid="decision-strip-model-pick"]').innerText())
    .replace(/\s+/g, ' ')
    .trim()
  check(`${theme} collapsed names tier and model`, pick.includes('complex') && pick.includes('gpt-5.6-terra'), `"${pick}"`)
  check(
    `${theme} collapsed carries NO baseline caption`,
    (await collapsed.locator('[data-testid="decision-strip-model-baseline"]').count()) === 0,
  )
  const collapsedText = (await collapsed.innerText()).replace(/\s+/g, ' ')
  check(`${theme} collapsed never says "default:"`, !collapsedText.includes('default:'))

  const panel = (await expanded.innerText()).replace(/\s+/g, ' ')
  check(
    `${theme} expanded still names the baseline`,
    panel.includes('Model without routing') && panel.includes('gpt-5.6-sol'),
  )

  const shot = `${OUT}/decision-strip-model-caption-${theme}.png`
  await page.locator('[data-capture-root]').screenshot({ path: shot })
  console.log(`  -> ${shot}`)
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`\n${failures} assertion(s) failed.`)
  process.exit(1)
}
console.log('\nthe receipt drops the caption and keeps the fact.')
