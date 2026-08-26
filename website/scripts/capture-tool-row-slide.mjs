/**
 * Screenshot + video + per-frame MEASUREMENT harness for the tool-row slide.
 *
 * Runs the REAL built SPA (website/dist) behind a static server with `/api/**`
 * answered from fixtures and `/api/ws` bound by Playwright, so the turn advances
 * exactly the way the backend advances it: a `chat_message` carrying the tool
 * bubble, a `tool_call` frame that puts the row in flight (`is_shell`, which is
 * what shows the "Running · Ns" line), then a `tool_result` that completes it.
 *
 * The defect is a MOTION defect — a row mounting or its status line unmounting
 * moved everything above it in a single frame — so a still cannot show it. Two
 * kinds of evidence are produced instead:
 *
 *   1. A video of the whole sequence, for the eyes.
 *   2. A per-frame trace of a REFERENCE row's viewport Y, sampled on every
 *      animation frame across each transition. The largest single-frame step in
 *      that trace is the number that matters: one big step is a teleport, a run
 *      of small steps is a slide.
 *
 * Run it twice — once against a dist built from the base ref, once from the fix —
 * and the pair of traces is the before/after evidence.
 *
 * Usage: node scripts/capture-tool-row-slide.mjs <outDir> [label]
 */
import { chromium } from 'playwright'
import { mkdirSync, renameSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/tool-row-slide'
const LABEL = process.argv[3] || 'after'
const SLOT = 'chat-tool-row-slide'
const PROJECT = '/home/user/workspace/demo-app'
const VIEW = { width: 1180, height: 720 }

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const slots = [{
  key: SLOT,
  title: 'Reinstall the toolchain in the container',
  running: true,
  last_message: 'The shared node_modules is largely root-owned.',
  messages: 5,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: now,
  source_links: [],
  source_links_total: 0,
}]

/** A tool bubble as the backend persists one: 🔧 + label, id on the meta. */
const tool = (id, label, purpose, ts) => ({
  role: 'tool',
  ts,
  content: `🔧 ${label}`,
  meta: { tool_call_id: id, purpose, output: 'done' },
})

// Scene: a turn already several steps in, with enough scrollback ABOVE it to
// overflow the viewport. The overflow is essential, not decorative — the defect
// only exists because the transcript is pinned to its bottom, so a shorter
// transcript grows downward into empty space and nothing moves at all.
const filler = [
  ['Set up the container toolchain for this worktree.', 'Host Node is 16 and the package floor is 22, so every install and test run goes through the container. I will keep the host untouched.'],
  ['Where do the dependencies come from?', 'The lockfile is identical to a sibling worktree, so the fastest path is reusing its install rather than downloading the tree again.'],
  ['Try that first then.', 'Attempting a hardlinked copy: same inodes, no extra disk, and it lands in seconds instead of minutes.'],
].flatMap(([q, a], i) => [
  { role: 'user', ts: now - 400 + i * 40, content: q },
  { role: 'assistant', ts: now - 390 + i * 40, content: a },
])

const detail = {
  running: true,
  has_more: false,
  total: filler.length + 4,
  queue: [],
  project: PROJECT,
  messages: [
    ...filler,
    { role: 'user', ts: now - 90, content: 'The worktree needs its own toolchain — set it up and keep me posted.' },
    tool('tc-a', 'Inspect the sibling worktree', 'Give the worktree a writable hardlinked node_modules', now - 70),
    {
      role: 'assistant',
      ts: now - 55,
      content: 'The shared `node_modules` in a sibling worktree is largely root-owned, so hardlinking into it is refused. Falling back to a clean install in the container.',
    },
    tool('tc-b', 'Reset the install directory', 'Start a clean npm ci in the Node 22 container', now - 30),
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: VIEW,
    deviceScaleFactor: 2,
    recordVideo: { dir: OUT, size: VIEW },
  })
  const page = await context.newPage()

  let wsServer = null
  await page.routeWebSocket(/\/api\/ws/, ws => { wsServer = ws })

  const fixedApi = makeFixedApi(PROJECT)
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/chat/slots/')) return json(route, detail)
    return handleBootRoute(route, path, { project: PROJECT, theme: 'light', fixedApi })
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => {
    if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300))
  })

  await page.addInitScript(([s]) => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot-chat', s)
  }, [SLOT])
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  // The pill's visible text is the agent's PURPOSE, not the raw tool label:
  // `simplifiedToolNames` defaults on, so waiting on the label would time out.
  await page.waitForSelector('text=Start a clean npm ci in the Node 22 container', { timeout: 20000 })
  await page.waitForTimeout(1500)
  if (!wsServer) throw new Error('websocket route never bound')

  const send = (type, data) => wsServer.send(JSON.stringify({ type, data: { slot: SLOT, ...data } }))
  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${LABEL}-${name}.png` })
    console.log('wrote', `${OUT}/${LABEL}-${name}.png`)
  }

  /**
   * Start sampling the reference row's viewport Y once per animation frame.
   *
   * The reference is the assistant paragraph mid-turn: it sits above everything
   * that changes, is on screen throughout, and is pushed upward by the
   * bottom-pinned transcript whenever content below it grows. Sampling in the
   * page (rAF) rather than from screenshots is what makes the single-frame step
   * size measurable at all.
   */
  const startProbe = () => page.evaluate(() => {
    const ref = Array.from(document.querySelectorAll('p, div'))
      .reverse()
      .find(el => el.textContent?.startsWith('The shared') && el.children.length <= 3)
    if (!ref) throw new Error('reference paragraph not found')
    const w = window
    // Seed the trace SYNCHRONOUSLY with the pre-transition position. Without
    // this baseline an instant jump is invisible to the trace: it completes
    // before the first animation frame runs, so every sample reads the final
    // position and the transition looks like it never moved.
    w.__trace = [[0, Math.round(ref.getBoundingClientRect().top * 100) / 100]]
    const t0 = performance.now()
    const tick = () => {
      const t = performance.now() - t0
      w.__trace.push([Math.round(t), Math.round(ref.getBoundingClientRect().top * 100) / 100])
      if (t < 2000) requestAnimationFrame(tick)
    }
    requestAnimationFrame(tick)
  })

  /** Stop sampling and reduce the trace to the numbers that decide it. */
  const readProbe = async name => {
    await page.waitForTimeout(2100)
    const trace = await page.evaluate(() => window.__trace)
    let maxStep = 0
    let moved = 0
    let framesMoved = 0
    for (let i = 1; i < trace.length; i++) {
      const step = Math.abs(trace[i][1] - trace[i - 1][1])
      if (step > 0.5) { framesMoved++; moved += step }
      if (step > maxStep) maxStep = step
    }
    const total = Math.abs(trace.at(-1)[1] - trace[0][1])
    const result = {
      transition: name,
      totalTravelPx: Math.round(total * 100) / 100,
      largestSingleFrameStepPx: Math.round(maxStep * 100) / 100,
      framesThatMoved: framesMoved,
      sumOfStepsPx: Math.round(moved * 100) / 100,
      samples: trace.length,
    }
    console.log(JSON.stringify(result))
    return { ...result, trace }
  }

  const traces = []

  await shot('01-before-new-row')

  // ── Transition 1: a new tool row arrives mid-turn (row mounts, in flight) ──
  await startProbe()
  send('chat_message', {
    role: 'tool',
    ts: new Date().toISOString(),
    content: '🔧 Watch the install finish',
    meta: { tool_call_id: 'tc-c', purpose: 'Check npm ci progress' },
  })
  send('tool_call', {
    tool: 'Watch the install finish',
    kind: 'execute',
    purpose: 'Check npm ci progress',
    input_preview: 'docker logs -f installer',
    tool_call_id: 'tc-c',
    is_shell: true,
  })
  traces.push(await readProbe('new tool row appears'))
  await shot('02-row-running')

  // Let the elapsed counter tick so the status line is unmistakably present.
  await page.waitForTimeout(2400)
  await shot('03-running-elapsed')

  // ── Transition 2: the tool completes, so the status line goes away ──
  await startProbe()
  send('tool_result', { output: 'added 948 packages in 9s', tool_call_id: 'tc-c' })
  traces.push(await readProbe('running status line disappears'))
  await shot('04-row-done')

  writeFileSync(join(OUT, `${LABEL}-trace.json`), JSON.stringify(traces, null, 2))
  console.log('wrote', join(OUT, `${LABEL}-trace.json`))

  // The entrance clips the row while its height eases, so the clip and the
  // inline height MUST be released when it ends — otherwise everything that
  // grows later is cut off at the entrance height. Expanding the row that just
  // animated is the check: its details panel is taller than the row was.
  // Click the PILL, addressed by its aria-label — the session card in the rail
  // shows the same purpose text, and clicking that re-selects the slot, which
  // refetches the fixture detail and drops the row this run just added.
  await page.click('button[aria-label*="Watch the install finish"]')
  await page.waitForTimeout(900)
  await shot('05-expanded-after-slide')
  const clipped = await page.evaluate(() => {
    const pill = Array.from(document.querySelectorAll('button[aria-label*="details for tool"]'))
      .find(b => b.textContent?.includes('Check npm ci progress'))
    // pill → the inline-flex label wrapper → the row container the entrance
    // animation owns.
    const row = pill?.parentElement?.parentElement
    const box = row?.getBoundingClientRect()
    return {
      rowHeightPx: Math.round(box?.height ?? 0),
      inlineHeight: (row instanceof HTMLElement && row.style.height) || '(none)',
      inlineOverflow: (row instanceof HTMLElement && row.style.overflow) || '(none)',
      outputVisible: row?.textContent?.includes('added 948 packages') ?? false,
    }
  })
  console.log('after-slide row state:', JSON.stringify(clipped))

  const video = page.video()
  await context.close()
  await browser.close()
  srv.close()

  if (video) {
    const src = await video.path()
    const dest = join(OUT, `${LABEL}-slide.webm`)
    if (src !== dest) renameSync(src, dest)
    console.log('wrote', dest)
  }
}

main().catch(err => { console.error(err); process.exit(1) })
