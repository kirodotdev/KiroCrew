/**
 * Evidence harness for folder-suggestion card EXPIRY (turn-based aging).
 *
 * Same shape as capture-folder-suggestion.mjs: the REAL built SPA behind the
 * loopback server, /api/** stubbed, the card pushed over the websocket exactly
 * as maybe_suggest_folder broadcasts it. This script then drives REAL composer
 * sends (POST /api/chat answered {ok:true}, each turn ended with a chat_done
 * frame the way the backend ends one) and proves the aging contract:
 *
 *   - the card survives sends 1..FOLDER_SUGGESTION_MAX_TURNS (3)
 *   - the card is gone on send 4
 *
 * Exits non-zero when either half fails, so it is a regression proof and not
 * just a camera. Also records a video of the whole sequence (temporal behavior
 * is the change, and a still cannot prove disappearance-over-time).
 *
 * Usage: node scripts/capture-folder-suggestion-expiry.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, mkdtempSync, readdirSync, renameSync, rmSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

// mise's node ships its own libstdc++ and exports LD_LIBRARY_PATH pointing at
// it; the spawned Chromium then resolves system libs (libLLVM, libgallium)
// against that older libstdc++ and dies with GLIBCXX_3.4.29 not found. The
// browser must resolve against the system loader path, so drop the override
// before launch.
delete process.env.LD_LIBRARY_PATH

const OUT = process.argv[2] || '../temp-screenshots/folder-suggestion-expiry'
const SLOT = 'chat-foldersug'
const PROJECT = '/home/user/workspace/KiroCrew'
const MAX_TURNS = 3 // mirrors FOLDER_SUGGESTION_MAX_TURNS in chatSlice.ts

mkdirSync(OUT, { recursive: true })
// THE video-artifact invariant (this file's history: three review rounds hit
// this span — stale-rename-skip, then a sweep that deleted caller files, then
// stale evidence surviving a failed rerun — so the shape below is chosen to
// make the whole class unreachable, not to patch the latest case):
//
//   ${OUT}/expiry-sequence.webm exists  <=>  the most recent run SUCCEEDED,
//   and no other caller-owned file in OUT is ever enumerated or deleted.
//
// Left side of the iff: invalidate only our named artifact NOW, before the
// first fallible step, so a run that dies anywhere leaves no prior recording
// behind to be collected as evidence for it. Right side: the recording is
// captured into a run-private temp dir (playwright names the file by internal
// id) and only moved into place after every assertion passed.
rmSync(`${OUT}/expiry-sequence.webm`, { force: true })
const VIDEO_DIR = mkdtempSync(`${OUT}/.video-`)

const slots = [{
  key: SLOT,
  title: 'Fix the render gate flake',
  running: false,
  last_message: 'Root-caused it to the SegmentedControl width spring.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
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
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'The artifacts.layout render gate keeps failing on CI. Why?' },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'Root cause is the `SegmentedControl` width spring. Added `settle:400`.' },
  ],
}

const folders = [
  { id: 'f-kc', name: 'Kiro Crew', order: 0, parent_id: '' },
  { id: 'f-i18n', name: 'i18n', order: 1, parent_id: 'f-kc' },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1500, height: 950 },
    deviceScaleFactor: 2,
    recordVideo: { dir: VIDEO_DIR, size: { width: 1500, height: 950 } },
  })

  let wsServer = null
  const sends = []

  const extra = (path, route) => {
    // Composer send. Answer ok (not queued) so the send path completes and the
    // optimistic startLocalTurn — the reducer under test — is the real one.
    if (path.startsWith('/api/chat') && route.request().method() === 'POST' && !path.includes('/slots/')) {
      sends.push(route.request().postDataJSON?.() ?? null)
      return json(route, { ok: true }), true
    }
    if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
    return false
  }

  /** Best-effort teardown for EVERY exit path. An assertion failure that
   *  leaves the browser or the static server alive keeps this event loop
   *  spinning forever, so a failing run would hang instead of exiting
   *  non-zero — the worst failure mode for a regression proof. Idempotent:
   *  the success path tears down explicitly too, because the recording file
   *  is only flushed when the context closes and the exactly-one-recording
   *  check must run after that. */
  async function cleanup() {
    try { await context.close() } catch { /* already closed */ }
    try { await browser.close() } catch { /* already closed */ }
    try { srv.close() } catch { /* already closed */ }
  }

  let page
  try {
    page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, { folders, slots, theme: 'dark', extra })
    await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
    await page.addInitScript(slot => localStorage.setItem('mc-active-slot', slot), SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)

    // Push the card exactly as the backend broadcasts it.
    wsServer.send(JSON.stringify({
      type: 'slot_folder_suggestion',
      data: { slot: SLOT, folder_id: 'f-i18n', folder_name: 'i18n', breadcrumb: 'Kiro Crew › i18n', ts: Date.now() / 1000 },
    }))
    await page.waitForTimeout(900)

    const cardCount = () => page.getByTestId('folder-suggestion-card').count()
    const shot = async name => {
      await page.screenshot({ path: `${OUT}/${name}.png` })
      console.log('wrote', `${OUT}/${name}.png`)
    }

    const survived = []
    if (await cardCount() !== 1) {
      // Throw, never process.exit(): exit() would skip the cleanup that
      // releases the browser and server. Same rule for every FAIL below.
      throw new Error('FAIL: card never appeared')
    }
    await shot('01-card-offered')

    /** One real composer send, then end the turn the way the backend does. */
    async function send(n) {
      const box = page.locator('textarea').first()
      await box.fill(`user turn ${n} — ignoring the folder offer`)
      await box.press('Enter')
      await page.waitForTimeout(600)
      // End the turn: assistant reply + chat_done, the frames a real turn ends with.
      wsServer.send(JSON.stringify({ type: 'chat_message', data: { slot: SLOT, role: 'assistant', content: `reply ${n}`, ts: new Date().toISOString() } }))
      wsServer.send(JSON.stringify({ type: 'chat_done', data: { slot: SLOT } }))
      await page.waitForTimeout(700)
    }

    for (let n = 1; n <= MAX_TURNS; n++) {
      await send(n)
      const alive = (await cardCount()) === 1
      survived.push(alive)
      await shot(`0${n + 1}-after-send-${n}${alive ? '-still-offered' : '-GONE-EARLY'}`)
    }

    await send(MAX_TURNS + 1)
    const goneAfterExpiry = (await cardCount()) === 0
    await shot(`0${MAX_TURNS + 2}-after-send-${MAX_TURNS + 1}-expired`)

    console.log('--- assertions ---')
    console.log(`card survived sends 1..${MAX_TURNS}:`, survived)
    console.log(`card gone after send ${MAX_TURNS + 1}:`, goneAfterExpiry)
    console.log('real sends reached the API:', sends.length)

    // The recording is finalised when the context closes, so teardown comes
    // BEFORE the exactly-one check and its move.
    await cleanup()

    const ok = survived.every(Boolean) && goneAfterExpiry && sends.length === MAX_TURNS + 1
    if (!ok) {
      throw new Error('FAIL: expiry did not behave as documented')
    }

    // Exactly one webm can exist in the run-private VIDEO_DIR — this run's
    // recording. Anything else is a recording failure, never a stale leftover.
    // The move is the TRUE last step, after every assertion above passed, so the
    // canonical name only ever holds a recording of a successful run (its prior
    // content was invalidated up top, before anything fallible).
    const vids = readdirSync(VIDEO_DIR).filter(f => f.endsWith('.webm'))
    if (vids.length !== 1) {
      throw new Error(`FAIL: expected exactly 1 fresh recording, found ${vids.length}`)
    }
    renameSync(`${VIDEO_DIR}/${vids[0]}`, `${OUT}/expiry-sequence.webm`)
    console.log('OK')
  } catch (err) {
    await cleanup()
    throw err
  }
}

// The run-private capture dir dies with the run, success or failure — only the
// canonical artifact (moved on success) outlives it. A hard kill can still
// strand one, which is harmless: every run mints a fresh mkdtemp dir, so a
// leftover can never feed a later run's exactly-one check.
main()
  .catch(err => { console.error(err); process.exitCode = 1 })
  .finally(() => rmSync(VIDEO_DIR, { recursive: true, force: true }))
