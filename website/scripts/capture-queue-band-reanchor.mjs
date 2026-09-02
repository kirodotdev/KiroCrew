/**
 * Screenshot harness + geometry check for the QUEUE-BAND RE-ANCHOR.
 *
 * A send that lands behind a busy turn mounts the queue stack between the
 * transcript and the composer AND appends a queued row to the message array.
 * The virtualizer re-pins for the scroller shrink, but the append regroups and
 * remounts tail rows while the band's spring animates, so the re-pin can land
 * on interior heights that are still settling — the view ends a card-height
 * (CARD_H − OVERLAP = 29px for one card) low. That offset eats the
 * transcript's tail clearance; wherever the remaining budget is thinner
 * than the offset — phones most of all, where the fade-band margin is single
 * digits (see TRANSCRIPT_TAIL_SPACER_PX's comment) — the last line lands under
 * the clip edge and renders as sliced glyphs directly above the queue card.
 * The fix is a ResizeObserver on the composer status stack that re-anchors
 * while FOLLOW holds, after every layout step of the animation.
 *
 * This asserts as well as photographs: it boots the REAL built SPA
 * (website/dist) parked at the bottom of a long transcript, pushes a
 * `queue_push` frame through the stubbed websocket, then samples the last
 * line's clearance to the band every ~40ms across the band's spring
 * animation. On the fixed build the observer re-anchors at every layout step,
 * so the clearance never dips more than a couple px. On a pre-fix build the
 * anchor is left to a RACE — a stray re-render can re-pin the bottom mid
 * animation — so the dip reaches the band's height and, depending on timing,
 * may or may not recover; that nondeterminism is why the defect reads as
 * intermittent. Run with --expect anchored against the fixed build and
 * --expect offset against a pre-fix build for the BEFORE shots (captured at
 * the moment of deepest dip); either way a wrong outcome exits non-zero.
 * Nothing in CI runs this file — the CI-enforced half is
 * src/test/ChatPage.queueBandReanchor.test.tsx.
 *
 * Usage: node scripts/capture-queue-band-reanchor.mjs [outDir] [--expect anchored|offset]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const args = process.argv.slice(2)
const OUT = args.find(a => !a.startsWith('--')) || '../temp-screenshots/queue-band-reanchor'
const EXPECT = args.includes('--expect') ? args[args.indexOf('--expect') + 1] : 'anchored'
const SLOT = 'chat-queue-reanchor'
const PROJECT = '/home/user/workspace/notes'

/** One collapsed queue card's layout height: CARD_H(40) − OVERLAP(11). */
const BAND_PX = 29

mkdirSync(OUT, { recursive: true })

const now = Date.now() / 1000

const slots = [{
  key: SLOT,
  title: 'PRFAQ review',
  running: false,
  last_message: 'Working through the FAQ…',
  messages: 22,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(now),
  source_links: [],
  source_links_total: 0,
}]

/** Long enough that the scroller actually scrolls, so the page boots parked at
 *  the bottom with FOLLOW held — the exact state the defect needs. The last
 *  message ends in a full text line whose glyphs make a slice obvious. */
const LAST_LINE = 'The two products share one engine; what changes is visibility and ownership — exactly the FAQ Q2 position.'
const messages = []
for (let i = 0; i < 10; i++) {
  messages.push({ role: 'user', ts: now - 1200 + i * 100, content: `Question ${i + 1}: how does the pricing story hold up for segment ${i + 1}?`, cls: 'msg msg-user' })
  messages.push({
    role: 'assistant', ts: now - 1150 + i * 100, cls: 'msg msg-assistant',
    content: `For segment ${i + 1}, the plan bundles the engine with the existing subscription so the marginal cost stays flat while the visible surface grows. That is the part the FAQ has to carry.`,
  })
}
messages.push({ role: 'user', ts: now - 80, content: 'Summarize the positioning argument one more time.', cls: 'msg msg-user' })
messages.push({ role: 'assistant', ts: now - 20, content: `Same engine, two object shapes, sold as two coexisting products. ${LAST_LINE}`, cls: 'msg msg-assistant' })

const detail = { running: false, has_more: false, total: messages.length, queue: [], project: PROJECT, messages }

const SCENES = [
  { name: 'desktop', theme: 'dark', viewport: { width: 1280, height: 800 } },
  { name: 'desktop', theme: 'light', viewport: { width: 1280, height: 800 } },
  // Where the offset turns into visible slicing: short viewports run the
  // thinnest tail budgets (the #6551 measurements were 1-5px on phones).
  { name: 'phone', theme: 'dark', viewport: { width: 390, height: 700 } },
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  const extra = async (path, route) => {
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  async function runScene(scene) {
    const context = await browser.newContext({ viewport: scene.viewport, deviceScaleFactor: 2, hasTouch: scene.name === 'phone' })
    const page = await context.newPage()
    logPageProblems(page)
    let wsServer = null
    await stubDashboardApi(page, { slots, theme: scene.theme, extra })
    // AFTER the shared stub so this wins: the stub swallows /api/ws, but this
    // harness needs the socket to push the queue frame.
    await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
    await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)

    const measure = async () => {
      const last = await page.getByText(LAST_LINE, { exact: false }).last().boundingBox().catch(() => null)
      const band = await page.getByTestId('composer-status-stack').boundingBox().catch(() => null)
      if (!last || !band) return null
      return band.y - (last.y + last.height)
    }

    const shoot = async (name) => {
      const band = await page.getByTestId('composer-status-stack').boundingBox()
      const top = Math.max(0, Math.min(scene.viewport.height - 420, band.y - 260))
      await page.screenshot({ path: `${OUT}/${name}.png`, clip: { x: 0, y: top, width: scene.viewport.width, height: Math.min(scene.viewport.height - top, 420) } })
    }

    const clear0 = await measure()
    if (wsServer == null || clear0 == null) {
      await context.close()
      return { failure: `${scene.name}-${scene.theme}: scene did not render (ws=${!!wsServer}, clearance=${clear0})` }
    }
    wsServer.send(JSON.stringify({
      type: 'queue_push',
      data: {
        slot: SLOT,
        content: '[Document feedback on /home/user/workspace/notes/PRFAQ-crew-members.md — 2 comments] 1. ("two object shapes"): "tighten this"',
        ts: new Date().toISOString(),
        queue_id: 'q-reanchor-1',
      },
    }))
    // Sample across the band's spring animation (~400ms) plus slack. The
    // metric is the DEEPEST dip: the pre-fix race can recover afterwards, and
    // a single settled-state reading would hide exactly the frames the user
    // sees sliced.
    const tag = EXPECT === 'offset' ? '00-BEFORE-offset' : '01-AFTER-anchored'
    const name = `${tag}-${scene.name}-${scene.theme}`
    let maxDip = 0
    let shotAtDip = false
    const deadline = Date.now() + 1400
    while (Date.now() < deadline) {
      const c = await measure()
      if (c != null) {
        const dip = clear0 - c
        if (dip > maxDip) maxDip = dip
        // Photograph the BEFORE at its worst frame, once it is past half a card.
        if (EXPECT === 'offset' && !shotAtDip && dip >= BAND_PX - 12) {
          await shoot(name)
          shotAtDip = true
        }
      }
      await page.waitForTimeout(40)
    }
    if (EXPECT !== 'offset' || !shotAtDip) await shoot(name)
    const settled = await measure()
    console.log(`${name}: clearance ${clear0.toFixed(1)} -> ${settled?.toFixed(1)} settled, deepest dip ${maxDip.toFixed(1)}px`)

    let failure = null
    if (EXPECT === 'anchored' && maxDip > 5) {
      failure = `${scene.name}-${scene.theme}: expected the anchor held through the animation (dip <= 5px), got ${maxDip.toFixed(1)}px`
    }
    if (EXPECT === 'offset' && maxDip < BAND_PX - 12) {
      failure = `${scene.name}-${scene.theme}: expected a pre-fix dip past ${BAND_PX - 12}px, got ${maxDip.toFixed(1)}px — re-render race landed lucky this run`
    }
    await context.close()
    return { failure }
  }

  const failures = []
  for (const scene of SCENES) {
    // The pre-fix behaviour is a RACE: a stray re-render can re-pin the bottom
    // before the dip is observable, so any single run can land lucky. For the
    // BEFORE expectation, give the race up to three runs to show itself; the
    // AFTER expectation stays single-run strict — the fix must hold every time.
    const attempts = EXPECT === 'offset' ? 3 : 1
    let lastResult = null
    for (let attempt = 0; attempt < attempts; attempt++) {
      lastResult = await runScene(scene)
      if (lastResult.failure == null) break
    }
    if (lastResult?.failure) failures.push(lastResult.failure)
  }


  await browser.close()
  srv.close()
  if (failures.length) { console.error('FAIL\n' + failures.join('\n')); process.exit(1) }
  console.log('OK')
}

main().catch(e => { console.error(e); process.exit(1) })
