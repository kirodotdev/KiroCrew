/** Real-browser evidence for #25977: the session header menu's "Initial prompt"
 * submenu.
 *
 * Drives website/capture/initial-prompt-25977.html — the real ChatHeaderMenu for
 * one seeded, active session whose transcript is served oldest-first. Asserted,
 * then shot:
 *
 *   menu               the header menu with the new "Initial prompt" row beside
 *                      MCP servers (BEFORE: run the same page against main, where
 *                      the row is absent; pass --before to only shoot the menu).
 *   submenu            the submenu: the FIRST prompt (not the later one), Copy
 *                      prompt, Draft a new session from this prompt. Dark and light.
 *   submenu-narrow     a 760px window: the submenu narrows to fit beside the menu.
 *   narrow-desktop     a 320px mouse-and-keyboard window: the submenu is inline.
 *   phone              a 320px touch screen: the submenu is inline and a long
 *                      prompt stays on screen.
 *   submenu-long       a long first prompt scrolls inside its box.
 *   submenu-empty      a session with no prompt yet says so and offers no action.
 *   (sent)             opened while empty, reopened after the first send: the
 *                      submenu re-reads and shows the prompt (no shot).
 *   loading            the history read is still running: the loading line.
 *   submenu-load-failed  the history read fails: an inline error notice, a Retry
 *                      row and its Ask the agent hand-off row (as on the copy and
 *                      create errors).
 *   (retry)            the first read fails, Retry reads again in place and the
 *                      prompt shows (no shot).
 *   copied             Copy prompt copies the prompt; the row reads Copied.
 *   copy-failed        both clipboard layers refuse: the failure notice stays.
 *   new-session        Draft a new session from this prompt creates a session scoped to
 *                      the same project and folder, then picks the same agent on
 *                      it (so a project-only agent resolves), stages the prompt for that
 *                      session's composer (delivered once), switches to it and closes the menu.
 *   (no-project)       a session with no project: one create carries the agent.
 *   (storage-refused)  storage refuses the staged prefill: the prompt is handed
 *                      over in memory instead, appended to that session's draft (no shot).
 *   starting / (moved-away)  the agent pick is slow: the row reads Opening a new
 *                      draft session… (shot); the user then opens another session, and the
 *                      new session is listed but the view stays put.
 *   (agent-refused)    the agent pick on the new session fails: that session is
 *                      deleted and the error stays in the submenu (no shot).
 *   new-session-failed the create fails: the menu stays open with the error.
 *
 * Usage (website/ is the vite root):
 *   npx vite --host 127.0.0.1 --port 6876 --strictPort
 *   node scripts/capture-25977-initial-prompt.mjs http://127.0.0.1:6876 ../temp-screenshots/initial-prompt [--before]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const BASE = process.argv[2] || 'http://127.0.0.1:6876'
const OUT = resolve(process.argv[3] || '../temp-screenshots/initial-prompt')
const BEFORE = process.argv.includes('--before')
mkdirSync(OUT, { recursive: true })
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (l, ok) => { console.log(`${l} => ${ok ? 'OK' : 'FAIL'}`); if (!ok) failures++ }

async function open(query, size = { width: 1100, height: 600 }, touch = false) {
  const ctx = await browser.newContext({ viewport: size, deviceScaleFactor: 2, ...(touch ? { hasTouch: true, isMobile: true } : {}) })
  ctx.setDefaultTimeout(120000)
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await page.goto(`${BASE}/capture/initial-prompt-25977.html?${query}`, { waitUntil: 'networkidle', timeout: 180000 })
  await page.getByRole('button', { name: 'Session options' }).click()
  await page.waitForSelector('[role="menu"]'); await page.waitForTimeout(300)
  return { ctx, page, errors }
}
async function openSub(page) {
  await page.getByTestId('initial-prompt-trigger').click()
  await page.waitForSelector('[data-testid="initial-prompt-panel"]')
  await page.waitForTimeout(400)
}
const labels = async page => (await page.getByRole('menuitem').allInnerTexts()).map(t => t.split('\n')[0].trim())

if (BEFORE) {
  const { ctx, page, errors } = await open('theme=dark')
  const l = await labels(page)
  check('[before] header menu open', l.length > 0)
  check('[before] no Initial prompt row on main', !l.includes('Initial prompt'))
  await page.screenshot({ path: resolve(OUT, 'before-menu.png') })
  check('[before] no page errors', errors.length === 0); await ctx.close()
  await browser.close()
  console.log(failures ? `${failures} check(s) FAILED` : 'all checks OK')
  process.exit(failures ? 1 : 0)
}

// The header menu gains the row, beside MCP servers.
{
  const { ctx, page, errors } = await open('theme=dark')
  const l = await labels(page)
  const iMcp = l.indexOf('MCP servers'); const iInit = l.indexOf('Initial prompt')
  check('[menu] Initial prompt row present', iInit >= 0)
  check('[menu] it sits right after MCP servers', iMcp >= 0 && iInit === iMcp + 1)
  check('[menu] nothing fetched before the submenu opens', !(await page.evaluate(() => window.__requests.some(r => r.url.includes('before=')))))
  await page.screenshot({ path: resolve(OUT, 'after-menu.png') })
  await openSub(page)
  const text = await page.getByTestId('initial-prompt-text').innerText()
  check('[submenu] shows the FIRST prompt', text.startsWith('Investigate why the composer loses its draft'))
  check('[submenu] keeps the prompt line breaks', text.includes('\n- no new dependencies'))
  check('[submenu] not the later prompt', !text.includes('popout window'))
  check('[submenu] read from the start of the transcript', await page.evaluate(() => window.__requests.some(r => r.method === 'GET' && r.url.includes('limit=50') && r.url.includes('before=50'))))
  check('[submenu] Copy prompt offered', (await labels(page)).includes('Copy prompt'))
  check('[submenu] New session from this prompt offered', (await labels(page)).includes('Draft a new session from this prompt'))
  await page.screenshot({ path: resolve(OUT, 'after-submenu.png') })
  check('[submenu] no page errors', errors.length === 0); await ctx.close()
}
{
  const { ctx, page, errors } = await open('theme=light')
  check('[light] the page is in the light theme', (await page.evaluate(() => document.documentElement.getAttribute('data-mode'))) === 'light')
  await openSub(page)
  check('[light] prompt shown', await page.getByTestId('initial-prompt-text').isVisible())
  await page.screenshot({ path: resolve(OUT, 'after-submenu-light.png') })
  check('[light] no page errors', errors.length === 0); await ctx.close()
}
{
  // A narrow window: neither side of the parent menu fits 320px, so the
  // submenu narrows to the room it has instead of running off the screen.
  const { ctx, page, errors } = await open('theme=dark', { width: 760, height: 600 })
  await openSub(page)
  const b = await page.getByTestId('initial-prompt-panel').boundingBox()
  check('[narrow] the submenu stays inside the window', !!b && b.x >= 0 && b.x + b.width <= 760 + 0.5)
  await page.screenshot({ path: resolve(OUT, 'after-submenu-narrow.png') })
  check('[narrow] no page errors', errors.length === 0); await ctx.close()
}
{
  // A 320px touch screen: the submenu renders inline inside the menu and a
  // long prompt still fits the screen width.
  const { ctx, page, errors } = await open('theme=dark&state=long', { width: 320, height: 640 }, true)
  await page.getByTestId('initial-prompt-trigger').click()
  await page.waitForSelector('[data-testid="initial-prompt-text"]', { timeout: 15000 }).catch(() => {})
  const box = await page.getByTestId('initial-prompt-text').boundingBox().catch(() => null)
  const menu = await page.locator('[role="menu"]').first().boundingBox()
  check('[phone] the prompt renders inline', !!box)
  check('[phone] the prompt stays on screen', !!box && box.x >= 0 && box.x + box.width <= 320 + 0.5)
  check('[phone] the menu stays on screen', !!menu && menu.x >= 0 && menu.x + menu.width <= 320 + 0.5)
  await page.screenshot({ path: resolve(OUT, 'after-phone.png') })
  check('[phone] no page errors', errors.length === 0); await ctx.close()
}
{
  // A 320px desktop window (mouse and keyboard): no room for a flyout on either
  // side of the menu, so the submenu renders inline, as on a touch screen.
  const { ctx, page, errors } = await open('theme=dark', { width: 320, height: 640 })
  await page.getByTestId('initial-prompt-trigger').click()
  await page.waitForSelector('[data-testid="initial-prompt-text"]', { timeout: 15000 }).catch(() => {})
  const box = await page.getByTestId('initial-prompt-text').boundingBox().catch(() => null)
  const panel = await page.getByTestId('initial-prompt-panel').boundingBox().catch(() => null)
  check('[narrow-desktop] the submenu renders inline and on screen', !!box && !!panel && panel.x >= 0 && panel.x + panel.width <= 320 + 0.5)
  check('[narrow-desktop] its actions are reachable', (await labels(page)).includes('Draft a new session from this prompt'))
  await page.screenshot({ path: resolve(OUT, 'after-narrow-desktop.png') })
  check('[narrow-desktop] no page errors', errors.length === 0); await ctx.close()
}
{
  const { ctx, page, errors } = await open('theme=dark&state=long')
  await openSub(page)
  const box = page.getByTestId('initial-prompt-text')
  const { scroll, client } = await box.evaluate(el => ({ scroll: el.scrollHeight, client: el.clientHeight }))
  check('[long] the prompt box scrolls instead of growing', scroll > client && client <= 200)
  await page.screenshot({ path: resolve(OUT, 'after-submenu-long.png') })
  check('[long] no page errors', errors.length === 0); await ctx.close()
}
{
  const { ctx, page, errors } = await open('theme=dark&state=empty')
  await openSub(page)
  check('[empty] says no prompt yet', (await page.getByTestId('initial-prompt-empty').innerText()).includes('No prompt has been sent'))
  check('[empty] offers no Copy / New session', !(await labels(page)).includes('Copy prompt') && !(await labels(page)).includes('Draft a new session from this prompt'))
  await page.screenshot({ path: resolve(OUT, 'after-submenu-empty.png') })
  check('[empty] no page errors', errors.length === 0); await ctx.close()
}
{
  // Opened while empty, then the first prompt is sent: reopening re-reads.
  const { ctx, page, errors } = await open('theme=dark&state=sent')
  await openSub(page)
  check('[sent] first open: no prompt yet', await page.getByTestId('initial-prompt-empty').isVisible())
  await page.getByRole('menuitem', { name: 'MCP servers' }).click(); await page.waitForTimeout(300)
  await openSub(page)
  await page.waitForSelector('[data-testid="initial-prompt-text"]', { timeout: 15000 }).catch(() => {})
  check('[sent] reopening shows the prompt sent since', (await page.getByTestId('initial-prompt-text').count()) > 0)
  check('[sent] no page errors', errors.length === 0); await ctx.close()
}
{
  const { ctx, page, errors } = await open('theme=dark&state=slow')
  await openSub(page)
  const line = page.getByTestId('initial-prompt-panel').getByRole('status')
  check('[loading] the loading line names the initial prompt', (await line.innerText()).includes('Loading the initial prompt'))
  await page.screenshot({ path: resolve(OUT, 'after-loading.png') })
  check('[loading] no page errors', errors.length === 0); await ctx.close()
}
{
  const { ctx, page, errors } = await open('theme=dark&state=fail')
  await openSub(page)
  await page.waitForSelector('[data-testid="initial-prompt-load-error"]')
  const loadNotice = await page.getByTestId('initial-prompt-load-error').innerText()
  check('[load-failed] the error notice renders', loadNotice.includes('Couldn’t load the initial prompt'))
  check('[load-failed] no close-and-reopen instruction', !/close|reopen/i.test(loadNotice))
  check('[load-failed] Retry is a menu item', (await labels(page)).includes('Retry'))
  check('[load-failed] its Ask the agent hand-off is a menu item', (await labels(page)).includes('Ask the agent'))
  await page.screenshot({ path: resolve(OUT, 'after-submenu-load-failed.png') })
  check('[load-failed] no page errors', errors.length === 0); await ctx.close()
}
{
  // The first read fails; Retry reads again inside the open submenu.
  const { ctx, page, errors } = await open('theme=dark&state=failonce')
  await openSub(page)
  await page.waitForSelector('[data-testid="initial-prompt-load-error"]')
  await page.getByTestId('initial-prompt-retry').click()
  await page.waitForSelector('[data-testid="initial-prompt-text"]', { timeout: 15000 }).catch(() => {})
  check('[retry] the submenu stayed open', await page.getByTestId('initial-prompt-panel').isVisible())
  check('[retry] the prompt shows after Retry', (await page.getByTestId('initial-prompt-text').count()) > 0)
  check('[retry] the notice is gone', (await page.getByTestId('initial-prompt-load-error').count()) === 0)
  check('[retry] no page errors', errors.length === 0); await ctx.close()
}
{
  const { ctx, page, errors } = await open('theme=dark')
  await openSub(page)
  await page.getByTestId('initial-prompt-copy').click(); await page.waitForTimeout(150)
  const writes = await page.evaluate(() => window.__clipWrites)
  check('[copied] the whole first prompt reached the clipboard', writes.length === 1 && writes[0].startsWith('Investigate why') && writes[0].endsWith('no new dependencies'))
  check('[copied] the row reads Copied', (await page.getByTestId('initial-prompt-copy-label').innerText()).trim() === 'Copied')
  check('[copied] the submenu is still open', await page.getByTestId('initial-prompt-panel').isVisible())
  await page.screenshot({ path: resolve(OUT, 'after-copied.png') })
  check('[copied] no page errors', errors.length === 0); await ctx.close()
}
{
  const { ctx, page, errors } = await open('theme=dark&clip=fail')
  await openSub(page)
  await page.getByTestId('initial-prompt-copy').click(); await page.waitForTimeout(250)
  check('[copy-failed] the failure notice renders', (await page.getByTestId('initial-prompt-copy-error').innerText()).includes('Couldn’t copy the prompt'))
  await page.waitForTimeout(1700)
  check('[copy-failed] the notice outlives the acknowledgment window', await page.getByTestId('initial-prompt-copy-error').isVisible())
  check('[copy-failed] its Ask the agent hand-off is a menu item', (await labels(page)).includes('Ask the agent'))
  await page.screenshot({ path: resolve(OUT, 'after-copy-failed.png') })
  check('[copy-failed] no page errors', errors.length === 0); await ctx.close()
}
{
  const { ctx, page, errors } = await open('theme=dark')
  await openSub(page)
  await page.getByTestId('initial-prompt-new-session').click()
  await page.waitForFunction(() => window.__store.getState().chat.activeSlot === 'chat-43-1700000500', null, { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(300)
  const reqs = await page.evaluate(() => window.__requests)
  const create = reqs.find(r => r.method === 'POST' && /\/api\/chat\/slots$/.test(r.url.split('?')[0]))
  check('[new-session] one create request', !!create)
  const NEW = '/api/chat/slots/chat-43-1700000500'
  const iAgent = reqs.findIndex(r => r.method === 'POST' && r.url.split('?')[0].endsWith(`${NEW}/agent`))
  const projectCalls = reqs.map((r, i) => [r, i]).filter(([r]) => r.url.split('?')[0].includes(NEW) && JSON.stringify(r.body || '').includes('/home/dev/KiroCrew')).map(([, i]) => i)
  check('[new-session] the create names no agent (it cannot see project-only agents)', !create?.body?.agent)
  check('[new-session] same sidebar folder', create?.body?.folder_id === 'f-kirocrew')
  check('[new-session] same agent, picked on the project-scoped session', iAgent >= 0 && reqs[iAgent].body?.agent === 'bug-resolve' && reqs[iAgent].body?.agent_kind === 'member')
  check('[new-session] same project, scoped before the agent pick and re-applied after it', projectCalls.length >= 2 && projectCalls[0] < iAgent && projectCalls[projectCalls.length - 1] > iAgent)
  check('[new-session] the create sends no message', !reqs.some(r => r.method === 'POST' && /\/(turn|send|message)s?\b/.test(r.url)))
  const st = await page.evaluate(() => ({ active: window.__store.getState().chat.activeSlot, pending: window.__store.getState().chat.mainComposerAppend, staged: sessionStorage.getItem('kirocrew_prefill') }))
  check('[new-session] the new session is now active', st.active === 'chat-43-1700000500')
  check('[new-session] delivered once, through the staged prefill (no second in-memory hand-off)', st.pending == null)
  check('[new-session] the prompt is staged for THAT session', !!st.staged && JSON.parse(st.staged).slotKey === 'chat-43-1700000500' && JSON.parse(st.staged).prompt.startsWith('Investigate why'))
  check('[new-session] the header menu closed', !(await page.locator('[role="menu"]').count()))
  await page.screenshot({ path: resolve(OUT, 'after-new-session.png') })
  check('[new-session] no page errors', errors.length === 0); await ctx.close()
}
{
  // A session with no project: one create carries the agent itself.
  const { ctx, page, errors } = await open('theme=dark&noproject=1')
  await openSub(page)
  await page.getByTestId('initial-prompt-new-session').click()
  await page.waitForFunction(() => window.__store.getState().chat.activeSlot === 'chat-43-1700000500', null, { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(300)
  const reqs = await page.evaluate(() => window.__requests)
  const create = reqs.find(r => r.method === 'POST' && /\/api\/chat\/slots$/.test(r.url.split('?')[0]))
  check('[no-project] the create carries the same agent', create?.body?.agent === 'bug-resolve' && create?.body?.agent_kind === 'member' && create?.body?.folder_id === 'f-kirocrew')
  check('[no-project] no separate agent pick', !reqs.some(r => r.url.split('?')[0].endsWith('/agent')))
  const st = await page.evaluate(() => ({ active: window.__store.getState().chat.activeSlot, pending: window.__store.getState().chat.mainComposerAppend, staged: sessionStorage.getItem('kirocrew_prefill') }))
  check('[no-project] the new session is active with the prompt staged once', st.active === 'chat-43-1700000500' && st.pending == null && !!st.staged && JSON.parse(st.staged).slotKey === 'chat-43-1700000500')
  check('[no-project] no page errors', errors.length === 0); await ctx.close()
}
{
  // Storage refuses the staged prefill: the prompt is handed over in memory instead.
  const { ctx, page, errors } = await open('theme=dark&storage=fail')
  await openSub(page)
  await page.getByTestId('initial-prompt-new-session').click()
  await page.waitForFunction(() => window.__store.getState().chat.activeSlot === 'chat-43-1700000500', null, { timeout: 15000 }).catch(() => {})
  await page.waitForTimeout(300)
  const st = await page.evaluate(() => ({ pending: window.__store.getState().chat.mainComposerAppend, staged: sessionStorage.getItem('kirocrew_prefill') }))
  check('[storage-refused] nothing staged in storage', st.staged == null)
  check('[storage-refused] the prompt is handed to THAT session\'s composer in memory, to append, unsent', st.pending?.slot === 'chat-43-1700000500' && st.pending.text.startsWith('Investigate why'))
  check('[storage-refused] no page errors', errors.length === 0); await ctx.close()
}
{
  // The user opens another session while the agent pick runs: the new session
  // is listed, the view stays where the user went, and no composer is seeded.
  const { ctx, page, errors } = await open('theme=dark&agent=slow')
  await openSub(page)
  await page.getByTestId('initial-prompt-new-session').click()
  await page.waitForFunction(() => window.__requests.some(r => r.method === 'POST' && r.url.split('?')[0].endsWith('/api/chat/slots/chat-43-1700000500/agent')), null, { timeout: 15000 }).catch(() => {})
  check('[starting] the row reads Opening a new draft session…', (await page.getByTestId('initial-prompt-new-session').innerText()).includes('Opening a new draft session'))
  await page.screenshot({ path: resolve(OUT, 'after-starting.png') })
  await page.evaluate(() => window.__openOther())
  await page.waitForTimeout(2500)
  const st = await page.evaluate(() => ({ active: window.__store.getState().chat.activeSlot, pending: window.__store.getState().chat.mainComposerAppend, listed: window.__store.getState().dashboard.slots.some(s => s.key === 'chat-43-1700000500') }))
  check('[moved-away] the view stays on the session the user opened', st.active === 'chat-41-1699999000')
  check('[moved-away] the new session is still listed', st.listed)
  check('[moved-away] nothing handed to a composer in memory', st.pending == null)
  check('[moved-away] no page errors', errors.length === 0); await ctx.close()
}
{
  // The agent pick on the new session is refused: the half-made session is
  // removed, the error stays in the submenu, and nothing moves or is seeded.
  const { ctx, page, errors } = await open('theme=dark&agent=fail')
  await openSub(page)
  await page.getByTestId('initial-prompt-new-session').click()
  await page.waitForSelector('[data-testid="initial-prompt-new-session-error"]', { timeout: 15000 }).catch(() => {})
  const reqs = await page.evaluate(() => window.__requests)
  check('[agent-refused] the half-made session is deleted', reqs.some(r => r.method === 'DELETE' && r.url.split('?')[0].endsWith('/api/chat/slots/chat-43-1700000500')))
  check('[agent-refused] the error shows in the submenu', (await page.getByTestId('initial-prompt-new-session-error').count()) > 0)
  check('[agent-refused] the session did not change', (await page.evaluate(() => window.__store.getState().chat.activeSlot)) === 'chat-42-1700000000')
  check('[agent-refused] nothing staged into a composer', (await page.evaluate(() => window.__store.getState().chat.mainComposerAppend)) == null)
  check('[agent-refused] no page errors', errors.length === 0); await ctx.close()
}
{
  const { ctx, page, errors } = await open('theme=dark&create=fail')
  await openSub(page)
  await page.getByTestId('initial-prompt-new-session').click()
  await page.waitForSelector('[data-testid="initial-prompt-new-session-error"]', { timeout: 15000 }).catch(() => {})
  const notice = page.getByTestId('initial-prompt-new-session-error')
  check('[new-session-failed] the error notice renders in the submenu', (await notice.count()) > 0 && (await notice.innerText()).includes('Couldn’t start a new session'))
  check('[new-session-failed] the session did not change', (await page.evaluate(() => window.__store.getState().chat.activeSlot)) === 'chat-42-1700000000')
  check('[new-session-failed] its Ask the agent hand-off is a menu item', (await labels(page)).includes('Ask the agent'))
  check('[new-session-failed] nothing staged into a composer', (await page.evaluate(() => window.__store.getState().chat.mainComposerAppend)) == null)
  await page.screenshot({ path: resolve(OUT, 'after-new-session-failed.png') })
  check('[new-session-failed] no page errors', errors.length === 0); await ctx.close()
}
await browser.close()
console.log(failures ? `${failures} check(s) FAILED` : 'all checks OK')
process.exit(failures ? 1 : 0)
