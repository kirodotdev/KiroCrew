/**
 * Screenshot harness for the FORK-FAILURE BANNER.
 *
 * A fork failure already rendered through `ErrorNotice` on the page's SHARED
 * `action-error` slot, which now also carries its structured report. Both copies are
 * photographed, because different failures reach them: the over-capacity refusal has
 * its own sentence per direction, every other failure carries the raw wire message.
 *
 * It ASSERTS before photographing, so a scene that did not render writes no frame:
 * each must show exactly one `role="alert"` and one hand-off button (the fork notice
 * sets `askAgent`). It also prints the banner-to-composer gap, so a frame records where
 * the notice lands relative to the composer rather than only that it rendered.
 *
 * Usage: node scripts/capture-fork-failure-banner.mjs [baseUrl] [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { chromiumExecutable } from './lib/chromium-executable.mjs'

const BASE = process.argv[2] || 'http://127.0.0.1:6831'
const OUT = process.argv[3] || '../temp-screenshots/fork-failure-banner'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { scene: 'too-large', direction: 'head', theme: 'dark', expect: /earlier message/i },
  { scene: 'too-large', direction: 'head', theme: 'light', expect: /earlier message/i },
  { scene: 'too-large', direction: 'tail', theme: 'dark', expect: /later message/i },
  { scene: 'too-large', direction: 'tail', theme: 'light', expect: /later message/i },
  { scene: 'generic', direction: 'head', theme: 'dark', expect: /fork failed/i },
  { scene: 'generic', direction: 'head', theme: 'light', expect: /fork failed/i },
  // The two offsite scenes carry no direction: off the transcript the copy names the
  // recovery without one, so a head/tail expectation waits for a word never rendered.
  // Each asserts ITS OWN verb rather than a phrase both copies share -- a shared-phrase
  // expectation passed a sidebar frame that carried the grid's wording.
  { scene: 'sidebar-duplicate', direction: 'head', theme: 'dark', expect: /too large to duplicate whole/i },
  { scene: 'sidebar-duplicate', direction: 'head', theme: 'light', expect: /too large to duplicate whole/i },
  { scene: 'grid-pane', direction: 'head', theme: 'dark', expect: /too large to fork whole/i },
  { scene: 'grid-pane', direction: 'head', theme: 'light', expect: /too large to fork whole/i },
]

// A version-managed Node (mise, asdf, nvm) puts its own lib dir on LD_LIBRARY_PATH, and the
// browser inherits it and loads that older libstdc++ instead of the system one, then exits.
const browserEnv = { ...process.env }
delete browserEnv.LD_LIBRARY_PATH
// Headless rasterization falls back to software GL, so name it rather than letting the shell
// probe a GPU it has no access to and fail at screenshot time, after launch already succeeded.
const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: browserEnv,
  args: ['--disable-gpu', '--no-sandbox', '--disable-dev-shm-usage', '--use-gl=swiftshader'],
})
const page = await browser.newPage({
  viewport: { width: 900, height: 640 },
  deviceScaleFactor: 2,
})

let failed = false
for (const s of SCENES) {
  await page.goto(
    `${BASE}/capture/fork-failure-banner.html` +
      `?scene=${s.scene}&direction=${s.direction}&theme=${s.theme}`,
  )
  await page.waitForSelector('[data-capture-root]')
  await page.getByText(s.expect).first().waitFor({ timeout: 15_000 })

  const alerts = await page.locator('[role="alert"]').count()
  const handoffs = await page.getByRole('button', { name: /^ask the agent$/i }).count()

  const gap = await page.evaluate(() => {
    const notice = document.querySelector('[data-testid="action-error"]')
    const composer = document.querySelector('.p-4 > .rounded-xl')
    if (!notice || !composer) return null
    return Math.round(composer.getBoundingClientRect().top - notice.getBoundingClientRect().bottom)
  })

  const ok = alerts === 1 && handoffs === 1
  console.log(
    `${s.scene}-${s.direction}-${s.theme}: alerts=${alerts} handoffs=${handoffs} ` +
      `bannerToComposerGap=${gap}px ${ok ? 'OK' : 'MISMATCH'}`,
  )
  if (!ok) {
    failed = true
    continue
  }
  await page
    .locator('[data-capture-root]')
    .screenshot({ path: `${OUT}/${s.scene}-${s.direction}-${s.theme}.png` })
}

await browser.close()
if (failed) {
  console.error('a scene did not render one alert with the hand-off -- no misleading frame written')
  process.exit(1)
}
console.log(`wrote ${SCENES.length} screenshots to ${OUT}`)
