/**
 * Screenshots of the System Monitor page (/monitor).
 *
 *   busy         loaded box: eight rows, tight posture, cgroup gauge near the
 *                ceiling, top-consumer highlight on the two heaviest rows.
 *   unsupported  no per-process sampling: header strip present, table replaced
 *                by the explanatory notice.
 *   confirm      the Stop confirmation dialog open for the heaviest chat.
 *
 * Drives the ISOLATED capture entry (website/capture/system-monitor.html).
 * Each scene asserts a marker and the script EXITS NONZERO when one is missing,
 * so it can never quietly emit a screenshot of the wrong state.
 *
 *   npx vite --host 127.0.0.1 --port 6815 --strictPort      # in another shell
 *   node scripts/capture-system-monitor.mjs http://127.0.0.1:6815 <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6815'
const OUT = process.argv[3] || '../temp-screenshots/system-monitor'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  {
    file: 'monitor-busy.png',
    scene: 'busy',
    marker: '[data-testid="monitor-table"]',
    alsoVisible: [
      '[data-testid="monitor-posture"]',
      '[data-testid="monitor-cgroup"]',
      '[data-testid="monitor-row"] >> nth=7',
      '[data-testid="monitor-stop"]',
    ],
    absent: ['[data-testid="monitor-unavailable"]'],
  },
  {
    file: 'monitor-unsupported.png',
    scene: 'unsupported',
    marker: '[data-testid="monitor-unavailable"]',
    alsoVisible: ['[data-testid="monitor-posture"]'],
    absent: ['[data-testid="monitor-table"]', '[data-testid="monitor-cgroup"]'],
  },
  {
    file: 'monitor-stop-confirm.png',
    scene: 'busy',
    marker: '[data-testid="monitor-table"]',
    act: async (page) => {
      await page.locator('[data-testid="monitor-stop"]').first().click()
      await page.waitForSelector('[role="dialog"]', { timeout: 10000 })
    },
    alsoVisible: ['[role="dialog"] >> text=Stop runtime'],
  },
]

const b = await chromium.launch()
let failed = 0
for (const s of SCENES) {
  const page = await b.newPage({ viewport: { width: 1200, height: 900 } })
  const url = `${BASE}/capture/system-monitor.html?scene=${s.scene}&theme=dark&lang=en`
  try {
    await page.goto(url, { waitUntil: 'networkidle' })
    await page.waitForSelector(s.marker, { timeout: 10000 })
    if (s.act) await s.act(page)
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
