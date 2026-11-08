/**
 * Screenshot harness for #21547: a default destination crew for "Send a copy to".
 *
 * Frames (each in dark and light):
 *  1. settings-default  Settings -> Remote Crew: the Default destination crew
 *                       picker above the crew list, set to a registered crew.
 *  2. folder-default    Folder settings: the folder's own destination crew, which
 *                       overrides the global one ("Use the global default (devdesk)" when empty).
 *  3. send-menu         A session in that folder: "Send a copy to" lists the
 *                       folder's crew first, marked "default"; an offline crew stays
 *                       disabled and the others stay usable. Nothing is sent.
 *  4. settings-open     The global picker open: "No default" (clears it) first, then
 *                       every registered crew.
 *  5. folder-open       The folder picker open: "Use the global default (devdesk)" (clears
 *                       the override) first, then every registered crew.
 *  6. not-registered    A stored global default that names no registered crew
 *                       stays selected as "{id} (not registered)".
 *  7. save-failed       The global save refused (PATCH /api/config/kirocrew
 *                       stubbed to fail): the catalog sentence leads, the
 *                       gateway's reason follows, the hint returns on dismiss.
 *  8. folder-crews-failed  GET /api/instances failing: Folder settings keeps the
 *                       field and says the crew list did not load, with Retry.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call stubbed, so no
 * gateway or crew is needed. What this does NOT show is a copy arriving on the
 * crew; the send itself is unchanged by this PR.
 *
 * Usage: npm run build && node scripts/capture-21547-default-destination-crew.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/21547-default-destination-crew'
mkdirSync(OUT, { recursive: true })

const crew = (id, name, state, port) => ({
  id, name, ssh_host: `${id}-alias`, remote_port: 7777, local_port: port, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: state === 'connected',
  status: { instance_id: id, state, local_port: port, remote_port: 7777 },
})
const CREWS = [
  crew('devdesk', 'devdesk', 'connected', 7801),
  crew('gian', 'gian', 'disconnected', 0),
  crew('nobita', 'nobita', 'connected', 7802),
]
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const SLOT = 'chat-ledger'
const FOLDERS = [{ id: 'f-led', name: 'Ledger', order: 0, collapsed: false, default_destination_crew: 'nobita' }]
const SLOTS = [{
  key: SLOT, title: 'Month-end close', messages: 4, running: false, agent: 'kirocrew', mode: '',
  memory_mode: 'persistent', folder_id: 'f-led', last_message: '', source_links: [], source_links_total: 0,
  created: '2026-10-19T08:00:00Z', last_ts: '2026-10-19T09:00:00Z', modified: Math.floor(Date.now() / 1000),
}]

// Per-frame stub state, reset by openPage.
let stub = {}
const extra = async (path, route) => {
  if (path === '/api/instances') {
    if (stub.instancesFail) {
      await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'instance registry unreadable' }) })
      return true
    }
    // The global default is devdesk; the Ledger folder overrides it with nobita.
    await json(route, { active: true, instances: CREWS, warm_set_cap: 5, sso: SSO, default_destination: stub.globalDefault ?? 'devdesk' })
    return true
  }
  if (path === '/api/config/kirocrew' && route.request().method() === 'PATCH') {
    if (stub.patchFails) {
      await route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'config.json is read-only on this host' }) })
    } else {
      await json(route, { ok: true })
    }
    return true
  }
  if (path === '/api/cloud/launch') { await json(route, { jobs: [] }); return true }
  if (path.endsWith('/send-session')) throw new Error('nothing may be sent without a click on a crew')
  return false
}

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
// PW_CHROMIUM lets a host whose cached browser revision differs from the pinned
// playwright reuse the one it has instead of downloading another.
const browser = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {})

const openPage = async (theme, path, state = {}) => {
  stub = state
  const context = await browser.newContext({ viewport: { width: 1300, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await stubDashboardApi(page, {
    folders: FOLDERS, slots: SLOTS, extra, theme,
    localStorageEntries: { 'mc-active-slot-chat': SLOT },
  })
  logPageProblems(page)
  await page.goto(`${base}${path}`, { waitUntil: 'domcontentloaded' })
  return page
}

const union = async (page, out, locators) => {
  const boxes = (await Promise.all(locators.map(l => l.boundingBox()))).filter(Boolean)
  const x = Math.max(0, Math.min(...boxes.map(b => b.x)) - 12), y = Math.max(0, Math.min(...boxes.map(b => b.y)) - 12)
  await page.screenshot({
    path: out,
    clip: { x, y, width: Math.max(...boxes.map(b => b.x + b.width)) + 12 - x, height: Math.max(...boxes.map(b => b.y + b.height)) + 12 - y },
  })
}

const openFolderSettings = async (page) => {
  await page.waitForSelector('[data-testid="folder-collapse-f-led"]', { timeout: 20000 })
  await page.waitForTimeout(800)
  await page.hover('[data-testid="folder-collapse-f-led"]')
  await page.click('[data-testid="folder-menu-f-led"]')
  await page.click('[data-testid="folder-settings-f-led"]')
  const modal = page.getByRole('dialog', { name: 'Folder settings' })
  await modal.waitFor({ timeout: 5000 })
  await page.waitForTimeout(400)
  return modal
}

const scrollIntoModalEnd = async (locator) => {
  // The modal body scrolls; bring its end (the destination field) into view.
  await locator.evaluate((el) => {
    for (let n = el.parentElement; n; n = n.parentElement) {
      if (n.scrollHeight > n.clientHeight + 4 && getComputedStyle(n).overflowY !== 'visible') { n.scrollTop = n.scrollHeight; return }
    }
  })
  await locator.page().waitForTimeout(300)
}

const optionTexts = async (page) => {
  const list = page.getByRole('listbox').last()
  await list.waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  return { list, texts: (await list.getByRole('option').allInnerTexts()).map(t => t.trim()) }
}

const settingsOpen = async (theme) => {
  const page = await openPage(theme, '/settings?tab=instances')
  const picker = page.getByTestId('default-destination-crew')
  await picker.waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  await picker.getByRole('button', { name: 'Default destination crew' }).click()
  const { list, texts } = await optionTexts(page)
  if (texts[0] !== 'No default') fail(`${theme}: the global picker must offer "No default" first: ${JSON.stringify(texts)}`)
  for (const name of ['devdesk', 'gian', 'nobita']) if (!texts.some(t => t.startsWith(name))) fail(`${theme}: the global picker must list ${name}: ${JSON.stringify(texts)}`)
  await union(page, `${OUT}/settings-open-${theme}.png`, [picker, list])
  console.log(`wrote settings-open-${theme}.png`)
  await page.context().close()
}

const folderOpen = async (theme) => {
  const page = await openPage(theme, '/chat')
  const modal = await openFolderSettings(page)
  const trigger = modal.getByRole('button', { name: 'Default destination crew' })
  await scrollIntoModalEnd(trigger)
  await trigger.click()
  const { list, texts } = await optionTexts(page)
  if (texts[0] !== 'Use the global default (devdesk)') fail(`${theme}: the folder picker's empty choice must name the global default: ${JSON.stringify(texts)}`)
  for (const name of ['devdesk', 'gian', 'nobita']) if (!texts.some(t => t.startsWith(name))) fail(`${theme}: the folder picker must list ${name}: ${JSON.stringify(texts)}`)
  await union(page, `${OUT}/folder-open-${theme}.png`, [trigger.locator('xpath=..'), list.locator('xpath=..')])
  console.log(`wrote folder-open-${theme}.png`)
  await page.context().close()
}

const notRegistered = async (theme) => {
  const page = await openPage(theme, '/settings?tab=instances', { globalDefault: 'retired-crew' })
  const picker = page.getByTestId('default-destination-crew')
  await picker.waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  const shown = await picker.getByRole('button', { name: 'Default destination crew' }).innerText()
  if (!/retired-crew \(not registered\)/.test(shown)) fail(`${theme}: a removed crew must stay selected as "(not registered)": ${JSON.stringify(shown)}`)
  await picker.getByRole('button', { name: 'Default destination crew' }).click()
  const { list, texts } = await optionTexts(page)
  if (!texts.includes('retired-crew (not registered)')) fail(`${theme}: the open list must keep the removed crew: ${JSON.stringify(texts)}`)
  await union(page, `${OUT}/not-registered-${theme}.png`, [picker, list])
  console.log(`wrote not-registered-${theme}.png`)
  await page.context().close()
}

const saveFailed = async (theme) => {
  const page = await openPage(theme, '/settings?tab=instances', { patchFails: true })
  const picker = page.getByTestId('default-destination-crew')
  await picker.waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  await picker.getByRole('button', { name: 'Default destination crew' }).click()
  await page.getByRole('option', { name: 'nobita' }).click()
  const notice = page.getByTestId('default-destination-crew-error')
  await notice.waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  const text = await notice.innerText()
  if (!/^Couldn't save the default destination crew\./.test(text.trim())) fail(`${theme}: the catalog sentence must lead the failure: ${JSON.stringify(text)}`)
  if (!/read-only/.test(text)) fail(`${theme}: the gateway's reason must follow it: ${JSON.stringify(text)}`)
  if ((await page.getByRole('alert').filter({ hasText: 'read-only' }).count()) !== 1) fail(`${theme}: exactly one alert must carry the reason`)
  await union(page, `${OUT}/save-failed-${theme}.png`, [picker])
  console.log(`wrote save-failed-${theme}.png`)
  await notice.getByRole('button', { name: /dismiss/i }).click()
  if (!/lists this crew first/.test(await picker.innerText())) fail(`${theme}: dismissing must bring the hint back`)
  await page.context().close()
}

const folderCrewsFailed = async (theme) => {
  const page = await openPage(theme, '/chat', { instancesFail: true })
  const modal = await openFolderSettings(page)
  const error = modal.getByTestId('folder-config-destination-error')
  await error.waitFor({ timeout: 5000 })
  await scrollIntoModalEnd(error)
  if (!/Crews couldn't load\./.test(await error.innerText())) fail(`${theme}: the field must say the crew list did not load`)
  if ((await modal.getByTestId('folder-config-destination-retry').count()) !== 1) fail(`${theme}: the field must offer Retry`)
  await modal.screenshot({ path: `${OUT}/folder-crews-failed-${theme}.png` })
  console.log(`wrote folder-crews-failed-${theme}.png`)
  await page.context().close()
}

const settingsDefault = async (theme) => {
  const page = await openPage(theme, '/settings?tab=instances')
  const picker = page.getByTestId('default-destination-crew')
  await picker.waitFor({ timeout: 20000 })
  await page.waitForTimeout(500)
  const text = await picker.innerText()
  if (!/\u201cSend a copy to\u201d lists this crew first/.test(text)) fail(`${theme}: the hint must quote the menu name: ${JSON.stringify(text)}`)
  if (!/Default destination crew/i.test(text) || !/devdesk/.test(text)) fail(`${theme}: the global picker must show devdesk: ${JSON.stringify(text)}`)
  const box = await picker.boundingBox()
  const row = await page.locator('[data-crew-id]').first().boundingBox()
  const y = Math.max(0, box.y - 16)
  await page.screenshot({
    path: `${OUT}/settings-default-${theme}.png`,
    clip: { x: Math.max(0, box.x - 16), y, width: box.width + 32, height: (row ? row.y + Math.min(row.height, 120) : box.y + box.height) + 16 - y },
  })
  console.log(`wrote settings-default-${theme}.png`)
  await page.context().close()
}

const folderDefault = async (theme) => {
  const page = await openPage(theme, '/chat')
  await page.waitForSelector('[data-testid="folder-collapse-f-led"]', { timeout: 20000 })
  await page.waitForTimeout(800)
  await page.hover('[data-testid="folder-collapse-f-led"]')
  await page.click('[data-testid="folder-menu-f-led"]')
  await page.click('[data-testid="folder-settings-f-led"]')
  const modal = page.getByRole('dialog', { name: 'Folder settings' })
  await modal.waitFor({ timeout: 5000 })
  await page.waitForTimeout(400)
  const field = modal.getByText('Default destination crew', { exact: true })
  // The modal body scrolls; bring its end (this field and its hint) into view.
  await field.evaluate((el) => {
    for (let n = el.parentElement; n; n = n.parentElement) {
      if (n.scrollHeight > n.clientHeight + 4 && getComputedStyle(n).overflowY !== 'visible') { n.scrollTop = n.scrollHeight; return }
    }
  })
  await page.waitForTimeout(300)
  const text = await modal.innerText()
  if (!/Default destination crew/.test(text) || !/nobita/.test(text)) fail(`${theme}: Folder settings must show the folder's destination crew: ${JSON.stringify(text)}`)
  await modal.screenshot({ path: `${OUT}/folder-default-${theme}.png` })
  console.log(`wrote folder-default-${theme}.png`)
  await page.context().close()
}

const sendMenu = async (theme) => {
  const page = await openPage(theme, '/chat')
  await page.getByLabel('Message input').waitFor({ timeout: 20000 })
  await page.waitForTimeout(800)
  await page.getByRole('button', { name: /session options/i }).click()
  await page.getByRole('menu').first().waitFor({ timeout: 10000 })
  await page.getByText(/send a copy to/i).hover()
  const first = page.getByRole('menuitem', { name: /nobita/ })
  await first.waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  const sub = page.getByRole('menu').filter({ has: first }).last()
  const items = await sub.getByRole('menuitem').allInnerTexts()
  if (!/nobita/.test(items[0] || '')) fail(`${theme}: the folder's crew must be listed first: ${JSON.stringify(items)}`)
  if ((await sub.getByTestId('send-to-instance-default').count()) !== 1) fail(`${theme}: exactly one row must carry the default mark`)
  const gian = sub.getByRole('menuitem', { name: /gian/ })
  if (!((await gian.getAttribute('aria-disabled')) === 'true' || (await gian.isDisabled()))) fail(`${theme}: an offline crew must stay disabled`)
  const menus = page.getByRole('menu')
  const a = await menus.first().boundingBox()
  const b = await sub.boundingBox()
  const x = Math.min(a.x, b.x) - 8, y = Math.min(a.y, b.y) - 8
  await page.screenshot({
    path: `${OUT}/send-menu-${theme}.png`,
    clip: { x, y, width: Math.max(a.x + a.width, b.x + b.width) + 8 - x, height: Math.max(a.y + a.height, b.y + b.height) + 8 - y },
  })
  console.log(`wrote send-menu-${theme}.png`)
  await page.context().close()
}

for (const theme of ['dark', 'light']) {
  await settingsDefault(theme)
  await folderDefault(theme)
  await sendMenu(theme)
  await settingsOpen(theme)
  await folderOpen(theme)
  await notRegistered(theme)
  await saveFailed(theme)
  await folderCrewsFailed(theme)
}

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
