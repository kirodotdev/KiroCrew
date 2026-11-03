/**
 * Frames of the in-chat and command-center approval surfaces showing a gone
 * approval as STATUS, not an error (#18203): the chat approval card
 * (`ApprovalCard`), the chat tool group (`CollapsibleToolGroup`), and the
 * command-center attention card (`AttentionCard`). Each presses its own
 * Approve, which the fixtures answer with a terminal 404.
 *
 * Asserts before writing each file: the gone sentence appears as `role="status"`
 * and never `role="alert"`, and the surfaces' Approve/Reject are withdrawn.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6824 --strictPort   # in another shell
 *   node scripts/capture-chat-approval-gone-status.mjs <baseUrl> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6824'
const OUT = process.argv[3] || '../temp-screenshots/18203-chat-approval-gone-status'
const EXPECTED = 'nothing is waiting on it'
mkdirSync(OUT, { recursive: true })

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 620, height: 520 }, deviceScaleFactor: 2 })
let failed = false

// fixture -> [expected status notices, shot name prefix]
const scenes = [
  { fixture: 'chat-approval-gone-status', notices: 2, name: 'chat-approval-gone' },
  { fixture: 'attention-approval-gone-status', notices: 1, name: 'attention-approval-gone' },
]

for (const scene of scenes) {
  for (const theme of ['dark', 'light']) {
    await page.goto(`${BASE}/capture/${scene.fixture}.html?theme=${theme}`)
    const root = page.locator('[data-capture-root]')
    await root.getByRole('status').first().waitFor()
    await page.waitForTimeout(400)
    const notices = await root.getByRole('status').allInnerTexts()
    const alerts = await root.getByRole('alert').allInnerTexts()
    const live = await root.locator('button', { hasText: /^\s*(Approve|Reject|Approve once)\s*$/ }).count()

    const ok = notices.length === scene.notices && notices.every(t => t.includes(EXPECTED)) && alerts.length === 0 && live === 0
    console.log(`${scene.name} ${theme}: notices=${JSON.stringify(notices)} alerts=${JSON.stringify(alerts)} live=${live} ${ok ? 'OK' : 'MISMATCH'}`)
    if (!ok) { failed = true; continue }

    await page.screenshot({ path: `${OUT}/${scene.name}-${theme}.png` })
    console.log('wrote', `${OUT}/${scene.name}-${theme}.png`)
  }
}

await browser.close()
process.exit(failed ? 1 : 0)
