/**
 * Screenshots of the composer's `$` skill picker over nested skills (#42694).
 *
 * Drives the ISOLATED capture entry (website/capture/skill-picker-nested.html),
 * which mounts the real SkillPickerMenu against the real stylesheet and theme
 * tokens with `api.skills` stubbed to the issue's five grouped skills plus one
 * flat one. Frames: the whole catalog (name over path on each nested row, the
 * flat `$grill` row unchanged), `$ops` (found by name prefix and path segment),
 * `$data-warehouse` (two name-prefix matches), `$oncall` (a middle segment) and
 * `$escalation` (two groups ending in the same leaf, both listed) and a 320px
 * composer, where the script also fails on any sideways listbox overflow.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort   # in another shell
 *   node scripts/capture-skill-picker-nested.mjs http://127.0.0.1:6842 ../temp-screenshots/skill-picker-nested
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6842'
const OUT = process.argv[3] || '../temp-screenshots/skill-picker-nested'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'all', q: '', marker: '$grill' },
  { name: 'ops', q: 'ops', marker: '$handoff-summary' },
  { name: 'data-warehouse', q: 'data-warehouse', marker: '$freshness-check' },
  { name: 'oncall', q: 'oncall', marker: '$handoff-summary' },
  { name: 'escalation', q: 'escalation', marker: '$support/triage/escalation' },
  // A 320px composer: name, path and description must each truncate inside the
  // row instead of pushing the listbox into a sideways scroll.
  { name: 'narrow-320', q: 'quarterly', marker: '$quarterly-close', width: 320 },
]

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const s of SCENES) {
      const ctx = await browser.newContext({
        viewport: { width: s.width ?? 560, height: 420 },
        deviceScaleFactor: 2,
        colorScheme: theme,
      })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(e.message))
      page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
      await page.goto(`${BASE}/capture/skill-picker-nested.html?theme=${theme}&q=${encodeURIComponent(s.q)}`, { waitUntil: 'networkidle' })
      // A row, or the settled "No matching skills" copy: the empty state is a
      // frame too (it is what the picker on main shows for `$ops`).
      await page.waitForSelector('[role="listbox"] [role="option"], [role="listbox"] [role="status"]', { timeout: 20000 })
      if (!(await page.getByText(s.marker, { exact: true }).count())) {
        console.error(`MISSING marker ${s.marker} in ${s.name}/${theme}`); failed++
      }
      // The listbox must not scroll sideways at any width.
      const overflow = await page.locator('[role="listbox"]').evaluate(el => el.scrollWidth - el.clientWidth)
      if (overflow > 0) { console.error(`SIDEWAYS OVERFLOW ${overflow}px in ${s.name}/${theme}`); failed++ }
      await page.screenshot({ path: `${OUT}/${s.name}-${theme}.png` })
      if (errors.length) { console.error(`PAGE ERRORS ${s.name}/${theme}:`, errors.slice(0, 3)); failed++ }
      else console.log(`ok ${s.name}-${theme}.png`)
      await ctx.close()
    }
  }
  await browser.close()
  if (failed) { console.error(`${failed} frame(s) failed`); process.exit(1) }
}

run().catch(e => { console.error(e); process.exit(1) })
