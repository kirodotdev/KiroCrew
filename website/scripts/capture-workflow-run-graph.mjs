/**
 * Screenshots of the Workflows graph mode (issue #11796, from #1652).
 *
 *   tree-before   the panel as it shipped: the run tree, which shows only what ran.
 *                 The graph must be ABSENT here, not merely empty.
 *   graph-mid     graph mode with the run inside "Write": "Ship" is a stage nothing has
 *                 entered, and its loop is a dashed marker beside dashed work.
 *   graph-loop    the same run after the loop produced three agents the plan drew as
 *                 one node — the prediction is replaced by reality, one of it failed.
 *   graph-no-plan a task-plan run: no plan is readable, and the graph says so.
 *   may-not-run   a stage the script only reaches under an `if`.
 *   not-planned   a stage the run entered that the plan never named.
 *   partial-plan  the previewer hit its ceiling, so the plan drawn is partial.
 *   empty         a readable plan that predicted nothing, with nothing run yet.
 *
 * Drives the ISOLATED capture entry (website/capture/workflow-run-graph.html). Each
 * scene asserts markers and the script EXITS NONZERO when one is missing, so it can
 * never quietly emit a screenshot of the wrong state.
 *
 *   npx vite --host 127.0.0.1 --port 6821 --strictPort      # in another shell
 *   node scripts/capture-workflow-run-graph.mjs http://127.0.0.1:6821 <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6821'
const OUT = process.argv[3] || '../temp-screenshots/workflow-run-graph'
mkdirSync(OUT, { recursive: true })

const SCENES = [
  {
    file: 'workflows-tree-before.png',
    scene: 'mid',
    graph: false,
    marker: '[data-testid="workflow-view-graph"]',
    alsoVisible: ['text=Research', 'text=Write'],
    // The tree cannot show a stage the run has not entered, which is the gap.
    absent: ['[data-testid="workflow-run-graph"]'],
  },
  {
    file: 'workflows-graph-planned-after.png',
    scene: 'mid',
    graph: true,
    marker: '[data-testid="workflow-run-graph"]',
    alsoVisible: [
      // Three stages including the one nothing has entered yet.
      '[data-testid="workflow-graph-phase"][data-phase-state="planned"]:has-text("Ship")',
      '[data-testid="workflow-graph-node"][data-node-state="unknown"]:has-text("for")',
      'text=shape decided at run time',
      '[data-testid="workflow-graph-node"][data-node-state="planned"]:has-text("file")',
      '[data-testid="workflow-graph-node"][data-node-state="ran_ok"]:has-text("spec")',
      '[data-testid="workflow-graph-node"][data-node-state="running"]:has-text("draft")',
    ],
    absent: ['[data-testid="workflow-graph-no-plan"]'],
    counts: { '[data-testid="workflow-graph-phase"]': 3 },
  },
  {
    file: 'workflows-graph-materialized-after.png',
    scene: 'loop',
    graph: true,
    marker: '[data-testid="workflow-graph-node"][data-node-state="ran_failed"]',
    alsoVisible: [
      // The SAME marker as the previous frame, now answered. This is what lets a reader
      // tell "the one box became three" from "three boxes replaced it": the marker is
      // still there and now says how many ran.
      '[data-testid="workflow-graph-node"][data-node-state="unknown"][data-node-resolved="3"]:has-text("for")',
      'text=ran 3',
      'text=file: spec gap',
      'text=file: diff gap',
      'text=file: release note',
    ],
    // The single predicted "file" node must be GONE: keeping it beside the three real
    // ones would claim it is one of them. The marker must no longer read as waiting.
    absent: [
      '[data-testid="workflow-graph-node"][data-node-state="planned"]',
      'text=shape decided at run time',
    ],
  },
  {
    file: 'workflows-graph-no-plan-after.png',
    scene: 'noplan',
    graph: true,
    marker: '[data-testid="workflow-graph-no-plan"]',
    alsoVisible: ['text=sort the queue'],
    // Nothing may read "not in the plan" here: with no plan there is nothing to have
    // missed this work, and the caption would report the preview as wrong.
    absent: ['[data-testid="workflow-graph-empty"]', 'text=not in the plan'],
  },
  {
    file: 'workflows-graph-may-not-run.png',
    scene: 'gated',
    graph: true,
    marker: '[data-testid="workflow-graph-phase"][data-phase-certain="no"]:has-text("Announce")',
    alsoVisible: ['text=may not run'],
  },
  {
    file: 'workflows-graph-not-planned.png',
    scene: 'surprise',
    graph: true,
    marker: '[data-testid="workflow-graph-phase"][data-phase-predicted="no"]:has-text("Hotfix")',
    alsoVisible: ['text=not in the plan', 'text=patch the build'],
  },
  {
    file: 'workflows-graph-partial-plan.png',
    scene: 'truncated',
    graph: true,
    marker: '[data-testid="workflow-graph-truncated"]',
    // Both halves: the banner says the drawing is partial AND that nothing which runs
    // is hidden by it. Asserting only the first half would pass a banner that leaves a
    // reader hunting for the missing part.
    alsoVisible: ['text=too large to draw fully', 'text=still appears below'],
    // The old wording named the previewer's ceiling, which a first-time reader cannot
    // parse. It must not come back.
    absent: ['text=stopped at its ceiling'],
  },
  {
    file: 'workflows-graph-empty.png',
    scene: 'empty',
    graph: true,
    marker: '[data-testid="workflow-graph-empty"]',
    alsoVisible: ['text=Nothing to draw yet.'],
    absent: ['[data-testid="workflow-run-graph"]'],
  },
  {
    file: 'workflows-graph-detail-failed.png',
    scene: 'detailfail',
    graph: false,
    noDetail: true,
    // The shared error surface, not a hand-written box: the hand-off control is what
    // tells the two apart, so the frame has to carry it.
    marker: 'text=Ask the agent',
  },
]

const b = await chromium.launch()
let failed = 0
for (const s of SCENES) {
  const page = await b.newPage({ viewport: { width: 1200, height: 900 } })
  const noise = []
  page.on('console', m => {
    if (m.type() === 'error') noise.push(m.text().slice(0, 300))
  })
  page.on('pageerror', e => noise.push('pageerror: ' + String(e).slice(0, 300)))
  const url = `${BASE}/capture/workflow-run-graph.html?scene=${s.scene}&theme=dark&lang=en`
  try {
    await page.goto(url, { waitUntil: 'networkidle' })
    // A run has to be selected before the panel has a detail to draw, exactly as a
    // reader would: the list is the left column, the run detail the right.
    await page.click('ul li [role="button"]', { timeout: 15000 })
    // A scene whose detail read FAILS renders no Tree/Graph toggle, because there is no
    // detail to switch views over. Waiting for it there would time out on a frame that
    // is exactly the state being captured.
    if (!s.noDetail) {
      await page.waitForSelector('[data-testid="workflow-view-graph"]', { timeout: 15000 })
      if (s.graph) await page.click('[data-testid="workflow-view-graph"]')
    }
    await page.waitForSelector(s.marker, { timeout: 15000 })
    for (const sel of s.alsoVisible || []) await page.waitForSelector(sel, { timeout: 15000 })
    for (const sel of s.absent || []) {
      if (await page.locator(sel).count()) throw new Error(`expected ${sel} absent in ${s.scene}`)
    }
    for (const [sel, want] of Object.entries(s.counts || {})) {
      const got = await page.locator(sel).count()
      if (got !== want) throw new Error(`expected ${want} of ${sel}, found ${got}`)
    }
    await page.screenshot({ path: `${OUT}/${s.file}`, fullPage: true })
    console.log(`ok   ${s.file}`)
  } catch (err) {
    failed += 1
    console.error(`FAIL ${s.file}: ${(err && err.message) || err}`)
    for (const line of noise.slice(0, 6)) console.error(`     page: ${line}`)
  }
  await page.close()
}
await b.close()
process.exit(failed ? 1 : 0)
