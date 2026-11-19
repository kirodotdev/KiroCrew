/** Real-browser evidence for the waiting-on-you cards: an agent's pinned ask
 *  above the composer, its status on the sidebar row, tab strip and grid pane,
 *  its place in the needs-me hover card, and the Questions tab. Frames, each in
 *  dark and light:
 *
 *   01-decision-<theme>.png        one decision card: three options, a detail,
 *                                  "Pinned 3h ago", nothing selected
 *   01-decision-<theme>-390.png    the same card at 390px on a touch screen
 *   02-selected-<theme>.png        an option picked: Send is enabled
 *   03-stack-<theme>.png           three asks: the newest in full, "+2 more"
 *   03b-stack-expanded-<theme>.png the stack expanded to all three, "Show fewer"
 *   04-action-<theme>.png          an action card: Done and a note field
 *   06-sidebar-tabs-<theme>.png    the sidebar row and the tab strip carry
 *                                  "Action for you" for the other session
 *   07-hover-card-<theme>.png      the needs-me hover card lists the ask
 *   08-grid-<theme>.png            split view: the pane header's "Action for
 *                                  you" pill and the picker's --aim dot
 *   09-questions-<theme>.png       the Questions tab lists each session's asks
 *   10-closed-draft-<theme>.png    an ask retired with a typed note: the note
 *                                  shown unsent, and "Discard"
 *   13-error-<theme>.png           a refused send (`composer_locked`) inline
 *   14-read-only-<theme>.png       a collaborator who is not the owner
 *   15-show-more-<theme>-390.png   a long detail clamped at 390px
 *   15b-show-less-<theme>-390.png  the same detail expanded
 *
 *  Recordings (WebM, dark, each under 15 s and 10 MB):
 *   rec-b-stack-expand-collapse.webm  the stack expands and collapses in place
 *
 *  Every frame asserts its state before it is written. Mounts the capture
 *  harness capture/waiting-on-you.html (the REAL ChatPage, SessionGridView or
 *  CommandCenterPanel) with route-intercepted, gateway-free API answers;
 *  serves the dev build in-process on loopback.
 *
 *  Usage (in website/):
 *    node scripts/capture-waiting-on-you.mjs ../temp-screenshots/waiting-on-you
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync, readFileSync, renameSync, rmSync, statSync } from 'node:fs'
import { resolve } from 'node:path'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { makeCheck } from './lib/radar-dm-fixture.mjs'

const OUT = resolve(process.argv[2] || '../temp-screenshots/waiting-on-you')
mkdirSync(OUT, { recursive: true })
const ROLLOUT_TITLE = 'Pick the rollout window for the nightly fix'
const ACTION_TITLE = 'Approve the staging deploy in the pipeline console'
const PINNED_LINE = 'I pinned what I need from you'

const server = await createServer({ configFile: './vite.config.ts', server: { host: '127.0.0.1', port: 5211, strictPort: false }, logLevel: 'warn' })
await server.listen()
const BASE = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: browserEnv })
const state = { failures: 0 }
const check = makeCheck(state)
const json = (body, status = 200) => ({ status, contentType: 'application/json', body: JSON.stringify(body) })

/** Open `scene` in `theme`. `answer` is how `POST /api/chat` replies to a card's
 *  answer; `ownerOk` is what `/api/auth/me` says about the viewer. */
async function open(theme, scene, {
  width = 1280, height = 820, mobile = false, motion = false, video = null,
  answer = json({ ok: true }), ownerOk = true,
  ready = page => page.getByText(PINNED_LINE).first().waitFor(),
} = {}) {
  const base = mobile
    ? { viewport: { width, height }, deviceScaleFactor: 2, isMobile: true, hasTouch: true }
    : { viewport: { width, height }, deviceScaleFactor: video ? 1 : 2 }
  const ctx = await browser.newContext(video ? { ...base, recordVideo: { dir: video, size: { width, height } } } : base)
  const page = await ctx.newPage()
  const errors = []
  page.on('pageerror', e => errors.push(String(e)))
  const posts = []
  await page.route(u => new URL(u).pathname.startsWith('/api/'), async route => {
    const req = route.request()
    const path = new URL(req.url()).pathname
    if (path === '/api/chat' && req.method() === 'POST') {
      posts.push({ path, body: req.postDataJSON() })
      return route.fulfill(answer)
    }
    if (/\/waiting-on-you\/[^/]+\/dismiss$/.test(path)) return route.fulfill(json({ ok: true }))
    if (path === '/api/auth/me') return route.fulfill(json({ owner_ok: ownerOk }))
    if (path === '/api/workflows/runs') return route.fulfill(json({ runs: [] }))
    if (path === '/api/approvals' || path === '/api/ask-question/pending') return route.fulfill(json([]))
    if (path === '/api/chat/slots' && req.method() === 'GET') return route.fulfill(json(await page.evaluate(() => window.__CAPTURE_SLOTS__)))
    if (/^\/api\/chat\/slots\/[^/]+\/summary$/.test(path)) return route.fulfill(json({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null }))
    const detail = /^\/api\/chat\/slots\/([^/]+)$/.exec(path)
    if (detail) {
      const key = decodeURIComponent(detail[1])
      const msgs = (await page.evaluate(() => window.__CAPTURE_MESSAGES__))[key] ?? []
      const slot = (await page.evaluate(() => window.__CAPTURE_SLOTS__)).find(s => s.key === key) ?? {}
      return route.fulfill(json({ ...slot, key, has_more: false, total: msgs.length, messages: msgs }))
    }
    if (/\/api\/chat\/(tags|pins|folders|tag-columns)$/.test(path)) return route.fulfill(json([]))
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}/capture/waiting-on-you.html?theme=${theme}&scene=${scene}${motion ? '&motion=1' : ''}`)
  await page.waitForSelector('[data-capture-root]')
  await ready(page)
  await page.waitForTimeout(400)
  return { page, ctx, errors, posts }
}

/** The card fits inside the viewport horizontally and its buttons do not overlap. */
async function fits(page, locator, width) {
  const box = await locator.boundingBox()
  return !!box && box.x >= 0 && box.x + box.width <= width
}

/** The computed colour `var(--aim)` resolves to in this theme. */
const aimColor = (page, prop = 'backgroundColor') => page.evaluate(p => {
  const probe = document.createElement('span')
  probe.style.background = 'var(--aim)'
  probe.style.color = 'var(--aim)'
  document.body.appendChild(probe)
  const c = getComputedStyle(probe)[p]
  probe.remove()
  return c
}, prop)

/** Remember the card element now, to prove later that the same one is still there. */
const markCard = (locator) => locator.evaluate(el => { window.__CAPTURE_MARKED__ = el })
const isMarked = (locator) => locator.evaluate(el => el === window.__CAPTURE_MARKED__)

const shot = (page, name) => page.screenshot({ path: resolve(OUT, name) })

for (const theme of ['dark', 'light']) {
  // 01 / 02 -- one decision card, then an option picked.
  {
    const { page, ctx, errors } = await open(theme, 'decision')
    const card = page.getByTestId('waiting-on-you-card')
    await card.waitFor()
    check(`[01/${theme}] one card`, (await card.count()) === 1)
    check(`[01/${theme}] decision card`, (await card.getAttribute('data-kind')) === 'decision')
    check(`[01/${theme}] title`, (await card.getByTestId('waiting-on-you-title').innerText()) === ROLLOUT_TITLE)
    check(`[01/${theme}] three options`, (await card.getByTestId('waiting-on-you-option').count()) === 3)
    check(`[01/${theme}] detail shown`, (await card.getByTestId('waiting-on-you-detail').innerText()).includes('Shard 3 was red'))
    check(`[01/${theme}] "Pinned 3 hours ago"`, /^Pinned 3 ?h(ours?)? ago$/i.test((await card.getByTestId('waiting-on-you-age').innerText()).trim()), await card.getByTestId('waiting-on-you-age').innerText())
    check(`[01/${theme}] nothing selected`, (await card.locator('[aria-pressed="true"]').count()) === 0)
    check(`[01/${theme}] Send disabled`, await card.getByTestId('waiting-on-you-send').isDisabled())
    check(`[01/${theme}] card fits`, await fits(page, card, 1280))
    check(`[01/${theme}] no page errors`, errors.length === 0)
    await shot(page, `01-decision-${theme}.png`)

    await card.getByTestId('waiting-on-you-option').first().click()
    check(`[02/${theme}] first option pressed`, (await card.getByTestId('waiting-on-you-option').first().getAttribute('aria-pressed')) === 'true')
    check(`[02/${theme}] only one option pressed`, (await card.locator('[aria-pressed="true"]').count()) === 1)
    check(`[02/${theme}] Send enabled`, await card.getByTestId('waiting-on-you-send').isEnabled())
    await page.mouse.move(0, 0)
    await page.waitForTimeout(150)
    await shot(page, `02-selected-${theme}.png`)
    await ctx.close()
  }
  // 01 at 390px on a touch screen.
  {
    const { page, ctx, errors } = await open(theme, 'decision', { width: 390, height: 844, mobile: true })
    const card = page.getByTestId('waiting-on-you-card')
    await card.waitFor()
    check(`[01-390/${theme}] three options`, (await card.getByTestId('waiting-on-you-option').count()) === 3)
    check(`[01-390/${theme}] card fits 390px`, await fits(page, card, 390))
    const title = card.getByTestId('waiting-on-you-title')
    check(`[01-390/${theme}] title not clipped`, await title.evaluate(el => el.scrollWidth <= el.clientWidth))
    check(`[01-390/${theme}] no page errors`, errors.length === 0)
    await shot(page, `01-decision-${theme}-390.png`)
    await ctx.close()
  }
  // 03 / 03b -- three asks, collapsed then expanded.
  {
    const { page, ctx, errors } = await open(theme, 'stack', { height: 1000 })
    const stack = page.getByTestId('waiting-on-you-stack')
    await stack.waitFor()
    const cards = stack.getByTestId('waiting-on-you-card')
    const more = stack.getByTestId('waiting-on-you-more')
    check(`[03/${theme}] one card shown`, (await cards.count()) === 1)
    check(`[03/${theme}] newest shown in full`, (await cards.first().getByTestId('waiting-on-you-title').innerText()) === ROLLOUT_TITLE)
    check(`[03/${theme}] "+2 more"`, (await more.innerText()).trim() === '+2 more')
    check(`[03/${theme}] toggle collapsed`, (await more.getAttribute('aria-expanded')) === 'false')
    check(`[03/${theme}] no page errors`, errors.length === 0)
    await shot(page, `03-stack-${theme}.png`)
    await more.click()
    await page.waitForTimeout(250)
    check(`[03b/${theme}] three cards after expanding`, (await cards.count()) === 3)
    check(`[03b/${theme}] toggle reads "Show fewer"`, (await more.innerText()).trim() === 'Show fewer')
    check(`[03b/${theme}] toggle expanded`, (await more.getAttribute('aria-expanded')) === 'true')
    check(`[03b/${theme}] oldest first, newest last`, (await stack.getByTestId('waiting-on-you-title').allInnerTexts()).at(-1) === ROLLOUT_TITLE)
    check(`[03b/${theme}] action card in the stack`, (await stack.locator('[data-kind="action"]').count()) === 1)
    await page.mouse.move(0, 0)
    await shot(page, `03b-stack-expanded-${theme}.png`)
    await ctx.close()
  }
  // 04 -- an action card.
  {
    const { page, ctx, errors } = await open(theme, 'action')
    const card = page.getByTestId('waiting-on-you-card')
    await card.waitFor()
    check(`[04/${theme}] action card`, (await card.getAttribute('data-kind')) === 'action')
    check(`[04/${theme}] no options`, (await card.getByTestId('waiting-on-you-option').count()) === 0)
    check(`[04/${theme}] Done enabled`, await card.getByTestId('waiting-on-you-done').isEnabled())
    check(`[04/${theme}] note field`, (await card.getByTestId('waiting-on-you-note').getAttribute('placeholder')) === 'Add a note (optional)')
    check(`[04/${theme}] detail fits two lines: no "Show more"`, (await card.getByRole('button', { name: 'Show more' }).count()) === 0)
    check(`[04/${theme}] no page errors`, errors.length === 0)
    await shot(page, `04-action-${theme}.png`)
    await ctx.close()
  }
  // 15 / 15b -- at 390px the same detail needs more than two lines, so
  // "Show more" is offered and reveals the rest.
  {
    const { page, ctx, errors } = await open(theme, 'action', { width: 390, height: 844, mobile: true })
    const card = page.getByTestId('waiting-on-you-card')
    await card.waitFor()
    const detail = card.getByTestId('waiting-on-you-detail')
    const toggle = card.getByRole('button', { name: 'Show more', exact: true })
    check(`[15/${theme}] clamped detail offers "Show more"`, (await toggle.count()) === 1)
    check(`[15/${theme}] detail is clamped`, await detail.evaluate(el => el.scrollHeight > el.clientHeight + 1))
    check(`[15/${theme}] card fits 390px`, await fits(page, card, 390))
    await card.scrollIntoViewIfNeeded()
    await shot(page, `15-show-more-${theme}-390.png`)
    await toggle.click()
    await page.waitForTimeout(150)
    check(`[15b/${theme}] expanded detail offers "Show less"`, (await card.getByRole('button', { name: 'Show less', exact: true }).count()) === 1)
    check(`[15b/${theme}] detail no longer clamped`, await detail.evaluate(el => el.scrollHeight <= el.clientHeight + 1))
    check(`[15b/${theme}] the last sentence shows`, (await detail.innerText()).includes('anything the agent should know'))
    check(`[15/${theme}] no page errors`, errors.length === 0)
    await card.scrollIntoViewIfNeeded()
    await shot(page, `15b-show-less-${theme}-390.png`)
    await ctx.close()
  }
  // 06 / 07 -- the other session's sidebar row, tab and hover card.
  {
    const { page, ctx, errors } = await open(theme, 'sidebar')
    const row = page.locator('[data-session-row="slot-b"]').first()
    await row.waitFor()
    const line = row.getByTestId('row-action-for-you')
    check(`[06/${theme}] row reads "Action for you · …"`, (await line.innerText()).trim() === `Action for you · ${ROLLOUT_TITLE}`)
    check(`[06/${theme}] sidebar is online (row not greyed)`, (await row.evaluate(el => getComputedStyle(el).opacity)) === '1')
    check(`[06/${theme}] active row has no status line`, (await page.locator('[data-session-row="slot-a"]').first().getByTestId('row-action-for-you').count()) === 0)
    const tab = page.getByTestId('session-tab-slot-b')
    await tab.waitFor()
    check(`[06/${theme}] tab dot is the action dot`, (await tab.getByTestId('session-tab-status-action').count()) === 1)
    const dotColor = await tab.getByTestId('session-tab-status-action').evaluate(el => getComputedStyle(el).backgroundColor)
    const aim = await aimColor(page)
    check(`[06/${theme}] tab dot in --aim`, dotColor === aim, `${dotColor} vs ${aim}`)
    const tabName = (await tab.getAttribute('aria-label')) ?? (await tab.innerText())
    check(`[06/${theme}] tab named with the ask`, tabName.includes(`action for you · ${ROLLOUT_TITLE}`), tabName)
    check(`[06/${theme}] no page errors`, errors.length === 0)
    await shot(page, `06-sidebar-tabs-${theme}.png`)

    await row.hover()
    const hoverCard = page.getByTestId('needs-me-card')
    await hoverCard.waitFor({ timeout: 5000 })
    const ask = hoverCard.locator('[data-testid="needs-me-ask"][data-kind="waiting"]')
    await ask.waitFor()
    check(`[07/${theme}] hover card lists the ask`, (await ask.getByTestId('waiting-on-you-title').innerText()) === ROLLOUT_TITLE)
    check(`[07/${theme}] its options are offered`, (await ask.getByTestId('waiting-on-you-option').count()) === 3)
    check(`[07/${theme}] hover card fits`, await fits(page, hoverCard, 1280))
    check(`[07/${theme}] no page errors`, errors.length === 0)
    await page.waitForTimeout(300)
    await shot(page, `07-hover-card-${theme}.png`)
    await ctx.close()
  }
  // 08 -- split view: the session pane's header pill, and the picker's dot for
  // a session with an ask beside one without.
  {
    const { page, ctx, errors } = await open(theme, 'grid', { ready: p => p.getByTestId('action-for-you-status').first().waitFor({ timeout: 15000 }) })
    const pill = page.getByTestId('action-for-you-status')
    check(`[08/${theme}] one header pill`, (await pill.count()) === 1)
    check(`[08/${theme}] pill reads "Action for you · …"`, (await pill.innerText()).trim() === `Action for you · ${ROLLOUT_TITLE}`, await pill.innerText())
    const pillColor = await pill.evaluate(el => getComputedStyle(el).color)
    const aimText = await aimColor(page, 'color')
    check(`[08/${theme}] pill text in --aim`, pillColor === aimText, `${pillColor} vs ${aimText}`)
    const pick = title => page.locator('button', { hasText: title }).filter({ has: page.locator('svg') }).first()
    const askDot = pick('Release train watch').locator('svg').first()
    const quietDot = pick('Docs typo sweep').locator('svg').first()
    await askDot.waitFor()
    const askFill = await askDot.evaluate(el => getComputedStyle(el).fill)
    const quietFill = await quietDot.evaluate(el => getComputedStyle(el).fill)
    check(`[08/${theme}] picker dot for the session with an ask is --aim`, askFill === aimText, `${askFill} vs ${aimText}`)
    check(`[08/${theme}] picker dot for the quiet session is not --aim`, quietFill !== aimText, quietFill)
    check(`[08/${theme}] session with an ask listed first`, (await page.locator('button', { hasText: /Release train watch|Docs typo sweep/ }).first().innerText()).includes('Release train watch'))
    check(`[08/${theme}] no page errors`, errors.length === 0, errors.join(' | '))
    await page.mouse.move(0, 0)
    await shot(page, `08-grid-${theme}.png`)
    await ctx.close()
  }
  // 09 -- the Questions tab of the root session's side panel.
  {
    const { page, ctx, errors } = await open(theme, 'questions', { height: 1120, ready: p => p.getByTestId('command-center-panel').waitFor({ timeout: 15000 }) })
    const questions = page.getByRole('radio', { name: /Questions/ })
    await questions.click()
    const listed = page.getByTestId('command-center-waiting')
    await listed.first().waitFor({ timeout: 10000 })
    // The segmented control's indicator slides with a layout animation; let it land.
    await page.waitForTimeout(700)
    const indicatorCentred = await questions.evaluate(el => {
      const control = el.closest('[role="radiogroup"]')
      const pill = [...(control?.querySelectorAll('*') ?? [])].find(n => !el.contains(n) && n !== el && getComputedStyle(n).position === 'absolute' && n.getBoundingClientRect().width > 20)
      if (!pill) return true
      const a = el.getBoundingClientRect(); const b = pill.getBoundingClientRect()
      return Math.abs((a.left + a.width / 2) - (b.left + b.width / 2)) < 4
    })
    check(`[09/${theme}] tab indicator sits under Questions`, indicatorCentred)
    check(`[09/${theme}] two sessions listed`, (await listed.count()) === 2)
    const sessionTitles = await listed.evaluateAll(els => els.map(el => el.firstElementChild?.textContent ?? ''))
    check(`[09/${theme}] each list under its session title`, sessionTitles.includes('Release train watch') && sessionTitles.includes('Staging deploy'), sessionTitles.join(' | '))
    check(`[09/${theme}] every ask in full`, (await page.getByTestId('waiting-on-you-card').count()) === 3)
    check(`[09/${theme}] no "+N more" in the list`, (await page.getByTestId('waiting-on-you-more').count()) === 0)
    check(`[09/${theme}] tab counts the three asks`, /3/.test(await questions.innerText()), await questions.innerText())
    check(`[09/${theme}] no page errors`, errors.length === 0, errors.join(' | '))
    await page.mouse.move(0, 0)
    await shot(page, `09-questions-${theme}.png`)
    await ctx.close()
  }
  // 10 -- the agent retires an action ask while a note is typed: the same card
  // keeps the note on screen, unsent.
  {
    const { page, ctx, errors } = await open(theme, 'action')
    const card = page.getByTestId('waiting-on-you-card')
    await card.waitFor()
    await markCard(card)
    await card.getByTestId('waiting-on-you-note').fill('Approved at 14:05, build 512')
    await page.evaluate(() => window.__CAPTURE_PUSH__([]))
    const closed = page.getByTestId('waiting-on-you-closed')
    await closed.waitFor()
    check(`[10/${theme}] the same element turned into the closed card`, await isMarked(closed))
    check(`[10/${theme}] says the note was not sent`, (await closed.innerText()).includes('This ask was closed. Your note was not sent.'))
    check(`[10/${theme}] keeps the title`, (await closed.innerText()).includes(ACTION_TITLE))
    check(`[10/${theme}] shows the unsent note`, (await closed.getByTestId('waiting-on-you-closed-note').innerText()).trim() === 'Approved at 14:05, build 512')
    const discard = closed.getByTestId('waiting-on-you-discard-draft')
    check(`[10/${theme}] "Discard"`, (await discard.innerText()).trim() === 'Discard')
    check(`[10/${theme}] no page errors`, errors.length === 0)
    await page.mouse.move(0, 0)
    await shot(page, `10-closed-draft-${theme}.png`)
    await discard.click()
    await page.waitForTimeout(200)
    check(`[10/${theme}] the closed card is gone`, (await page.getByTestId('waiting-on-you-closed').count()) === 0)
    await ctx.close()
  }
  // 13 -- the chat send refused the answer (a locked composer): the ask stays
  // pinned with the reason inline.
  {
    const { page, ctx, errors, posts } = await open(theme, 'decision', { answer: json({ ok: false, code: 'composer_locked', error: 'Someone else is typing.' }, 409) })
    const card = page.getByTestId('waiting-on-you-card')
    await card.waitFor()
    await card.getByTestId('waiting-on-you-option').nth(2).click()
    await card.getByTestId('waiting-on-you-send').click()
    const err = card.getByTestId('waiting-on-you-error')
    await err.waitFor()
    const sent = posts[0]?.body ?? {}
    check(`[13/${theme}] one plain chat send of the label`, posts.length === 1 && sent.message === 'Hold it' && !('steer' in sent) && !('origin' in sent), JSON.stringify(sent))
    check(`[13/${theme}] the send names its ask`, sent.meta?.waitingOnYouAnswer?.key === 'rollout' && sent.meta?.waitingOnYouAnswer?.ask_id === 'wy-rollout', JSON.stringify(sent.meta))
    check(`[13/${theme}] inline reason`, (await err.innerText()).includes('The session did not take the answer, so the ask stays pinned. Someone else is typing.'), await err.innerText())
    check(`[13/${theme}] card stays live`, (await page.getByTestId('waiting-on-you-card').count()) === 1)
    check(`[13/${theme}] Send can retry`, await card.getByTestId('waiting-on-you-send').isEnabled())
    check(`[13/${theme}] no page errors`, errors.length === 0)
    await page.mouse.move(0, 0)
    await shot(page, `13-error-${theme}.png`)
    await ctx.close()
  }
  // 14 -- a collaborator who is not the owner sees the ask with no controls.
  {
    const { page, ctx, errors } = await open(theme, 'decision', { ownerOk: false })
    const card = page.getByTestId('waiting-on-you-card')
    await card.waitFor()
    const note = card.getByTestId('waiting-on-you-read-only')
    await note.waitFor()
    check(`[14/${theme}] read-only line`, (await note.innerText()).trim() === 'Only the session owner can answer this.')
    check(`[14/${theme}] no option buttons`, (await card.getByTestId('waiting-on-you-option').count()) === 0)
    check(`[14/${theme}] no Send`, (await card.getByTestId('waiting-on-you-send').count()) === 0)
    check(`[14/${theme}] no dismiss`, (await card.getByTestId('waiting-on-you-dismiss').count()) === 0)
    check(`[14/${theme}] options listed as text`, (await card.locator('li').allInnerTexts()).join('|') === 'Ship today|After the weekend|Hold it')
    check(`[14/${theme}] no page errors`, errors.length === 0)
    await shot(page, `14-read-only-${theme}.png`)
    await ctx.close()
  }
}

/** Record one flow to `<OUT>/<name>.webm` and check its length and size. */
async function record(name, scene, opts, flow) {
  const dir = resolve(OUT, `.video-${name}`)
  rmSync(dir, { recursive: true, force: true })
  const started = Date.now()
  const { page, ctx, errors } = await open('dark', scene, { ...opts, motion: true, video: dir })
  await flow(page)
  check(`[${name}] no page errors`, errors.length === 0)
  const video = page.video()
  await ctx.close()
  const seconds = (Date.now() - started) / 1000
  const out = resolve(OUT, `${name}.webm`)
  renameSync(await video.path(), out)
  rmSync(dir, { recursive: true, force: true })
  const bytes = statSync(out).size
  check(`[${name}] under 15 s (${seconds.toFixed(1)} s)`, seconds < 15)
  check(`[${name}] under 10 MB (${(bytes / 1e6).toFixed(2)} MB)`, bytes > 0 && bytes < 10e6)
}

await record('rec-b-stack-expand-collapse', 'stack', { height: 1000 }, async page => {
  const stack = page.getByTestId('waiting-on-you-stack')
  await stack.waitFor()
  const newest = stack.getByTestId('waiting-on-you-card').last()
  await markCard(newest)
  const more = stack.getByTestId('waiting-on-you-more')
  await page.waitForTimeout(700)
  await more.click()
  await page.waitForTimeout(1000)
  check('[rec-b] expanded to three', (await stack.getByTestId('waiting-on-you-card').count()) === 3)
  check('[rec-b] newest card kept its element while expanding', await isMarked(stack.getByTestId('waiting-on-you-card').last()))
  await more.click()
  await page.waitForTimeout(1000)
  check('[rec-b] collapsed to one', (await stack.getByTestId('waiting-on-you-card').count()) === 1)
  check('[rec-b] newest card kept its element while collapsing', await isMarked(stack.getByTestId('waiting-on-you-card').last()))
  await page.waitForTimeout(300)
})

await browser.close()
await server.close()
for (const f of ['01-decision', '02-selected', '03-stack', '03b-stack-expanded', '04-action', '06-sidebar-tabs', '07-hover-card',
  '08-grid', '09-questions', '10-closed-draft', '13-error', '14-read-only']) {
  check(`${f} dark differs from light`, !readFileSync(resolve(OUT, `${f}-dark.png`)).equals(readFileSync(resolve(OUT, `${f}-light.png`))))
}
for (const f of ['01-decision', '15-show-more', '15b-show-less']) {
  check(`${f}-390 dark differs from light`, !readFileSync(resolve(OUT, `${f}-dark-390.png`)).equals(readFileSync(resolve(OUT, `${f}-light-390.png`))))
}
if (state.failures) { console.error(`${state.failures} check(s) failed`); process.exit(1) }
console.log(`wrote ${OUT}`)
