/**
 * Screenshots of the expanded reasoning panel following its own tail.
 *
 * Drives the isolated capture entry (website/capture/thinking-tail-follow.html)
 * and is SELF-CHECKING: every frame is taken only after the panel's measured
 * scroll position matches what that scene claims, so the run cannot quietly
 * emit a screenshot of the state it is meant to prove. A mismatch prints the
 * measured numbers and exits non-zero.
 *
 * The `build` argument is the frame's own label AND its expectation, which is
 * what makes the pair evidence rather than decoration:
 *
 *   after   on-open  -> settled trace is at the TOP  (prose reads top-down)
 *   after   tailing  -> panel is at the END   after new paragraphs land
 *   before  tailing  -> panel is at the TOP   after new paragraphs land
 *   after   released -> panel is parked MID-trace after the reader scrolls up
 *
 * `released` and `on-open` are captured for the patched build only, and
 * deliberately so: the pre-change panel never scrolls at all, so it has no
 * following to release, and for a settled trace it lands at the top exactly as
 * the patched build now does -- no delta to show. `tailing` is the
 * discriminating pair, and it is the live case the contract is for.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6813 --strictPort   # in another shell
 *   node scripts/capture-thinking-tail-follow.mjs http://127.0.0.1:6813 ../temp-screenshots/thinking-tail-follow after
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6813'
const OUT = process.argv[3] || '../temp-screenshots/thinking-tail-follow'
const BUILD = process.argv[4] || 'after'
mkdirSync(OUT, { recursive: true })

const PARAS = 10
/** The `released` scene needs a longer trace than the others: enough already on
 *  screen that the reader's scroll-up has room to land, and enough still to come
 *  that the frame proves LATER growth did not undo it. */
const RELEASED_PARAS = 16
const RELEASED_WHEEL_AT = 11
/** Same slack the component uses for "is this at the end". */
const SLACK = 20

/** Read the panel's scroll geometry. Selected by CLASS, not by a test id, so the
 *  identical probe works against the pre-change build (which has no test id).
 *  `vis` is the height actually ON SCREEN -- the animating AnimatePresence
 *  wrapper clips the container with overflow:hidden while it opens, so `ch`
 *  alone reads a settled 360 alone even on a frame that shows a third of it. */
const metrics = (page) => page.evaluate(() => {
  const el = [...document.querySelectorAll('[data-capture-root] div')]
    .find(d => typeof d.className === 'string' && d.className.includes('overflow-auto'))
  if (!el) return null
  const clip = el.closest('[style*="height"]') || el.parentElement
  return {
    top: el.scrollTop,
    sh: el.scrollHeight,
    ch: el.clientHeight,
    vis: Math.round(Math.min(el.getBoundingClientRect().height, clip ? clip.getBoundingClientRect().height : 1e9)),
  }
})

const atEnd = (m) => m.top + m.ch >= m.sh - SLACK
const atTop = (m) => m.top <= 2
const overflowing = (m) => m.sh > m.ch + SLACK
/** The open animation has finished revealing the container. Without this the
 *  frame is taken mid-spring and shows the TOP of the scrolled window rather
 *  than the window itself -- the metrics all look right while the screenshot
 *  proves the opposite of what the scene claims. */
const revealed = (m) => m.vis >= m.ch - 5

/** Expand the panel the way a reader does, and wait until it actually overflows
 *  -- a panel that fits has no scroll position to prove anything about -- and
 *  until the open animation has fully revealed it. */
async function openPanel(page) {
  await page.waitForSelector('button[aria-expanded]')
  await page.click('button[aria-expanded]')
  await page.waitForFunction(() => {
    const el = [...document.querySelectorAll('[data-capture-root] div')]
      .find(d => typeof d.className === 'string' && d.className.includes('overflow-auto'))
    if (!el || el.scrollHeight <= el.clientHeight + 20) return false
    const clip = el.closest('[style*="height"]') || el.parentElement
    const vis = Math.min(el.getBoundingClientRect().height, clip ? clip.getBoundingClientRect().height : 1e9)
    return vis >= el.clientHeight - 5
  }, null, { timeout: 8000 })
}

/** Wait for a given paragraph of the streamed trace to arrive, so "after new
 *  paragraphs land" is a fact about the frame rather than a hope about timing. */
async function awaitParagraph(page, i, total) {
  await page.waitForFunction(
    ([n, t]) => (document.body.textContent || '').includes(`[${n}/${t}]`),
    [i, total],
    { timeout: 20000 },
  )
}

const awaitFullTrace = (page) => awaitParagraph(page, PARAS, PARAS)

const SCENES = {
  'on-open': {
    // A settled trace, so this scene now pins the TOP-DOWN contract: prose that
    // has finished arriving opens at its first paragraph, not its last.
    builds: ['after'],
    query: `paras=${PARAS}`,
    async drive(page) { await openPanel(page) },
    expect: { after: 'top' },
  },
  tailing: {
    builds: ['after', 'before'],
    query: `paras=${PARAS}&stream=1`,
    async drive(page) { await openPanel(page); await awaitFullTrace(page) },
    expect: { after: 'end', before: 'top' },
  },
  released: {
    builds: ['after'],
    // A longer trace so the reader's scroll-up has ample range to land in AND
    // there is still growth left afterwards to resist -- the whole claim is
    // that LATER growth does not yank the reader back.
    query: `paras=${RELEASED_PARAS}&stream=1`,
    async drive(page) {
      await openPanel(page)
      await awaitParagraph(page, RELEASED_WHEEL_AT, RELEASED_PARAS)
      // The reader's scroll-up. Assigning scrollTop fires a native `scroll`
      // event, so this exercises the component's own scroll listener -- the
      // thing under test -- and only the input device differs from a real wheel.
      //
      // A synthesized `mouse.wheel` is NOT usable here: measured against this
      // panel in headless Chromium it does not land at all while the panel is
      // being programmatically scrolled by the follow effect (0 of 4 attempts
      // produced any scroll, and a rAF-sampled timeline showed no dip at any
      // frame). It lands reliably once the trace has stopped growing, so the
      // loss is in input synthesis, not in the component: driven this way the
      // release holds across 300px of subsequent growth.
      await page.evaluate(() => {
        const el = [...document.querySelectorAll('[data-capture-root] div')]
          .find(d => typeof d.className === 'string' && d.className.includes('overflow-auto'))
        if (el) el.scrollTop = 120
      })
      // Assert the SETUP, not just the outcome: a scroll that fails to land
      // leaves the panel at the end, and the scene would then "prove" the
      // release while never having released anything.
      await page.waitForFunction(() => {
        const el = [...document.querySelectorAll('[data-capture-root] div')]
          .find(d => typeof d.className === 'string' && d.className.includes('overflow-auto'))
        return !!el && el.scrollTop + el.clientHeight < el.scrollHeight - 100
      }, null, { timeout: 5000 })
      await awaitParagraph(page, RELEASED_PARAS, RELEASED_PARAS)
    },
    expect: { after: 'mid' },
  },
}

const CHECKS = {
  end: { ok: (m) => atEnd(m), why: 'scrolled to the END of the trace' },
  top: { ok: (m) => atTop(m), why: 'parked at the TOP of the trace' },
  mid: { ok: (m) => !atEnd(m) && !atTop(m), why: 'parked MID-trace (reader position kept)' },
}

const run = async () => {
  // node exports its OWN lib dirs as LD_LIBRARY_PATH to every child it spawns,
  // and a version-manager-installed node (mise, nvm) ships a libstdc++ older
  // than the system Mesa/LLVM that chromium links against -- so the browser dies
  // at launch on `GLIBCXX_3.4.29 not found`. Dropping the variable for the
  // browser process alone lets it resolve /lib64 normally. It cannot be fixed
  // from the parent environment: node re-sets the variable for children, so
  // unsetting or prepending before `node` has no effect.
  const env = { ...process.env }
  delete env.LD_LIBRARY_PATH
  const browser = await chromium.launch({ env })
  let failed = 0
  for (const [scene, spec] of Object.entries(SCENES)) {
    if (!spec.builds.includes(BUILD)) continue
    const want = spec.expect[BUILD]
    for (const theme of ['dark', 'light']) {
      const ctx = await browser.newContext({ viewport: { width: 900, height: 470 }, deviceScaleFactor: 2 })
      const page = await ctx.newPage()
      await page.goto(`${BASE}/capture/thinking-tail-follow.html?theme=${theme}&${spec.query}`)
      let ok = false
      let m = null
      try {
        await spec.drive(page)
        m = await metrics(page)
        if (!m) throw new Error('no overflow-auto panel found')
        if (!overflowing(m)) throw new Error(`panel does not overflow (sh=${m.sh} ch=${m.ch})`)
        if (!revealed(m)) throw new Error(`open animation not settled (vis=${m.vis} ch=${m.ch}) -- frame would show only the top of the window`)
        ok = CHECKS[want].ok(m)
        if (!ok) throw new Error(`expected ${CHECKS[want].why}; measured top=${m.top} sh=${m.sh} ch=${m.ch}`)
      } catch (e) {
        failed += 1
        console.error(`FAIL ${BUILD}/${scene}/${theme}: ${e.message}`)
      }
      const name = `${scene}-${BUILD}-${theme}.png`
      await page.screenshot({ path: `${OUT}/${name}` })
      const measured = m ? `top=${m.top} sh=${m.sh} ch=${m.ch} vis=${m.vis}` : 'unmeasured'
      console.log(`${ok ? 'ok  ' : 'FAIL'} ${name} -- expect ${CHECKS[want].why} (${measured})`)
      await ctx.close()
    }
  }
  await browser.close()
  if (failed) process.exit(1)
}

run().catch((e) => { console.error(e); process.exit(1) })
