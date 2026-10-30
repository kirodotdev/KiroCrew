/**
 * Screenshot harness: a session's "Move to folder…" submenu
 * ends with "New folder…", which names a folder, creates it, and moves the
 * session into it in one step.
 *
 * Drives the session row's right-click menu on the REAL built SPA behind the
 * shared loopback static server, every /api/** call answered from fixtures
 * (gateway-free). The folder and slot stubs are STATEFUL: the create appends to
 * the folder list and the move rewrites the slot, so the refetches that follow
 * show what the server would.
 *
 * MODE=before stops at the submenu (the build without the entry); MODE=after
 * (default) also opens the dialog, ticks "Create inside", submits, and checks
 * the requests and the sidebar; MODE=narrow opens the same dialog from the chat
 * header's session menu on a 390px phone viewport; MODE=movefail answers the
 * first move with a 503 and checks the dialog reports it, naming the parent,
 * and retries the move alone; MODE=offline drops the gateway socket with a name
 * typed in the dialog, then reopens the submenu, and checks both hold the
 * create; MODE=closed closes the session while the dialog is open and checks
 * Create says so and writes nothing; MODE=folderserr lets the create through,
 * then answers the folder-list re-read that follows it, and the move, with
 * server errors while the socket stays up, and checks the dialog shows both. THEME picks dark (default) or light.
 *
 * Usage: DIST=<dist dir> MODE=after THEME=dark node scripts/capture-22779-new-folder-move.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/22779'
const MODE = process.env.MODE || 'after'
const THEME = process.env.THEME || 'dark'
const SLOT = 's1'
mkdirSync(OUT, { recursive: true })

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` — ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

const now = Math.floor(Date.now() / 1000)
const folders = [
  { id: 'f-assist', name: 'Assistants', order: 0, parent_id: '' },
  { id: 'f-video', name: 'Video Projects', order: 1, parent_id: '' },
  { id: 'f-runway', name: 'Runway', order: 2, parent_id: '' },
  { id: 'f-archived', name: 'Archived', order: 3, parent_id: '' },
  { id: 'f-exp', name: 'Experiments', order: 4, parent_id: '' },
]
const slots = [
  {
    key: SLOT, title: 'Storyboard the launch video', running: false, messages: 2,
    agent: 'kirocrew', modified: now, last_ts: '2026-10-23T09:00:00Z', folder_id: 'f-video',
    last_message: 'Six scenes, thirty seconds.',
  },
  {
    key: 's2', title: 'Draft the release notes', running: false, messages: 2,
    agent: 'kirocrew', modified: now - 600, last_ts: '2026-10-23T08:50:00Z', folder_id: '',
    last_message: 'Grouped the entries by area.',
  },
]
const detail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: 'Storyboard the launch video.', ts: now - 120 },
    { role: 'assistant', content: 'Six scenes, thirty seconds.', ts: now - 60 },
  ],
}

const created = []
const moves = []
// MODE=folderserr: set on submit, so the menus load the list first.
let foldersFail = false
const extra = (path, route) => {
  const method = route.request().method()
  if (path === '/api/chat/folders' && method === 'POST') {
    const body = JSON.parse(route.request().postData() || '{}')
    created.push(body)
    const row = { id: 'f-new', name: body.name, parent_id: body.parent_id || '', order: folders.length }
    folders.push(row)
    return json(route, row), true
  }
  if (path === '/api/chat/folders' && foldersFail) return json(route, { error: 'Folder list unavailable' }, 500), true
  if (path === '/api/chat/folders') return json(route, folders), true
  if (path === '/api/chat/slots') return json(route, slots), true
  const m = path.match(/^\/api\/chat\/slots\/([^/]+)\/folder$/)
  if (m && method === 'PATCH') {
    const body = JSON.parse(route.request().postData() || '{}')
    moves.push({ slot: decodeURIComponent(m[1]), ...body })
    if ((MODE === 'movefail' && moves.length === 1) || foldersFail) return json(route, { error: 'Gateway unreachable' }, 503), true
    const s = slots.find(x => x.key === decodeURIComponent(m[1]))
    if (s) s.folder_id = body.folder_id
    return json(route, { ok: true }), true
  }
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

/** Clip around every open menu (and the dialog, when given), padded. */
async function clipAround(page, locators) {
  const boxes = []
  for (const l of locators) {
    const n = await l.count()
    for (let i = 0; i < n; i++) {
      const b = await l.nth(i).boundingBox()
      if (b) boxes.push(b)
    }
  }
  const x = Math.max(0, Math.min(...boxes.map(b => b.x)) - 24)
  const y = Math.max(0, Math.min(...boxes.map(b => b.y)) - 24)
  const r = Math.max(...boxes.map(b => b.x + b.width)) + 24
  const btm = Math.max(...boxes.map(b => b.y + b.height)) + 24
  return { x, y, width: Math.min(1500, r) - x, height: Math.min(950, btm) - y }
}

/** 390px phone: the header's session menu → Move to folder… → New folder…. */
async function narrow(page) {
  await page.getByRole('button', { name: 'Session options' }).first().click()
  // By text: the phone menu's submenu trigger is not exposed under the menuitem role here.
  const trigger = page.getByText('Move to folder…', { exact: true })
  await trigger.waitFor({ state: 'visible', timeout: 5000 })
  await trigger.click()
  const entry = page.getByText('New folder…', { exact: true })
  await entry.waitFor({ state: 'visible', timeout: 5000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/narrow-01-move-submenu-${THEME}.png` })
  await entry.click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor({ state: 'visible', timeout: 5000 })
  await dialog.getByRole('textbox', { name: 'Folder name' }).fill('Storyboards')
  await page.waitForTimeout(400)
  const box = await dialog.boundingBox()
  assert('narrow: dialog fits the 390px viewport', !!box && box.x >= 0 && box.x + box.width <= 390, JSON.stringify(box))
  await page.screenshot({ path: `${OUT}/narrow-02-dialog-${THEME}.png` })
}

async function main() {
  const { srv, base } = await serveDist(process.env.DIST || undefined)
  const browser = await chromium.launch()
  const viewport = MODE === 'narrow' ? { width: 390, height: 844 } : { width: 1500, height: 950 }
  const context = await browser.newContext({ viewport, deviceScaleFactor: 2, hasTouch: MODE === 'narrow', isMobile: MODE === 'narrow' })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots, folders, theme: THEME, extra,
    localStorageEntries: { 'mc-active-slot': SLOT, 'mc-privacy-notice-v1': '1', 'mc-sidebar-pinned': 'true' },
  })
  // Offline mode owns the dashboard socket (registered after the stub's, so it
  // wins): while `offline` is set every connection is closed on arrival, which
  // is how the page sees a gateway that went away.
  let offline = false
  const sockets = []
  if (MODE === 'offline' || MODE === 'closed') {
    await page.routeWebSocket(/\/api\/ws/, ws => { if (offline) ws.close(); else sockets.push(ws) })
  }
  await page.goto(base + '/chat?sid=' + SLOT, { waitUntil: 'domcontentloaded' })
  if (MODE === 'narrow') {
    await page.getByRole('button', { name: 'Session options' }).first().waitFor({ state: 'visible', timeout: 20000 })
    await page.waitForTimeout(1500)
    await narrow(page)
    await browser.close(); srv.close(); return
  }
  const row = page.locator(`[data-session-row="${SLOT}"]`).first()
  await row.waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1500)

  // 01: the row's right-click menu with "Move to folder…" open.
  await row.click({ button: 'right' })
  const trigger = page.getByRole('menuitem', { name: /^Move to folder/ })
  await trigger.waitFor({ state: 'visible', timeout: 5000 })
  await trigger.hover()
  await page.keyboard.press('ArrowRight')
  const rootItem = page.getByRole('menuitem', { name: 'No folder (root)' })
  await rootItem.waitFor({ state: 'visible', timeout: 5000 })
  await page.waitForTimeout(400)
  const entry = page.getByRole('menuitem', { name: 'New folder…' })
  const hasEntry = await entry.count() > 0
  await page.screenshot({ path: `${OUT}/${MODE}-01-move-submenu-${THEME}.png`, clip: await clipAround(page, [page.getByRole('menu')]) })
  if (MODE === 'before') {
    assert('before: submenu has no New folder… entry', !hasEntry)
    await browser.close(); srv.close(); return
  }
  assert('after: submenu ends with New folder…', hasEntry)
  if (MODE === 'offline') {
    await entry.click()
    const dialog = page.getByRole('dialog')
    await dialog.waitFor({ state: 'visible', timeout: 5000 })
    await dialog.getByRole('textbox', { name: 'Folder name' }).fill('Storyboards')
    offline = true
    for (const ws of sockets) ws.close()
    const create = dialog.getByRole('button', { name: /^Create and move/ })
    await page.waitForFunction(el => el?.getAttribute('aria-disabled') === 'true', await create.elementHandle(), { timeout: 15000 })
    await page.waitForTimeout(400)
    assert('offline: Create held, name kept', (await create.isDisabled()) && (await dialog.getByRole('textbox', { name: 'Folder name' }).inputValue()) === 'Storyboards')
    const reason = (await dialog.getByTestId('new-folder-move-offline').textContent()) ?? ''
    assert('offline: the reason is shown in the dialog', reason === 'Gateway offline — reconnect to create folders', reason)
    await page.screenshot({ path: `${OUT}/offline-01-dialog-${THEME}.png`, clip: await clipAround(page, [dialog]) })
    await dialog.getByRole('button', { name: 'Cancel' }).click()
    await dialog.waitFor({ state: 'hidden', timeout: 5000 })
    // Offline the row reads as aria-disabled, which Playwright's actionability
    // check refuses; its context menu still opens, which is what is shown here.
    await row.click({ button: 'right', force: true })
    await trigger.waitFor({ state: 'visible', timeout: 5000 })
    await trigger.hover()
    await page.keyboard.press('ArrowRight')
    await entry.waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(400)
    assert('offline: entry dimmed and refused', (await entry.getAttribute('aria-disabled')) === 'true')
    assert('offline: reason shown under it', await page.getByTestId('folder-move-new-folder-offline').isVisible())
    await page.screenshot({ path: `${OUT}/offline-02-submenu-${THEME}.png`, clip: await clipAround(page, [page.getByRole('menu')]) })
    // Forced past the aria-disabled check: the point is that selecting it is refused.
    await entry.click({ force: true })
    await page.waitForTimeout(300)
    assert('offline: selecting it opens no dialog', (await page.getByRole('dialog').count()) === 0)
    assert('offline: nothing was written', created.length === 0 && moves.length === 0)
    await browser.close(); srv.close(); return
  }
  if (MODE === 'closed') {
    await entry.click()
    const dialog = page.getByRole('dialog')
    await dialog.waitFor({ state: 'visible', timeout: 5000 })
    await dialog.getByRole('textbox', { name: 'Folder name' }).fill('Storyboards')
    // Closed elsewhere: the server's list drops it, and a reconnect re-reads that list.
    slots.splice(slots.findIndex(s => s.key === SLOT), 1)
    for (const ws of sockets) ws.close()
    await row.waitFor({ state: 'detached', timeout: 15000 })
    const create = dialog.getByRole('button', { name: /^Create and move/ })
    await page.waitForFunction(el => el?.getAttribute('aria-disabled') !== 'true' && !el?.disabled, await create.elementHandle(), { timeout: 15000 })
    await create.click()
    const hint = dialog.getByTestId('new-folder-move-closed')
    await hint.waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(300)
    assert('closed: the dialog says the session was closed', ((await hint.textContent()) ?? '').includes('This session was closed'))
    assert('closed: as status text, not an error', (await hint.getAttribute('role')) === 'status' && (await page.getByTestId('new-folder-move-error').count()) === 0)
    assert('closed: nothing was written', created.length === 0 && moves.length === 0)
    assert('closed: Create is held, leaving Cancel', await create.isDisabled())
    await page.screenshot({ path: `${OUT}/closed-01-dialog-${THEME}.png`, clip: await clipAround(page, [dialog]) })
    await browser.close(); srv.close(); return
  }
  if (MODE === 'folderserr') {
    await entry.click()
    const dialog = page.getByRole('dialog')
    await dialog.waitFor({ state: 'visible', timeout: 5000 })
    await dialog.getByRole('textbox', { name: 'Folder name' }).fill('Storyboards')
    // The create lands; the server then refuses the list re-read and the move.
    foldersFail = true
    await dialog.getByRole('button', { name: 'Create and move' }).click()
    const err = dialog.getByTestId('new-folder-move-folders-error')
    await err.waitFor({ state: 'visible', timeout: 30000 })
    await dialog.getByTestId('new-folder-move-error').waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(300)
    const text = (await err.textContent()) ?? ''
    assert('folderserr: the dialog shows the folder-list error', text.includes('Folder list unavailable'), text)
    const moveText = (await dialog.getByTestId('new-folder-move-error').textContent()) ?? ''
    assert('folderserr: and the refused move, naming the folder', moveText === 'Created “Storyboards”, but the session was not moved into it: Gateway unreachable', moveText)
    assert('folderserr: one create', created.length === 1)
    await page.screenshot({ path: `${OUT}/folderserr-01-dialog-${THEME}.png`, clip: await clipAround(page, [dialog]) })
    await browser.close(); srv.close(); return
  }
  if (MODE === 'movefail') {
    await entry.click()
    const dialog = page.getByRole('dialog')
    await dialog.waitFor({ state: 'visible', timeout: 5000 })
    await dialog.getByRole('textbox', { name: 'Folder name' }).fill('Storyboards')
    await dialog.getByRole('checkbox', { name: 'Create inside “Video Projects”' }).check()
    await dialog.getByRole('button', { name: 'Create and move' }).click()
    // A page shortcut pressed mid-write must not reach the page: focus stays in the dialog.
    assert('movefail: focus stays in the dialog after a click submit', await dialog.evaluate(el => el.contains(document.activeElement)))
    const notice = page.getByTestId('new-folder-move-error')
    await notice.waitFor({ state: 'visible', timeout: 5000 })
    await page.waitForTimeout(300)
    const text = (await notice.textContent()) ?? ''
    assert('movefail: the notice names the created folder and its parent', text.includes('Created “Storyboards” inside “Video Projects”, but the session was not moved into it'), text)
    assert('movefail: one create, one refused move', created.length === 1 && moves.length === 1)
    assert('movefail: focus is back in the name field', await dialog.getByRole('textbox', { name: 'Folder name' }).evaluate(el => el === document.activeElement))
    await page.screenshot({ path: `${OUT}/movefail-01-notice-${THEME}.png`, clip: await clipAround(page, [dialog]) })
    await dialog.getByRole('button', { name: 'Retry move' }).click()
    await dialog.waitFor({ state: 'hidden', timeout: 5000 })
    assert('movefail: the retry moves without creating again', created.length === 1 && moves.length === 2 && moves[1].folder_id === 'f-new', JSON.stringify({ created, moves }))
    await browser.close(); srv.close(); return
  }
  const lastItem = await page.getByRole('menu').last().getByRole('menuitem').last().textContent()
  assert('after: New folder… is the last item', lastItem?.trim() === 'New folder…', lastItem ?? '')
  assert('after: entry is live (gateway connected)', (await entry.getAttribute('aria-disabled')) !== 'true')

  // 02: picking it closes the menu and opens the dialog, focus in the name field.
  await entry.click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor({ state: 'visible', timeout: 5000 })
  const name = dialog.getByRole('textbox', { name: 'Folder name' })
  await page.waitForTimeout(400)
  assert('dialog: name field has focus', await name.evaluate(el => el === document.activeElement))
  assert('dialog: menu closed', (await page.getByRole('menu').count()) === 0)
  await page.screenshot({ path: `${OUT}/after-02-dialog-empty-${THEME}.png`, clip: await clipAround(page, [dialog]) })

  // 03: a name typed and the current-folder offer ticked.
  await name.fill('Storyboards')
  const inside = dialog.getByRole('checkbox', { name: 'Create inside “Video Projects”' })
  assert('dialog: offers the session\'s current folder, unticked', !(await inside.isChecked()))
  await inside.check()
  await page.waitForTimeout(200)
  await page.screenshot({ path: `${OUT}/after-03-dialog-filled-${THEME}.png`, clip: await clipAround(page, [dialog]) })

  // 04: Create and move — one create, one move, dialog gone, row under the new folder.
  await dialog.getByRole('button', { name: 'Create and move' }).click()
  await dialog.waitFor({ state: 'hidden', timeout: 5000 })
  const newFolderRow = page.getByText('Storyboards', { exact: true }).first()
  await newFolderRow.waitFor({ state: 'visible', timeout: 8000 })
  await page.waitForTimeout(1200)
  assert('create request', created.length === 1 && created[0].name === 'Storyboards' && created[0].parent_id === 'f-video', JSON.stringify(created))
  assert('move request', moves.length === 1 && moves[0].slot === SLOT && moves[0].folder_id === 'f-new', JSON.stringify(moves))
  const sidebar = page.locator('.sidebar-inner').first()
  const sb = await sidebar.boundingBox()
  await page.screenshot({ path: `${OUT}/after-04-moved-${THEME}.png`, clip: { x: sb.x, y: sb.y, width: sb.width, height: Math.min(560, sb.height) } })
  console.log(JSON.stringify({ created, moves }))
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
