/**
 * Frames of the chat approval card after a terminal refusal (#14711): the
 * approval is gone, so the card withdraws its buttons, and keyboard focus moves
 * to the notice that replaced them instead of falling to the page body.
 *
 * Drives the isolated capture entry (website/capture/approval-card-terminal-focus.html),
 * which mounts the REAL ApprovalCard. Approve is pressed from the keyboard, and
 * each frame asserts before it is written that the buttons are gone, the notice
 * says the press came too late and was not recorded, and focus is on the
 * notice's wrapper.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6822 --strictPort   # in another shell
 *   node scripts/capture-approval-card-terminal-focus.mjs http://127.0.0.1:6822 ../temp-screenshots/notification-approval-refusal
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6822'
const OUT = process.argv[3] || '../temp-screenshots/notification-approval-refusal'
mkdirSync(OUT, { recursive: true })

const GONE = 'This approval has expired or was already decided'
const browser = await chromium.launch()
let failed = false

for (const [name, theme] of [['10-chat-card-404-focus', 'dark'], ['11-chat-card-404-focus-light', 'light']]) {
  const page = await browser.newPage({ viewport: { width: 760, height: 220 }, colorScheme: theme })
  await page.goto(`${BASE}/capture/approval-card-terminal-focus.html?theme=${theme}`)
  await page.waitForSelector('[data-capture-root]')
  await page.getByRole('button', { name: /Approve/ }).focus()
  await page.keyboard.press('Enter')
  const alert = page.getByRole('alert')
  await alert.waitFor()
  await page.waitForTimeout(300)
  const text = await alert.innerText()
  const buttons = await page.getByRole('button', { name: /^(Approve|Reject)$/ }).count()
  const focused = await page.evaluate(() => {
    const el = document.querySelector('[role="alert"]')
    return !!el && document.activeElement === el.closest('[tabindex="-1"]')
  })
  const ok = text.includes(GONE) && buttons === 0 && focused
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${JSON.stringify({ text, buttons, focused })}`)
  if (ok) await page.screenshot({ path: `${OUT}/${name}.png` })
  else failed = true
  await page.close()
}

await browser.close()
process.exit(failed ? 1 : 0)
