/**
 * Screenshots of the Enter that an IME guard declines.
 *
 * Drives the ISOLATED capture entry (website/capture/ime-enter-swallow.html),
 * which mounts the REAL ChatInput and prints its controlled draft.
 *
 * The sequence per scene is the one a Chinese IME produces when a candidate is
 * confirmed and the user immediately presses Enter to send:
 *
 *   1. type the text                       (draft is one line)
 *   2. dispatch compositionstart/end       (the candidate commits)
 *   3. press Enter FOR REAL                (send, or the guard declines it)
 *
 * Step 3 goes through Playwright's keyboard rather than a dispatched event on
 * purpose: the newline under review is the browser's DEFAULT ACTION for a key
 * no handler consumed, and a synthetic keydown carries no default action at all
 * — it would come out clean whether the bug is present or not.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort   # in another shell
 *   node scripts/capture-ime-enter-swallow.mjs http://127.0.0.1:6821 ../temp-screenshots/ime-enter-swallow after
 *
 * The third argument only names the files; run it once on the branch and once
 * with the two source files checked out from the base to get the pair.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/ime-enter-swallow'
const PHASE = process.argv[4] || 'after'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 860, height: 460 } })

async function shoot(theme, name) {
  await page.goto(`${BASE}/capture/ime-enter-swallow.html?lang=zh-CN&theme=${theme}`)
  const box = page.locator('textarea').first()
  await box.waitFor({ timeout: 20000 })
  await box.click()
  await box.type('晚上好')

  // The commit of an IME candidate, as the browser reports it. React reads these
  // through its own listeners and does not check isTrusted, so the guard sees
  // exactly what a real candidate confirmation gives it.
  await page.evaluate(() => {
    const ta = document.querySelector('textarea')
    ta.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true }))
    ta.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: '晚上好' }))
  })

  // Immediately after, inside the guard's post-compositionEnd window — the same
  // place a fast typist's send lands.
  await page.keyboard.press('Enter')
  await page.waitForTimeout(150)

  const lines = await page.getByTestId('line-count').textContent()
  const sends = await page.getByTestId('send-count').textContent()
  await page.screenshot({ path: `${OUT}/${name}`, fullPage: false })
  console.log(`captured ${name} — draft lines: ${lines}, sends: ${sends}`)
}

await shoot('dark', `${PHASE}-dark.png`)
await shoot('light', `${PHASE}-light.png`)

await browser.close()
