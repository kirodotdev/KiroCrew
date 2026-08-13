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

/** Folders the recommender would have been shown. */
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

  const NESTED = { folderId: 'f-i18n', folderName: 'i18n', breadcrumb: 'Kiro Crew › i18n' }
  const ROOT = { folderId: 'f-errands', folderName: 'Errands', breadcrumb: 'Errands' }

  // 1. Nested folder, dark — the common case: glyph, question, ancestry line.
  await load('dark')
  await pushCard(NESTED)
  await shot('01-nested-dark')
  await band('02-nested-dark-crop')

  // 2. Root folder — no ancestry line, since it would only repeat the name.
  await pushCard(ROOT)
  await band('03-root-no-breadcrumb-crop')

  // 3. Light theme, to prove the color-mix tokens track the theme.
  await load('light')
  await pushCard(NESTED)
  await band('04-nested-light-crop')

  // 4. Accept — the card must clear AND the real move endpoint must be hit.
  await page.getByTestId('folder-suggestion-accept').click()
  await page.waitForTimeout(700)
  const goneAfterAccept = (await page.getByTestId('folder-suggestion-card').count()) === 0
  await band('05-after-accept-light')

  // 5. Decline — clears with no API call.
  const movesBefore = moves.length
  await load('dark')
  await pushCard(NESTED)
  await page.getByTestId('folder-suggestion-decline').click()
  await page.waitForTimeout(700)
  const goneAfterDecline = (await page.getByTestId('folder-suggestion-card').count()) === 0
  await band('06-after-decline-dark')

  console.log('--- assertions ---')
  console.log('accept cleared the card:', goneAfterAccept)
  console.log('accept called the move API:', JSON.stringify(moves))
  console.log('decline cleared the card:', goneAfterDecline)
  console.log('decline made no extra API call:', moves.length === movesBefore)

  await browser.close()
  srv.close()

  const ok = goneAfterAccept
    && goneAfterDecline
    && moves.length === 1
    && moves[0].body?.folder_id === 'f-i18n'
    && moves[0].path.endsWith(`/${SLOT}/folder`)
  if (!ok) {
    console.error('FAIL: the card did not behave as documented')
    process.exit(1)
  }
  console.log('OK')
}

main().catch(err => { console.error(err); process.exit(1) })
