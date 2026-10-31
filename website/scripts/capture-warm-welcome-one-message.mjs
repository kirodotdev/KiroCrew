/** Real-browser evidence for the one-message warm welcome.
 *
 * While the gateway prepares the catch-up there is no opener row any more:
 * the working indicator says "Preparing your catch-up…" instead, in both pane
 * kinds, and the finished brief is the thread's only new row. Frames:
 *
 *   01-crewmate-preparing-<theme>.png  the crewmate typing bubble's caption
 *   02-pane-preparing-<theme>.png      an ordinary pane's footer caption
 *   03-card-<theme>-<width>.png        a no-goal brief at the new caps (four
 *                                      asks, expanded, a long body, no orb
 *                                      since there is no count), the only new row
 *
 * Every frame asserts its state before it is written. Mounts the capture
 * harnesses (members-page.html, chatpane-loader.html) with route-intercepted,
 * gateway-free API answers; serves the dev build in-process on loopback.
 *
 * Usage (in website/):
 *   node scripts/capture-warm-welcome-one-message.mjs ../temp-screenshots/warm-welcome-one-message
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { THREAD, routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/warm-welcome-one-message')
mkdirSync(OUT, { recursive: true })
const CAPTION = 'Preparing your catch-up…'

const BRIEF = {
  status: 'needs_you',
  headline: 'Goal: land the nightly fix. Four asks wait on you.',
  needs_you: [
    'Approve the push to the fix branch so CI can run',
    'Pick the rollout: today or after the weekend',
    'Reply to Bolin on the shared helper change',
    'Re-arm the patrol loop before it stops tonight',
  ],
  body: [
    'Since you left (as of 09:30):',
    '- worker report 09:12: the fix PR is open, two checks still run',
    '- work ledger 09:20: the flaky shard item closed as fixed',
    '',
    '1. Approve the push. Only you can approve it. If you approve, CI runs now; if not, the fix waits.',
    '   I recommend approving: the diff is one helper and its test.',
    '2. Rollout. Today ships the fix sooner; after the weekend keeps one more nightly as proof.',
    '   I recommend today: the shard was red four nights in a row.',
    '',
    'Next without you: rerun the shard once CI is green.',
  ].join('\n'),
}
const RESULT = {
  role: 'assistant', cls: 'msg msg-a', ts: '2026-10-05T09:30:09Z',
  content: `Waiting on you: ${BRIEF.headline}`,
  meta: { mid: 'w2', kind: 'warm_task_brief_result', fingerprint: 'fp', brief: BRIEF },
}

const server = await createServer({ configFile: './vite.config.ts', server: { host: '127.0.0.1', port: 5199, strictPort: false }, logLevel: 'warn' })
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
const state = { failures: 0 }
const check = makeCheck(state)
const fire = (page, detail) => page.evaluate((d) => window.dispatchEvent(new CustomEvent('capture:frame', { detail: d })), detail)

for (const theme of ['dark', 'light']) {
  // 01 -- the crewmate thread while the catch-up is prepared.
  {
    const ctx = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    await routeRadarDm(page, { thread: [THREAD[3], THREAD[4]] })
    await openRadarDm(page, BASE, theme)
    await fire(page, { kind: 'preparing', slot: 'member-radar' })
    const line = page.getByTestId('crewmate-live-activity')
    await line.waitFor()
    await page.waitForTimeout(900)
    check(`[01/${theme}] caption reads "${CAPTION}"`, (await line.innerText()).includes(CAPTION))
    check(`[01/${theme}] no opener row`, (await page.getByText('Welcome back, let me catch you up').count()) === 0)
    await page.screenshot({ path: resolve(OUT, `01-crewmate-preparing-${theme}.png`) })
    await ctx.close()
  }
  // 02 -- an ordinary pane (a split pane, a Crew Mode root) while preparing.
  {
    const ctx = await browser.newContext({ viewport: { width: 860, height: 640 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
      const path = new URL(route.request().url()).pathname
      const isList = /commands|skills|agents|sessions|files|history|models|artifacts|folders/.test(path)
      return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
    })
    await page.goto(`${BASE}/capture/chatpane-loader.html?theme=${theme}&state=preparing`)
    await page.waitForSelector('[data-capture-root]')
    const caption = page.getByTestId('chat-pane-preparing-catch-up')
    await caption.waitFor()
    await page.waitForTimeout(600)
    check(`[02/${theme}] footer caption reads "${CAPTION}"`, (await caption.innerText()).includes(CAPTION))
    check(`[02/${theme}] working footer shown`, (await page.getByTestId('chat-footer').count()) === 1)
    await page.screenshot({ path: resolve(OUT, `02-pane-preparing-${theme}.png`) })
    await ctx.close()
  }
  // 03 -- the finished brief at the new caps, the only new row.
  for (const width of [1280, 320]) {
    const ctx = await browser.newContext({ viewport: { width, height: 1100 }, deviceScaleFactor: 2 })
    const page = await ctx.newPage()
    const errors = []; page.on('pageerror', e => errors.push(String(e)))
    await routeRadarDm(page, { thread: [THREAD[3], THREAD[4], RESULT] })
    await openRadarDm(page, BASE, theme)
    const card = page.getByTestId('warm-brief-card')
    await card.waitFor()
    check(`[03/${theme}/${width}] one card`, (await card.count()) === 1)
    // The orb shows three asks and folds the rest behind one toggle.
    const asks = card.getByTestId('warm-brief-waiting-card')
    check(`[03/${theme}/${width}] three asks shown, one folded`, (await asks.count()) === 3)
    await card.getByTestId('warm-brief-more').click()
    check(`[03/${theme}/${width}] four asks after expanding`, (await asks.count()) === 4)
    check(`[03/${theme}/${width}] body kept whole`, (await card.getByTestId('warm-brief-body').innerText()).includes('Next without you'))
    check(`[03/${theme}/${width}] no count: no orb is drawn`, (await card.locator('.wb-orb').count()) === 0)
    check(`[03/${theme}/${width}] no count caption`, (await card.getByTestId('warm-brief-progress-count').count()) === 0)
    check(`[03/${theme}/${width}] status tag kept`, (await card.getByTestId('warm-brief-status').count()) === 1)
    check(`[03/${theme}/${width}] no opener row`, (await page.getByText('Welcome back, let me catch you up').count()) === 0)
    const box = await card.boundingBox()
    check(`[03/${theme}/${width}] card fits the viewport`, box.x >= 0 && box.x + box.width <= width)
    check(`[03/${theme}/${width}] no page errors`, errors.length === 0)
    const clip = { x: Math.max(0, box.x - 16), y: Math.max(0, box.y - 16), width: Math.min(width - Math.max(0, box.x - 16), box.width + 32), height: box.height + 32 }
    await page.screenshot({ path: resolve(OUT, `03-card-${theme}-${width}.png`), clip })
    await ctx.close()
  }
}
await browser.close()
await server.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
