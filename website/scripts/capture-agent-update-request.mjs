/**
 * Screenshots of the agent update-request card in Settings › About (issue #503).
 *
 *   none     no request — the BEFORE frame; the card must be ABSENT.
 *   pending  chat-42 asked for v0.6.0: version, requester, remaining window,
 *            Install & restart beside Decline.
 *   differs  the updater found v0.7.0 instead; the card says installing gets that.
 *
 * Drives the ISOLATED capture entry (website/capture/agent-update-request.html).
 * Each scene asserts a marker and the script EXITS NONZERO when one is missing,
 * so it can never quietly emit a screenshot of the wrong state.
 *
 *   npx vite --host 127.0.0.1 --port 6814 --strictPort      # in another shell
 *   node scripts/capture-agent-update-request.mjs http://127.0.0.1:6814 <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6814'
const OUT = process.argv[3] || '../temp-screenshots/agent-update-request'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  {
    file: 'about-no-request-before.png',
    scene: 'none',
    marker: 'text=Check for updates',
    absent: ['[data-testid="agent-update-request"]'],
  },
  {
    file: 'about-agent-request-after.png',
    scene: 'pending',
    marker: '[data-testid="agent-update-request"]',
    alsoVisible: [
      'text=An agent requested an update',
      'text=0.6.0',
      'text=chat-42',
      // Humanized expiry, not minutes:seconds.
      '[data-testid="agent-update-request-countdown"]:has-text("hours")',
      '[data-testid="agent-update-request-install"]',
      '[data-testid="agent-update-request-decline"]',
    ],
    absent: ['[data-testid="agent-update-request-differs"]'],
  },
  {
    file: 'about-agent-request-differs-after.png',
    scene: 'differs',
    marker: '[data-testid="agent-update-request-differs"]',
    // The button names the version it delivers, and it is the ONLY install
    // control on the page while the request is live.
    alsoVisible: ['[data-testid="agent-update-request-install"]:has-text("0.7.0")'],
    absent: ['[data-testid="update-card"] button'],
  },
]

const b = await chromium.launch()
let failed = 0
for (const s of SCENES) {
  const page = await b.newPage({ viewport: { width: 900, height: 1000 } })
  const url = `${BASE}/capture/agent-update-request.html?scene=${s.scene}&theme=dark&lang=en`
  try {
    await page.goto(url, { waitUntil: 'networkidle' })
    await page.waitForSelector(s.marker, { timeout: 10000 })
    for (const sel of s.alsoVisible || []) await page.waitForSelector(sel, { timeout: 10000 })
    for (const sel of s.absent || []) {
      if (await page.locator(sel).count()) throw new Error(`expected ${sel} absent in ${s.scene}`)
    }
    await page.screenshot({ path: `${OUT}/${s.file}`, fullPage: true })
    console.log(`ok   ${s.file}`)
  } catch (err) {
    failed += 1
    console.error(`FAIL ${s.file}: ${(err && err.message) || err}`)
  }
  await page.close()
}
await b.close()
process.exit(failed ? 1 : 0)
