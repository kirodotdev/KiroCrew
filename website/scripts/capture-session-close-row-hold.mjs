/**
 * Screenshot harness for holding a closing session's row hidden until the close
 * resolves.
 *
 * The bug this proves gone is a FLICKER, so the evidence has to catch the frame
 * mid-close. Three things have to be true at once for the old behaviour to show,
 * and the harness manufactures all three:
 *
 *   1. a close is IN FLIGHT — the DELETE is held open by the fixture, so the row
 *      sits in the window between the optimistic removal and the server agreeing;
 *   2. an AUTHORITATIVE slot frame that STILL NAMES the closing slot lands inside
 *      that window — pushed over the mocked `/api/ws` as a real `slots` frame,
 *      which is what the gateway sends while its own DELETE is still mid-pop; and
 *   3. the sidebar is watched ACROSS the whole window, not sampled at the end.
 *
 * Before the fix, (2) reinstated the row: `applySlots` took the server's membership
 * literally, so the closed row came back and then vanished again when the DELETE
 * landed. After it, `closingSlots` withholds the key from every authoritative list
 * until the close retires, so the resurrecting frame changes nothing on screen.
 *
 * WHY THE MID-CLOSE AND SETTLED SHOTS LOOK IDENTICAL: that IS the fix. A frame that
 * never renders cannot be photographed, so a screenshot on its own cannot tell "the
 * push was ignored" from "the push never arrived". Two checks close that gap, and
 * both can fail:
 *
 *   - DELIVERY IS MEASURED, IN BAND. Every pushed frame also stamps a SURVIVING
 *     row's preview with its own frame number. The stamp appearing in the sidebar
 *     is proof the frame reached the store and `applySlots` ran on it — so the
 *     doomed row's absence in that same paint is the withhold and not a dead
 *     socket. Zero stamps is a harness failure, not a pass.
 *   - A NEGATIVE CONTROL runs after the close retires. The same kind of frame is
 *     pushed again and the row MUST come back — that is the server legitimately
 *     reporting the key again, which is what a resume looks like. Each frame also
 *     carries a changed field on purpose: `useWebSocket` skips a byte-identical
 *     repeat, so a literal replay would be dropped by the dedupe and the control
 *     would fail for the wrong reason.
 *
 * Together: the push path is live (control), frames did arrive mid-close (tally),
 * and the row still never appeared (poll). Only the tombstone explains that.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static server
 * with every /api/** call answered from fixtures (gateway-free), so the store logic
 * under test is the shipped store logic.
 *
 * Usage: node scripts/capture-session-close-row-hold.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/session-close-row-hold'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

// The row under test is NOT the active slot: closing the active one navigates, and
// the navigation would confound "the row is gone" with "the page changed".
const DOOMED = 'chat-nightly-triage'
const DOOMED_TITLE = 'Nightly triage sweep'
// The row that PROVES each pushed frame landed: its preview text is stamped with the
// frame number, so the stamp appearing in the sidebar is delivery, in band.
const WITNESS = 'chat-perf'
const WITNESS_TITLE = 'Sidebar render profile'

const slot = (key, title, modified, extra = {}) => ({
  key, title, running: false, messages: 6, agent: 'kirocrew',
  project_dir: '/home/z/KiroCrew', modified, last_ts: '2026-08-29T18:00:00Z',
  folder_id: '', last_message: 'Read the runbook and moved on.', ...extra,
})

const ALL = [
  slot('chat-current', 'Release checklist review', now, { messages: 12 }),
  slot(DOOMED, DOOMED_TITLE, now - 300),
  slot(WITNESS, WITNESS_TITLE, now - 900),
]
// What the server keeps reporting while its own DELETE is still mid-pop.
const AFTER = ALL.filter(s => s.key !== DOOMED)

async function main() {
  const { srv, base } = await serveDist()
  // mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
  // older than the system Mesa needs; children inherit it, so scrub it here.
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  // The DELETE is held here so the in-flight window is ours to photograph.
  let releaseDelete = null
  const deleteHeld = new Promise(r => { releaseDelete = r })
  let deleteSeen = false
  // Slot reads answer with the CLOSED list once the close has begun — that is what
  // retires the tombstone, and answering with the full list for ever would make the
  // hold indistinguishable from a row the server never dropped.
  let closeBegun = false
  const readLog = []

  await stubDashboardApi(page, {
    folders: [], slots: ALL,
    extra: async (path, route) => {
      if (path === '/api/chat/slots' && route.request().method() === 'GET') {
        readLog.push(closeBegun ? 'GET slots -> closed list' : 'GET slots -> full list')
        await json(route, closeBegun ? AFTER : ALL)
        return true
      }
      if (path === `/api/chat/slots/${DOOMED}` && route.request().method() === 'DELETE') {
        deleteSeen = true
        await deleteHeld           // the in-flight window opens here
        await json(route, { ok: true })
        return true
      }
      if (path === `/api/chat/slots/${DOOMED}`) { await json(route, { messages: [], has_more: false, total: 0 }); return true }
      if (path === '/api/chat/slots/chat-current') {
        await json(route, { messages: [{ role: 'user', content: 'scratch', ts: '2026-08-29T18:00:00Z', meta: { mid: 'm-1' } }], has_more: false, total: 1 })
        return true
      }
      return false
    },
  })

  // The resurrecting frame. Registered AFTER stubDashboardApi so it wins the route,
  // and it keeps a handle on the socket so a frame can be pushed on cue.
  //
  // Every push carries TWO changes, and both are load-bearing:
  //   - the DOOMED row is still listed, with a fresh `last_message` so the frame is
  //     not byte-identical to the last one (`useWebSocket` drops an exact repeat, so
  //     a literal replay would exercise the dedupe rather than the tombstone); and
  //   - the WITNESS row's preview is stamped with the frame number, which is how
  //     delivery is PROVEN rather than assumed. If the stamp shows up in the sidebar
  //     the frame reached the store and `applySlots` ran on it — so the doomed row's
  //     absence in that same paint is the withhold, not a dead socket. (Counting
  //     frames inside the page does NOT work here: `routeWebSocket` replaces the
  //     page's own `WebSocket`, so a wrapper installed by an init script never sees
  //     the traffic — measured, it tallied 0 while frames were demonstrably applied.)
  let sock = null
  let pushed = 0
  const stamp = n => `applied push #${n}`
  const pushFrame = () => {
    if (!sock) throw new Error('ws never opened')
    pushed++
    const data = ALL.map(s => {
      if (s.key === DOOMED) return { ...s, last_message: `still listed by the server (frame ${pushed})` }
      if (s.key === WITNESS) return { ...s, last_message: stamp(pushed) }
      return s
    })
    sock.send(JSON.stringify({ type: 'slots', data }))
  }
  await page.routeWebSocket(/\/api\/ws/, ws => { sock = ws })

  logPageProblems(page)

  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
  const row = page.locator('[role="button"]').filter({ hasText: DOOMED_TITLE }).first()
  await row.waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(900)

  // 1 — the row is there, before anything is asked of it.
  await row.hover()
  await page.waitForTimeout(250)
  await page.screenshot({ path: `${OUT}/1-row-present.png` })

  const closeBtn = row.getByRole('button', { name: 'Close session' }).first()
  await closeBtn.waitFor({ state: 'visible', timeout: 10_000 })
  closeBegun = true
  await closeBtn.click()

  // The close is now in flight. Push frames that still name the slot and watch the
  // row for the whole window — a flicker is a frame, not an end state. The witness
  // row's preview is read in the same loop, so delivery and absence are measured
  // against the SAME paints rather than at two different moments.
  const witness = page.locator('[role="button"]').filter({ hasText: WITNESS_TITLE }).first()
  let resurrections = 0
  let samples = 0
  let framesApplied = 0
  let lastWitnessText = ''
  const deadline = Date.now() + 3000
  while (Date.now() < deadline) {
    if (samples % 8 === 0) pushFrame()
    if (await row.isVisible()) resurrections++
    const text = (await witness.textContent().catch(() => '')) ?? ''
    const seen = /applied push #(\d+)/.exec(text)
    if (seen && seen[0] !== lastWitnessText) { framesApplied++; lastWitnessText = seen[0] }
    samples++
    await page.waitForTimeout(50)
  }

  // 2 — mid-close, with the server's own frames still naming the slot.
  await page.screenshot({ path: `${OUT}/2-row-held-hidden-mid-close.png` })

  releaseDelete()
  await page.waitForTimeout(1500)

  // 3 — settled: the close resolved and the row stayed gone.
  await page.screenshot({ path: `${OUT}/3-settled-after-close.png` })
  const visibleAfterSettle = await row.isVisible()

  // 4 — NEGATIVE CONTROL. The tombstone has retired, so the very same kind of frame
  // must now be honoured and the row must come back. If it does not, the push path
  // was never live and shots 2 and 3 prove nothing.
  pushFrame()
  let returned = false
  try {
    await row.waitFor({ state: 'visible', timeout: 5000 })
    returned = true
  } catch { /* reported below as a failure */ }
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/4-control-row-returns-after-retire.png` })

  console.log(`slot reads: ${JSON.stringify(readLog)}`)
  console.log(`DELETE observed=${deleteSeen} pushes=${pushed} framesAppliedInWindow=${framesApplied} (witness preview last read: "${lastWitnessText}")`)
  console.log(`samples=${samples} resurrections=${resurrections} visibleAfterSettle=${visibleAfterSettle} controlRowReturned=${returned}`)

  await browser.close()
  srv.close()

  if (!deleteSeen) throw new Error('no DELETE was issued — the close gesture did not fire')
  if (samples < 20) throw new Error(`only ${samples} samples of the in-flight window`)
  if (framesApplied < 1) throw new Error('no pushed frame was applied during the close — the witness row never showed a stamp, so the harness proved nothing')
  if (resurrections > 0) throw new Error(`row reappeared in ${resurrections}/${samples} samples of the in-flight window`)
  if (visibleAfterSettle) throw new Error('row is still visible after the close resolved')
  if (!returned) throw new Error('CONTROL FAILED: a post-retire frame naming the slot did not restore the row, so the push path was not live')
  console.log(`wrote ${OUT}/{1-row-present,2-row-held-hidden-mid-close,3-settled-after-close,4-control-row-returns-after-retire}.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
