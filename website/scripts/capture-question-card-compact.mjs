/**
 * Screenshot harness for the question card's starting fold shape.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` loopback
 * server, with every /api/** call answered by the shared `stubDashboardApi` and
 * the /api/ws websocket answered by Playwright from fixtures. No gateway, no token, no worktrees. The client code under test is
 * unmodified -- only the network is stubbed -- and the card is driven the way the
 * backend drives it, by pushing a `question_card` frame into the live websocket
 * after the page has rendered.
 *
 * Captures the case the report describes: three questions, four options each.
 *
 *   01  a fresh card, which opens at its first question
 *   02  the same card after Expand all, which is the shape it used to open in
 *   04  answering the first question, which hands off to the second
 *   03  a single question, which opens as it always did
 *
 * Then records the hand-off click as webm, plus a 3x-slowed GIF when ffmpeg is
 * present, because two simultaneous height springs are motion a still cannot
 * show. Rebuild dist first or it records the previous behaviour.
 *
 * Shot 02 is not a separate build. Expand all clears the fold map to `{}`, which
 * is precisely the state the card used to mount in, so one build renders both.
 *
 * Usage: node scripts/capture-question-card-compact.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, mkdtempSync, readdirSync, renameSync, rmSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '/tmp/question-card-shots'
const SLOT = 'chat-ask'
const PROJECT = '/home/user/workspace/KiroCrew'
mkdirSync(OUT, { recursive: true })

/** Three questions with four options each: the card the report calls unusable. */
const THREE = [
  {
    question: 'Which trust model should the connector use?',
    options: [
      { label: 'Carve-out per tenant', description: 'One scoped credential per tenant, rotated independently.' },
      { label: 'Public endpoints only', description: 'No credential at all; refuse anything that needs one.' },
      { label: 'Shared service account', description: 'One credential for every tenant.' },
      { label: 'Defer to the caller', description: 'Accept whatever the caller presents, unverified.' },
    ],
  },
  {
    question: 'Which environments should it roll out to first?',
    options: [
      { label: 'staging' }, { label: 'staging then prod' }, { label: 'prod only' }, { label: 'a single canary host' },
    ],
  },
  {
    question: 'How should a refused request surface to the user?',
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
    viewport: { width: 1440, height: 900 },
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

  /** Push the card exactly as the question_card broadcast delivers it. */
  async function pushCard(questions, askId) {
    if (!wsServer) throw new Error('websocket route never bound')
    wsServer.send(JSON.stringify({
      type: 'question_card',
      data: { slot: SLOT, ask_id: askId, questions },
    }))
    await page.waitForTimeout(1200)
  }

  async function shot(name) {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  await load()

  // 1. A fresh three-question card: it opens at its first question, so the
  //    composer and the conversation above it stay on screen.
  await pushCard(THREE, 'ask-compact')
  await shot('01-fresh-card-opens-at-one-question')

  // 2. The same card fully expanded, which is the state it used to mount in.
  //    Two clicks, not one: a fresh card is MIXED, so the control offers
  //    "Collapse all" first; only once everything is folded does it offer
  //    "Expand all", which clears the fold map to the old empty default.
  const collapseAll = page.getByText('Collapse all')
  if (!(await collapseAll.count())) throw new Error('no Collapse all control: the card did not render')
  await collapseAll.first().click()
  await page.waitForTimeout(600)
  const expandAll = page.getByText('Expand all')
  if (!(await expandAll.count())) throw new Error('no Expand all control after collapsing')
  await expandAll.first().click()
  await page.waitForTimeout(800)
  await shot('02-same-card-fully-expanded-old-default')

  // 3. Answering the first question hands off to the second. This is the step
  //    the compact default would otherwise make worse: the answered question
  //    folds to its answer, and the next one needing an answer opens in the same
  //    beat, so no question has to be hunted for on a muted row.
  await load()
  await pushCard(THREE, 'ask-handoff')
  const firstPick = page.getByText(THREE[0].options[0].label)
  if (!(await firstPick.count())) throw new Error('first question is not open: nothing to answer')
  await firstPick.first().click()
  await page.waitForTimeout(900)
  const secondOpen = page.getByText(THREE[1].options[0].label)
  if (!(await secondOpen.count())) {
    throw new Error('answering the first question did not open the second: the hand-off is broken')
  }
  await shot('04-answering-hands-off-to-the-next-question')

  // 4. A single question is untouched: nothing follows it, so folding it would
  //    only hide the options being compared.
  await load()
  await pushCard(ONE, 'ask-single')
  await shot('03-single-question-unchanged')

  /* The hand-off is TWO height springs on one click -- the answered question
     folding while the next one opens -- and a still can only show where they
     landed. Recorded as well as photographed so the motion itself is reviewable:
     whether the card settles or jumps, and whether anything moves under the
     pointer. 0.28s at Playwright's fixed 25fps is about seven frames, which a
     real-time GIF at 10fps would cut to three, so the slowed pass is the one
     that is actually legible. */
  /* A run-private directory, NOT a fixed `OUT/video-raw`: the cleanup at the end
     is a recursive force-delete and OUT is caller-supplied, so a fixed name would
     destroy an unrelated directory that happened to share it. mkdtemp's suffix is
     unique per run, and it is created INSIDE OUT so the finished video can be
     renamed instead of copied across filesystems. Only this directory is ever
     removed, so nothing the caller already had in OUT is touched. */
  const raw = mkdtempSync(join(OUT, 'video-intermediates-'))
  const videoCtx = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    recordVideo: { dir: raw, size: { width: 1440, height: 900 } },
  })
  await load(videoCtx)
  await pushCard(THREE, 'ask-motion')
  await page.waitForTimeout(700)
  await page.getByText(THREE[0].options[0].label).first().click()
  await page.waitForTimeout(1600)
  await videoCtx.close()                       // flushes the webm to disk

  const webm = join(OUT, 'answer-hand-off.webm')
  const src = readdirSync(raw).find(f => f.endsWith('.webm'))
  if (!src) throw new Error('no video written: the recorded pass captured nothing')
  renameSync(join(raw, src), webm)
  console.log('wrote', webm)

  const ff = args => spawnSync('ffmpeg', ['-y', ...args], { stdio: 'ignore' }).status === 0
  const slow = join(OUT, 'answer-hand-off-slow.gif')
  const pal = join(raw, 'palette.png')
  const filters = 'setpts=3*PTS,fps=16,scale=1000:-1:flags=lanczos'
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
