/** Real-browser evidence for #29616: a chat-transcript link to a missing file
 *  reads and behaves like the inline copy chip instead of opening a dead
 *  dashboard tab, while a root-relative in-app route link keeps working.
 *
 * Drives website/capture/missing-file-link.html (the REAL MarkdownRenderer over
 * a stubbed `/api/file-kinds` probe that answers `missing`). Asserted per theme:
 *  - a missing FILE link (`/tmp/x.md`, a project-relative `notes/guide.md`, and
 *    `~/notes.md`) carries no `target="_blank"` and renders muted;
 *  - the in-app ROUTE link (`/settings/security/rules`) keeps `target="_blank"`
 *    and the accent link colour.
 *
 * Run from the fix branch the shot shows the AFTER state; run from main (fix
 * reverted) it shows the BEFORE state (every file link accent + target=_blank).
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6829 --strictPort    # in another shell (website/)
 *   node scripts/capture-29616-missing-file-link.mjs http://127.0.0.1:6829 ../temp-screenshots/29616
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6829'
const OUT = process.argv[3] || '../temp-screenshots/29616'
const PHASE = process.argv[4] || 'after' // 'before' when run against main
mkdirSync(OUT, { recursive: true })

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => {
  console.log(`${label} => ${ok ? 'OK' : 'FAIL'}`)
  if (!ok) failures++
}

for (const theme of ['dark', 'light']) {
  const page = await browser.newPage({ viewport: { width: 760, height: 360 } })
  page.on('pageerror', e => { console.error(`[${theme}] pageerror:`, e.message); failures++ })
  await page.goto(`${BASE}/capture/missing-file-link.html?theme=${theme}`, { waitUntil: 'networkidle' })

  const fileLink = page.getByRole('link', { name: 'the note' })      // /tmp/x.md
  const guideLink = page.getByRole('link', { name: 'the guide' })    // notes/guide.md
  const homeLink = page.getByRole('link', { name: 'my notes' })      // ~/notes.md
  const routeLink = page.getByRole('link', { name: 'the rules page' })

  await routeLink.waitFor({ state: 'visible', timeout: 15000 })
  // Let the batch probe settle so the file links reach their final dress.
  await page.waitForTimeout(400)

  const routeTarget = await routeLink.getAttribute('target')
  const routeClass = await routeLink.getAttribute('class')
  check(`[${theme}] route link keeps target=_blank`, routeTarget === '_blank')
  check(`[${theme}] route link keeps the accent colour`, !!routeClass && routeClass.includes('text-accent'))

  for (const [name, link] of [['file', fileLink], ['guide', guideLink], ['home', homeLink]]) {
    const target = await link.getAttribute('target')
    const cls = await link.getAttribute('class')
    if (PHASE === 'before') {
      check(`[${theme}] BEFORE ${name}: accent + target=_blank (dead tab)`,
        target === '_blank' && !!cls && cls.includes('text-accent'))
    } else {
      check(`[${theme}] AFTER ${name}: muted, no target`, target === null && !!cls && cls.includes('text-muted'))
    }
  }

  await page.mouse.move(5, 5)
  await page.waitForTimeout(150)
  await page.screenshot({ path: `${OUT}/${theme}-${PHASE}.png` })
  await page.close()
}

await browser.close()
if (failures) {
  console.error(`${failures} assertion(s) failed`)
  process.exit(1)
}
console.log(`done - evidence in ${OUT} (phase=${PHASE})`)
