/**
 * Screenshots of what cancel-queued puts back in the composer.
 *
 * Drives the ISOLATED capture entry (website/capture/queued-cancel-restore.html),
 * which mounts the REAL QueueStack card and REAL ChatInput; the card content is
 * the real prepareSendPayload serialization and the `after` phase restores
 * through the SHIPPED restoreQueuedContent helper.
 *
 * Per scene: wait for the queued card, click its real cancel button, then
 * photograph the composer + readout. SELF-CHECKING: the script reads the
 * harness's own counters and fails loudly if a shot does not show what its
 * filename claims — `before` must show markers and zero staged chips, `after`
 * must show zero markers and both files staged.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6822 --strictPort   # in another shell
 *   node scripts/capture-queued-cancel-restore.mjs http://127.0.0.1:6822 ../temp-screenshots/queued-cancel-restore
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6822'
const OUT = process.argv[3] || '../temp-screenshots/queued-cancel-restore'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 860, height: 620 } })

async function shoot(phase, theme, name) {
  await page.goto(`${BASE}/capture/queued-cancel-restore.html?lang=en&theme=${theme}&phase=${phase}`)
  const card = page.locator('.queue-card')
  await card.waitFor({ timeout: 20000 })
  await page.waitForTimeout(300)
  await page.getByLabel('Cancel queued message').click()
  await page.waitForTimeout(400)

  const markers = Number(await page.getByTestId('marker-count').textContent())
  const staged = Number(await page.getByTestId('staged-count').textContent())
  if (phase === 'before' && (markers === 0 || staged !== 0)) {
    throw new Error(`before shot wrong: markers=${markers} staged=${staged} — expected markers>0, staged=0`)
  }
  if (phase === 'after' && (markers !== 0 || staged !== 2)) {
    throw new Error(`after shot wrong: markers=${markers} staged=${staged} — expected markers=0, staged=2`)
  }
  await page.screenshot({ path: `${OUT}/${name}`, fullPage: false })
  console.log(`captured ${name} — markers in draft: ${markers}, staged files: ${staged}`)
}

await shoot('before', 'dark', '01-before-dark.png')
await shoot('after', 'dark', '02-after-dark.png')
await shoot('after', 'light', '03-after-light.png')

await browser.close()
