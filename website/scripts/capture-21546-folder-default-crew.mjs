/**
 * Screenshot harness for #21546: a chat folder with a default crew.
 *
 * Frames (each in dark and light):
 *  1. folder-settings  Folder settings of a folder pinned to an ONLINE crew: the
 *                      Default crew picker sits under Default agent, with its hint.
 *  2. crew-picker      The same picker opened: Local first, then every registered
 *                      crew with its online state.
 *  3. offline-notice   New chat in a folder whose crew is OFFLINE: no chat starts,
 *                      the notice under the folder names the crew, and "Start this
 *                      chat on Local instead" inside it answers that one chat.
 *  4. folder-settings-offline  Folder settings of a folder pinned to an OFFLINE
 *                      crew: the hint turns into the warning, before saving.
 *  5. crew-not-registered  The picker of a folder pinned to a removed crew: that
 *                      crew stays listed as "(not registered)".
 *  6. create-failed    A crew create that fails (here the version check): the
 *                      error under the folder, in the error style.
 *  7. phone-menu       Phone, a session in the offline crew's folder: the session
 *                      menu offers Local right under "New chat in {folder}".
 *  8. phone-offline    Phone, "New chat in {folder}" pressed: the notice says the
 *                      crew is offline, nothing starts, and the Local action sits
 *                      inside it.
 *  9. crew-starting    A crew create under way (the peer's create held open): the
 *                      folder says "Starting a chat on nobita…" in the info tone.
 * 10. crew-background  Cmd/Ctrl-click on the folder's +: the chat is created on the
 *                      crew, no window opens, and the folder says where it went.
 * 11. not-registered-notice  New chat in a folder pinned to a removed crew: the
 *                      sidebar says it is no longer registered (not offline),
 *                      with the Local action inside the notice.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call stubbed and the
 * Chat on a crew preview seeded on, since the picker and the routing are gated on
 * it. What this does NOT show is a chat opening ON the crew: that needs a reachable
 * peer on the same build, so it is pinned in ChatSidebar.folderDefaultCrew.test.tsx.
 *
 * Usage: npm run build && node scripts/capture-21546-folder-default-crew.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/21546-folder-default-crew'
mkdirSync(OUT, { recursive: true })

/** Mirrors `PREVIEW_FLAG_PREFIX + 'remote-crew-chat'` in utils/previewFlags.ts. */
const PREVIEW_REMOTE_CREW_CHAT = 'mc-preview-remote-crew-chat'

const crew = (id, name, state, port) => ({
  id, name, ssh_host: `${id}-alias`, remote_port: 7777, local_port: port, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: state === 'connected',
  status: { instance_id: id, state, local_port: port, remote_port: 7777 },
})
const CREWS = [crew('nobita', 'nobita', 'connected', 7801), crew('gian', 'gian', 'disconnected', 0)]
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

const FOLDERS = [
  { id: 'f-pay', name: 'Payments', order: 0, collapsed: false, default_crew: 'nobita' },
  { id: 'f-led', name: 'Ledger', order: 1, collapsed: false, default_crew: 'gian' },
  { id: 'f-loc', name: 'Notes', order: 2, collapsed: false },
  // Pinned to a crew that was since removed from Remote Crew.
  { id: 'f-old', name: 'Archive', order: 3, collapsed: false, default_crew: 'suneo' },
]
const slot = (key, title, folder_id) => ({
  key, title, messages: 4, running: false, agent: 'kirocrew', mode: '', memory_mode: 'persistent',
  folder_id, last_message: '', source_links: [], source_links_total: 0,
  created: '2026-10-19T08:00:00Z', last_ts: '2026-10-19T09:00:00Z', modified: Math.floor(Date.now() / 1000),
})
const SLOTS = [slot('s1', 'Refund retries', 'f-pay'), slot('s2', 'Month-end close', 'f-led'), slot('s3', 'Reading list', 'f-loc')]

// Per-frame stub state, reset by openPage: `capsOk` lets the version check pass,
// `holdCreate` leaves the peer's create pending (the "starting" frame).
let stub = {}
const extra = async (path, route) => {
  if (/^\/api\/instances\/[^/]+\/proxy\/api\/chat\/slots$/.test(path) && route.request().method() === 'POST') {
    if (stub.holdCreate) return true
    await json(route, { key: 'peer-new-1' })
    return true
  }
  if (path === '/api/instances') {
    await json(route, { active: true, instances: CREWS, warm_set_cap: 5, sso: SSO })
    return true
  }
  const tunnel = /^\/api\/instances\/([^/]+)\/(connect|refresh-token|status)$/.exec(path)
  if (tunnel) {
    const found = CREWS.find((c) => c.id === decodeURIComponent(tunnel[1]))
    if (found && found.status.state === 'connected') await json(route, { ...found.status, token: 'stub-token' })
    else await json(route, { instance_id: tunnel[1], state: 'disconnected', error: 'not connected' }, 409)
    return true
  }
  if (/^\/api\/instances\/[^/]+\/capabilities$/.test(path)) {
    // A crew a release behind: the crew create's version check refuses it,
    // unless the frame needs the create itself.
    await json(route, stub.capsOk
      ? { version_match: true, version: '0.9.2', local_version: '0.9.2' }
      : { version_match: false, version: '0.9.0rc7', local_version: '0.9.2' })
    return true
  }
  if (path.startsWith('/api/instances/')) { await json(route, {}); return true }
  return false
}

let failures = 0
const fail = (msg) => { console.error(`FAIL: ${msg}`); failures++ }

const { srv, base } = await serveDist()
// PW_CHROMIUM lets a host whose cached browser revision differs from the pinned
// playwright reuse the one it has instead of downloading another.
const browser = await chromium.launch(process.env.PW_CHROMIUM ? { executablePath: process.env.PW_CHROMIUM } : {})

const openPage = async (theme, state = {}) => {
  stub = state
  const context = await browser.newContext({ viewport: { width: 1300, height: 900 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.route(/127\.0\.0\.1:78\d\d/, (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }))
  await stubDashboardApi(page, { folders: FOLDERS, slots: SLOTS, extra, theme, localStorageEntries: { [PREVIEW_REMOTE_CREW_CHAT]: '1' } })
  logPageProblems(page)
  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[data-testid="folder-collapse-f-pay"]', { timeout: 20000 })
  await page.waitForTimeout(1500) // let the auto-warm of the connected crew land
  return page
}

const settings = async (theme) => {
  const page = await openPage(theme)
  const { modal } = await openSettings(page, 'f-pay')
  const text = await modal.innerText()
  if (!/Default crew/.test(text)) fail(`${theme}: Folder settings must show the Default crew picker: ${JSON.stringify(text)}`)
  if (!/nobita/.test(text)) fail(`${theme}: the picker must show the folder's crew`)
  const hint = await page.getByTestId('folder-config-crew-hint').getAttribute('class')
  if (/text-warn/.test(hint || '')) fail(`${theme}: an online crew must not raise the offline notice`)
  if (!/uses that crew's own default agent/.test(text)) fail(`${theme}: with a crew set, the agent hint must say the crew uses its own agent`)
  await modal.screenshot({ path: `${OUT}/folder-settings-${theme}.png` })
  console.log(`wrote folder-settings-${theme}.png`)

  await modal.getByRole('combobox', { name: 'Default crew' }).or(modal.getByRole('button', { name: 'Default crew' })).first().click()
  const list = page.getByRole('listbox').last()
  await list.waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  const options = await list.innerText()
  if (!/Local/.test(options) || !/nobita/.test(options) || !/gian/.test(options)) fail(`${theme}: the picker must list Local and both crews: ${JSON.stringify(options)}`)
  if (!/online/i.test(options) || !/offline/i.test(options)) fail(`${theme}: the picker must show each crew's state: ${JSON.stringify(options)}`)
  // The list portals out of the modal, so the frame is the union of the two.
  const a = await modal.boundingBox()
  const b = await list.boundingBox()
  const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y)
  await page.screenshot({
    path: `${OUT}/crew-picker-${theme}.png`,
    clip: { x, y, width: Math.max(a.x + a.width, b.x + b.width) - x, height: Math.max(a.y + a.height, b.y + b.height) - y },
  })
  console.log(`wrote crew-picker-${theme}.png`)
  await page.context().close()
}

const offline = async (theme) => {
  const page = await openPage(theme)
  const before = await page.locator('[data-testid^="session-row"], [data-slot-key]').count()
  await page.hover('[data-testid="folder-collapse-f-led"]')
  await page.click('[data-testid="folder-new-chat-f-led"]')
  const local = page.getByTestId('folder-create-error-local-f-led')
  await local.waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  const notice = await page.getByTestId('folder-create-error-f-led').innerText()
  if (!/gian/.test(notice)) fail(`${theme}: the offline notice must name the crew: ${JSON.stringify(notice)}`)
  if (!/Local/.test(await local.innerText())) fail(`${theme}: the notice must offer Local for this chat`)
  if ((await page.getByTestId('folder-create-error-f-led').getByTestId('folder-create-error-local-f-led').count()) !== 1) fail(`${theme}: the Local action must sit inside the offline notice`)
  if ((await page.locator('[data-testid^="session-row"], [data-slot-key]').count()) !== before) fail(`${theme}: no chat may start while the crew is offline`)
  // The sidebar column: from the first folder header down past the notice.
  const top = await page.getByTestId('folder-collapse-f-pay').boundingBox()
  const box = await page.getByTestId('folder-create-error-f-led').locator('xpath=..').boundingBox()
  const y = Math.max(0, top.y - 12)
  await page.screenshot({
    path: `${OUT}/offline-notice-${theme}.png`,
    clip: { x: Math.max(0, top.x - 12), y, width: Math.max(top.width, box.width) + 24, height: box.y + box.height + 72 - y },
  })
  console.log(`wrote offline-notice-${theme}.png`)
  await page.context().close()
}

/** Folder settings for one folder, with the Default crew hint scrolled into the
 *  modal's view so the frame shows the picker AND the line under it. */
const openSettings = async (page, folderId) => {
  await page.hover(`[data-testid="folder-collapse-${folderId}"]`)
  await page.click(`[data-testid="folder-menu-${folderId}"]`)
  await page.click(`[data-testid="folder-settings-${folderId}"]`)
  const modal = page.getByRole('dialog', { name: 'Folder settings' })
  await modal.waitFor({ timeout: 5000 })
  const hint = page.getByTestId('folder-config-crew-hint')
  await hint.scrollIntoViewIfNeeded()
  await page.waitForTimeout(400)
  const m = await modal.boundingBox()
  const h = await hint.boundingBox()
  if (!h || h.y < m.y || h.y + h.height > m.y + m.height) fail(`${folderId}: the Default crew hint must be inside the modal's frame`)
  return { modal, hint }
}

const offlinePick = async (theme) => {
  const page = await openPage(theme)
  const { modal, hint } = await openSettings(page, 'f-led')
  const text = await hint.innerText()
  if (!/offline/i.test(text)) fail(`${theme}: a folder pinned to an offline crew must warn before saving: ${JSON.stringify(text)}`)
  if (!/text-warn/.test((await hint.getAttribute('class')) || '')) fail(`${theme}: the offline warning must use the warn tone`)
  await modal.screenshot({ path: `${OUT}/folder-settings-offline-${theme}.png` })
  console.log(`wrote folder-settings-offline-${theme}.png`)
  await page.context().close()
}

const notRegistered = async (theme) => {
  const page = await openPage(theme)
  const { modal } = await openSettings(page, 'f-old')
  await modal.getByRole('combobox', { name: 'Default crew' }).or(modal.getByRole('button', { name: 'Default crew' })).first().click()
  const list = page.getByRole('listbox').last()
  await list.waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  const options = await list.innerText()
  if (!/suneo \(not registered\)/.test(options)) fail(`${theme}: a crew no longer registered must stay listed as such: ${JSON.stringify(options)}`)
  const hintText = await modal.getByTestId('folder-config-crew-hint').innerText()
  if (!/no longer registered/.test(hintText) || /offline/i.test(hintText)) fail(`${theme}: a removed crew must not be called offline: ${JSON.stringify(hintText)}`)
  const a = await modal.boundingBox()
  const b = await list.boundingBox()
  const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y)
  await page.screenshot({
    path: `${OUT}/crew-not-registered-${theme}.png`,
    clip: { x, y, width: Math.max(a.x + a.width, b.x + b.width) - x, height: Math.max(a.y + a.height, b.y + b.height) - y },
  })
  console.log(`wrote crew-not-registered-${theme}.png`)
  await page.context().close()
}

const notRegisteredNotice = async (theme) => {
  const page = await openPage(theme)
  const before = await page.locator('[data-testid^="session-row"], [data-slot-key]').count()
  await page.hover('[data-testid="folder-collapse-f-old"]')
  await page.click('[data-testid="folder-new-chat-f-old"]')
  const notice = page.getByTestId('folder-create-error-f-old')
  await notice.waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  const text = await notice.innerText()
  if (!/suneo is no longer a registered crew/.test(text) || /offline/i.test(text)) fail(`${theme}: the sidebar must say the crew is no longer registered, not offline: ${JSON.stringify(text)}`)
  if ((await notice.getByTestId('folder-create-error-local-f-old').count()) !== 1) fail(`${theme}: the Local action must sit inside the not-registered notice`)
  if ((await page.locator('[data-testid^="session-row"], [data-slot-key]').count()) !== before) fail(`${theme}: no chat may start for a crew that is no longer registered`)
  const top = await page.getByTestId('folder-collapse-f-old').boundingBox()
  const box = await notice.locator('xpath=..').boundingBox()
  const y = Math.max(0, top.y - 12)
  await page.screenshot({
    path: `${OUT}/not-registered-notice-${theme}.png`,
    clip: { x: Math.max(0, top.x - 12), y, width: Math.max(top.width, box.width) + 24, height: box.y + box.height + 12 - y },
  })
  console.log(`wrote not-registered-notice-${theme}.png`)
  await page.context().close()
}

const createFailed = async (theme) => {
  // The capabilities stub answers {} for every crew, so the version check the
  // crew create runs first refuses: the error lands under the folder.
  const page = await openPage(theme)
  await page.hover('[data-testid="folder-collapse-f-pay"]')
  await page.click('[data-testid="folder-new-chat-f-pay"]')
  const notice = page.getByTestId('folder-create-error-f-pay')
  await notice.waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  const text = await notice.innerText()
  if (!/0\.9\.0rc7/.test(text) || !/0\.9\.2/.test(text)) fail(`${theme}: a failed crew create must say why, naming both versions: ${JSON.stringify(text)}`)
  if ((await notice.getAttribute('role')) === 'status') fail(`${theme}: a failed create is an error, not the offline state`)
  const top = await page.getByTestId('folder-collapse-f-pay').boundingBox()
  const box = await notice.boundingBox()
  const y = Math.max(0, top.y - 12)
  await page.screenshot({
    path: `${OUT}/create-failed-${theme}.png`,
    clip: { x: Math.max(0, top.x - 12), y, width: Math.max(top.width, box.width) + 24, height: box.y + box.height + 72 - y },
  })
  console.log(`wrote create-failed-${theme}.png`)
  await page.context().close()
}

const folderStatusShot = async (page, file) => {
  const top = await page.getByTestId('folder-collapse-f-pay').boundingBox()
  const box = await page.getByTestId('folder-crew-status-f-pay').boundingBox()
  const y = Math.max(0, top.y - 12)
  await page.screenshot({
    path: `${OUT}/${file}`,
    clip: { x: Math.max(0, top.x - 12), y, width: Math.max(top.width, box.width) + 24, height: box.y + box.height + 72 - y },
  })
  console.log(`wrote ${file}`)
}

const starting = async (theme) => {
  const page = await openPage(theme, { capsOk: true, holdCreate: true })
  await page.hover('[data-testid="folder-collapse-f-pay"]')
  await page.click('[data-testid="folder-new-chat-f-pay"]')
  const status = page.getByTestId('folder-crew-status-f-pay')
  await status.waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  if (!/Starting a chat on nobita/.test(await status.innerText())) fail(`${theme}: the folder must say the crew create is under way`)
  if ((await status.getAttribute('role')) !== 'status' || (await status.getAttribute('data-tone')) !== 'info') fail(`${theme}: progress is a neutral status`)
  await folderStatusShot(page, `crew-starting-${theme}.png`)
  await page.context().close()
}

const background = async (theme) => {
  const page = await openPage(theme, { capsOk: true })
  await page.hover('[data-testid="folder-collapse-f-pay"]')
  await page.click('[data-testid="folder-new-chat-f-pay"]', { modifiers: ['ControlOrMeta'] })
  const status = page.getByTestId('folder-crew-status-f-pay')
  await status.getByText(/Started a chat on nobita/).waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  if (await page.getByTestId('crew-chat-window').count()) fail(`${theme}: a background create must open no crew window`)
  await folderStatusShot(page, `crew-background-${theme}.png`)
  await page.context().close()
}

const phone = async (theme) => {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true })
  const page = await context.newPage()
  await page.route(/127\.0\.0\.1:78\d\d/, (route) => route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }))
  await stubDashboardApi(page, { folders: FOLDERS, slots: SLOTS, extra, theme, localStorageEntries: { [PREVIEW_REMOTE_CREW_CHAT]: '1' } })
  logPageProblems(page)
  await page.goto(`${base}/chat?sid=s2`, { waitUntil: 'domcontentloaded' })
  const trigger = page.getByRole('button', { name: /Session options/ })
  await trigger.waitFor({ timeout: 20000 })
  await page.waitForTimeout(1500)
  await trigger.click()
  const local = page.getByTestId('mobile-new-session-here-local')
  await local.waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  if (!/^New chat in Ledger on Local$/.test((await local.innerText()).trim())) fail(`${theme}: the phone's Local item must name a new chat in the folder: ${JSON.stringify(await local.innerText())}`)
  await page.screenshot({ path: `${OUT}/phone-menu-${theme}.png` })
  console.log(`wrote phone-menu-${theme}.png`)
  const before = await page.evaluate(() => location.search)
  await page.getByTestId('mobile-new-session-here').click()
  await page.getByText(/gian is offline/).first().waitFor({ timeout: 5000 })
  await page.waitForTimeout(300)
  if ((await page.evaluate(() => location.search)) !== before) fail(`${theme}: no chat may start on the phone while the crew is offline`)
  if ((await page.getByTestId('action-status').getAttribute('data-tone')) !== 'warn') fail(`${theme}: the phone's offline notice must be the warn-tone status`)
  if (!/New chat in .+ on Local/.test(await page.getByTestId('action-status').getByTestId('action-status-action').innerText())) fail(`${theme}: the phone's offline notice must carry the Local action inside it`)
  await page.screenshot({ path: `${OUT}/phone-offline-${theme}.png` })
  console.log(`wrote phone-offline-${theme}.png`)
  await context.close()
}

for (const theme of ['dark', 'light']) {
  await settings(theme)
  await offline(theme)
  await offlinePick(theme)
  await notRegistered(theme)
  await notRegisteredNotice(theme)
  await createFailed(theme)
  await starting(theme)
  await background(theme)
  await phone(theme)
}

await browser.close()
srv.close()
if (failures) { console.error(`${failures} assertion(s) failed`); process.exit(1) }
console.log('OK')
