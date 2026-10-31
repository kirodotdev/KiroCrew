// Render benchmark: counts React commits, component renders, store dispatches
// and selector calls while the SPA handles synthetic streaming traffic, and
// optionally checks them against budgets. Gateway-free: the React profiling
// build (scripts/vite.profiling.config.mjs) is served by serve-dist, /api/** is
// answered by stub-dashboard-api with a seeded 222-session world
// (lib/perf-fixture.mjs), and the dashboard websocket is mocked so the script
// can push gateway-shaped frames into the page.
//
// Scenarios (open slot / streaming), each a fixed number of 100 ms ticks:
//   idle   perf-long, 100 rows loaded   nothing streams
//   s1x1   perf-long                    1 background slot, agentic mix
//   s1x5   perf-long                    5 background slots, agentic mix
//   s2     perf-stream                  the open slot streams a prose answer
// The agentic mix per slot per second: 10 chunks, 2 status changes, one tool
// call with its row and result, and an assistant row every 5 s.
//
// Counts, not milliseconds, are the gated numbers: one scenario sends the same
// frames every run, so dispatches are fixed and commits / renders / selector
// calls move only when the code's render behaviour does. A slower host batches
// more frames per commit, which lowers counts, so a budget is an upper bound.
//
// Usage (from website/):
//   node ./node_modules/vite/bin/vite.js build --config scripts/vite.profiling.config.mjs
//   node scripts/bench-render.mjs                          # all scenarios, 3 runs each
//   node scripts/bench-render.mjs --scenario s2 --runs 1 --why ChatPage,ChatInput
//   node scripts/bench-render.mjs --check perf-budgets.json   # exit 1 over budget
//
// Flags: --scenario a,b  --runs N  --ticks N (default 300 = 30 s)
//   --viewport desktop|mobile  --cpu N (CDP throttle during the window)
//   --why A,B  (classify why each render of these components happened)
//   --check FILE  --out DIR (default $KIROCREW_SCRATCH/bench-render or /tmp)
import { chromium, devices } from 'playwright'
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { PROFILING_DIST } from './lib/profiling-dist.mjs'
import { renderProbe } from './lib/render-probe.mjs'
import {
  ACTIVE_LONG, ACTIVE_STREAM, agenticTurn, buildWorld, doneFrame, fillerKey, proseAnswer,
} from './lib/perf-fixture.mjs'

export const SCENARIOS = {
  idle: { active: ACTIVE_LONG, streaming: [] },
  s1x1: { active: ACTIVE_LONG, streaming: [ACTIVE_STREAM] },
  s1x5: { active: ACTIVE_LONG, streaming: [ACTIVE_STREAM, fillerKey(0), fillerKey(1), fillerKey(2), fillerKey(3)] },
  s2: { active: ACTIVE_STREAM, streaming: [ACTIVE_STREAM], prose: true },
}
/** The counters a budget file may cap. */
export const GATED = ['commits', 'renders', 'dispatches', 'selectorCalls']

const args = {}
process.argv.slice(2).forEach((a, i, arr) => {
  if (a.startsWith('--')) args[a.slice(2)] = arr[i + 1] && !arr[i + 1].startsWith('--') ? arr[i + 1] : true
})
const scenarios = String(args.scenario || Object.keys(SCENARIOS).join(',')).split(',')
const RUNS = Number(args.runs || 3)
const TICKS = Number(args.ticks || 300)
const VIEWPORT = args.viewport || 'desktop'
const CPU = Number(args.cpu || 1)
const WHY = args.why && args.why !== true ? String(args.why).split(',') : []
const OUT = resolve(args.out || join(process.env.KIROCREW_SCRATCH || '/tmp', 'bench-render'))
const SETTLE_MS = Number(process.env.SETTLE_MS || 3000)

for (const s of scenarios) if (!SCENARIOS[s]) throw new Error(`unknown scenario ${s}; one of ${Object.keys(SCENARIOS).join(', ')}`)
if (!existsSync(join(PROFILING_DIST, 'index.html'))) {
  console.error(`No profiling build at ${PROFILING_DIST}.\nBuild it first: node ./node_modules/vite/bin/vite.js build --config scripts/vite.profiling.config.mjs`)
  process.exit(2)
}
mkdirSync(OUT, { recursive: true })

const median = (xs) => {
  const s = [...xs].sort((a, b) => a - b)
  return s.length % 2 ? s[(s.length - 1) / 2] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2
}
const top = (o, n) => Object.fromEntries(Object.entries(o).sort((a, b) => b[1] - a[1]).slice(0, n))

async function runOnce(browser, base, name) {
  const scen = SCENARIOS[name]
  const world = buildWorld()
  const ctxOpts = VIEWPORT === 'mobile'
    ? { ...devices['iPhone 13'], viewport: { width: 390, height: 844 } }
    : { viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 }
  delete ctxOpts.defaultBrowserType
  const ctx = await browser.newContext(ctxOpts)
  await ctx.addInitScript(renderProbe, { why: WHY })
  const page = await ctx.newPage()
  logPageProblems(page)
  // A component that throws stops rendering, which lowers every counter, so a
  // broken page would pass its upper-bound budgets. Any page error or console
  // error fails the run; the fixture boots with none.
  const problems = []
  page.on('pageerror', (err) => problems.push(`pageerror: ${String(err).slice(0, 200)}`))
  page.on('console', (msg) => { if (msg.type() === 'error') problems.push(`console: ${msg.text().slice(0, 200)}`) })
  let ws = null
  await stubDashboardApi(page, {
    slots: world.slots,
    folders: world.folders,
    onWebSocket: (route) => { ws = route },
    extra: async (path, route) => {
      const m = path.match(/^\/api\/chat\/slots\/([^/]+)$/)
      if (m && route.request().method() === 'GET') { await json(route, world.detail(decodeURIComponent(m[1]))); return true }
      if (path === '/api/apps') { await json(route, world.apps); return true }
      return false
    },
  })
  const cdp = await ctx.newCDPSession(page)
  await cdp.send('Performance.enable')
  await page.goto(`${base}/chat?sid=${scen.active}`, { waitUntil: 'load' })
  // Wait on the store, not the sidebar DOM: on mobile the sidebar is a closed
  // drawer, so no session row is mounted.
  await page.waitForFunction(() => {
    const s = window.__kcStore?.getState()
    return s && s.dashboard.slots.length > 0 && s.chat.messages.length > 0
  }, null, { timeout: 30000 })
  if (!ws) throw new Error('the dashboard never opened its websocket')
  // The gateway pushes a status frame every 5 s, and the client's silence
  // watchdog (WS_SILENCE_MS) reconnects a socket that stays quiet for 20 s. The
  // tick loop sends it on the same 5 s cadence, so every run gets the same count.
  let uptime = 120
  const status = () => ws.send(JSON.stringify({ type: 'dashboard', data: { sessions: world.slots.length, crons: 0, lessons: 0, uptime: (uptime += 5), version: '0.5.0' } }))
  status()
  await page.waitForTimeout(SETTLE_MS)
  const loaded = await page.evaluate(() => {
    const s = window.__kcStore.getState()
    return { slots: s.dashboard.slots.length, activeRows: s.chat.messages.length }
  })

  if (CPU > 1) await cdp.send('Emulation.setCPUThrottlingRate', { rate: CPU })
  const metric = async (n) => (await cdp.send('Performance.getMetrics')).metrics.find((m) => m.name === n)?.value ?? 0
  const script0 = await metric('ScriptDuration')
  await page.evaluate(() => { window.__perf.counting = true })
  let clock = Date.now()
  const tick = () => (clock += 100)
  const plans = scen.prose ? [proseAnswer(scen.active, tick)] : scen.streaming.map((s, k) => agenticTurn(s, k, tick))
  let frames = 0
  // Paced from the wall clock so a slow tick does not shorten the next gap.
  const t0 = Date.now()
  for (let i = 0; i < TICKS; i++) {
    if (i % 50 === 0) status()
    for (const p of plans) for (const f of p.next().value) { ws.send(f); frames++ }
    const wait = t0 + (i + 1) * 100 - Date.now()
    if (wait > 0) await new Promise((r) => setTimeout(r, wait))
  }
  // Read before chat_done: the done frame retires the open slot's streaming row.
  const streamChars = await page.evaluate(() => {
    const live = window.__kcStore.getState().chat.messages.filter((m) => m.role === 'streaming')
    return live.reduce((a, m) => a + (m.content?.length ?? 0), 0)
  })
  for (const s of scen.streaming) ws.send(doneFrame(s))
  await page.waitForTimeout(1500)
  const perf = await page.evaluate(() => {
    const P = window.__perf
    P.counting = false
    const s = window.__kcStore.getState()
    const bg = Object.values(s.chat.slotMessages || {}).reduce((a, v) => a + (v ? v.length : 0), 0)
    return {
      commits: P.commits, renders: P.renders, dispatches: P.dispatches, selectorCalls: P.sel,
      longTasks: P.long.length, longTaskMs: P.long.reduce((a, b) => a + b, 0),
      disp: P.disp, comp: P.comp, why: P.why, roots: P.roots, setState: P.setState,
      err: P.err, profiling: !!P.profiling,
      slotMessagesRows: bg, activeRows: s.chat.messages.length,
    }
  })
  const scriptMs = Math.round((await metric('ScriptDuration') - script0) * 1000)
  await ctx.close()

  // A run whose frames never reached the store would pass any budget; refuse it.
  if (!perf.profiling) throw new Error('react-dom/profiling hooks missing: is this the profiling build?')
  if (perf.err) throw new Error(`commit walk failed: ${perf.err}`)
  if (problems.length) throw new Error(`the page reported ${problems.length} error(s), first: ${problems[0]}`)
  // Frames land in the store batched per animation frame, and the action that
  // carries them is an implementation detail, so check for any dispatch here and
  // let the store-state checks below prove the frames arrived.
  if (scen.streaming.length && !perf.dispatches) throw new Error(`no dispatches for ${frames} frames: the frames did not reach the store`)
  if (scen.prose && streamChars < TICKS * 30) throw new Error(`the streamed answer reached only ${streamChars} chars`)
  if (!scen.prose && scen.streaming.length && perf.slotMessagesRows === 0) throw new Error('no background rows reached chat.slotMessages')
  return { scenario: name, loaded, frames, scriptMs, streamChars, ...perf }
}

const { srv, base } = await serveDist(PROFILING_DIST)
const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || undefined })
const results = {}
try {
  for (const name of scenarios) {
    const runs = []
    for (let r = 1; r <= RUNS; r++) {
      const res = await runOnce(browser, base, name)
      runs.push(res)
      console.log(`[${name} r${r}] ${GATED.map((k) => `${k}=${res[k]}`).join(' ')} script=${res.scriptMs}ms long=${res.longTasks}`)
    }
    const med = Object.fromEntries([...GATED, 'scriptMs', 'longTasks'].map((k) => [k, median(runs.map((x) => x[k]))]))
    const last = runs[runs.length - 1]
    results[name] = {
      median: med,
      runs: runs.map((x) => Object.fromEntries([...GATED, 'scriptMs', 'longTasks', 'frames'].map((k) => [k, x[k]]))),
      loaded: last.loaded,
      topRenders: top(Object.fromEntries(Object.entries(last.comp).map(([k, v]) => [k, v.n])), 25),
      dispatchesByType: top(last.disp, 25),
      commitRoots: top(last.roots, 15),
      setState: top(last.setState, 15),
      why: last.why,
    }
  }
} finally {
  await browser.close()
  srv.close()
}

const report = { viewport: VIEWPORT, cpu: CPU, ticks: TICKS, runs: RUNS, chromium: browser.version(), scenarios: results }
writeFileSync(join(OUT, 'bench-render.json'), JSON.stringify(report, null, 2))
console.log('\nscenario  ' + GATED.map((k) => k.padStart(14)).join(''))
for (const [name, r] of Object.entries(results)) console.log(name.padEnd(10) + GATED.map((k) => String(r.median[k]).padStart(14)).join(''))
console.log(`\nreport: ${join(OUT, 'bench-render.json')}`)

if (args.check) {
  const budgets = JSON.parse(readFileSync(resolve(String(args.check)), 'utf8'))
  const tol = budgets.tolerance ?? 0.15
  const over = []
  for (const [name, r] of Object.entries(results)) {
    const b = budgets.scenarios?.[name]
    if (!b) continue
    for (const k of GATED) {
      if (b[k] == null) continue
      const limit = Math.floor(b[k] * (1 + tol))
      const v = r.median[k]
      const tag = v > limit ? 'OVER' : v < b[k] * (1 - tol) ? 'under (lower the budget)' : 'ok'
      console.log(`${name}.${k}: ${v} against budget ${b[k]} (limit ${limit}) ${tag}`)
      if (v > limit) over.push(`${name}.${k}`)
    }
  }
  if (over.length) {
    console.error(`\nOver budget by more than ${Math.round(tol * 100)}%: ${over.join(', ')}.`
      + '\nIf the extra renders are intended, raise the budget in the same PR and say why.')
    process.exit(1)
  }
}
