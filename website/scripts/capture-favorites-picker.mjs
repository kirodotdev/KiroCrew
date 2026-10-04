/**
 * Screenshots of the project picker's Favourites tab.
 *
 * Drives the ISOLATED capture entry (website/capture/favorites-picker.html), which
 * mounts the real ProjectPicker against the real stylesheet and theme tokens with the
 * three project reads stubbed on the api client. What a frame falsifies and a test
 * cannot: that the star reads as on or off at a glance, that it clears the row's two
 * lines of text rather than overlapping them, and that both states carry a theme token
 * for background AND text so neither disappears in dark mode. The `recent-stars` frame
 * is the one worth diffing: it shows a favourited recent filled and its unfavourited
 * siblings hollow, which is the cross-list membership comparison rendered.
 *
 * Why not the full SPA: the picker is a portalled popover anchored to a rect measured
 * inside ChatPage, which needs the app shell, a live websocket and a seeded session;
 * a half-stubbed shell renders its ERROR BOUNDARY instead, and a screenshot of the
 * wrong thing is worse evidence than none.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6841 --strictPort   # in another shell
 *   node scripts/capture-favorites-picker.mjs http://127.0.0.1:6841 ../temp-screenshots/favorites
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6841'
const OUT = process.argv[3] || '../temp-screenshots/favorites'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  { name: 'favorites', q: '', marker: 'Favorite projects' },
  { name: 'recent-stars', q: '', marker: 'Recent projects', click: 'Recent' },
  // With no favourites the picker opens on Recent (its list has rows), which is the
  // landing rule working; click through to see the empty pane itself.
  { name: 'empty', q: '&tab=empty', marker: 'Favorite projects', click: 'Favorites' },
  // The Browse pane's own toggle: a labelled row under the path field, not a third control
  // beside Back and Select.
  { name: 'browse', q: '', click: 'Browse', testId: 'pp-browse-star' },
  // Browse ON a favourite: the toggle filled and reading "Remove … from favorites".
  { name: 'browse-favorited', q: '&browse=fav', click: 'Browse', testId: 'pp-browse-star' },
  // A failed favourites read: the picker lands on Recent, so click back to see the notice.
  { name: 'read-failed', q: '&read=fail', click: 'Favorites', testId: 'pp-favorites-error' },
  // A refused star click: the write notice above the rows, which are left as they were.
  { name: 'write-failed', q: '&write=fail', click: 'Recent', star: 'pp-recent-star-0', testId: 'pp-favorite-write-error' },
]

const run = async () => {
  const browser = await chromium.launch()
  let failed = 0
  for (const theme of ['dark', 'light']) {
    for (const s of SCENES) {
      const ctx = await browser.newContext({
        viewport: { width: 760, height: 620 },
        deviceScaleFactor: 2,
        colorScheme: theme,
      })
      const page = await ctx.newPage()
      const errors = []
      page.on('pageerror', e => errors.push(e.message))
      page.on('console', m => { if (m.type() === 'error') errors.push(m.text()) })
      await page.goto(`${BASE}/capture/favorites-picker.html?theme=${theme}${s.q}`, { waitUntil: 'networkidle' })
      await page.waitForSelector('text=Favorites', { timeout: 20000 })
      if (s.click) {
        await page.getByText(s.click, { exact: true }).dispatchEvent('mousedown')
        await page.waitForTimeout(250)
      }
      if (s.star) {
        await page.getByTestId(s.star).dispatchEvent('mousedown')
        await page.waitForTimeout(250)
      }
      if (s.marker) {
        const found = await page.locator(`[aria-label="${s.marker}"]`).count()
        if (!found) { console.error(`MISSING marker ${s.marker} in ${s.name}/${theme}`); failed++ }
      }
      if (s.testId && !(await page.getByTestId(s.testId).count())) {
        console.error(`MISSING ${s.testId} in ${s.name}/${theme}`); failed++
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
