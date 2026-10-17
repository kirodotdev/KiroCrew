/**
 * Screenshot harness + assertions for the composer UPLOAD CANCEL control
 * (issue #5744).
 *
 * The state being photographed cannot be reached with a normal stub: the
 * control only exists while an upload is genuinely in flight. So
 * `/api/upload/file` is intercepted and never answered, which is exactly what a
 * slow 150 MB recording looks like to the composer, and the shots are taken
 * against the REAL built SPA (website/dist) with no gateway.
 *
 * Three scenarios per host, and each ASSERTS rather than only photographing:
 *
 *   1. idle -- no cancel control, attach control live;
 *   2. uploading -- the spinner has replaced the attach control AND a cancel
 *      control sits beside it, inside the same composer;
 *   3. cancelled -- the control is gone, the attach control is live again, and
 *      no error banner appeared. That last one is the trap: the blanket catch
 *      would otherwise render "check file type and size (max 50 MB)" for an
 *      upload the user deliberately stopped.
 *
 * Both composer hosts are covered, because a cancel control in one and not the
 * other is its own bug report: ChatPage's single composer and ChatPane's
 * in-pane composer in split view.
 *
 * Nothing in CI runs this file. The CI-enforced half of the behaviour is
 * src/test/ComposerUploadCancel.test.tsx and the ChatInput cases in
 * src/test/ChatInput.test.tsx.
 *
 * Usage: node scripts/capture-composer-upload-cancel.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, writeFileSync, mkdtempSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { tmpdir } from 'node:os'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'
import { prepareSplitChatPage } from './lib/prepare-split-chat-page.mjs'

const OUT = process.argv[2] || '../temp-screenshots/composer-upload-cancel'
const SLOT = 'chat-cancel'
const CANCEL = 'Cancel upload'
const ATTACH = /Add files & options|Attach files/

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const slot = (key, title, last) => ({
  key, title, running: false, last_message: last, messages: 2,
  agent: 'kirocrew', memory_mode: 'persistent', project: '', folder_id: '',
  modified: now, source_links: [], source_links_total: 0,
})

const detail = (a, b) => ({
  running: false, has_more: false, total: 2, queue: [], project: '',
  messages: [
    { role: 'user', ts: now - 300, content: a, cls: 'msg msg-user' },
    { role: 'assistant', ts: now - 240, content: b, cls: 'msg msg-assistant' },
  ],
})

const PAGE_DETAIL = detail(
  'Here is the screen recording of the crash.',
  'Attach it and I will pull the frames out with ffmpeg.',
)

/** A file big enough to be a plausible recording without allocating one. */
function stageRecording() {
  const dir = mkdtempSync(join(tmpdir(), 'composer-cancel-'))
  const file = join(dir, 'crash-repro.mp4')
  // A real ftyp box header, so nothing downstream reads it as a broken file.
  writeFileSync(file, Buffer.concat([
    Buffer.from('\x00\x00\x00\x18ftypisom\x00\x00\x02\x00', 'latin1'),
    Buffer.alloc(4096),
  ]))
  return { dir, file }
}

/**
 * Hold the upload open forever. Returning without fulfilling leaves the request
 * pending in the browser, which is the whole point: the composer stays in its
 * uploading state until the client aborts it.
 */
let uploadRequests = 0
async function hangUpload(path, route) {
  if (path !== '/api/upload/file') return false
  uploadRequests += 1
  void route // deliberately never fulfilled
  return true
}

/**
 * Assert and report one scenario's three facts about a composer scope.
 *
 * `attach` is 'live' or 'replaced', not enabled/disabled: the bottom icon row is
 * at `max-two-buttons-per-row`, so the cancel control takes the attach control's
 * slot for the duration of the upload instead of joining the row. "The attach
 * control is gone and exactly one cancel control stands there" is therefore the
 * property, and it is also what proves the row did not grow.
 */
async function check(scope, label, { cancel, attach }) {
  const seen = await scope.getByRole('button', { name: CANCEL }).count()
  if (seen !== (cancel ? 1 : 0)) {
    throw new Error(`${label}: expected ${cancel ? 1 : 0} cancel control(s), found ${seen}`)
  }
  const attachCount = await scope.getByTitle(ATTACH).count()
  if (attach === 'replaced' && attachCount !== 0) {
    throw new Error(`${label}: the attach control is still in the row (${attachCount}) beside the cancel control`)
  }
  if (attach === 'live') {
    if (attachCount === 0) throw new Error(`${label}: the attach control did not come back`)
    const live = await scope.getByTitle(ATTACH).first().evaluate(el => (
      el.getAttribute('aria-disabled') !== 'true' && !el.hasAttribute('disabled')
    ))
    if (!live) throw new Error(`${label}: the attach control is present but still switched off`)
  }
  const banner = await scope.getByText(/max 50 MB|Upload failed/i).count()
  if (banner !== 0) throw new Error(`${label}: an upload failure banner is on screen`)
  console.log(`${label}: cancel=${seen} attach=${attach}(n=${attachCount}) banner=${banner}`)
}

/**
 * A tight crop of the composer's bottom icon row. The control is a 32px button;
 * a 1500px page shot cannot show whether the X inside the spinner reads, and
 * that legibility is the whole question a reviewer has about it.
 */
async function cropRow(page, scope, path) {
  const box = await scope.locator('.flex.items-center.gap-0\\.5').first().boundingBox()
  if (!box) throw new Error('bottom icon row has no box to crop')
  const pad = 10
  await page.screenshot({
    path,
    clip: { x: box.x - pad, y: box.y - pad, width: Math.min(box.width + pad * 2, 260), height: box.height + pad * 2 },
  })
}

const { srv, base } = await serveDist()

/** ChatPage: the single full-width composer. */
async function capturePage(browser) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots: [slot(SLOT, 'Crash repro', 'Attach it and I will pull the frames out.')],
    extra: async (path, route) => {
      if (await hangUpload(path, route)) return true
      if (path.startsWith('/api/chat/slot/')) { await json(route, PAGE_DETAIL); return true }
      return false
    },
  })
  await page.goto(`${base}/chat/${SLOT}`)
  await page.waitForSelector('textarea[data-composer-input]')
  await page.waitForTimeout(500)

  await check(page, 'page/idle', { cancel: false, attach: 'live' })
  await page.screenshot({ path: `${OUT}/page-01-idle.png` })
  await cropRow(page, page, `${OUT}/page-01b-idle-closeup.png`)

  const { dir, file } = stageRecording()
  await page.locator('input[type="file"]').first().setInputFiles(file)

  const control = page.getByRole('button', { name: CANCEL })
  await control.waitFor({ state: 'visible', timeout: 15000 })
  // The attach control's slot now holds the way out, and nothing else moved.
  await check(page, 'page/uploading', { cancel: true, attach: 'replaced' })
  await page.screenshot({ path: `${OUT}/page-02-uploading-with-cancel.png` })
  await cropRow(page, page, `${OUT}/page-02b-control-closeup.png`)

  await control.click()
  await control.waitFor({ state: 'detached', timeout: 10000 })
  await page.waitForTimeout(300)
  await check(page, 'page/cancelled', { cancel: false, attach: 'live' })
  await page.screenshot({ path: `${OUT}/page-03-cancelled-attach-restored.png` })

  rmSync(dir, { recursive: true, force: true })
  await context.close()
}

/** ChatPane: the in-pane composer of the split grid, the second host. */
async function capturePane(browser) {
  const context = await browser.newContext({ viewport: { width: 1500, height: 950 }, deviceScaleFactor: 2 })
  const panes = { 'pane-a': 'seed-a', 'pane-b': 'seed-b' }
  const splitLayouts = {
    'pane-a': {
      type: 'split', id: 'seed-split', dir: 'row',
      children: Object.entries(panes).map(([s, id]) => ({ type: 'leaf', id, kind: 'session', slot: s })),
      sizes: [0.5, 0.5],
    },
  }
  const page = await prepareSplitChatPage(context, {
    base,
    fixtures: {
      '/api/chat/slots': [
        slot('pane-a', 'Design notes', 'Working through phase one.'),
        slot('pane-b', 'Crash repro', 'Drop the recording in.'),
      ],
      // session_grid gates split view; without it the persisted layout is
      // ignored and the app stays in single-chat mode.
      '/api/dashboard/config': { session_grid: true },
      '/api/kiro-prerequisite': {
        platform: 'linux', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      },
    },
    detailA: detail('Compare the two layouts.', 'Option A keeps the sidebar fixed.'),
    detailB: detail('Here is the screen recording.', 'Drop the recording in.'),
    splitLayouts,
    json,
    pre: hangUpload,
  })

  const paneNodes = page.locator('[data-chat-pane]')
  await paneNodes.first().waitFor({ state: 'visible', timeout: 20000 })
  const count = await paneNodes.count()
  if (count < 2) throw new Error(`expected a 2-pane split, got ${count}`)
  // Drive the SECOND pane, so the shot shows one composer offering the exit
  // while its untouched neighbour is right beside it for contrast.
  const pane = paneNodes.nth(1)

  await check(pane, 'pane/idle', { cancel: false, attach: 'live' })
  await page.screenshot({ path: `${OUT}/pane-01-idle.png` })

  const { dir, file } = stageRecording()
  await pane.locator('input[type="file"]').first().setInputFiles(file)

  const control = pane.getByRole('button', { name: CANCEL })
  await control.waitFor({ state: 'visible', timeout: 15000 })
  await check(pane, 'pane/uploading', { cancel: true, attach: 'replaced' })
  // The neighbour must be untouched: cancel belongs to the pane that uploaded.
  await check(paneNodes.nth(0), 'pane/neighbour-untouched', { cancel: false, attach: 'live' })
  await page.screenshot({ path: `${OUT}/pane-02-uploading-with-cancel.png` })
  await pane.screenshot({ path: `${OUT}/pane-03-closeup.png` })
  await cropRow(page, pane, `${OUT}/pane-03b-control-closeup.png`)

  await control.click()
  await control.waitFor({ state: 'detached', timeout: 10000 })
  await page.waitForTimeout(300)
  await check(pane, 'pane/cancelled', { cancel: false, attach: 'live' })
  await page.screenshot({ path: `${OUT}/pane-04-cancelled-attach-restored.png` })

  rmSync(dir, { recursive: true, force: true })
  await context.close()
}

async function main() {
  const browser = await chromium.launch()
  await capturePage(browser)
  await capturePane(browser)
  await browser.close()
  srv.close()
  if (uploadRequests < 2) throw new Error(`expected an upload request per host, saw ${uploadRequests}`)
  console.log(`upload requests held open: ${uploadRequests}`)
  console.log('done ->', OUT)
}

main().catch(err => { console.error(err); srv.close(); process.exit(1) })
