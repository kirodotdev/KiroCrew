/**
 * Screenshot harness for the question card's pager.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` loopback
 * server, with every /api/** call answered by the shared `stubDashboardApi` and
 * the /api/ws websocket answered by Playwright from fixtures. No gateway, no
 * token, no worktrees. The client code under test is unmodified -- only the
 * network is stubbed -- and the card is driven the way the backend drives it, by
 * pushing a `question_card` frame into the live websocket after the page has
 * rendered.
 *
 * Replaces capture-question-card-compact.mjs, which drove the card through
 * "Collapse all" / "Expand all". Those controls are gone: a stacked card needed
 * three mechanisms to stay usable (fold all but the first, auto-fold on answer,
 * a viewport cap for whatever was still open) and the pager retires all three,
 * because one question on screen is bounded by construction.
 *
 *   01  a fresh card: question 1 of 3, Next held shut until it is answered
 *   02  answering a single-select auto-advances -- one gesture per question
 *   03  a multi-select, which does NOT auto-advance, so Next carries it forward
 *   04  the last question, where Submit replaces Next
 *   05  Submit locked with a question outstanding, naming it and offering the jump
 *   06  a single question: Submit alone, no pager to show
 *   07  after Submit: the answers as the agent receives them, each under its own
 *       question -- the format this change exists to fix
 *
 * Then records the walk as webm, plus a 2x-slowed GIF when ffmpeg is present,
 * since the page transition is motion a still cannot show. Rebuild dist first or
 * it records the previous behaviour.
 *
 * Usage: node scripts/capture-question-card-pager.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, mkdtempSync, readdirSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/question-card-pager-shots'
/* Desktop by default, because that is what the PR body leads with. Overridable
   because the corner arrows carry a touch-target claim, and a claim about phone
   layout has to be photographed at phone width rather than argued from class
   names: CARD_VIEWPORT=390x844 retakes the same pages at iPhone-class width. */
const [VW, VH] = (process.env.CARD_VIEWPORT || '1440x900').split('x').map(Number)
const SLOT = 'chat-ask'
const PROJECT = '/home/user/workspace/KiroCrew'
mkdirSync(OUT, { recursive: true })

/** Three questions, mixed modes: the multi-select in the middle is the case the
 *  pager needs Next for, because answering it cannot auto-advance. */
const THREE = [
  {
    question: 'Which trust model should the connector use?',
    header: 'TRUST',
    options: [
      { label: 'Carve-out per tenant', description: 'One scoped credential per tenant, rotated independently.' },
      { label: 'Public endpoints only', description: 'No credential at all; refuse anything that needs one.' },
      { label: 'Shared service account', description: 'One credential for every tenant.' },
      { label: 'Defer to the caller', description: 'Accept whatever the caller presents, unverified.' },
    ],
  },
  {
    question: 'Which checks must pass before this is done?',
    header: 'GATES',
    multiSelect: true,
    options: [
      { label: 'Unit tests for the changed module', description: 'Fast, targeted. Usually enough.' },
      { label: 'Full test suite', description: 'Slow and memory-hungry.' },
      { label: 'Linter and type checks', description: 'Catches what the tests miss.' },
      { label: 'Manual browser verification', description: 'Only meaningful for UI work.' },
    ],
  },
  {
    question: 'How should a refused request surface to the user?',
    header: 'REPORTING',
    options: [
      { label: 'Inline on the row' }, { label: 'A dashboard toast' }, { label: 'Both' }, { label: 'Log only' },
    ],
  },
]

const ONE = [{
  question: 'Rebase onto main before opening the PR?',
  options: [{ label: 'Yes, rebase first' }, { label: 'No, open it as is' }],
}]

const slots = [{
  key: SLOT,
  title: 'Wire the tenant connector',
  running: true,
  last_message: 'I need three decisions before I can continue.',
  messages: 4,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: true,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', content: 'Wire up the tenant connector.' },
    { role: 'assistant', content: 'I need three decisions before I can continue.' },
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: VW, height: VH },
    // The card is dense small type; a 1x shot renders it soft on GitHub.
    deviceScaleFactor: 2,
  })

  let wsServer = null
  let page = null

  /**
   * Only the routes the shared stub does not already own. Each branch awaits
   * `json()` and returns true, because the stub treats a falsy return as "not
   * handled" and would then fulfil the route a second time.
   */
  const extra = async (path, route) => {
    if (path.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    /* The stateless card's Submit sends its answer as a turn. Unstubbed this
       resolves as a transport failure and the card keeps the answer for retry
       instead of committing the bubble the shot is about. */
    if (path === '/api/chat') { await json(route, { ok: true, steered: true }); return true }
    if (path === '/api/models') { await json(route, { models: [], default: 'auto' }); return true }
    if (path === '/api/chat/nav/resolve-links') { await json(route, { summaries: [] }); return true }
    return false
  }

  async function load(ctx = context) {
    page = await ctx.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots,
      theme: 'dark',
      extra,
      // Seeded through the stub rather than our own addInitScript: Playwright
      // does not order separately registered init scripts, so seeding here
      // would race the stub's localStorage.clear().
      localStorageEntries: { 'mc-onboarded': '1', 'mc-active-slot': SLOT },
    })
    /* AFTER the stub, never before: the stub swallows /api/ws with a no-op to
       stop the dashboard retry-storming a gateway that is not there, and
       Playwright matches the most recently registered route first. Registered
       ahead of it, this handler never binds and pushCard has no socket. */
    await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })
    await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2500)
  }

  /** Push the card exactly as the question_card broadcast delivers it.
   *
   *  `askId` null pushes a STATELESS card. The distinction decides what Submit
   *  does: a card carrying `ask_id` resolves a server-side wait through
   *  `POST /api/ask-question/<id>/answer` and writes nothing to the transcript,
   *  while a stateless one sends its answer as the user's own message. Only the
   *  stateless path can show the submitted `Q.`/`A.` text. */
  async function pushCard(questions, askId) {
    if (!wsServer) throw new Error('websocket route never bound')
    wsServer.send(JSON.stringify({
      type: 'question_card',
      data: { slot: SLOT, ...(askId ? { ask_id: askId } : {}), questions },
    }))
    await page.waitForTimeout(1200)
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Click by visible text, failing loudly rather than screenshotting a card that
   *  never rendered the control the shot is supposed to be about. */
  async function click(text, why) {
    const el = page.getByText(text, { exact: true })
    if (!(await el.count())) throw new Error(`no "${text}" control: ${why}`)
    await el.first().click()
    await page.waitForTimeout(800)
  }

  await load()

  // 1. A fresh three-question card: one question on screen, the position in the
  //    corner, and Next disabled because nothing has been answered yet.
  await pushCard(THREE, 'ask-pager')
  if (!(await page.getByText('1/3').count())) throw new Error('no pager: the card did not render')
  await shot('01-fresh-card-question-one-of-three')

  // 2. Answering a single-select auto-advances, so a question costs one gesture
  //    rather than an answer plus a Next.
  await click(THREE[0].options[0].label, 'the first question is not on screen')
  if (!(await page.getByText('2/3').count())) throw new Error('answering did not auto-advance')
  await shot('02-single-select-auto-advances')

  // 3. The multi-select case. It deliberately does not auto-advance -- one click
  //    does not finish it -- so before Next existed the only primary control here
  //    was a disabled Submit and the only way on was the corner arrow.
  await click(THREE[1].options[0].label, 'the multi-select is not on screen')
  await click(THREE[1].options[2].label, 'the multi-select lost its options')
  await shot('03-multi-select-needs-next')

  // 4. Next carries it to the last question, where Submit replaces Next.
  await click('Next', 'Next is missing on a non-final question')
  if (!(await page.getByText('3/3').count())) throw new Error('Next did not advance')
  if (await page.getByText('Next', { exact: true }).count()) {
    throw new Error('Next is still offered on the last question')
  }
  await shot('04-last-question-offers-submit')

  // 5. Submit stays locked while a question is outstanding, and the footer names
  //    how many and jumps to the first -- the review a folded row used to carry.
  await shot('05-submit-locked-with-one-outstanding')

  // 6. A single question is its own last question, so Next would point nowhere.
  await load()
  await pushCard(ONE, 'ask-single')
  if (await page.getByText('Next', { exact: true }).count()) {
    throw new Error('a single-question card should not offer Next')
  }
  await shot('06-single-question-submit-only')

  /* 7. What the agent actually receives. Every answer above is discarded by the
        card unless Submit commits it, and the committed form is the thing this
        change exists to fix: before, three answers arrived as bare lines joined
        by a newline, so a two-item multi-select was indistinguishable from two
        separate answers and neither carried the question it belonged to. This is
        the only shot taken AFTER Submit, and it is taken on a stateless card
        because a card carrying `ask_id` resolves a server-side wait instead of
        writing a message. */
  await load()
  await pushCard(THREE, null)
  await click(THREE[0].options[0].label, 'post-submit pass: first question missing')
  await click(THREE[1].options[0].label, 'post-submit pass: multi-select missing')
  await click(THREE[1].options[2].label, 'post-submit pass: multi-select lost its options')
  await click('Next', 'post-submit pass: Next missing on the multi-select')
  await click(THREE[2].options[0].label, 'post-submit pass: last question missing')
  await click('Submit', 'post-submit pass: Submit missing on the last question')
  await page.waitForTimeout(1500)
  if (await page.getByText('1/3').count()) throw new Error('the card did not clear on Submit')
  for (const q of THREE) {
    if (!(await page.getByText(`Q. ${q.question}`, { exact: false }).count())) {
      throw new Error(`submitted message is missing its question label: ${q.question}`)
    }
  }
  await shot('07-post-submit-labelled-answers')

  /* The page transition is a short crossfade plus a directional slide, and a
     still can only show where it landed. Recorded as well as photographed so the
     motion is reviewable: whether the swap settles or jumps, and whether anything
     moves under the pointer. */
  /* A run-private directory, NOT a fixed `OUT/video-raw`: the cleanup at the end
     is a recursive force-delete and OUT is caller-supplied, so a fixed name would
     destroy an unrelated directory that happened to share it. mkdtemp's suffix is
     unique per run, and it is created INSIDE OUT so the finished video can be
     renamed instead of copied across filesystems. Only this directory is ever
     removed, so nothing the caller already had in OUT is touched. */
  const raw = mkdtempSync(join(OUT, 'video-intermediates-'))
  const videoCtx = await browser.newContext({
    viewport: { width: VW, height: VH },
    recordVideo: { dir: raw, size: { width: VW, height: VH } },
  })
  await load(videoCtx)
  await pushCard(THREE, null)
  await page.waitForTimeout(700)
  await click(THREE[0].options[0].label, 'motion pass: first question missing')
  await click(THREE[1].options[0].label, 'motion pass: multi-select missing')
  await click('Next', 'motion pass: Next missing')
  await click(THREE[2].options[0].label, 'motion pass: last question missing')
  /* Through Submit, not up to it: a recording that stops on the last page shows
     the walk but never the answer the walk produces. */
  await click('Submit', 'motion pass: Submit missing on the last question')
  await page.waitForTimeout(2000)
  await videoCtx.close()                       // flushes the webm to disk

  const webm = join(OUT, 'pager-walk.webm')
  const src = readdirSync(raw).find(f => f.endsWith('.webm'))
  if (!src) throw new Error('no video written: the recorded pass captured nothing')
  renameSync(join(raw, src), webm)
  console.log('wrote', webm)

  const ff = args => spawnSync('ffmpeg', ['-y', ...args], { stdio: 'ignore' }).status === 0
  const slow = join(OUT, 'pager-walk-slow.gif')
  const pal = join(raw, 'palette.png')
  const filters = 'setpts=2*PTS,fps=16,scale=1000:-1:flags=lanczos'
  if (ff(['-i', webm, '-vf', `${filters},palettegen`, pal])
      && ff(['-i', webm, '-i', pal, '-lavfi', `${filters}[x];[x][1:v]paletteuse`, slow])) {
    console.log('wrote', slow)
  } else {
    console.log('GIF skipped: ffmpeg unavailable or failed; the webm is still written')
  }
  rmSync(raw, { recursive: true, force: true })   // this run's own directory only

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
