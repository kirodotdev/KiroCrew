/**
 * Screenshots of where the project picker's Browse pane opens (#17129).
 *
 * Drives the ISOLATED capture entry (website/capture/browse-start-picker.html), which
 * mounts the real ProjectPicker against the real stylesheet and theme tokens with its
 * reads stubbed on the api client. `project` is the change: with a current selection the
 * pane opens inside it, path field and rows alike. `home` is the unchanged no-selection
 * case, kept beside it so the two openings can be compared. `stale` is a selection deleted
 * since it was chosen: home listing, with a notice naming the selection.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6842 --strictPort   # in another shell
 *   node scripts/capture-browse-start-picker.mjs http://127.0.0.1:6842 ../temp-screenshots/browse-start
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6842'
const OUT = process.argv[3] || '../temp-screenshots/browse-start'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'project', q: '&start=/home/dev/projects/api-gateway', value: '/home/dev/projects/api-gateway/', row: 'src' },
  { name: 'home', q: '', value: '/home/dev/', row: 'projects' },
  { name: 'stale', q: '&start=/home/dev/projects/old-service', value: '/home/dev/', row: 'projects', notice: 'old-service' },
]

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const s of SCENES) {
      const ctx = await browser.newContext({ viewport: { width: 760, height: 620 }, deviceScaleFactor: 2, colorScheme: theme })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(e.message))
      page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
      await page.goto(`${BASE}/capture/browse-start-picker.html?theme=${theme}${s.q}`, { waitUntil: 'networkidle' })
      await page.waitForSelector(`text=${s.row}`, { timeout: 20000 })
      await page.waitForTimeout(200)
      const value = await page.getByRole('combobox').inputValue()
      if (value !== s.value) { console.error(`WRONG path field in ${s.name}/${theme}: ${value}`); failed++ }
      if (s.notice) {
        const notice = await page.getByTestId('pp-listing-error').textContent().catch(() => '')
        if (!notice.includes(s.notice)) { console.error(`MISSING notice in ${s.name}/${theme}: ${notice}`); failed++ }
      }
      await page.screenshot({ path: `${OUT}/${s.name}-${theme}.png` })
      if (errors.length) { console.error(`PAGE ERRORS ${s.name}/${theme}:`, errors.slice(0, 3)); failed++ }
      else console.log(`ok ${s.name}-${theme}.png`)
      await ctx.close()
    }
  }
  await browser.close()
  process.exit(failed ? 1 : 0)
}
run()
