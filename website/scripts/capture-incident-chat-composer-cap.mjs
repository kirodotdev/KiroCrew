/**
 * Before/after measurement + screenshots for the ChatEmbed `composerMaxHeight`
 * prop (#10881), on the IncidentChat 420px box.
 *
 * Drives the ISOLATED capture entry (website/capture/incident-chat-composer-cap.html),
 * which mounts the real components; the transcript comes from route
 * interception here. The script types a 30-line draft into the composer and
 * reads the textarea's rendered height off window.__measure(). The unit suite
 * pins the prop's plumbing under happy-dom (no layout), so this is the only
 * check that exercises the REAL box: how much of the fixed-height panel the
 * maxed-out draft actually claims.
 *
 * Assertions:
 *  - scene=before (no cap passed): the textarea reaches the shared 240px
 *    default — the defect must reproduce, or the before frame is meaningless.
 *  - scene=after (real IncidentChat): the textarea stops at the panel's own
 *    cap, 160px, and at least 240px of the 420px box is left above it.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6811 --strictPort   # in another shell
 *   node scripts/capture-incident-chat-composer-cap.mjs http://127.0.0.1:6811 ../temp-screenshots/incident-chat-composer-cap-10881
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6811'
const OUT = process.argv[3] || '../temp-screenshots/incident-chat-composer-cap-10881'
mkdirSync(OUT, { recursive: true })

const SHARED_DEFAULT_CAP = 240
const INCIDENT_CAP = 160
const BOX = 420

const TRANSCRIPT = {
  messages: [
    { role: 'user', content: 'Checkout p99 latency crossed 2s at 14:02. What changed?', cls: '' },
    { role: 'assistant', content: 'Deploy `checkout-svc` v4.12.0 landed at 13:58. Its rollout note adds a synchronous inventory re-check on every cart line.', cls: '' },
    { role: 'user', content: 'Is the inventory service itself slow?', cls: '' },
    { role: 'assistant', content: 'No — `inventory-svc` p99 is flat at 40ms. The regression is the fan-out: one call per line, 30+ lines on large carts, serialised.', cls: '' },
    { role: 'assistant', content: 'Proposed action: roll back to v4.11.3, then batch the re-check. Want me to open the rollback?', cls: '' },
  ],
  running: false,
  title: 'INC-42 · Checkout latency',
  has_more: false,
}

const LONG_DRAFT = Array.from({ length: 30 }, (_, i) => `line ${i + 1}: yes, roll back first — then batch the inventory check per cart`).join('\n')

const browser = await chromium.launch()
const page = await browser.newPage({ viewport: { width: 720, height: 560 } })
await page.route('**/api/chat/slots/**', route => route.fulfill({ json: TRANSCRIPT }))
await page.route('**/api/chat', route => route.fulfill({ status: 200, body: '' }))
let failures = 0

for (const scene of ['before', 'after']) {
  await page.goto(`${BASE}/capture/incident-chat-composer-cap.html?theme=dark&scene=${scene}`, { waitUntil: 'networkidle' })
  const textarea = page.locator('textarea')
  await textarea.waitFor()
  await page.getByText('Proposed action').waitFor()
  const resting = await page.evaluate(() => window.__measure())
  await textarea.fill(LONG_DRAFT)
  // The auto-grow runs in a layout effect after the draft state lands.
  await page.waitForTimeout(100)
  const grown = await page.evaluate(() => window.__measure())
  const expected = scene === 'before' ? SHARED_DEFAULT_CAP : INCIDENT_CAP
  const transcriptLeft = BOX - grown.textareaHeight
  console.log(
    `scene=${scene}: resting=${resting.textareaHeight}px grown=${grown.textareaHeight}px ` +
    `(expected cap ${expected}px) transcriptLeft≈${transcriptLeft}px of ${BOX}px`,
  )
  if (Math.round(grown.textareaHeight) !== expected) {
    console.error(`FAIL: scene=${scene} textarea grew to ${grown.textareaHeight}px, expected the ${expected}px cap`)
    failures++
  }
  if (scene === 'after' && transcriptLeft < 240) {
    console.error(`FAIL: scene=after leaves only ${transcriptLeft}px of the box above the draft`)
    failures++
  }
  await page.screenshot({ path: `${OUT}/${scene}-long-draft.png`, fullPage: true })
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion failure(s)`)
  process.exit(1)
}
console.log('ALL GREEN')
