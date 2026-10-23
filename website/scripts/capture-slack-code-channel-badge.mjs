/**
 * Screenshot harness for the Slack code channel badge on dashboard sessions.
 *
 * Loads the REAL built SPA with canned backend responses. The slot rows carry
 * `code_channel` exactly as `slack/code_channels/dashboard_view.py` serializes
 * it, so what is under test is how the dashboard draws the field:
 *
 *   - a bound, active channel: the sidebar row's Slack mark alone (a link named
 *     "Open #channel in Slack", no channel text) and the session header's full
 *     link (mark, "#channel", link glyph), both opening the channel (Slack's
 *     app_redirect URL) in a new tab;
 *   - an archived channel and a wrapped-up one with no known name: the header
 *     says the state in words, the row's tooltip explains it, the unnamed one
 *     falls back to "Code channel";
 *   - a session mirrored to a Slack thread: the header links to the thread;
 *   - an ordinary session: no badge anywhere (the row and header are as on main,
 *     which is also what every row looks like with the toggle off, because the
 *     backend then sends no `code_channel` at all);
 *   - 320px phone width, dark and light themes.
 *
 * Every scene asserts the DOM before it shoots, so a broken render fails the
 * run rather than producing a plausible PNG.
 *
 * Usage: node scripts/capture-slack-code-channel-badge.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/slack-code-channel-badge'
mkdirSync(OUT, { recursive: true })

const now = Date.now()
const iso = ago => new Date(now - ago * 1000).toISOString()
const channel = (id, name, status) => ({
  channel_name: name,
  status,
  web_url: `https://slack.com/app_redirect?channel=${id}&team=T0TEAM1`,
})
const row = (key, title, ago, extra = {}) => ({
  key, title, running: false, messages: 4, agent: 'kirocrew', last_ts: iso(ago), last_turn_ts: iso(ago),
  folder_id: '', last_message: 'Pushed the fix and opened the pull request.', source_links: [], source_links_total: 0,
  ...extra,
})

const BOUND = 'chat-bound'
const PLAIN = 'chat-plain'
const THREAD = 'chat-thread'
const BORN = 'slack:1792588893.063579'
const slackLink = { channel: 'slack', label: 'Slack', target: '…0001', binding: 'b', direction: 'out', drives_session: true, live: true }
const SLOTS = [
  row(BOUND, 'Fix the login redirect loop', 60, { links: [slackLink], code_channel: channel('C0CODE1', 'fix-login-redirect', 'active') }),
  row('chat-archived', 'Bump the Node runtime', 600, { links: [slackLink], code_channel: channel('C0CODE2', 'bump-node-runtime', 'archived') }),
  row('chat-wrapped', 'Draft the release notes', 1200, { links: [slackLink], code_channel: channel('C0CODE3', '', 'wrapped_up') }),
  row(PLAIN, 'Plan the Q4 roadmap', 1800, { links: [slackLink] }),
  row(THREAD, 'Review the API spec', 2400, {
    links: [slackLink], slack_linked: true, slack_channel: 'C0THREAD1', slack_thread_ts: '1712793600.123456',
  }),
  // Started in Slack (Kyle's build-9 session): its own thread is an origin,
  // not a mirror, so it is not slack_linked and carries the origin ids.
  row(BORN, 'Triage the flaky deploy', 3000, {
    links: [{ ...slackLink, target: '…UXM5', direction: 'origin' }], slack_linked: false, slack_channel: '', slack_thread_ts: '',
    slack_origin_channel: 'C0C9R9WUXM5', slack_origin_thread_ts: '1792588893.063579',
  }),
]
// BEFORE=1 runs against a build without the Slack-born header link and
// asserts the bug (no link) instead of the fix.
const BEFORE = process.env.BEFORE === '1'

const detail = { running: false, has_more: false, total: 0, queue: [], messages: [] }
const extra = (path, route) => {
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  return false
}

function check(label, cond) {
  if (!cond) throw new Error(`assertion failed: ${label}`)
}

async function load(context, { theme, active }) {
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots: SLOTS, theme, extra })
  await page.addInitScript(slot => {
    localStorage.setItem('mc-active-slot', slot)
    localStorage.setItem('mc-privacy-notice-v1', '1')
    localStorage.setItem('mc-sidebar-pinned', 'true')
    localStorage.setItem('mc-session-stale-collapse-ms', '0')
  }, active)
  await page.goto(context._base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.locator('.session-row').first().waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForTimeout(1500)
  await page.mouse.move(5, 890)
  await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur() })
  return page
}

const rowOf = (page, title) => page.locator('.session-row').filter({ hasText: title })

async function assertRows(page) {
  const bound = rowOf(page, 'Fix the login redirect loop').getByTestId('code-channel-badge-row')
  check('bound row chip is visible', await bound.isVisible())
  check('bound row chip links to the channel', (await bound.getAttribute('href')) === 'https://slack.com/app_redirect?channel=C0CODE1&team=T0TEAM1')
  check('bound row chip opens a new tab', (await bound.getAttribute('target')) === '_blank')
  check('bound row chip has noopener', (await bound.getAttribute('rel')) === 'noopener noreferrer')
  check('bound row link is named for its channel', (await bound.getAttribute('aria-label')) === 'Open #fix-login-redirect in Slack')
  check('bound row tooltip names the channel', (await bound.getAttribute('title')) === 'Open #fix-login-redirect in Slack')
  check('bound row mark renders no text', (await bound.textContent()) === '')
  check('no channel name anywhere in the bound row', !(await rowOf(page, 'Fix the login redirect loop').textContent()).includes('fix-login-redirect'))
  const archivedChip = rowOf(page, 'Bump the Node runtime').getByTestId('code-channel-badge-row')
  check('archived mark is marked archived', (await archivedChip.getAttribute('data-code-channel-status')) === 'archived')
  check('archived tooltip names the channel and the state', (await archivedChip.getAttribute('title')) === 'Open #bump-node-runtime in Slack\nArchived in Slack. The channel is read-only.')
  const wrapped = rowOf(page, 'Draft the release notes').getByTestId('code-channel-badge-row')
  check('unnamed chip tooltip falls back to the generic hint', (await wrapped.getAttribute('title')).includes('Open this code channel in Slack'))
  check('wrapped-up tooltip explains the state', (await wrapped.getAttribute('title')).includes('The task is done'))
  // One Slack mark per row: a code channel's badge stands in for the
  // connected-to-Slack glyph, which an ordinary Slack-linked row still shows.
  const slackMark = '[role="img"][aria-label="Connected to Slack"]'
  check('code-channel row shows no second Slack mark', (await rowOf(page, 'Fix the login redirect loop').locator(slackMark).count()) === 0)
  check('ordinary Slack-linked row keeps its mark', (await rowOf(page, 'Plan the Q4 roadmap').locator(slackMark).count()) === 1)
  check('plain row has no chip', (await rowOf(page, 'Plan the Q4 roadmap').getByTestId('code-channel-badge-row').count()) === 0)
  // The chip sits on the meta line and must never paint over the timestamp.
  for (const title of ['Fix the login redirect loop', 'Bump the Node runtime', 'Draft the release notes']) {
    const r = rowOf(page, title)
    const chip = await r.getByTestId('code-channel-badge-row').boundingBox()
    const time = await r.getByTestId('session-row-time').boundingBox()
    check(`${title}: mark clears the timestamp`, chip && time && chip.x + chip.width <= time.x + 0.5)
    const mark = await r.getByTestId('code-channel-slack-mark').boundingBox()
    check(`${title}: mark is its fixed 10px`, mark && Math.abs(mark.width - 10) < 0.5 && Math.abs(mark.height - 10) < 0.5)
  }
}

async function shootRows(page, path) {
  const boxes = await page.locator('.session-row').evaluateAll(els => els.map(el => {
    const r = el.getBoundingClientRect()
    return { x: r.x, y: r.y, right: r.right, bottom: r.bottom }
  }))
  const pad = 6
  const x = Math.max(0, Math.floor(Math.min(...boxes.map(b => b.x)) - pad))
  const y = Math.max(0, Math.floor(Math.min(...boxes.map(b => b.y)) - pad))
  const right = Math.ceil(Math.max(...boxes.map(b => b.right)) + pad)
  const bottom = Math.ceil(Math.max(...boxes.map(b => b.bottom)) + pad)
  await page.screenshot({ path, clip: { x, y, width: right - x, height: bottom - y } })
}

async function shootHeader(page, path, testId = 'code-channel-badge-header') {
  const chip = page.getByTestId(testId)
  const box = await chip.boundingBox()
  check('header chip has a box', !!box)
  const x = Math.max(0, Math.floor(box.x - 520))
  await page.screenshot({ path, clip: { x, y: Math.max(0, box.y - 14), width: box.x + box.width + 24 - x, height: box.height + 28 } })
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const theme of ['dark', 'light']) {
      const context = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 2 })
      context._base = base
      const page = await load(context, { theme, active: BOUND })
      await assertRows(page)
      const header = page.getByTestId('code-channel-badge-header')
      check('header chip is visible', await header.isVisible())
      check('header chip links to the channel', (await header.getAttribute('href')).includes('channel=C0CODE1'))
      check('header chip shows #channel', (await header.getByTestId('code-channel-name').textContent()) === '#fix-login-redirect')
      check('header chip shows the link glyph', await header.getByTestId('code-channel-link-glyph').isVisible())
      check('header link carries no credential', !/token|auth/i.test(await header.getAttribute('href')))
      await shootRows(page, join(OUT, `01-sidebar-${theme}.png`))
      await shootHeader(page, join(OUT, `02-header-${theme}.png`))
      // The archived and wrapped-up states in the header.
      for (const [title, state, file] of [
        ['Bump the Node runtime', 'Channel archived', 'archived'],
        ['Draft the release notes', 'Task done', 'wrapped-up'],
      ]) {
        await page.locator('.session-row [data-session-title]').filter({ hasText: title }).click()
        await page.waitForTimeout(800)
        await page.mouse.move(5, 890)
        const chip = page.getByTestId('code-channel-badge-header')
        check(`${file} header says ${state}`, (await chip.textContent()).includes(state))
        await shootHeader(page, join(OUT, `02-header-${file}-${theme}.png`))
      }
      // A session mirrored to a Slack thread: the header links to the thread.
      await page.locator('.session-row [data-session-title]').filter({ hasText: 'Review the API spec' }).click()
      await page.waitForTimeout(800)
      await page.mouse.move(5, 890)
      const thread = page.getByTestId('slack-thread-link-header')
      check('thread header links to the thread', (await thread.getAttribute('href')) === 'https://slack.com/archives/C0THREAD1/p1712793600123456')
      check('thread session shows no code-channel chip', (await page.getByTestId('code-channel-badge-header').count()) === 0)
      await shootHeader(page, join(OUT, `02-header-thread-${theme}.png`), 'slack-thread-link-header')
      // A session started in Slack: the header links to the thread it lives in.
      await page.locator('.session-row [data-session-title]').filter({ hasText: 'Triage the flaky deploy' }).click()
      await page.waitForTimeout(800)
      await page.mouse.move(5, 890)
      const born = page.getByTestId('slack-thread-link-header')
      if (BEFORE) {
        check('BEFORE: Slack-born header has no thread link (the bug)', (await born.count()) === 0)
      } else {
        check('Slack-born header links to its own thread', (await born.getAttribute('href')) === 'https://slack.com/archives/C0C9R9WUXM5/p1792588893063579')
        check('Slack-born link opens a new tab with noopener', (await born.getAttribute('rel')) === 'noopener noreferrer')
        check('Slack-born header shows no code-channel chip', (await page.getByTestId('code-channel-badge-header').count()) === 0)
      }
      await page.screenshot({ path: join(OUT, `06-header-slack-born-${theme}.png`), clip: { x: 0, y: 40, width: 1600, height: 80 } })
      if (BEFORE) {
        await page.close()
        await context.close()
        console.log(`${theme}: BEFORE bug reproduced`)
        continue
      }
      await page.close()
      await context.close()

      // An ordinary session: the header carries no chip.
      const plainContext = await browser.newContext({ viewport: { width: 1600, height: 900 }, deviceScaleFactor: 2 })
      plainContext._base = base
      const plain = await load(plainContext, { theme, active: PLAIN })
      await rowOf(plain, 'Plan the Q4 roadmap').click()
      await plain.waitForTimeout(1000)
      await plain.mouse.move(5, 890)
      check('plain session header has no chip', (await plain.getByTestId('code-channel-badge-header').count()) === 0)
      check('plain session header has no thread link', (await plain.getByTestId('slack-thread-link-header').count()) === 0)
      await plain.screenshot({ path: join(OUT, `03-plain-header-${theme}.png`), clip: { x: 0, y: 40, width: 1600, height: 80 } })
      await plainContext.close()
      console.log(`${theme}: desktop OK`)
    }

    // 320px phone: the drawer's rows and the top bar's glyph.
    for (const theme of BEFORE ? [] : ['dark', 'light']) {
      const context = await browser.newContext({ viewport: { width: 320, height: 640 }, deviceScaleFactor: 3, hasTouch: true, isMobile: true })
      context._base = base
      const page = await context.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, { slots: SLOTS, theme, extra })
      await page.addInitScript(slot => {
        localStorage.setItem('mc-active-slot', slot)
        localStorage.setItem('mc-privacy-notice-v1', '1')
      }, BOUND)
      await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
      await page.getByTestId('mobile-topbar-title').waitFor({ state: 'visible', timeout: 20_000 })
      await page.waitForTimeout(1200)
      check('phone top bar marks the code channel', await page.getByTestId('mobile-code-channel-glyph').isVisible())
      await page.screenshot({ path: join(OUT, `04-phone-topbar-${theme}.png`), clip: { x: 0, y: 0, width: 320, height: 64 } })
      await page.getByTestId('mobile-topbar-sessions-toggle').tap()
      await page.locator('.session-row').first().waitFor({ state: 'visible', timeout: 10_000 })
      await page.waitForTimeout(800)
      await assertRows(page)
      const overflow = await rowOf(page, 'Fix the login redirect loop').evaluate(el => {
        const chip = el.querySelector('[data-testid="code-channel-badge-row"]')
        return !chip || chip.getBoundingClientRect().right > el.getBoundingClientRect().right + 0.5
      })
      check('bound row chip stays inside its row at 320px', !overflow)
      await page.screenshot({ path: join(OUT, `05-phone-sidebar-${theme}.png`) })
      await context.close()
      console.log(`${theme}: 320px OK`)
    }
    console.log(`\nOK — screenshots in ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
