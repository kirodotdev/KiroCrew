/**
 * Screenshots of the Context budget screen's startup-index line (#42713).
 *
 * Drives the ISOLATED capture entry (website/capture/skill-context-budget-index.html),
 * which mounts the real SkillContextBudget with `GET /api/skills/-/budget` answered
 * locally. Frames: `truncated` (143 of 212 listed, with the skill_search hint) and
 * `complete` (12 of 12, no hint), each in dark and light.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6843 --strictPort   # in another shell
 *   node scripts/capture-42713-skill-index-count.mjs http://127.0.0.1:6843 ../temp-screenshots/skill-index-count
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6843'
const OUT = process.argv[3] || '../temp-screenshots/skill-index-count'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { state: 'truncated', marker: '143 of 212 skills listed by name at session start' },
  { state: 'complete', marker: '12 of 12 skills listed by name at session start' },
]

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const s of SCENES) {
      const ctx = await browser.newContext({
        viewport: { width: 1100, height: 560 },
        deviceScaleFactor: 2,
        colorScheme: theme,
      })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(e.message))
      page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
      await page.goto(`${BASE}/capture/skill-context-budget-index.html?theme=${theme}&state=${s.state}`, { waitUntil: 'networkidle' })
      await page.waitForSelector('[data-testid="startup-index-count"]', { timeout: 20000 })
      if (!(await page.getByText(s.marker).count())) {
        console.error(`MISSING marker "${s.marker}" in ${s.state}/${theme}`); failed++
      }
      await page.screenshot({ path: `${OUT}/${s.state}-${theme}.png` })
      if (errors.length) { console.error(`PAGE ERRORS ${s.state}/${theme}:`, errors.slice(0, 3)); failed++ }
      else console.log(`ok ${s.state}-${theme}.png`)
      await ctx.close()
    }
  }
  await browser.close()
  if (failed) { console.error(`${failed} frame(s) failed`); process.exit(1) }
}

run().catch(e => { console.error(e); process.exit(1) })
