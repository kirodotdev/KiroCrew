/**
 * Screenshot harness for the folder-suggestion card.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server, with
 * /api/** answered by the shared fixture stub. No gateway, no folders created.
 *
 * The client code under test is unmodified — only the network is stubbed — so the
 * card is exercised exactly as it runs in production, and it is driven the way
 * the backend drives it: by pushing a `slot_folder_suggestion` frame into the
 * websocket after the page has rendered. The accept path's
 * PATCH /api/chat/slots/{slot}/folder is intercepted and asserted, so this also
 * proves the button reaches the real move endpoint — the harness exits non-zero
 * when it does not, which makes it a regression test and not just a camera.
 *
 * The card carries a prefilled dropdown of every folder (the suggestion
 * preselected), so beyond the accept/decline contract this also asserts the
 * dropdown's own promise: picking a different folder and accepting must move to
 * the PICKED folder, not the suggested one.
 *
 * Usage: node scripts/capture-folder-suggestion.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-suggestion'
const SLOT = 'chat-foldersug'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Fix the render gate flake',
  running: false,
  last_message: 'Root-caused it to the SegmentedControl width spring.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',          // unfiled — the whole precondition for the card
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    {
      role: 'user',
      ts: Date.now() / 1000 - 600,
      content: 'The artifacts.layout render gate keeps failing on CI. Why?',
    },
    {
      role: 'assistant',
      ts: Date.now() / 1000 - 30,
      content:
        'Root cause is the `SegmentedControl` width spring — the scan sampled ' +
        '`scrollWidth` mid-animation. Added `settle:400` to the artifacts surface.',
    },
  ],
}

/** Folders the recommender would have been shown — also the dropdown's options. */
const folders = [
  { id: 'f-kc', name: 'Kiro Crew', order: 0, parent_id: '' },
  { id: 'f-i18n', name: 'i18n', order: 1, parent_id: 'f-kc' },
  { id: 'f-errands', name: 'Errands', order: 2, parent_id: '' },
]

/** Recorded so the run can prove the accept button hits the real move API. */
const moves = []

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    // The card is dense small type (11–12px); a 1x shot renders it soft on GitHub.
    deviceScaleFactor: 2,
  })

  // Routes the shared stub does not know about: the per-slot detail fetch, and
  // the move endpoint whose call is the thing being asserted. Each branch returns
  // an explicit `true` — the stub treats a falsy return as "not handled" and
  // fulfils it itself, and `json()` resolves to undefined, so `return json(...)`
  // alone would double-fulfil ("Route is already handled!").
  const extra = (path, route) => {
    if (/^\/api\/chat\/slots\/[^/]+\/folder$/.test(path)) {
      moves.push({ path, body: route.request().postDataJSON?.() ?? null })
      return json(route, { ok: true, folder_id: 'f-i18n' }), true
    }
    if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
    return false
  }

  let page = null
  let wsServer = null

  /** Best-effort teardown for EVERY exit path: an assertion failure that left
   *  the browser or the static server alive kept this event loop spinning, so
   *  a failing run hung instead of exiting non-zero. Idempotent — the success
   *  path tears down explicitly too, right before its final assertions. */
  async function cleanup() {
    try { await context.close() } catch { /* already closed */ }
    try { await browser.close() } catch { /* already closed */ }
    try { srv.close() } catch { /* already closed */ }
  }

  try {

  /**
   * A FRESH page per theme. stubDashboardApi installs one `**\/api\/**` handler
   * and bakes the theme into /api/theme/boot, so calling it twice on one page
   * throws "Route is already handled!" and the theme could not change anyway.
   */
  async function load(theme) {
    if (page) await page.close()
    wsServer = null
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { folders, slots, theme, extra })
    // Registered AFTER the shared stub so this handler wins: the stub swallows
    // /api/ws to stop a retry-storm, but this harness needs the socket handle to
    // push the card frame the backend would have sent.
    await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
    await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  /** Push the card exactly as maybe_suggest_folder broadcasts it. */
  async function pushCard({ folderId, folderName, breadcrumb }) {
    if (!wsServer) throw new Error('websocket route never bound')
    wsServer.send(JSON.stringify({
      type: 'slot_folder_suggestion',
      data: {
        slot: SLOT,
        folder_id: folderId,
        folder_name: folderName,
        breadcrumb,
        ts: Date.now() / 1000,
      },
    }))
    await page.waitForTimeout(900)
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Tight crop on the card + composer band, which is the whole story. */
  async function band(name) {
    const card = page.getByTestId('folder-suggestion-card')
    if (await card.count()) {
      const box = await card.first().boundingBox()
      if (box) {
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: {
            x: Math.max(0, box.x - 24),
            y: Math.max(0, box.y - 16),
            width: Math.min(1500 - Math.max(0, box.x - 24), box.width + 48),
            height: box.height + 120,
          },
        })
        console.log('wrote', `${OUT}/${name}.png`)
        return
      }
    }
    await shot(name)
  }

  const select = () => page.getByTestId('folder-suggestion-select')

  const NESTED = { folderId: 'f-i18n', folderName: 'i18n', breadcrumb: 'Kiro Crew › i18n' }
  const ROOT = { folderId: 'f-errands', folderName: 'Errands', breadcrumb: 'Errands' }

  // 1. Nested folder, dark — the common case. The dropdown arrives prefilled
  //    with the suggestion, its option labeled by the full ancestry path.
  await load('dark')
  await pushCard(NESTED)
  const prefilled = await select().inputValue()
  const nestedLabel = await select().evaluate(el => el.selectedOptions[0]?.textContent)
  await shot('01-nested-dark')
  await band('02-nested-dark-crop')

  // 2. Root folder — the option is the bare name; a path would just repeat it.
  await pushCard(ROOT)
  const rootLabel = await select().evaluate(el => el.selectedOptions[0]?.textContent)
  await band('03-root-bare-name-crop')

  // 3. Light theme, to prove the color-mix tokens track the theme.
  await load('light')
  await pushCard(NESTED)
  await band('04-nested-light-crop')

  // 4. Accept untouched — one click must still move to the SUGGESTED folder.
  await page.getByTestId('folder-suggestion-accept').click()
  await page.waitForTimeout(700)
  const goneAfterAccept = (await page.getByTestId('folder-suggestion-card').count()) === 0
  await band('05-after-accept-light')

  // 5. The dropdown's own promise: pick a different folder, accept, and the
  //    move must target the PICKED folder — not the suggestion.
  await load('dark')
  await pushCard(NESTED)
  await select().selectOption('f-errands')
  await page.waitForTimeout(300)
  await band('06-picked-other-folder-dark-crop')
  await page.getByTestId('folder-suggestion-accept').click()
  await page.waitForTimeout(700)
  const goneAfterPickedAccept = (await page.getByTestId('folder-suggestion-card').count()) === 0

  // 6. Decline — clears with no API call.
  const movesBefore = moves.length
  await load('dark')
  await pushCard(NESTED)
  await page.getByTestId('folder-suggestion-decline').click()
  await page.waitForTimeout(700)
  const goneAfterDecline = (await page.getByTestId('folder-suggestion-card').count()) === 0
  await band('07-after-decline-dark')

  // 7. 320px viewport — the AUTOSDE narrow-viewport floor. flex-wrap must
  //    reflow the label, select, and actions into rows: nothing may clip.
  //    Asserted, not just pictured: no horizontal overflow inside the card,
  //    and every interactive control fully inside the viewport.
  await load('dark')
  await page.setViewportSize({ width: 320, height: 700 })
  await pushCard(NESTED)
  const narrowNoOverflow = await page
    .getByTestId('folder-suggestion-card')
    .evaluate(el => el.scrollWidth <= el.clientWidth + 1)
  const narrowControlsInside = await page.evaluate(() => {
    const ids = ['folder-suggestion-select', 'folder-suggestion-accept', 'folder-suggestion-decline']
    return ids.every(id => {
      const r = document.querySelector(`[data-testid="${id}"]`)?.getBoundingClientRect()
      return !!r && r.width > 0 && r.left >= 0 && r.right <= window.innerWidth + 0.5
    })
  })
  await shot('08-narrow-320-dark')

  console.log('--- assertions ---')
  console.log('dropdown prefilled with the suggestion:', prefilled === 'f-i18n')
  console.log('nested option labeled by path:', JSON.stringify(nestedLabel))
  console.log('root option labeled by bare name:', JSON.stringify(rootLabel))
  console.log('accept cleared the card:', goneAfterAccept)
  console.log('picked-accept cleared the card:', goneAfterPickedAccept)
  console.log('move API calls:', JSON.stringify(moves))
  console.log('decline cleared the card:', goneAfterDecline)
  console.log('decline made no extra API call:', moves.length === movesBefore)
  console.log('320px: card has no horizontal overflow:', narrowNoOverflow)
  console.log('320px: all controls inside the viewport:', narrowControlsInside)

  await cleanup()

  const ok = prefilled === 'f-i18n'
    && nestedLabel === 'Kiro Crew › i18n'
    && rootLabel === 'Errands'
    && goneAfterAccept
    && goneAfterPickedAccept
    && goneAfterDecline
    && moves.length === 2
    && moves[0].body?.folder_id === 'f-i18n'
    && moves[1].body?.folder_id === 'f-errands'
    && moves.every(m => m.path.endsWith(`/${SLOT}/folder`))
    && narrowNoOverflow
    && narrowControlsInside
  if (!ok) {
    // Throw, never process.exit(): exit() would skip the catch below, whose
    // cleanup releases the browser and server this run still holds.
    throw new Error('FAIL: the card did not behave as documented')
  }
  console.log('OK')
  } catch (err) {
    await cleanup()
    throw err
  }
}

main().catch(err => { console.error(err); process.exit(1) })
