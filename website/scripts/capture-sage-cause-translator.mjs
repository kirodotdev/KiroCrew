/**
 * Screenshot + assertion harness: Code Review Sage's failure causes.
 *
 * The run failure card translates the driver's own wording into a sentence a
 * reader can act on. PR #7240 gave the backend four new failure strings and the
 * translator had a branch for none of them, so each rendered as raw backend
 * English. This proves the branches now render, and — the part a screenshot alone
 * would not — that the ONE message which must still pass through verbatim does.
 *
 * Runs the REAL built SPA (website/dist) behind the shared `serveDist` server,
 * with every /api/** call answered from fixtures through the shared
 * `stubDashboardApi` helper - so this file carries only the Sage routes the scene
 * needs, not another copy of the dashboard's boot endpoints. No gateway, no
 * kiro-cli.
 *
 * Every fixture `error` below is a string the backend really emits, quoted from
 * its source:
 *   - review_pool.py::runtime_preflight  — the two things the preflight can find
 *     missing on a host.
 *   - review_driver.py::run_review       — the per-change wording for a record
 *     that was written but never completed.
 *   - routes.py::_first_change_error     — the run-level sentence each
 *     `skipped_reason` maps to.
 *   - acp/runtime.py                     — the missing-agent-spec message, which
 *     also names kiro-cli and already carries its own repair command. It is in
 *     the scene deliberately: it is what a broader /kiro-cli/ branch would have
 *     swallowed.
 *
 * Two captures:
 *   1. list-causes — the reviews list, one card per cause. Asserts each card
 *      shows its translated sentence and that the agent-spec card still shows
 *      the backend text.
 *   2. detail-notice — the same run open in the detail pane, where the notice
 *      shows the sentence AND the driver's raw wording under it.
 *
 * Output filenames are the ones committed under
 * `temp-screenshots/sage-cause-translator/`, so re-running this after a change
 * lands on the reviewed files instead of a parallel set someone has to copy.
 *
 * Usage: node scripts/capture-sage-cause-translator.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/sage-cause-translator'

mkdirSync(OUT, { recursive: true })

const SAGE = '/api/apps/code-review-sage'

/** The backend strings under test, newest cause first.
 *
 *  `expect` is a fragment of the ENGLISH catalog value the card must render;
 *  `verbatim: true` marks the one case whose backend text must survive
 *  untranslated. */
const CAUSES = [
  {
    id: 'no-agent-cli',
    pr: 41,
    error: 'the reviewer cannot run: no kiro-cli executable was found on this host'
      + ' (the reviewer session is driven by kiro-cli — install it or add it to PATH)',
    expect: 'no kiro-cli executable was found on this host. Install it, or add it to PATH.',
  },
  {
    id: 'runtime-not-importable',
    pr: 42,
    error: 'the reviewer cannot run: the ACP runtime (kiro_crew.acp.runtime) is not'
      + ' importable in this install',
    expect: 'this install cannot load the agent runtime it needs',
  },
  {
    id: 'runtime-unavailable',
    pr: 43,
    error: 'the reviewer never ran: its agent runtime is unavailable on this host',
    // Deliberately includes the localized OPENING, not just the tail: the tail
    // alone is a substring of the backend string above, so an untranslated card
    // would satisfy it.
    expect: 'Reviewer never started — its agent runtime is unavailable on this host.',
  },
  {
    id: 'record-incomplete',
    pr: 44,
    error: 'review wrote a result record but never completed the review',
    expect: 'Reviewer wrote a findings record but stopped before completing the review.',
  },
  {
    id: 'agent-spec-verbatim',
    pr: 45,
    error: "Agent spec 'code-review-sage-reviewer' is not installed: kiro-cli found no"
      + " 'code-review-sage-reviewer.json' in /home/u/.kiro/agents. Every turn fails"
      + ' until it is restored — repair with `kirocrew setup --agent-only --clean`,'
      + ' then restart the gateway.',
    expect: 'repair with `kirocrew setup --agent-only --clean`',
    verbatim: true,
  },
  {
    // The reword case. This sentence is NOT one the backend emits -- it is what
    // any of the cases above becomes the day someone rewords it, and no prose
    // branch matches it. Before the run payload carried the cause as a token this
    // card showed exactly this raw string; it now translates, because the token
    // travels beside the sentence. Keeping it in the scene makes the reword class
    // visible instead of only unit-tested.
    id: 'reworded-cause-keyed-by-token',
    pr: 46,
    error: 'the reviewer stopped before it finished',
    reason: 'review_record_incomplete',
    expect: 'Reviewer wrote a findings record but stopped before completing the review.',
  },
]

const runFor = (cause, index) => {
  const url = `https://github.com/example-org/example-service/pull/${cause.pr}`
  const changeId = `example-org_example-service_${cause.pr}`
  // Staggered so the list order is the CAUSES order (the list sorts by recency)
  // and the reader can match a card to the case it demonstrates.
  const started = Date.now() - (index + 1) * 600_000
  return {
    run_id: `run-${cause.id}`,
    repo: 'example-org/example-service',
    changes: [url],
    change_ids: [changeId],
    status: 'error',
    started_at: new Date(started).toISOString(),
    finished_at: new Date(started + 30_000).toISOString(),
    error: cause.error,
    // `reason` is the backend's cause TOKEN, sent beside the sentence. Only the
    // reword fixture carries one; the rest deliberately omit it, so they keep
    // proving the prose branches on their own -- which is also the shape of every
    // run already stored on disk.
    ...(cause.reason ? { reason: cause.reason } : {}),
    progress: {
      [changeId]: {
        phase: 'failed', error: cause.error,
        ...(cause.reason ? { reason: cause.reason } : {}),
      },
    },
    summary: { red: 0, yellow: 0, green: 0 },
    report_slug: null,
  }
}

const RUNS = CAUSES.map(runFor)

/** The Sage-app routes this scene needs. Everything the DASHBOARD needs to boot
 *  comes from the shared `stubDashboardApi`, which exists precisely so each
 *  harness does not carry its own copy of those ~25 endpoints. */
const SAGE_FIXTURES = {
  [`${SAGE}/runs`]: { runs: RUNS, pool: null, reviewer: { model: 'auto', effort: 'high' } },
  [`${SAGE}/repos`]: { repos: [{ owner: 'example-org', repo: 'example-service' }] },
  [`${SAGE}/repo-prs`]: { repo: 'example-org/example-service', prs: [], count: 0 },
  [`${SAGE}/settings`]: {
    settings: { model: null, effort: 'high', active_namespaces: ['default'], max_concurrent: 2 },
    models: [], efforts: ['low', 'high'], namespaces: ['default'], reviewer: null,
  },
  [`${SAGE}/namespaces`]: { namespaces: [{ name: 'default', count: 0 }] },
  [`${SAGE}/learnings`]: { namespace: 'default', learnings: [] },
}

/** Consulted BEFORE the shared stub's own map; a truthy return marks the request
 *  handled. Each branch awaits `json()` then returns true. */
const sageRoutes = async (path, route) => {
  if (path in SAGE_FIXTURES) {
    await json(route, SAGE_FIXTURES[path])
    return true
  }
  const run = RUNS.find(r => path === `${SAGE}/runs/${r.run_id}`)
  if (run) {
    await json(route, { run })
    return true
  }
  // A failed run has no report; the detail pane must render its own empty state
  // rather than a loading skeleton that would sit in the frame.
  if (/\/report$/.test(path)) {
    await json(route, { run_id: '', status: 'error', ready: false, rows: [], total: 0 })
    return true
  }
  return false
}

const uiState = runId => ({
  v: 1,
  state: {
    mainView: 'reviews',
    listTab: 'reviews',
    activeRepo: { owner: 'example-org', repo: 'example-service' },
    selectedRunId: runId,
    selectedPr: null,
    detailTab: null,
  },
})

async function preparePage(context, { selectedRunId = null } = {}) {
  const page = await context.newPage()
  logPageProblems(page)
  // The repo and the Reviews tab are always seeded - without them the app opens
  // on its "pick a repo" shell and no card is on screen to photograph. Only the
  // SELECTION varies: null keeps the list capture on the list. The language is
  // pinned too, or the SPA negotiates one from the environment and the shot comes
  // out in whichever locale the runner offers.
  //
  // Seeded THROUGH the shared stub rather than via our own addInitScript:
  // Playwright does not order separately registered init scripts, so our writes
  // would race that stub's localStorage.clear().
  await stubDashboardApi(page, {
    theme: 'light',
    extra: sageRoutes,
    localStorageEntries: {
      'mc-lang': 'en',
      'kc-onboarded': '1',
      'kc:code-review-sage:ui-state': JSON.stringify(uiState(selectedRunId)),
    },
  })
  await page.goto(`${BASE}/code-review-sage`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  return page
}

/** Every failure line the page renders, as text. Found from the app's own DOM
 *  rather than from class names, so the assertion survives a restyle. */
const failureLines = page => page.evaluate(() => [...document.querySelectorAll('div')]
  .map(el => (el.textContent || '').trim())
  .filter(t => t && t.length < 400))

const { srv, base } = await serveDist()
const BASE = base

async function main() {
  // This host's node exports its own LD_LIBRARY_PATH; a browser child that
  // inherits it loads an older libstdc++ and dies before opening a page.
  const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '' } })
  const context = await browser.newContext({
    viewport: { width: 1480, height: 1000 }, deviceScaleFactor: 2,
  })

  // ---- 1. The list: one card per cause ----
  const page = await preparePage(context)
  // Shot FIRST, assertions after: a run against code that does not translate
  // these causes still leaves a frame to look at, which is how the committed
  // `0-before-*` frame was taken (this same script, on the pre-fix translator,
  // renamed). Assertions after a written file cost nothing.
  await page.screenshot({ path: `${OUT}/1-after-list-causes.png` })
  const lines = await failureLines(page)
  const missing = CAUSES.filter(c => !lines.some(t => t.includes(c.expect)))
  if (missing.length) {
    console.log('rendered candidates:', JSON.stringify(lines.slice(0, 40), null, 1))
    throw new Error(`cards did not render: ${missing.map(c => c.id).join(', ')}`)
  }
  // The translated cards must NOT be showing the backend text as their headline
  // sentence — that is the defect this change fixes, and a card can only be
  // judged translated if the raw wording is absent from it.
  for (const cause of CAUSES.filter(c => !c.verbatim)) {
    const cardText = lines.find(t => t.includes(cause.expect) && t.length < 300) || ''
    if (cardText.includes(cause.error.slice(0, 40))) {
      throw new Error(`${cause.id} card still shows the backend wording: ${cardText}`)
    }
  }
  // The check above finds a card BY its expected sentence, so it cannot see a
  // regression on a cause whose sentence another fixture also renders: the
  // `find` returns the sibling's healthy card and the broken one is never
  // examined. Two fixtures here do share a sentence on purpose (one reaches it
  // through prose, one through the token), so assert the complement as well --
  // no cause's raw backend wording may appear ANYWHERE in the list. Untranslated
  // text is a defect wherever it surfaces, so the weaker per-card form was never
  // the property worth checking.
  for (const cause of CAUSES.filter(c => !c.verbatim)) {
    const fingerprint = cause.error.slice(0, 40)
    const leaked = lines.find(t => t.includes(fingerprint))
    if (leaked) {
      throw new Error(
        `${cause.id} rendered untranslated backend wording: ${leaked}`)
    }
  }
  console.log(`OK list: ${CAUSES.length} causes render, each with its own sentence`)

  // ---- 2. The detail notice: sentence + the driver's own wording ----
  const detailCtx = await browser.newContext({
    viewport: { width: 1480, height: 1000 }, deviceScaleFactor: 2,
  })
  const incomplete = CAUSES.find(c => c.id === 'record-incomplete')
  const detail = await preparePage(detailCtx, { selectedRunId: `run-${incomplete.id}` })
  await detail.screenshot({ path: `${OUT}/2-after-detail-notice.png` })
  const detailLines = await failureLines(detail)
  if (!detailLines.some(t => t.includes(incomplete.expect))) {
    throw new Error('the detail notice does not carry the translated sentence')
  }
  if (!detailLines.some(t => t.includes(incomplete.error))) {
    throw new Error('the detail notice dropped the driver\'s raw wording')
  }
  console.log('OK detail: the notice shows the sentence and keeps the raw wording')

  await detailCtx.close()
  await context.close()
  await browser.close()
  srv.close()
  console.log('done →', OUT)
}

main().catch(err => { console.error(err); srv.close(); process.exit(1) })
