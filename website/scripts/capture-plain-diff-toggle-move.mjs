/**
 * Screenshot harness for moving the Plain diffs toggle from Display → View to
 * Chat → Messages.
 *
 * The whole PR is a relocation, so the evidence has to show BOTH ends: the
 * toggle present and working in its new home, and gone from its old one. A
 * shot of the new home alone would look identical to a PR that duplicated the
 * control, which is the failure mode worth ruling out — two switches over one
 * localStorage key.
 *
 * Runs the REAL built SPA (website/dist) behind this folder's shared
 * `lib/serve-dist.mjs` (SPA fallback + the server-routed /logo.png the real
 * dashboard serves outside the bundle), answering every /api/** call from
 * fixtures. No gateway, no dashboard auth, no kiro-cli spawn.
 *
 * Shots:
 *  1. Chat → Messages, toggle off — beside File Change Chips, the sibling
 *     control it shares a surface with (how a diff reads in the transcript).
 *  2. Chat → Messages, toggle on — plus an assertion that the click wrote
 *     `mc-diff-plain`, the key PierrePatch and DiffBlock actually read. The
 *     row is browser-local, so nothing on the wire would prove it persisted.
 *  3. Display → View, toggle absent — asserted, not just photographed.
 *
 * Labels are read from the CATALOGS, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-plain-diff-toggle-move.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/plain-diff-toggle-chat-messages'
// fileURLToPath, not URL.pathname: on Windows .pathname yields "/C:/…", which
// join() then turns into an invalid "\C:\…" and every read fails with ENOENT.
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))

mkdirSync(OUT, { recursive: true })

const manual = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8'))
const generated = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8'))
const PLAIN_DIFF = manual.settings?.chat?.plainDiff?.label
const PLAIN_DIFF_DESC = manual.settings?.chat?.plainDiff?.description
const FILE_CHIPS = generated.pages?.settings?.chatPanel?.file_change_chips
if (!PLAIN_DIFF || !PLAIN_DIFF_DESC || !FILE_CHIPS) {
  throw new Error('catalog keys missing — settings.chat.plainDiff.* or chatPanel.file_change_chips renamed?')
}
// The move is the point: a stale `settings.display.plainDiff` left behind would
// mean the old key is still live and some surface may still resolve it.
if (manual.settings?.display?.plainDiff) {
  throw new Error('settings.display.plainDiff still in en.manual.json — the key move is incomplete')
}

const PLAIN_DIFF_KEY = 'mc-diff-plain'

const PROJECT = '/home/user/project'
const fixedApi = makeFixedApi(PROJECT)

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({
  viewport: { width: 1400, height: 900 },
  // Settings rows are 12–13px type; a 1x shot renders soft on GitHub.
  deviceScaleFactor: 2,
})
const page = await context.newPage()

await page.routeWebSocket(/\/api\/ws/, () => {})

await page.route('**/api/**', route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/chat/slots') return json(route, [])
  return handleBootRoute(route, path, { project: PROJECT, fixedApi })
})

await page.addInitScript(() => {
  localStorage.clear()
  localStorage.setItem('mc-theme', 'dark')
  localStorage.setItem('mc-onboarded', '1')
  localStorage.setItem('mc-import-onboarded', '1')
  localStorage.setItem('mc-privacy-acked', '1')
})

// ── Shots 1 & 2: the new home, Chat → Messages ─────────────────────────────
await page.goto(`${base}/settings?tab=chat`, { waitUntil: 'domcontentloaded' })

const toggle = page.getByRole('switch', { name: PLAIN_DIFF })
await toggle.waitFor({ state: 'visible', timeout: 15_000 })
// Panels finish their fixture fetches well inside this; a settling wait keeps
// the background out of any loading skeleton state in the shot.
await page.waitForTimeout(600)

// Frame the pair, not the row: the claim is that the toggle sits with File
// Change Chips, so both have to be in the same shot.
await page.getByText(FILE_CHIPS, { exact: true }).scrollIntoViewIfNeeded()
await page.waitForTimeout(300)
if (await toggle.getAttribute('aria-checked') !== 'false') {
  throw new Error('expected the toggle off on a fresh profile — highlighted diffs are the default')
}
await page.screenshot({ path: join(OUT, '01-chat-messages-toggle-off.png'), fullPage: false })
console.log('captured 01-chat-messages-toggle-off.png')

await toggle.click()
await page.waitForFunction(
  key => localStorage.getItem(key) === '1',
  PLAIN_DIFF_KEY,
  { timeout: 5_000 },
)
if (await toggle.getAttribute('aria-checked') !== 'true') {
  throw new Error('switch did not follow the stored value — inverted checkbox?')
}
await page.screenshot({ path: join(OUT, '02-chat-messages-toggle-on.png'), fullPage: false })
console.log(`captured 02-chat-messages-toggle-on.png (${PLAIN_DIFF_KEY}=1 asserted)`)

// ── Shot 3: the old home, Display → View, now without it ───────────────────
// A fresh load rather than a tab click: the assertion below is an ABSENCE, and
// a stale Chat panel still mounted behind a tab transition would satisfy the
// locator and hide a duplicate.
await page.goto(`${base}/settings?tab=display`, { waitUntil: 'domcontentloaded' })
// Language is the View section's first row — waiting on it proves the panel
// mounted, so the absence below is a real absence and not an early shot.
await page.getByText(manual.settings.display.language.label, { exact: true }).waitFor({ timeout: 15_000 })
await page.waitForTimeout(600)
if (await page.getByRole('switch', { name: PLAIN_DIFF }).count() !== 0) {
  throw new Error('Plain diffs still renders on the Display tab — the move left a duplicate')
}
await page.screenshot({ path: join(OUT, '03-display-view-no-toggle.png'), fullPage: false })
console.log('captured 03-display-view-no-toggle.png (absence asserted)')

await browser.close()
srv.close()
