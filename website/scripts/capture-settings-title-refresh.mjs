/**
 * Screenshot harness for auto-title refresh in Settings -> Chat -> Sessions:
 * the "Refresh auto titles" toggle (`dashboard.title_refresh_enabled`) and,
 * below it while it is on, "Auto-title refresh interval (turns)"
 * (`dashboard.title_refresh_every_turns`), a +/- stepper whose box also takes
 * a typed number. Both save through PATCH /api/config/kirocrew and read back
 * from GET /api/config/kirocrew. The harness answers that route from one
 * stateful fixture, so a save is read back the way the gateway would return
 * it, and the frames show what the server holds rather than an optimistic
 * display that a refetch would undo.
 *
 * Runs the REAL built SPA (website/dist) behind this folder's shared
 * `lib/serve-dist.mjs`, answering every other /api/** call from fixtures. No
 * gateway, no dashboard auth, no kiro-cli spawn.
 *
 * Shots:
 *  1. Sessions page, nothing stored: the toggle is on and the interval box
 *     shows "Default", the built-in schedule, with − disabled.
 *  2. After typing 50 and Enter: the box reads 50, and the harness asserts the
 *     PATCH carried exactly that value.
 *  3. The interval row's info tip open, so its full explanation is readable.
 *  4. Light theme, the same cadence, so the rows are shown in both themes.
 *  5. The toggle switched off: the harness asserts the PATCH carried false and
 *     the interval row is gone.
 *  6. A failed save: switching the toggle back on answers 500, the "Failed to
 *     save title refresh setting" banner shows, and the toggle reads off again.
 *
 * Labels are read from the CATALOG, so a key rename breaks the capture loudly
 * instead of silently screenshotting the wrong element.
 *
 * Usage: node scripts/capture-settings-title-refresh.mjs [outDir]
 */
import { mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { json } from './lib/boot-api.mjs'
import { openSettingsPage } from './lib/settings-capture.mjs'

const OUT = process.argv[2] || '../temp-screenshots/settings-title-refresh'
const LOCALES = fileURLToPath(new URL('../src/i18n/locales/', import.meta.url))
const KEY = 'dashboard.title_refresh_every_turns'
const ENABLED_KEY = 'dashboard.title_refresh_enabled'

mkdirSync(OUT, { recursive: true })

const chat = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8')).pages?.settings?.chatPanel
const MORE_INFO = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8')).components?.infoTip?.more_information
const LABEL = chat?.title_refresh_every_turns
const TOGGLE = chat?.title_refresh_enabled
const DEFAULT = chat?.title_refresh_default
const SAVE_FAILED = chat?.failed_to_save_title_refresh
if (!LABEL || !TOGGLE || !DEFAULT || !SAVE_FAILED || !MORE_INFO) {
  throw new Error('catalog keys missing -- pages.settings.chatPanel.title_refresh_* or components.infoTip.more_information renamed?')
}

const { browser, page, srv, base } = await openSettingsPage({ tab: 'chat' })

// Registered after the shared catch-all, so these take precedence for their paths.
// The shared fixture answers /api/models with an object, which the model catalog
// reads as a degraded list and flags with a "Failed to load config." banner that
// has nothing to do with these rows; a one-entry live list keeps the frame clean.
await page.route('**/api/models', route => json(route, [{ model_name: 'auto', description: 'Auto' }]))
const stored = {}
// Frame 6 arms this: the next PATCH is recorded but answered 500 and not stored,
// so the refetch after the failure reads back the value the server still holds.
let failNextPatch = false
const patches = []
await page.route('**/api/config/kirocrew', route => {
  const req = route.request()
  if (req.method() === 'PATCH') {
    const { path, value } = req.postDataJSON()
    patches.push({ path, value })
    if (failNextPatch) {
      failNextPatch = false
      return json(route, { error: 'config file is read-only' }, 500)
    }
    if (path === KEY) stored.title_refresh_every_turns = value
    if (path === ENABLED_KEY) stored.title_refresh_enabled = value
    return json(route, { ok: true })
  }
  return json(route, Object.keys(stored).length ? { dashboard: { ...stored } } : {})
})
const sentFor = key => patches.filter(p => p.path === key).map(p => p.value)

await page.goto(`${base}/settings/chat/sessions`, { waitUntil: 'domcontentloaded' })
const row = page.locator(`[data-setting-key="${KEY}"]`)
const toggle = page.getByRole('switch', { name: TOGGLE })
const box = row.getByRole('spinbutton', { name: LABEL })
const inc = row.getByRole('button', { name: /increase/i })
const dec = row.getByRole('button', { name: /decrease/i })

async function waitForRows() {
  await row.waitFor({ state: 'visible', timeout: 15_000 })
  await page.waitForFunction(el => !el.disabled, await inc.elementHandle())
  await toggle.scrollIntoViewIfNeeded()
  await page.waitForTimeout(400)
}
// An empty box shows its placeholder, which is what the reader sees.
const readout = () => box.evaluate(el => el.value === '' ? el.placeholder : el.value)

await waitForRows()
if (!(await toggle.isChecked())) throw new Error('expected the toggle to be on by default')
if ((await readout()) !== DEFAULT) throw new Error(`expected "${DEFAULT}" with nothing stored, saw ${await readout()}`)
if (!(await dec.isDisabled())) throw new Error('expected − to be disabled at Default')
await page.screenshot({ path: join(OUT, '01-sessions-built-in-schedule.png'), fullPage: false })
console.log('captured 01-sessions-built-in-schedule.png')

await box.fill('50')
await box.press('Enter')
await page.waitForTimeout(600)
const typed = sentFor(KEY)
if (typed.length !== 1 || typed[0] !== 50) throw new Error(`expected one PATCH of 50, saw ${JSON.stringify(typed)}`)
if ((await readout()) !== '50') throw new Error(`expected the box to read back 50, saw ${await readout()}`)
await page.mouse.move(0, 0)
await page.screenshot({ path: join(OUT, '02-sessions-typed-50.png'), fullPage: false })
console.log('captured 02-sessions-typed-50.png')

await row.getByRole('button', { name: MORE_INFO }).hover()
await page.getByRole('tooltip').waitFor({ state: 'visible', timeout: 5_000 })
await page.waitForTimeout(300)
await page.screenshot({ path: join(OUT, '03-sessions-info-tip.png'), fullPage: false })
console.log('captured 03-sessions-info-tip.png')

// Light theme, same cadence. This one overrides the shared preamble's dark seed
// on the reload only while Chromium runs init scripts in the order they were
// added: Playwright does not define that order.
await page.addInitScript(() => localStorage.setItem('mc-theme', 'light'))
await page.route('**/api/theme/boot', route => json(route, { mode: 'light', theme: '' }))
await page.reload({ waitUntil: 'domcontentloaded' })
// The pointer still rests on the info tip from frame 3; move it off so the tip closes.
await page.mouse.move(0, 0)
await waitForRows()
if ((await readout()) !== '50') throw new Error(`expected the box to read back 50 in light theme, saw ${await readout()}`)
await page.screenshot({ path: join(OUT, '04-sessions-light.png'), fullPage: false })
console.log('captured 04-sessions-light.png')

await toggle.click()
await row.waitFor({ state: 'detached', timeout: 5_000 })
await page.waitForTimeout(400)
const off = sentFor(ENABLED_KEY)
if (off.length !== 1 || off[0] !== false) throw new Error(`expected one PATCH of false, saw ${JSON.stringify(off)}`)
if (await toggle.isChecked()) throw new Error('expected the toggle to read back off')
await page.screenshot({ path: join(OUT, '05-sessions-refresh-off.png'), fullPage: false })
console.log('captured 05-sessions-refresh-off.png')

// A save that fails. The click shows the toggle on at once, the PATCH answers
// 500, and the failure raises the banner and rolls the toggle back to off.
failNextPatch = true
await toggle.click()
const banner = page.getByRole('alert').filter({ hasText: SAVE_FAILED })
await banner.waitFor({ state: 'visible', timeout: 5_000 })
await page.waitForFunction(el => el.getAttribute('aria-checked') === 'false', await toggle.elementHandle(), { timeout: 5_000 })
const failed = sentFor(ENABLED_KEY)
if (failed.length !== 2 || failed[1] !== true) throw new Error(`expected a second PATCH of true, saw ${JSON.stringify(failed)}`)
if (stored.title_refresh_enabled !== false) throw new Error(`expected the failed PATCH to leave false stored, saw ${stored.title_refresh_enabled}`)
// The panel smooth-scrolls the banner to the viewport's centre and focuses it.
// Once that scroll has settled, put the banner at the top edge so the toggle
// below it fits in the frame when the viewport is tall enough.
await page.waitForTimeout(800)
await banner.evaluate(el => el.scrollIntoView({ block: 'start' }))
await page.waitForTimeout(400)
await page.screenshot({ path: join(OUT, '06-sessions-save-failed.png'), fullPage: false })
console.log('captured 06-sessions-save-failed.png')

await browser.close()
srv.close()
