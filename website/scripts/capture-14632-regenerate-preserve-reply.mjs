/**
 * Screenshots + a recording for #14632: Regenerate keeps the previous reply
 * until the new turn ends.
 *
 * Drives the isolated entry (capture/regenerate-preserve-reply.html, the REAL
 * ChatPage). The script presses the real Regenerate button, then plays the
 * server's side through the store paths the live socket uses. Asserted
 * before each PNG:
 *   inflight-<theme>.png  the old reply is still on screen, marked
 *                         "Previous version", and the new reply streams below
 *                         it under a "New version" label
 *   settled-<theme>.png   at turn end the variant_switch refetch collapses the
 *                         two into one reply carrying a version switcher, the
 *                         previous reply one step back in it
 *   empty-inflight-<theme>.png  pressed, nothing new yet: the old reply is
 *                         marked "Kept if no new reply arrives"
 *   empty-<theme>.png     a turn that ends with no reply keeps the old reply,
 *                         unmarked, as the current answer
 *   regenerate.webm       the whole successful Regenerate, end to end
 *
 * Starts its own loopback vite dev server.
 *
 * Usage (in website/):
 *   node scripts/capture-14632-regenerate-preserve-reply.mjs ../temp-screenshots/14632
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync, readdirSync, renameSync, rmSync } from 'node:fs'

const OUT = process.argv[2] || '../temp-screenshots/14632-regenerate-preserve-reply'
mkdirSync(OUT, { recursive: true })
// A crashed run leaves its recording behind; start from an empty video dir.
rmSync(`${OUT}/.video`, { recursive: true, force: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const USER = { role: 'user', content: 'Summarize what changed in the release branch this week.', cls: '', ts: '2026-10-24T14:00:00Z', meta: { mid: 'm-user-1' } }
const OLD_TEXT = 'Three changes landed this week:\n\n- the retry budget moved to config\n- the health check now times out after 5s\n- two flaky tests were quarantined'
const OLD = { role: 'assistant', content: OLD_TEXT, cls: '', ts: '2026-10-24T14:00:20Z', meta: { mid: 'm-reply-1' } }
const NEW_TEXT = 'This week the release branch picked up three changes. Retries now read their budget from config, the health check gives up after 5 seconds instead of hanging, and two flaky tests are quarantined until their owners fix them.'
const NEW_CHUNKS = NEW_TEXT.match(/.{1,24}(\s|$)/g)
// What the server persists after a successful swap: one reply whose variants
// hold the old answer and the new one, the new one current.
const SWAPPED = [USER, {
  role: 'assistant', content: NEW_TEXT, cls: '', ts: '2026-10-24T14:02:10Z', meta: { mid: 'm-reply-2' },
  variants: [{ content: OLD_TEXT, ts: OLD.ts }, { content: NEW_TEXT, ts: '2026-10-24T14:02:10Z' }], variant_idx: 1,
}]

const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
let failures = 0
const check = (label, ok) => { console.log(`${label} -> ${ok ? 'ok' : 'FAIL'}`); if (!ok) failures++ }

async function open(theme, video) {
  const ctx = await browser.newContext({
    viewport: { width: 1100, height: 720 }, deviceScaleFactor: 1,
    ...(video ? { recordVideo: { dir: `${OUT}/.video`, size: { width: 1100, height: 720 } } } : {}),
  })
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  const state = { transcript: [USER, OLD], regenerateCalls: 0 }
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/regenerate') && route.request().method() === 'POST') {
      state.regenerateCalls++
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{"ok":true}' })
    }
    if (/^\/api\/chat\/slots\/[^/]+$/.test(path)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ key: 'slot-a', title: 'release notes', running: false, has_more: false, total: state.transcript.length, messages: state.transcript }) })
    }
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill({ status: 200, contentType: 'application/json', body: '[]' })
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/regenerate-preserve-reply.html?theme=${theme}`)
  await page.getByText('two flaky tests were quarantined').first().waitFor()
  await page.waitForTimeout(400)
  return { ctx, page, state, errors }
}

const marker = page => page.getByTestId('regenerate-previous-reply')
async function pressRegenerate(page, state) {
  const btn = page.getByRole('button', { name: 'Regenerate response' }).last()
  await page.getByText('two flaky tests were quarantined').first().hover()
  await btn.click()
  await page.waitForTimeout(150)
  check('regenerate POST sent', state.regenerateCalls === 1)
  await page.evaluate(() => window.__capture.startTurn())
}

async function successFlow(theme, { video, pace }) {
  const { ctx, page, state, errors } = await open(theme, video)
  if (pace) await page.waitForTimeout(900)
  await pressRegenerate(page, state)
  let seq = 0
  for (const c of NEW_CHUNKS) {
    await page.evaluate(([t, s]) => window.__capture.chunk(t, s), [c, ++seq])
    await page.waitForTimeout(pace ? 140 : 15)
  }
  await page.getByText('until their owners fix them').first().waitFor()
  check(`${theme} inflight: old reply marked`, (await marker(page).count()) === 1 && await marker(page).getByText('two flaky tests were quarantined').count() === 1 && await marker(page).getByText('until their owners fix them').count() === 0)
  check(`${theme} inflight: label says where the previous reply goes`, await page.getByText('Previous version. New reply started: this one moves to the version arrows. Stopping keeps both.').isVisible())
  check(`${theme} inflight: new reply labelled`, await page.getByTestId('regenerate-new-reply').isVisible())
  if (!video) await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/inflight-${theme}.png` })
  if (pace) await page.waitForTimeout(1500)
  // Turn end: the server swaps and pushes chat_variant_switch, which refetches.
  state.transcript = SWAPPED
  await page.evaluate(() => window.__capture.endTurn())
  await page.evaluate(() => window.__capture.refetch())
  await page.getByRole('button', { name: 'Previous version' }).first().waitFor()
  // The switcher sits in the reply's hover footer: point at the reply so the
  // frame shows where the old answer went.
  await page.getByText('until their owners fix them').first().hover()
  await page.waitForTimeout(300)
  check(`${theme} settled: switcher visible`, await page.getByRole('button', { name: 'Previous version' }).first().isVisible())
  check(`${theme} settled: marker cleared`, (await marker(page).count()) === 0)
  check(`${theme} settled: old reply folded into variants`, (await page.getByText('two flaky tests were quarantined').count()) === 0)
  if (!video) await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/settled-${theme}.png` })
  if (pace) {
    // Hover the switcher so the recording ends on where the old answer went.
    await page.getByRole('button', { name: 'Previous version' }).first().hover()
    await page.waitForTimeout(2000)
  }
  if (errors.length) { console.error(errors.join('\n')); failures++ }
  await ctx.close()
}

async function emptyFlow(theme) {
  const { ctx, page, state, errors } = await open(theme, false)
  await pressRegenerate(page, state)
  await page.waitForTimeout(300)
  check(`${theme} empty inflight: old reply marked`, (await marker(page).count()) === 1)
  check(`${theme} empty inflight: label says it is kept`, await page.getByText('Previous version. Kept if no new reply arrives. Stopping now keeps it.').isVisible())
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/empty-inflight-${theme}.png` })
  // The turn ends with no reply: nothing is swapped, the transcript is unchanged.
  await page.evaluate(() => window.__capture.endTurn())
  await page.evaluate(() => window.__capture.refetch())
  await page.waitForTimeout(400)
  check(`${theme} empty: marker cleared`, (await marker(page).count()) === 0)
  check(`${theme} empty: old reply kept`, (await page.getByText('two flaky tests were quarantined').count()) === 1)
  check(`${theme} empty: no version switcher`, (await page.getByRole('button', { name: 'Previous version' }).count()) === 0)
  await page.locator('[data-capture-root]').screenshot({ path: `${OUT}/empty-${theme}.png` })
  if (errors.length) { console.error(errors.join('\n')); failures++ }
  await ctx.close()
}

for (const theme of ['dark', 'light']) {
  await successFlow(theme, { video: false, pace: false })
  await emptyFlow(theme)
}
await successFlow('dark', { video: true, pace: true })

const vids = readdirSync(`${OUT}/.video`).filter(f => f.endsWith('.webm'))
if (vids.length === 1) renameSync(`${OUT}/.video/${vids[0]}`, `${OUT}/regenerate.webm`)
else { console.error(`expected one recording, got ${vids.length}`); failures++ }
rmSync(`${OUT}/.video`, { recursive: true, force: true })

await browser.close()
await server.close()
if (failures) { console.error(`\n${failures} assertion(s) failed.`); process.exit(1) }
console.log('\nall states render as specified.')
