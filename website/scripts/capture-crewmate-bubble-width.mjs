/** Real-browser evidence for #30200 on the REAL Members page: a crewmate's
 * bubbles follow Settings → Chat → Content Width the way the user's bubble
 * does. Every reply (prose, a wide table, a code block, a widget-only reply)
 * and the crewmate's change card reach the content column, the typing bubble
 * stays three dots wide. The reply thread has no Content Width clamp, so it
 * keeps a 72ch reading measure: a short crewmate reply hugs its text and a
 * long one stops at 72ch, before and after.
 *
 * Mounts through capture/members-page.html with ?route=/members?member=Radar;
 * the API is route-intercepted, gateway-free. Serves the dev build itself
 * (vite in-process, loopback, ephemeral port). Each frame runs at a wide
 * viewport under two Content Width values (`compact`, `full`), stored the way
 * Settings stores them (`mc-chat-config`).
 *
 * Frames per theme (dark, light) and width (compact, full):
 *   bubble-width-<theme>-<width>-<tag>.png   the transcript
 *   thread-<theme>-<tag>.png                 the reply thread panel
 *
 * Usage (in website/):
 *   node scripts/capture-crewmate-bubble-width.mjs ../temp-screenshots/crewmate-bubble-width after
 *   node scripts/capture-crewmate-bubble-width.mjs ../temp-screenshots/crewmate-bubble-width before --expect-cap
 * `--expect-cap` runs the transcript's width checks the OTHER way round, for a
 * "before" frame taken on a tree whose crewmate bubbles still stop at a fixed
 * measure. The thread's checks are the same both ways.
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { routeRadarDm, openRadarDm, makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/crewmate-bubble-width')
const TAG = process.argv[3] || 'after'
const EXPECT_CAP = process.argv.includes('--expect-cap')
mkdirSync(OUT, { recursive: true })
const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ env: browserEnv })
const state = { failures: 0 }
const check = makeCheck(state)

const now = Date.now()
const LONG = 'Shard 1 went red twice on the nightly. The test takes the crew-log lock on the event loop, so a slow disk stalls every other coroutine behind it, and the watchdog reads the stall as a hang. Builds are all green on every platform, so it is the test and not the code. I filed it with the fix sketched and will pick it up once triage agrees.'
const TABLE = ['| Shard | Status | Duration | Flaky test | Owner | Last green | Notes |', '| --- | --- | --- | --- | --- | --- | --- |',
  ...[1, 2, 3].map(n => `| ${n} | ${n === 1 ? 'Red' : 'Green'} | ${12 + n}m 04s | \`test_spawn_approval_crew_log_${n}\` | Radar | 2026-10-0${n}T07:40Z | Lock taken on the event loop, retried twice |`)].join('\n')
const CODE = '```python\nawait asyncio.to_thread(crew_log.acquire, timeout=CREW_LOG_LOCK_TIMEOUT_SECONDS)  # take the lock off the event loop so a slow disk cannot stall the watchdog\n```'
const WIDGET = '<mcwidget title="Shard durations">\n<div style="padding:8px;font:14px sans-serif">Shard durations chart</div>\n</mcwidget>'
const row = (role, content, mid, sec, meta = {}) => ({ role, content, ts: `2026-10-05T07:${String(40 + sec).padStart(2, '0')}:00Z`, meta: { mid, ...meta } })
const CARD = {
  id: 'card-bw-1', slot_key: 'member-radar', kind: 'setting.change', revision: 1, status: 'pending', risk: 'normal',
  title: 'Shorter replies', reason: 'You asked me to keep my answers short.', editable: [], params: { path: 'chat.verbosity', value: 'brief' },
  changes: [{ label: 'Reply length', before: 'standard', after: 'brief' }],
  plan: { apply: [{ method: 'PATCH', path: '/api/config/kirocrew', body: { path: 'chat.verbosity', value: 'brief' } }], undo: null },
  created_at: now - 60_000, expires_at: now + 3600_000,
}
const THREAD = [
  row('user', `Anything new on the nightly? ${LONG}`, 'u1', 0),
  row('assistant', 'Yes, I can do that.', 'a-short', 1),
  row('assistant', LONG, 'a-long', 2),
  row('assistant', TABLE, 'a-table', 3),
  row('assistant', CODE, 'a-code', 4),
  row('assistant', WIDGET, 'a-widget', 5),
  row('user', 'Keep them short from now on.', 'u2', 6),
  row('card', '', 'c1', 7, { card: { surface: 'change', id: CARD.id, slot: 'member-radar', kind: CARD.kind, title: CARD.title, status: 'pending' } }),
  row('assistant', 'Good. And the dispatcher?\n\n[OPTIONS: Check the dispatcher now | Leave it for tomorrow]', 'a-opts', 8),
]
const DETAIL = {
  parent: { mid: 'a-long', role: 'assistant', content: LONG, ts: THREAD[2].ts },
  replies: [
    { id: 'r1', role: 'user', content: 'Which lock?', ts: '2026-10-05T07:50:00Z' },
    { id: 'r2', role: 'assistant', content: 'Ok.', ts: '2026-10-05T07:50:10Z' },
    { id: 'r3', role: 'assistant', content: LONG, ts: '2026-10-05T07:50:12Z' },
  ],
  in_flight: false,
}
const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

async function open(theme, width) {
  const ctx = await browser.newContext({ viewport: { width: 2560, height: 2000 }, deviceScaleFactor: 1 })
  // Top frame only: the widget's sandboxed frame has no storage to write.
  await ctx.addInitScript(w => { if (window === window.top) localStorage.setItem('mc-chat-config', JSON.stringify({ contentWidth: w })) }, width)
  const page = await ctx.newPage()
  const errors = []; page.on('pageerror', e => errors.push(String(e)))
  await routeRadarDm(page, { thread: THREAD })
  // Registered after the fixture's catch-all, so these answer first.
  await page.route(u => new URL(u).pathname === '/__bw-widget.html', route => route.fulfill({ status: 200, contentType: 'text/html', body: '<!doctype html><body style="margin:0;font:14px sans-serif">Shard durations chart</body>' }))
  await page.route(u => /^\/api\/(cards|chat\/threads|sandbox-doc|config\/kirocrew$)/.test(new URL(u).pathname), route => {
    const path = new URL(route.request().url()).pathname
    // Reply threads ship behind `dashboard.crewmate_threads`.
    if (path === '/api/config/kirocrew') return route.fulfill(json({ dashboard: { crewmate_threads: true } }))
    if (path === '/api/cards/pending') return route.fulfill(json({ cards: [CARD] }))
    if (path === '/api/chat/threads') return route.fulfill(json({ threads: { 'a-long': { count: 3, last_reply_ts: DETAIL.replies[2].ts, participants: ['user', 'assistant'] } } }))
    if (path === '/api/chat/threads/a-long') return route.fulfill(json(DETAIL))
    if (path === '/api/sandbox-doc') return route.fulfill(json({ url: `${BASE}/__bw-widget.html` }))
    return route.fulfill(json({}))
  })
  await openRadarDm(page, BASE, theme).catch(() => {})
  await page.getByText('Keep them short from now on.').waitFor()
  await page.getByText('Good. And the dispatcher?').waitFor()
  // The capture page seeds Radar's slot as running, so the typing bubble is up.
  await page.locator('[data-testid="crewmate-typing-bubble"][data-shown="true"]').waitFor()
  await page.locator('[data-testid="change-card"]').first().waitFor().catch(() => {})
  return { ctx, page, errors }
}

/** Widths of every surface, plus the content column they sit in. */
const measure = (page) => page.evaluate(() => {
  const w = el => el ? el.getBoundingClientRect().width : -1
  const rowOf = text => [...document.querySelectorAll('[data-testid="crewmate-message"]')].find(e => e.textContent.includes(text))
  const bubbleOf = text => rowOf(text)?.querySelector('.message-bubble')
  const anyMate = document.querySelector('[data-testid="crewmate-message"]')
  let col = anyMate
  while (col && !(col.style && /mc-content-width/.test(col.style.maxWidth))) col = col.parentElement
  const cs = col ? getComputedStyle(col) : null
  const colW = col ? col.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) : -1
  const user = [...document.querySelectorAll('.user-bubble')].find(e => e.textContent.includes('Anything new'))
  const card = rowOf('Shorter replies')
  const typing = document.querySelector('[data-testid="crewmate-typing-bubble"] .message-bubble')
  const pre = bubbleOf('asyncio.to_thread')?.querySelector('pre')
  const longB = bubbleOf('Shard 1 went red')
  return {
    col: colW, user: w(user), short: w(bubbleOf('Yes, I can do that.')), long: w(longB),
    table: w(bubbleOf('Last green')), code: w(bubbleOf('asyncio.to_thread')), widget: w(bubbleOf('Shard durations')),
    opts: w(bubbleOf('Good. And the dispatcher?')), card: w(card?.firstElementChild), typing: w(typing),
    codeScrolls: !!pre && pre.scrollWidth > pre.clientWidth,
    chips: [...document.querySelectorAll('button')].filter(b => /^(Check the dispatcher now|Leave it for tomorrow)$/.test(b.textContent.trim())).length,
    pane: w(document.querySelector('[data-testid="member-thread-pane"]')),
    longRight: longB ? longB.getBoundingClientRect().right : -1, colRight: col ? col.getBoundingClientRect().right - parseFloat(cs.paddingRight) : -1,
  }
})

const near = (a, b) => Math.abs(a - b) <= 1.5
for (const theme of ['dark', 'light']) {
  for (const width of ['compact', 'full']) {
    const { ctx, page, errors } = await open(theme, width)
    const m = await measure(page)
    const L = `[${theme}/${width}/${TAG}]`
    console.log(`${L} widths ${JSON.stringify(m)}`)
    check(`${L} the user's long bubble reaches the content column`, near(m.user, m.col))
    if (EXPECT_CAP) {
      check(`${L} the crewmate's long reply stops short of the column (fixed measure)`, m.long < m.col - 40)
      check(`${L} the crewmate's short reply is as wide as its long one (fixed measure)`, near(m.short, m.long))
    } else {
      check(`${L} the crewmate's long reply reaches the content column, like the user's`, near(m.long, m.col) && m.longRight <= m.colRight + 1)
      check(`${L} a short reply spans the same column`, near(m.short, m.col))
      check(`${L} a wide table reply reaches the column and no further`, near(m.table, m.col))
      check(`${L} a long code block reaches the column and scrolls inside it`, near(m.code, m.col) && m.codeScrolls)
      check(`${L} a widget-only reply reaches the column`, near(m.widget, m.col))
      check(`${L} the change card reaches the column`, near(m.card, m.col))
    }
    check(`${L} the option chips are drawn`, m.chips === 2)
    check(`${L} the typing bubble stays three dots wide (${m.typing}px)`, m.typing > 0 && m.typing < 120)
    check(`${L} no page errors`, errors.length === 0)
    if (errors.length) console.log(errors.join('\n'))
    await page.locator('[data-testid="member-thread-pane"]').screenshot({ path: resolve(OUT, `bubble-width-${theme}-${width}-${TAG}.png`) })
    await ctx.close()
  }
  // Reply thread: the crewmate's bubbles in the side panel.
  {
    const { ctx, page, errors } = await open(theme, 'compact')
    await page.locator('[data-testid="thread-footer"]').first().click()
    await page.locator('[data-testid="thread-reply"][data-reply-from="assistant"]').nth(1).waitFor()
    const t = await page.evaluate(() => {
      // 72ch at the bubble's own font: the measure the thread keeps.
      const ch72 = el => { const p = document.createElement('span'); p.style.cssText = 'position:absolute;visibility:hidden;width:72ch'; el.appendChild(p); const v = p.getBoundingClientRect().width; p.remove(); return v }
      const of = (bubble, row) => ({ bubble: bubble.getBoundingClientRect().width, measure: ch72(bubble), row: row.getBoundingClientRect().width })
      const left = [...document.querySelectorAll('[data-testid="thread-reply"][data-reply-from="assistant"]')]
      return { replies: left.map(li => of(li.querySelector('.crewmate-bubble'), li)), parent: of(document.querySelector('[data-testid="thread-parent-bubble"]'), document.querySelector('[data-testid="thread-parent"]')) }
    })
    console.log(`[${theme}/thread/${TAG}] widths ${JSON.stringify(t)}`)
    const [short, long] = t.replies
    check(`[${theme}/thread/${TAG}] a short thread reply hugs its text`, short.bubble < 120)
    check(`[${theme}/thread/${TAG}] a long thread reply keeps the 72ch reading measure, short of the panel`, near(long.bubble, long.measure) && long.bubble < long.row - 40)
    check(`[${theme}/thread/${TAG}] the quoted crewmate message keeps the same 72ch measure`, near(t.parent.bubble, t.parent.measure) && t.parent.bubble < t.parent.row - 40)
    check(`[${theme}/thread/${TAG}] no page errors`, errors.length === 0)
    await page.locator('[data-testid="thread-panel"]').screenshot({ path: resolve(OUT, `thread-${theme}-${TAG}.png`) })
    await ctx.close()
  }
}
await browser.close()
await server.close()
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
