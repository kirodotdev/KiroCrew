/**
 * Screenshot harness for "Reset chat" on a crewmate's DM (#16339).
 *
 * The control is a small muted "Reset chat" link under the crewmate's name on
 * its profile card — quiet, beside the person, not in the header (the one occasion
 * anybody reaches for it is a crewmate stuck in a turn). The states a reviewer
 * has to judge:
 *
 *   01  the profile card with the small "Reset chat" link under the name
 *   02  the confirm dialog — what is forgotten, what survives, and that a
 *       running turn is stopped first
 *   03  the thread after the reset: the discarded rows behind
 *       "Show earlier messages", and no "Session ready" hint stacked on it
 *   04  the same thread with the earlier messages revealed
 *   05  the row still AVAILABLE while the crewmate is working, which is the
 *       whole reason it moved here
 *   06  the row held with its spinner while the stop-and-wait runs
 *   12  a turn that outlasts the stop budget: the route's own 409, under the row
 *
 * Runs the REAL built SPA behind `serveDist` with every `/api/**` answered from
 * fixtures (`stubDashboardApi`): no gateway, no auth, no kiro-cli. The boundary
 * is served the way production serves it — on the roster row's own `roster`
 * projection block — so what the pane draws here is what it draws live.
 *
 * Usage: node scripts/capture-16339-new-conversation.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'

import { json } from './lib/boot-api.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || join(process.env.KIROCREW_SCRATCH || '.', '16339')
mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)
const iso = (secsAgo) => new Date((now - secsAgo) * 1000).toISOString()
const SLOT = 'member-kiro'
/** The reset moment: 30 minutes ago, between the two halves of the transcript. */
const BOUNDARY_MS = (now - 1800) * 1000

const member = (name, extra = {}) => ({
  name, slug: name, bound: true, slot_key: `member-${name}`, running: false,
  kiro_agent: 'kirocrew', workspace: `/srv/repos/${name}`, memory_store: `member-${name}`,
  memory_version: 2, memory_owner: name, model: '', source: 'kirocrew', ...extra,
})

/** The roster row's projection block, exactly as `GET /api/members` ships it.
 *
 *  `asOfSeq` is the log POSITION the block was read at, and the projection store
 *  seeds a row only up to it -- so a reset has to advance it. A real reset
 *  appends `slot/reset` and the next roster read is one event further on; a
 *  fixture that answered the same position twice would be claiming the boundary
 *  appeared without the log moving, and the store would rightly ignore it. */
const projections = (conversationStarts, asOfSeq = 41, evicted = 0) => ({
  asOfSeq,
  values: {
    roster: {
      name: 'kiro',
      slug: 'kiro',
      kiro_agent: 'kirocrew',
      workspace: '/srv/repos/kiro',
      memory_store: 'member-kiro',
      display_name: 'Kiro',
      slot_key: SLOT,
      ...(conversationStarts ? { conversation_starts: conversationStarts } : {}),
      ...(evicted ? { conversation_starts_evicted: evicted } : {}),
    },
  },
})

const members = ({ reset = false, running = false, boundaryMs = BOUNDARY_MS, evicted = 0 } = {}) => [
  member('kiro', {
    display_name: 'Kiro',
    running,
    last_active_ts: now - 120,
    last_message: 'Opening the PR now.',
    projections: projections(reset ? { [SLOT]: { ts: boundaryMs } } : null, reset ? 42 : 41, evicted),
  }),
  member('atlas', { display_name: 'Atlas', last_active_ts: now - 5400, last_message: 'The gateway boots in 1.4s.' }),
  member('scout', { display_name: 'Scout', last_active_ts: now - 90000, last_message: 'Same conclusion as #3202.' }),
]

/** Two conversations in ONE transcript: the reset sits between them. */
const DISCARDED = [
  { role: 'user', content: 'Which file owns the slot reset route?', cls: '', ts: iso(5400) },
  { role: 'assistant', content: 'The chat API owns it: `slot_lifecycle.py` holds the reset handler beside the shared close path.', cls: '', ts: iso(5340) },
  { role: 'user', content: 'And the member log?', cls: '', ts: iso(5200) },
  { role: 'assistant', content: 'One log per crewmate, append-only. The `slot` domain is the member kind\u2019s, so the boundary belongs there.', cls: '', ts: iso(5100) },
]
const CURRENT = [
  { role: 'user', content: 'Start again: what does the reset button do?', cls: '', ts: iso(900) },
  { role: 'assistant', content: 'It gives this thread a fresh context. The slot key, the channel linkage and the transcript all stay; only what the model remembers is dropped.', cls: '', ts: iso(840) },
]

const detail = (messages) => ({
  messages, running: false, has_more: false, total: messages.length, next_before: 0,
})

/** Live presence, which is what the control's disabled state reads: a slot frame
 *  WINS over the roster row's own `running`, because the frame is the live fact
 *  and the row is the last read. So the busy case is seeded here, not on the
 *  row. */
const slots = (running = false) => [
  { key: SLOT, title: 'Kiro', mode: 'member', created: iso(86400), last_ts: iso(120), running, project: '/srv/repos/kiro', agent: 'kiro' },
]
const STORAGE = {
  'mc-lang': 'en',
  'mc-crewmates-onboarded': '1',
  'mc-crewmates-page-entered': '1',
  'mc-nav': '1',
  'mc-members-panel-open': '0',
}

let failed = false
function check(name, ok, detailText = '') {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detailText}`)
  if (!ok) failed = true
  return ok
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()

/** One page on the Crewmates page with Kiro's DM open.
 *
 *  `live` makes the reset a TRANSITION rather than two fixtures. The roster
 *  read answers with no boundary until the reset route is called and with the
 *  boundary after, which is what the page itself does: the handler invalidates
 *  the roster query, so the collapse a reviewer watches is the real refetch
 *  landing, not a second page loaded in the shape of an outcome.
 *
 *  `record` writes a webm of the whole context (see the RECORD_VIDEO notes at
 *  the clip block). Playwright caps the video at the viewport, so the scale
 *  drops to 1 there -- 2x only costs time in a clip. */
async function open({ reset = false, running = false, messages, boundaryMs = BOUNDARY_MS, refuse = false, boundaryFailed = false, viewport = { width: 1500, height: 940 }, live = false, record = false, evicted = 0 }) {
  let boundaryLanded = reset
  // In `live` mode the boundary is the instant the POST is handled, which is
  // where the route itself stamps it (`boundary_ms` is read before the teardown
  // begins). A fixture constant here would put the line in the middle of the
  // transcript -- a state only reachable by TALKING after a reset -- and the
  // shot would then be claiming a reset leaves a conversation under its own
  // line, which it does not.
  let liveBoundaryMs = boundaryMs
  // How many times the page asked for the turn to stop. Two is the escalation:
  // the route turns any second stop into a hard kill, which is what moves a
  // turn whose cooperative cancel never took.
  let stops = 0
  const extra = async (path, route) => {
    if (path === '/api/members') {
      await json(route, { members: members({ reset: live ? boundaryLanded : reset, running, boundaryMs: live ? liveBoundaryMs : boundaryMs, evicted }), default_agent: 'kirocrew' })
      return true
    }
    if (path === '/api/default-agent') { await json(route, { default_agent: 'kirocrew' }); return true }
    if (path === '/api/teams') { await json(route, { teams: [] }); return true }
    if (path === '/api/autonudge') { await json(route, { enabled: true, loops: [] }); return true }
    if (path.startsWith('/api/crew-board')) {
      // No work ledger, which is how a crewmate with no goal in flight answers.
      // The greeting card then draws nothing and the thread opens as it always
      // did -- the state this harness is about. The generic stub's `[]` guess
      // reaches `crewBoardRows` as a non-iterable and takes the whole DM down
      // behind an error boundary.
      await route.fulfill({
        status: 404,
        contentType: 'application/json',
        body: JSON.stringify({ error: 'no ledger', code: 'no_ledger' }),
      })
      return true
    }
    const thread = path.match(/^\/api\/members\/([^/]+)\/thread$/)
    if (thread) {
      const slug = decodeURIComponent(thread[1])
      await json(route, { slot_key: `member-${slug}`, slug, member: slug, created: false })
      return true
    }
    if (/^\/api\/members\/[^/]+\/activity$/.test(path)) {
      await json(route, { slug: '', member: '', capped: false, entries: [] })
      return true
    }
    if (/^\/api\/members\/[^/]+\/briefing$/.test(path)) {
      await json(route, { slug: '', member: '', supported: true, text: '', updated_ts: null, redacted: false, truncated: false })
      return true
    }
    if (/^\/api\/members\/[^/]+\/panel$/.test(path)) { await json(route, { panel: null, html: null }); return true }
    if (/^\/api\/chat\/slots\/[^/]+\/stop(\?|$)/.test(path)) {
      stops += 1
      // `not running` is the route's own word for "there was nothing to stop",
      // and it is what makes the ordinary idle press skip the wait entirely.
      // With a turn in flight the route answers a bare ok, and the page then
      // watches the slot until it lets go or the budget runs out.
      await json(route, running ? { ok: true } : { ok: true, info: 'not running' })
      return true
    }
    if (/^\/api\/chat\/slots\/[^/]+\/reset-conversation$/.test(path)) {
      if (refuse) {
        // Exactly what the route answers for busy state the page cannot see:
        // an inbound channel message, or a turn admitted between the render
        // and the press.
        await route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ error: 'a turn is in flight', code: 'turn_in_flight', slot: SLOT }),
        })
        return true
      }
      boundaryLanded = true
      liveBoundaryMs = Date.now()
      await json(route, { slot: SLOT, reset: true, replay: false, boundary: boundaryFailed ? 'failed' : 'recorded' })
      return true
    }
    if (path.startsWith('/api/chat/slots/') && !path.endsWith('/folder')) {
      await json(route, detail(messages))
      return true
    }
    return false
  }

  const context = await browser.newContext({
    viewport,
    // The header row is 11-13px type; 1x renders it soft on GitHub.
    deviceScaleFactor: record ? 1 : 2,
    colorScheme: 'light',
    ...(record ? { recordVideo: { dir: join(OUT, 'video-raw'), size: viewport } } : {}),
  })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { theme: 'light', extra, slots: slots(running), localStorageEntries: STORAGE })
  await page.goto(`${base}/members?member=kiro`, { waitUntil: 'domcontentloaded' })
  await page.getByTestId('member-identity-pill').waitFor({ state: 'visible', timeout: 30000 })
  await page.waitForTimeout(700)
  return { context, page, stopCount: () => stops }
}

/** Open the profile card and hand back the "Reset chat" link under the name. */
async function openProfileRow(page) {
  await page.getByTestId('member-identity-pill').click()
  const row = page.getByTestId('crew-profile-new-conversation')
  await row.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(450)
  return row
}

/** Press the row and answer its dialog. */
async function pressAndConfirm(page) {
  const row = await openProfileRow(page)
  await row.click()
  // Scoped to the dialog: the row and the dialog's action now share the one
  // name "Reset chat", so an unscoped by-name match is ambiguous.
  const confirm = page.getByRole('dialog').getByRole('button', { name: 'Reset chat' })
  await confirm.waitFor({ state: 'visible', timeout: 10000 })
  await confirm.click()
  return row
}

// THE CLIP (RECORD_VIDEO=1), which is a separate run and not an extra state.
// Review asked for the one thing a still frame cannot carry: that the collapse
// and the reveal are the same transcript moving, rather than two screenshots a
// reader has to take on trust. Opt-in, because recording every run halves the
// resolution of the stills the geometry checks read and buys them nothing.
//
// The run STOPS after the clip, deliberately: everything below is seven more
// contexts at three viewports, which record as a slideshow of page loads --
// a clip that looks broken while proving nothing. The states a recording is
// wanted for are all inside this one context. So this mode runs NO assertions
// and says so, rather than letting a clip that played through imply a pass.
//
// The raw webm is NOT committed -- only the two derived files, from:
//   RECORD_VIDEO=1 node scripts/capture-16339-new-conversation.mjs <out>
//   ffmpeg -ss 1.2 -i <out>/video-raw/*.webm -an -c:v libx264 -pix_fmt yuv420p \
//     -crf 30 -vf scale=1200:-2 <out>/reset-collapse-reveal.mp4
//   ffmpeg -ss 1.2 -i <webm> -vf "fps=8,scale=760:-1:flags=lanczos,\
//     palettegen=stats_mode=diff:max_colors=128" -f image2 pal.png
//   ffmpeg -ss 1.2 -i <webm> -i pal.png -lavfi "fps=8,scale=760:-1:flags=lanczos[v];\
//     [v][1:v]paletteuse=dither=bayer:bayer_scale=5" <out>/reset-collapse-reveal.gif
// The 1.2s offset drops the boot frames, so the clip opens on the whole thread.
if (process.env.RECORD_VIDEO === '1') {
  const { context, page } = await open({ live: true, record: true, messages: [...DISCARDED, ...CURRENT] })
  // The whole conversation, both halves, before anyone presses anything.
  await page.getByText('And the member log?').waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1600)

  const row = await openProfileRow(page)
  await page.waitForTimeout(1200)
  await row.click()
  const confirm = page.getByRole('dialog').getByRole('button', { name: 'Reset chat' })
  await confirm.waitFor({ state: 'visible', timeout: 10000 })
  // Long enough to read the dialog, which is the half of the flow that says
  // what is kept.
  await page.waitForTimeout(2600)
  await confirm.click()
  await page.getByTestId('crew-profile-close').waitFor({ state: 'detached', timeout: 15000 })

  // The collapse: the roster refetch lands and the discarded half goes behind
  // the control.
  const earlier = page.getByTestId('chat-pane-show-earlier')
  await earlier.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(2000)

  await earlier.click()
  await page.getByText('And the member log?').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(2200)

  const hide = page.getByTestId('chat-pane-hide-earlier')
  await hide.click()
  await earlier.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(1800)

  // The path is only readable from the page, and the file is only finalized on
  // context close -- so read it first, then close in order.
  const clip = await page.video()?.path()
  await context.close()
  await browser.close()
  srv.close()
  console.log(`WEBM ${clip}`)
  console.log('NOTE: clip mode -- reset, collapse, reveal and re-collapse recorded; assertions SKIPPED. Run without RECORD_VIDEO=1 to verify.')
  process.exit(0)
}

// 01 - 04: ONE REAL RESET, in one page, from the thread before the press to the
// history revealed after it. Not four fixtures: a reviewer reading four
// screenshots of four pages has to take on trust that they are the same thread,
// and the line's own time is the thing that proves it -- so all four come from
// this context, the boundary is the instant the press produced, and the line
// therefore sits directly below the last message sent before the reset.
//
// The roster read answers with no boundary until the reset route is called and
// with one after, which is what the page itself does (the handler invalidates
// the roster query), so the collapse here is the real refetch landing.
{
  const { context, page, stopCount } = await open({ live: true, messages: [...DISCARDED, ...CURRENT] })
  check('the control is NOT in the thread header any more', (await page.getByTestId('member-new-conversation').count()) === 0)
  check('the whole transcript is drawn before any reset', (await page.getByText('And the member log?').count()) === 1)
  check('no boundary marker before a reset', (await page.getByTestId('conversation-boundary-row').count()) === 0)
  check('and nothing is collapsed before it either', (await page.getByTestId('chat-pane-show-earlier').count()) === 0)

  const button = await openProfileRow(page)
  check('the link is on the profile card', await button.isVisible())
  check('it sits right under the name', await page.evaluate(() => document.querySelector('[data-testid="crew-profile-name"]')?.nextElementSibling?.getAttribute('data-testid') === 'crew-profile-new-conversation'))
  check('it is quiet: muted, no danger colour', await button.evaluate((el) => el.className.includes('text-muted') && !el.className.includes('text-danger')))
  check('it carries the feature\'s own name', (await button.innerText()).trim() === 'Reset chat')
  check('nothing in the header', (await page.getByTestId('member-thread-menu').count()) === 0)
  await page.screenshot({ path: join(OUT, '01-profile-row.png') })
  await page.getByTestId('crew-profile-face').locator('..').screenshot({ path: join(OUT, '01b-profile-row-closeup.png') })

  // 02: the ask.
  await button.click()
  const ask = page.getByText(/will forget this chat/)
  await ask.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)
  const copy = await ask.innerText()
  check('the ask names the crewmate, quoted', /^\u201cKiro\u201d will forget this chat/.test(copy), copy)
  check('the ask names the memory that survives', /Its long-term memory is kept/.test(copy), copy)
  check('the ask says what the crewmate forgets', /will forget this chat and start fresh/.test(copy))
  check('the ask says where the earlier messages are', /above a \u201cChat reset\u201d line/.test(copy))
  // The second thing being consented to: the flow stops a running turn before
  // it asks for the reset, and the user presses this precisely when one runs.
  check('the ask says a running turn is stopped first', /If it is working now, that work is stopped first\./.test(copy), copy)
  check('the ask uses the control\'s own name', /Reset chat/.test(await page.getByRole('dialog').getByRole('button', { name: 'Reset chat' }).innerText()))
  await page.screenshot({ path: join(OUT, '02-confirm-dialog.png') })

  // 03: the press has landed. The boundary is NOW, so it sits past every
  // message in the thread -- which is what a reset actually leaves behind, and
  // the state the user lands on.
  await page.getByRole('dialog').getByRole('button', { name: 'Reset chat' }).click()
  await page.getByTestId('crew-profile-close').waitFor({ state: 'detached', timeout: 15000 })
  check('a clean reset closes the profile card', (await page.getByTestId('crew-profile-close').count()) === 0)
  const earlier = page.getByTestId('chat-pane-show-earlier')
  await earlier.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(500)
  check('an idle slot is not made to wait for a stop it did not need', stopCount() === 1)
  check('the press collapses the whole discarded conversation', (await page.getByText('And the member log?').count()) === 0)
  check('nothing from before the reset is drawn', (await page.getByText(/Start again: what does the reset button do\?/).count()) === 0)
  check('the thread reads as fresh, not as a quiet crewmate', (await page.getByTestId('crewmate-quiet-hint').count()) === 0)
  check('no second empty-state line above the reset line', (await page.getByText('Session ready. Type a message to start.').count()) === 0)
  check('the line is drawn', (await page.getByTestId('conversation-boundary-row').count()) === 1)
  check('nothing is reported when the boundary is recorded', (await page.getByTestId('member-new-conversation-error').count()) === 0)
  // The row holds the reveal control too, and its label flips on reveal -- so
  // the comparable part is the line's own words and instant.
  const lineOnly = async () => (await page.getByTestId('conversation-boundary-row').innerText())
    .replace(/\n/g, ' ').replace(/(Show|Hide) earlier messages/, '').trim()
  const lineAfter = await lineOnly();
  check(`the line names itself and carries the reset's own time (${lineAfter})`, /Chat reset/.test(lineAfter))
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '03-after-reset-collapsed.png') })

  // 04: the same reset, with its history revealed -- the line stays put, with
  // every discarded message above it and the identical time on it.
  await earlier.click()
  await page.getByText('And the member log?').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400)
  check('the boundary stays marked once the history is drawn', (await page.getByTestId('conversation-boundary-row').count()) === 1)
  check('and it now collapses instead', (await page.getByTestId('chat-pane-hide-earlier').count()) === 1)
  const lineRevealed = await lineOnly();
  check('the revealed line is the SAME line, same instant', lineRevealed === lineAfter, `${lineAfter} | ${lineRevealed}`)
  check('the last message sent before the reset is directly above it', (await page.getByText(/Start again: what does the reset button do\?/).count()) === 1)
  await page.screenshot({ path: join(OUT, '04-earlier-messages-revealed.png') })

  // And Hide puts it back, from the same page.
  await page.getByTestId('chat-pane-hide-earlier').click()
  await earlier.waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(300)
  check('Hide collapses it again', (await page.getByText('And the member log?').count()) === 0)
  await context.close()
}

// 05 and 06 and 12: the STUCK case. The reset route refuses a busy slot (409
// `turn_in_flight`), and busy is what a stuck crewmate is -- so a row disabled
// on `running`, or a flow that posts the reset straight away, is unavailable in
// exactly the situation it exists for.
//
// One page, three frames: the row AVAILABLE while the crewmate works, the row
// held while the stop and the wait run, and the refusal the route gives when the
// turn outlasts the budget. The slot here never lets go, which is the worst case
// and the one a reviewer cannot otherwise see.
{
  const { context, page, stopCount } = await open({ running: true, refuse: true, messages: [...DISCARDED, ...CURRENT] })
  const row = await openProfileRow(page)
  check('the link is AVAILABLE while the crewmate works', await row.isEnabled())
  await page.screenshot({ path: join(OUT, '05-row-while-working.png') })

  await row.click()
  const confirm = page.getByRole('dialog').getByRole('button', { name: 'Reset chat' })
  await confirm.waitFor({ state: 'visible', timeout: 10000 })
  await confirm.click()

  // The stop goes first. The row is held with its spinner for as long as the
  // wait runs: a second press would stack a second stop-and-reset on one slot.
  await page.waitForTimeout(1200)
  check('the turn is asked to stop before the reset is', stopCount() >= 1)
  check('the link is held with a spinner while its own flow runs', (await row.isDisabled()) && (await row.locator('.animate-spin').count()) === 1)
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '06-stopping-then-resetting.png') })

  // Past the halfway mark the page presses Stop a second time, which the route
  // escalates to a hard kill -- what a person does after watching a cooperative
  // stop fail to take.
  await page.waitForTimeout(11_000)
  check(`the wait escalates to a second stop (${stopCount()} stops)`, stopCount() === 2)

  // And the budget is BOUNDED: the reset is asked for regardless, so a provider
  // that never answers cannot park the user on a spinner for good. The route is
  // the authority on busy, and its 409 is what the user is shown.
  const notice = page.getByTestId('member-new-conversation-error')
  await notice.waitFor({ state: 'visible', timeout: 25_000 })
  await page.waitForTimeout(400)
  check('the refusal is reported in the card, under the link', await notice.isVisible())
  check('the link is available again, so a retry is one press away', await row.isEnabled())
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '12-stuck-turn-refusal.png') })
  await context.close()
}

// 07: the refusal. The route answers 409 for busy state the button cannot see,
// and swallowing it would read as "nothing happened" over a conversation the
// model still remembers.
{
  const { context, page } = await open({ refuse: true, messages: [...DISCARDED, ...CURRENT] })
  await pressAndConfirm(page)
  const notice = page.getByTestId('member-new-conversation-error')
  await notice.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400)
  await page.getByTestId('crew-profile-close').click()
  await page.getByTestId('crew-profile-panel').waitFor({ state: 'detached', timeout: 10000 })
  await notice.waitFor({ state: 'visible', timeout: 10000 })
  check('closing the card moves the one notice above the thread', await notice.isVisible())
  check('and there is exactly one copy of it', (await notice.count()) === 1)
  check('the conversation is still drawn under it', (await page.getByText(/Start again: what does the reset button do\?/).count()) === 1)
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '07-refusal-notice.png') })
  await context.close()
}

// 09: the reset ran and its boundary did not. The one outcome where a clean
// success would be a lie about what is on screen: the crewmate has forgotten the
// messages still drawn, and no line marks them. Its own heading, because the
// refusal heading over this body says the reverse of the body.
{
  const { context, page } = await open({ boundaryFailed: true, messages: [...DISCARDED, ...CURRENT] })
  await pressAndConfirm(page)
  const notice = page.getByTestId('member-new-conversation-error')
  await notice.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400)
  const copy = await notice.innerText()
  check('the heading does not contradict the body', /Chat reset, but the line is missing/.test(copy) && !/Couldn't reset the chat/.test(copy), copy.replace(/\n/g, ' | '))
  check('it names an action a reload cannot do', /Reset again to add it/.test(copy) && !/Reload/.test(copy))
  check('it names the line with the words the pane prints on it', /\u201cChat reset\u201d line/.test(copy), copy.replace(/\n/g, ' | '))
  check('it says what is wrong on screen', /old messages still look current\./.test(copy), copy.replace(/\n/g, ' | '))
  check('and the cost is still stated, after it', /Reset again to add it; that also clears anything said since\./.test(copy), copy.replace(/\n/g, ' | '))
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '09-boundary-not-saved.png') })
  await context.close()
}

// 10: the busy refusal, in the user's words. "a turn is in flight" is the code's
// term for a state the pill beside it already renders as working.
{
  const { context, page } = await open({ refuse: true, messages: [...DISCARDED, ...CURRENT] })
  await pressAndConfirm(page)
  const notice = page.getByTestId('member-new-conversation-error')
  await notice.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400)
  const copy = await notice.innerText()
  check('the busy refusal says WHERE the message is, so it does not fight the Idle pill', /Kiro is still answering a message from somewhere else/.test(copy) && /such as a Slack channel/.test(copy) && !/turn is in flight/.test(copy), copy.replace(/\n/g, ' | '))
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '10-busy-refusal.png') })
  await context.close()
}

// 08: 320px. The boundary row has to stay inside the pane -- a non-wrapping
// boundary group is clipped by the transcript's own hidden horizontal overflow,
// taking the reveal with it -- and the header 22ee and its item have to be reachable
// and readable at phone width, where the card floats over the DM column rather
// than taking one of its own.
//
// The header is back to the layout it had before this feature: with the control
// gone there is nothing in a side cell that could run under the centred pill,
// which is what the narrow-width short label and the `sm:` grid existed for.
{
  const { context, page } = await open({ reset: true, messages: [...DISCARDED, ...CURRENT], viewport: { width: 320, height: 760 } })
  const control = page.getByTestId('chat-pane-show-earlier')
  await control.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400)
  const box = await control.boundingBox()
  check(`the reveal is inside the 320px pane (right edge ${Math.round(box.x + box.width)})`, box.x >= 0 && box.x + box.width <= 320)
  const pillBox = await page.getByTestId('member-identity-pill').boundingBox()
  // Main folds the pill to its face at this width (#18549); only its bounds are this PR's concern.
  check(`the identity pill stays inside the pane (${Math.round(pillBox.width)}px)`, pillBox.x + pillBox.width <= 320)

  const row = await openProfileRow(page)
  const rowBox = await row.boundingBox()
  check(`the link is inside the 320px pane (right edge ${Math.round(rowBox.x + rowBox.width)})`, rowBox.x >= 0 && rowBox.x + rowBox.width <= 320)
  // Readable, not merely present: the label is what a reader needs before
  // pressing, so it must not be the first thing the width eats.
  check('its label is not truncated away at phone width', /Reset chat/.test(await row.innerText()))
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '08-narrow-320px.png') })
  await context.close()
}

// 13: the STEADY state, which the one-reset run above cannot show: a boundary
// with a live conversation under it, reached by talking after a reset. Served
// the way a RELOAD serves it -- the boundary off the roster projection, the
// transcript untouched -- so this is also the proof that the line survives a
// reload and a gateway restart rather than living in page state.
{
  const { context, page } = await open({ reset: true, messages: [...DISCARDED, ...CURRENT] })
  const earlier = page.getByTestId('chat-pane-show-earlier')
  await earlier.waitFor({ state: 'visible', timeout: 15000 })
  check('a reload still hides the discarded rows', (await page.getByText('And the member log?').count()) === 0)
  check('and still draws the current conversation under the line', (await page.getByText(/Start again: what does the reset button do\?/).count()) === 1)
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '13-boundary-between-two-halves.png') })
  await context.close()
}

// 14: the evicted-boundary note, with its dismiss control. The fold keeps at
// most 16 boundaries per crewmate and counts what it drops; this line is that
// count's reader, and it is drawn only while the count is non-zero AND the open
// slot has no boundary of its own -- precisely the state where the transcript
// is drawn whole and may be whole only because a boundary was dropped.
{
  const { context, page } = await open({ evicted: 3, messages: [...DISCARDED, ...CURRENT] })
  const note = page.getByTestId('member-boundary-evicted-notice')
  await note.waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(400)
  const text = (await note.innerText()).replace(/\n/g, ' ')
  check('the note is on screen above the thread', await note.isVisible())
  check('it names the line with the words the pane prints on it', /\u201cChat reset\u201d line/.test(text), text)
  check('it says "for this crewmate", never "for this thread"', /for this crewmate/.test(text) && !/for this thread/.test(text), text)
  check('it has a dismiss control', await page.getByTestId('member-boundary-evicted-dismiss').isVisible())
  check('the conversation is drawn whole under it', (await page.getByText('And the member log?').count()) === 1)
  await page.mouse.move(5, 5)
  await page.screenshot({ path: join(OUT, '14-evicted-boundary-note.png') })
  await page.getByTestId('member-boundary-evicted-dismiss').click()
  await note.waitFor({ state: 'detached', timeout: 10000 })
  check('and closing it leaves the thread alone', (await page.getByText('And the member log?').count()) === 1)
  await context.close()
}

await browser.close()
srv.close()
console.log(`\nwrote ${OUT}`)
process.exit(failed ? 1 : 0)
