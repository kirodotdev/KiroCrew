/**
 * Screenshots of the channel approval whose command text the provider removed.
 *
 * Drives website/capture/channel-hidden-command-text.html, which mounts the REAL
 * MessageBubble so the posted marker (the thing this change edits) is in frame
 * along with the controls it describes.
 *
 * Each frame ASSERTS its state before writing the file:
 *   after (default): the body says the exact text is unverified, and the one
 *     Trust control names the blanket channel tier.
 *   --before: the identical single-tier menu under a body that says "allow
 *     once" — the promise the card never kept.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort   # in another shell
 *   node scripts/capture-channel-hidden-command-text.mjs http://127.0.0.1:6842 ../temp-screenshots/channel-hidden-command-text [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6842'
const OUT = process.argv[3] || '../temp-screenshots/channel-hidden-command-text'
const BEFORE = process.argv.includes('--before')
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'channel-hidden-command-text-dark', theme: 'dark' },
  { name: 'channel-hidden-command-text-light', theme: 'light' },
]

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 800, height: 360 }, deviceScaleFactor: 2 })

let failed = false
for (const s of SCENES) {
  const scene = BEFORE ? 'before' : 'after'
  await page.goto(`${BASE}/capture/channel-hidden-command-text.html?theme=${s.theme}&scene=${scene}`)
  await page.waitForSelector('[data-capture-root]')

  const body = await page.locator('[data-capture-root]').innerText()
  const trust = page.locator('[data-capture-root] button', { hasText: /^Trust/ })
  await trust.first().waitFor()
  const trustLabel = (await trust.first().innerText()).trim()

  const markerOk = BEFORE
    ? /allow once/i.test(body) && !/exact text unverified/i.test(body)
    : /exact text unverified/i.test(body) && !/allow once/i.test(body)
  // The channel card offers exactly one tier, so that tier IS the control: its
  // own label names the channel-wide, restart-surviving grant, and there is no
  // menu to open. A lone floating menu item read as a tooltip to cold readers.
  const ok = markerOk
    && /Trust all tools in this channel/i.test(trustLabel)
    && (await page.locator('[role="menuitem"]').count()) === 0
  console.log(`${s.name} (${scene}): marker=${markerOk} trust=${JSON.stringify(trustLabel)} ${ok ? 'OK' : 'MISMATCH'}`)
  if (!ok) { failed = true; continue }

  await page.screenshot({ path: `${OUT}/${s.name}${BEFORE ? '-before' : ''}.png` })
}

await browser.close()
process.exit(failed ? 1 : 0)
