/**
 * Screenshot harness for "Auto-title refresh interval (turns)" in Settings ->
 * Chat -> Sessions.
 *
 * The row is a +/- stepper over `dashboard.title_refresh_every_turns`, like
 * Message Font Size: a plain text readout, no typed input box. It saves through
 * PATCH /api/config/kirocrew and reads back from GET /api/config/kirocrew. The
 * harness answers that route from one stateful fixture, so a save is read back
 * the way the gateway would return it, and the frames show what the server
 * holds rather than an optimistic display that a refetch would undo.
 *
 * Runs the REAL built SPA (website/dist) behind this folder's shared
 * `lib/serve-dist.mjs`, answering every other /api/** call from fixtures. No
 * gateway, no dashboard auth, no kiro-cli spawn.
 *
 * Shots:
 *  1. Sessions page, nothing stored: the row reads "Default", the built-in
 *     schedule, with − disabled, beside Session summaries.
 *  2. After one + click from Default: the row reads 4 (the minimum cadence),
 *     and the harness asserts the PATCH carried exactly that value.
 *  3. The row's info tip open, so its full explanation is readable.
 *  4. Light theme, the same minimum cadence, so the row is shown in both themes.
 *  5. A failed save: the PATCH for one + click answers 500, the "Failed to save
 *     title refresh setting" banner shows, and the row reads the stored 4 again.
 *     The banner sits at the top of the panel and the row low in the Sessions
 *     card, so the frame is taken with the banner at the top edge and the row in
 *     it when the viewport fits both.
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

mkdirSync(OUT, { recursive: true })

const chat = JSON.parse(readFileSync(LOCALES + 'en.manual.json', 'utf-8')).pages?.settings?.chatPanel
const MORE_INFO = JSON.parse(readFileSync(LOCALES + 'en.json', 'utf-8')).components?.infoTip?.more_information
const LABEL = chat?.title_refresh_every_turns
// The readout for 0, and the banner a failed save raises; both come from the
// catalog for the same reason as the label.
const DEFAULT = chat?.title_refresh_default
const SAVE_FAILED = chat?.failed_to_save_title_refresh
if (!LABEL || !DEFAULT || !SAVE_FAILED || !chat?.session_summaries || !MORE_INFO) {
  throw new Error('catalog keys missing -- pages.settings.chatPanel.title_refresh_* or components.infoTip.more_information renamed?')
}

const { browser, page, srv, base } = await openSettingsPage({ tab: 'chat' })

// Registered after the shared catch-all, so these take precedence for their paths.
// The shared fixture answers /api/models with an object, which the model catalog
// reads as a degraded list and flags with a "Failed to load config." banner that
// has nothing to do with this row; a one-entry live list keeps the frame clean.
await page.route('**/api/models', route => json(route, [{ model_name: 'auto', description: 'Auto' }]))
let stored
// Frame 5 arms this: the next PATCH is recorded but answered 500 and not stored,
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
    if (path === KEY) stored = value
    return json(route, { ok: true })
  }
  return json(route, stored === undefined ? {} : { dashboard: { title_refresh_every_turns: stored } })
})

await page.goto(`${base}/settings/chat/sessions`, { waitUntil: 'domcontentloaded' })
const rowSel = `[data-setting-key="${KEY}"]`
const row = page.locator(rowSel)
await row.waitFor({ state: 'visible', timeout: 15_000 })
const inc = row.getByRole('button', { name: /increase/i })
const dec = row.getByRole('button', { name: /decrease/i })
await page.waitForFunction(el => !el.disabled, await inc.elementHandle())
await row.scrollIntoViewIfNeeded()
await page.waitForTimeout(400)

// The readout is a plain text span (no input box), like Message Font Size. The
// box also holds invisible, aria-hidden copies of the widest values it must fit
// (`reserveWidthFor`), so the shown value is read with those stripped out.
const shownText = el => {
  const copy = el.cloneNode(true)
  copy.querySelectorAll('[aria-hidden="true"]').forEach(n => n.remove())
  return copy.textContent?.trim()
}
const readout = async () => row.locator('span.cursor-default').evaluate(shownText)

// 0 reads as "Default", and − is disabled there: nothing is below the built-in schedule.
if ((await readout()) !== DEFAULT) throw new Error(`expected "${DEFAULT}" with nothing stored, saw ${await readout()}`)
if (!(await dec.isDisabled())) throw new Error('expected − to be disabled at Default')
await page.screenshot({ path: join(OUT, '01-sessions-built-in-schedule.png'), fullPage: false })
console.log('captured 01-sessions-built-in-schedule.png')

// One + from Default jumps to the minimum cadence of 4, and − comes alive.
await inc.click()
await page.waitForTimeout(600)
const sent = patches.filter(p => p.path === KEY).map(p => p.value)
if (sent.length !== 1 || sent[0] !== 4) throw new Error(`expected one PATCH of 4, saw ${JSON.stringify(sent)}`)
if ((await readout()) !== '4') throw new Error(`expected the row to read back 4, saw ${await readout()}`)
if (await dec.isDisabled()) throw new Error('expected − to be enabled at the minimum cadence')
await page.screenshot({ path: join(OUT, '02-sessions-every-4-turns.png'), fullPage: false })
console.log('captured 02-sessions-every-4-turns.png')

await row.getByRole('button', { name: MORE_INFO }).hover()
await page.getByRole('tooltip').waitFor({ state: 'visible', timeout: 5_000 })
await page.waitForTimeout(300)
await page.screenshot({ path: join(OUT, '03-sessions-info-tip.png'), fullPage: false })
console.log('captured 03-sessions-info-tip.png')

// Light theme, same cadence. Init scripts run in the order they were added,
// so this one overrides the shared preamble's dark seed on the reload.
await page.addInitScript(() => localStorage.setItem('mc-theme', 'light'))
await page.route('**/api/theme/boot', route => json(route, { mode: 'light', theme: '' }))
await page.reload({ waitUntil: 'domcontentloaded' })
// The pointer still rests on the info tip from frame 3; move it off so the tip closes.
await page.mouse.move(0, 0)
await row.waitFor({ state: 'visible', timeout: 15_000 })
await page.waitForFunction(el => !el.disabled, await inc.elementHandle())
await row.scrollIntoViewIfNeeded()
await page.waitForTimeout(400)
if ((await readout()) !== '4') throw new Error(`expected the row to read back 4 in light theme, saw ${await readout()}`)
await page.screenshot({ path: join(OUT, '04-sessions-light.png'), fullPage: false })
console.log('captured 04-sessions-light.png')

// A save that fails. The + click shows 5 at once, the PATCH answers 500, and
// the failure raises the banner and rolls the readout back to the stored 4.
failNextPatch = true
await inc.click()
const banner = page.getByRole('alert').filter({ hasText: SAVE_FAILED })
await banner.waitFor({ state: 'visible', timeout: 5_000 })
// Same reading as `shownText`, inlined: a page function cannot reach a Node closure.
await page.waitForFunction(
  ([sel, want]) => {
    const el = document.querySelector(`${sel} span.cursor-default`)
    if (!el) return false
    const copy = el.cloneNode(true)
    copy.querySelectorAll('[aria-hidden="true"]').forEach(n => n.remove())
    return copy.textContent?.trim() === want
  },
  [rowSel, '4'],
  { timeout: 5_000 },
)
const failed = patches.filter(p => p.path === KEY).map(p => p.value)
if (failed.length !== 2 || failed[1] !== 5) throw new Error(`expected a second PATCH of 5, saw ${JSON.stringify(failed)}`)
if (stored !== 4) throw new Error(`expected the failed PATCH to leave 4 stored, saw ${stored}`)
// The panel smooth-scrolls the banner to the viewport's centre and focuses it.
// Once that scroll has settled, put the banner at the top edge instead: that
// is the most room the row below can get, and the frame then holds both when
// the viewport is tall enough.
await page.waitForTimeout(800)
await banner.evaluate(el => el.scrollIntoView({ block: 'start' }))
await page.waitForTimeout(400)
const rowInFrame = await row.evaluate(el => {
  const r = el.getBoundingClientRect()
  return r.top >= 0 && r.bottom <= window.innerHeight
})
console.log(rowInFrame ? 'row is in frame below the banner' : 'row is below the fold; the banner alone is in frame')
await page.screenshot({ path: join(OUT, '05-sessions-save-failed.png'), fullPage: false })
console.log('captured 05-sessions-save-failed.png')

await browser.close()
srv.close()
