/** Real-browser evidence for #23539 on the REAL Members page: the crewmate DM
 * transcript shows no scrollbar at rest, shows a thumb only while it scrolls,
 * and reserves the same column in both states (the reveal moves nothing).
 *
 * Mounts through capture/members-page.html with ?route=/members?member=Radar and
 * a thread long enough to overflow a 720px frame; the API is route-intercepted,
 * gateway-free. Serves the dev build itself (vite in-process, loopback,
 * ephemeral port). Run it against the branch and against a main checkout: the
 * PNGs are the before/after pair, the probe is the verdict.
 *
 * A screenshot alone is weak evidence for a thumb that is transparent at rest
 * ("I don't see it"), and Playwright's default `--hide-scrollbars` would zero
 * every gutter whatever the stylesheet says, so that flag is turned OFF here and
 * the probe reports what a PNG cannot: on the transcript scroller, the gutter it
 * reserves at rest and again mid-scroll (must be equal and non-zero: the column
 * is always there, only the paint changes), the `data-scrolling` attribute
 * arriving on a driven scroll and leaving after the linger, and that the box
 * still overflows. A second probe reads the same off Pierre's `[data-code]`
 * scroller INSIDE its shadow root, which the light-DOM rule and the document
 * listener cannot reach and `PIERRE_SCROLL_REVEAL_CSS` + the shadow-root
 * listener must. The frames: `<prefix>-rest-*.png` with nothing scrolling and
 * `<prefix>-scrolling-*.png` taken inside the linger, so the thumb is painted.
 *
 * Frames per theme (dark, light), full viewport:
 *   <prefix>-rest-<theme>.png, <prefix>-scrolling-<theme>.png
 * and, for the `after` run, a recording of the transcript and the code block
 * being wheel-scrolled and then left alone, so the thumb can be watched
 * appearing, lingering and leaving without the rows moving:
 *   <prefix>-scroll-<theme>.webm
 *
 * Usage (in website/):
 *   node scripts/capture-23539-hidden-scrollbars.mjs ../temp-screenshots/hidden-scrollbars [prefix]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { THREAD, routeRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/hidden-scrollbars')
const PREFIX = process.argv[3] || 'after'
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5198, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv, ignoreDefaultArgs: ['--hide-scrollbars'] })
const state = { failures: 0 }
const check = makeCheck(state)

// Eight runs of the thread: ~40 bubbles, several screens tall at 720px, so the
// transcript is scrollable by a wide margin whatever the row heights.
const RUNS = 8
const LONG_THREAD = Array.from({ length: RUNS }, (_, run) =>
  THREAD.map((m, i) => ({
    ...m,
    ts: new Date(Date.parse(m.ts) + run * 3600_000).toISOString(),
    meta: { mid: `r${run}-m${i + 1}` },
  })),
).flat()
// One wide fenced block at the tail, so Pierre's code scroller overflows sideways.
LONG_THREAD.push({
  role: 'assistant',
  content: 'The failing assertion, verbatim:\n\n```python\n' + 'assert crew_log.lock_owner() is None, "the crew-log lock must be released on the event loop before the next spawn approval is read"  # ' + 'x'.repeat(160) + '\n```',
  ts: '2026-10-05T16:00:00Z',
  meta: { mid: 'code-wide' },
})

// Not openRadarDm: its waiter is a strict getByText on one bubble, which this
// repeated thread renders several times. Wait on the scroller instead.
async function openLongDm(page, theme) {
  await page.goto(`${BASE}/capture/members-page.html?theme=${theme}&route=/members%3Fmember%3DRadar`)
  await page.waitForSelector('[data-capture-root]')
  await page.locator('.chat-container').first().waitFor()
  await page.getByText('Good. And the dispatcher?').first().waitFor()
}

for (const theme of ['dark', 'light']) {
  const record = PREFIX === 'after'
  const ctx = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    deviceScaleFactor: 2,
    ...(record ? { recordVideo: { dir: resolve(OUT, 'video-raw'), size: { width: 1280, height: 720 } } } : {}),
  })
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await routeRadarDm(page, { thread: LONG_THREAD })
  await openLongDm(page, theme)

  const scroller = page.locator('.chat-container').first()
  await scroller.waitFor()
  const LINGER_MS = 800
  // Mounting scrolls the transcript to its tail (follow-output), which is a
  // scroll like any other; "at rest" begins once that linger has run out.
  await page.waitForFunction(() => !document.querySelector('.chat-container')?.hasAttribute('data-scrolling'), null, { timeout: 5000 })
  const rest = await scroller.evaluate((el) => ({
    gutterPx: el.offsetWidth - el.clientWidth,
    overflows: el.scrollHeight > el.clientHeight,
    scrolling: el.hasAttribute('data-scrolling'),
  }))
  await page.screenshot({ path: resolve(OUT, `${PREFIX}-rest-${theme}.png`) })
  // Drive a scroll, then read inside the linger: the attribute must be on and
  // the reserved column unchanged.
  const mid = await scroller.evaluate(async (el) => {
    const before = el.scrollTop
    el.scrollTop = Math.max(0, el.scrollHeight - el.clientHeight) / 2
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))
    return {
      moved: el.scrollTop !== before,
      scrolling: el.hasAttribute('data-scrolling'),
      gutterPx: el.offsetWidth - el.clientWidth,
    }
  })
  await page.screenshot({ path: resolve(OUT, `${PREFIX}-scrolling-${theme}.png`) })
  await page.waitForFunction((ms) => new Promise((r) => setTimeout(() => r(true), ms)), LINGER_MS + 200)
  const after = await scroller.evaluate((el) => el.hasAttribute('data-scrolling'))
  console.log(`[${theme}] ${PREFIX}:`, JSON.stringify({ rest, mid, clearedAfterLinger: !after }))
  check(`[${theme}] transcript overflows (precondition)`, rest.overflows)
  check(`[${theme}] a driven scroll still moves it`, mid.moved)
  check(`[${theme}] the reserved column is the same at rest and mid-scroll`, rest.gutterPx === mid.gutterPx)
  if (PREFIX === 'after') {
    check(`[${theme}] not marked scrolling at rest`, rest.scrolling === false)
    check(`[${theme}] marked scrolling right after a scroll`, mid.scrolling === true)
    check(`[${theme}] mark cleared after the linger`, !after)
    check(`[${theme}] a column is reserved (the thumb has somewhere to paint without moving content)`, rest.gutterPx > 0)
  }
  check(`[${theme}] no page errors`, errors.length === 0)
  if (errors.length) console.log(errors.join('\n'))

  // The wide block is the last row: scroll the (virtualized) transcript to its
  // tail so the row mounts, then wait for Pierre's lazy chunk to draw it.
  await scroller.evaluate((el) => { el.scrollTop = el.scrollHeight })
  const codeHost = page.locator('.pierre-surface diffs-container').last()
  await codeHost.waitFor({ state: 'attached', timeout: 30000 })
  // The highlighted surface arrives from a worker; the host mounts first.
  await page.waitForFunction(() => {
    const hosts = document.querySelectorAll('.pierre-surface diffs-container')
    const host = hosts[hosts.length - 1]
    return !!host?.shadowRoot?.querySelector('[data-code]')
  }, null, { timeout: 30000 })
  const code = await codeHost.evaluate(async (host) => {
    const el = host.shadowRoot?.querySelector('[data-code]')
    if (!el) return null
    const rest = { scrolling: el.hasAttribute('data-scrolling'), gutterPx: el.offsetHeight - el.clientHeight }
    el.scrollLeft = 40
    await new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))
    return {
      overflowsX: el.scrollWidth > el.clientWidth,
      rest,
      mid: { scrolling: el.hasAttribute('data-scrolling'), gutterPx: el.offsetHeight - el.clientHeight },
    }
  })
  console.log(`[${theme}] ${PREFIX} pierre:`, JSON.stringify(code))
  check(`[${theme}] pierre code scroller found in the shadow root`, code != null)
  check(`[${theme}] pierre code block overflows sideways (precondition)`, !!code?.overflowsX)
  if (PREFIX === 'after' && code) {
    check(`[${theme}] pierre: not marked at rest`, code.rest.scrolling === false)
    check(`[${theme}] pierre: marked inside its shadow root on scroll`, code.mid.scrolling === true)
    check(`[${theme}] pierre: the reserved track is the same at rest and mid-scroll`, code.rest.gutterPx === code.mid.gutterPx)
  }

  if (record) {
    // Wheel the transcript in steps (each step is a real scroll event), rest
    // past the linger, then the same on the wide code block; the recording
    // shows the thumb come and go while nothing else moves.
    const tBox = await scroller.boundingBox()
    await page.mouse.move(tBox.x + tBox.width / 2, tBox.y + tBox.height / 2)
    await scroller.evaluate((el) => { el.scrollTop = 0 })
    await page.waitForTimeout(LINGER_MS + 300)
    for (let i = 0; i < 12; i++) { await page.mouse.wheel(0, 120); await page.waitForTimeout(90) }
    await page.waitForTimeout(LINGER_MS + 500)
    for (let i = 0; i < 8; i++) { await page.mouse.wheel(0, -120); await page.waitForTimeout(90) }
    await page.waitForTimeout(LINGER_MS + 500)
    await scroller.evaluate((el) => { el.scrollTop = el.scrollHeight })
    await page.waitForTimeout(LINGER_MS + 300)
    const cBox = await codeHost.boundingBox()
    if (cBox) {
      await page.mouse.move(cBox.x + cBox.width / 2, cBox.y + cBox.height / 2)
      for (let i = 0; i < 10; i++) { await page.mouse.wheel(60, 0); await page.waitForTimeout(90) }
      await page.waitForTimeout(LINGER_MS + 500)
    }
    const video = page.video()
    await ctx.close()
    if (video) await video.saveAs(resolve(OUT, `${PREFIX}-scroll-${theme}.webm`))
    continue
  }
  await ctx.close()
}
await browser.close()
await server.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
