/**
 * Screenshot harness for the System > Services "Tasks & capacity" card.
 *
 * Runs the REAL built SPA (website/dist) behind the shared static server with
 * every /api/** call answered by the shared boot stub — no gateway, no token.
 * Only `/api/tasks/summary` is scene-specific. Each scene exists because some
 * copy or layout the UX lane judges is unreachable from the others: the empty
 * and populated pair for a lane AT its ceiling beside one BELOW it, the "Open
 * chat" action on a row blocked on the user and "Retrying now" vs "Waiting to
 * retry"; `degraded` for the mapped degrade reason in the "Degrade reason" ROW —
 * the home it takes when the controller lowered BOTH lanes, which is the ordinary
 * case — and `degraded-one-lane` for the OTHER home of that same reason, the lane
 * line it moves onto when exactly one lane is under its ceiling (both branches
 * exist, so a frame of only one leaves half the rule unphotographed); `stalled`
 * for the red badge, the row the count points at, and no degrade row beside it;
 * `folded` for the "Show N more" control past `WAIT_ROWS_SHOWN` and `expanded` for
 * what that control opens; `narrow` for the 320px stack and `narrow-folded` for
 * that stack at the height a busy queue actually reaches; and `no-store` / `error`
 * for the two arms that render a notice instead of a queue — the first with the
 * health badge WITHHELD, since no verdict stands on unrecorded numbers, and the
 * second with a body that carries no readable reason, which is what the sentence
 * has to survive.
 *
 * Every shot is the card element alone, at 1x, so they stay small and match
 * the earlier evidence frames at the same paths. `frameCard` is what makes each
 * one READABLE — see its own header: a frame that hides the copy under review
 * costs a review round and proves nothing, so this harness refuses to write one.
 *
 * Usage: node scripts/capture-tasks-capacity.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/overload-resilience'
mkdirSync(OUT, { recursive: true })

const NOW = 1_757_800_000
const AGE = 15 * 60 + 55

/** Shared skeleton; the store IS available so zeros read as facts. */
const base = {
  generated_at: NOW,
  available: true,
  depth: { by_state: {}, queued: 0, waiting: 0, recovering: 0, running: 0, total: 0 },
  oldest_wait_secs: 0,
  // One lane BELOW the operator's ceiling and one AT it: the two capacity lines
  // say different things, and only a payload holding both shows the pair.
  lanes: {
    subagents: { effective: 4, user_max: 14, running: 0 },
    spawn_gate: { effective: 8, user_max: 8 },
  },
  degrade_reason: null,
  adaptive: null,
  slots: [],
  waiting: [],
  recovering: { tasks: [], task_attempts: 0, slots: [], ladder: [] },
  stalled: {},
  counts: {},
  stall_after_secs: 600,
}

function row(id, state, extra = {}) {
  return {
    id, kind: 'workflow_agent', state, lane: 'chat-1', session_key: 'dashboard:chat-1',
    parent_id: null, root_id: id, attempts: 1, generation: 1, next_run_at: null,
    deadline_at: null, lease_owner: null, lease_expires_at: null, wait: null,
    wait_reason: null, wait_since: null, wait_deadline_at: null, age_secs: 0,
    created_at: NOW - 1000, updated_at: NOW, terminal: false, ...extra,
  }
}

const EMPTY = base

const POPULATED = {
  ...base,
  depth: {
    by_state: { queued: 14, running: 2, waiting_input: 1, waiting_permission: 1, waiting_children: 1, waiting_dependency: 1, recovering: 1, retry_wait: 1 },
    queued: 14, waiting: 4, recovering: 2, running: 2, total: 22,
  },
  oldest_wait_secs: AGE,
  waiting: [
    row('input-sudo', 'waiting_input', { wait_reason: '`sudo apt-get install` is waiting for a password', age_secs: AGE }),
    row('perm-deploy', 'waiting_permission', { wait_reason: 'deploy_artifact needs approval', age_secs: AGE, session_key: 'dashboard:chat-2' }),
    row('parent-wave', 'waiting_children', { wait_reason: 'waiting on 3 subagent(s)', age_secs: AGE }),
    row('dep-gh-1', 'waiting_dependency', { wait_reason: 'GitHub API rate limited; retry at reset', age_secs: 7 * 60 + 18 }),
  ],
  recovering: {
    tasks: [
      row('rec-backend', 'recovering', { attempts: 2, age_secs: 12, next_run_at: NOW + 14 }),
      row('retry-429', 'retry_wait', { attempts: 1, age_secs: 12, next_run_at: NOW + 81 }),
    ],
    task_attempts: 3,
    slots: [],
    ladder: [],
  },
}

/** A row per wait state the fold line has to count past `WAIT_ROWS_SHOWN` (8). */
const MANY = {
  ...POPULATED,
  waiting: Array.from({ length: 11 }, (_, i) =>
    row(`dep-gh-${i + 1}`, 'waiting_dependency', {
      wait_reason: 'GitHub API rate limited; retry at reset',
      age_secs: AGE - i * 30,
    }),
  ),
}

// BOTH lanes under their ceiling: the controller lowers global concurrency, so
// this is the ordinary shape, and `degrade_reason` is one global string — the
// cause therefore lands in the "Degrade reason" row, on neither lane.
const DEGRADED = {
  ...POPULATED,
  degrade_reason: 'adaptive_decrease',
  lanes: { subagents: { effective: 2, user_max: 14, running: 2 }, spawn_gate: { effective: 2, user_max: 8 } },
}

// EXACTLY ONE lane under its ceiling, the other at it: the cause moves onto that
// lane's own lines and the row disappears. This is the only payload that renders
// the lane-line home, and it also puts a mapped reason directly beneath the line
// that says what the cap was lowered from.
const DEGRADED_ONE_LANE = {
  ...POPULATED,
  degrade_reason: 'adaptive_probe',
  lanes: { subagents: { effective: 1, user_max: 14, running: 1 }, spawn_gate: { effective: 8, user_max: 8, running: 2 } },
}

// A stall as `session_health` actually files it: keyed by the BARE slot key, its
// `reason` the `no_progress` token (the sentence form only comes from a log scan,
// which this endpoint never runs) and the reading a reader needs in `evidence`.
// The card lists it as a row, so this scene is also the drill-in the red badge
// and the "Stalled" count point at.
const STALLED = {
  ...POPULATED,
  stalled: {
    'chat-7': {
      reason: 'no_progress',
      since_ts: NOW - 11 * 60,
      age_secs: 11 * 60,
      evidence: ['shell child absent', 'no cpu delta'],
    },
  },
}

// A gateway answering with no store publishes no lane caps either (the
// exception arm of `api_tasks_summary` sends `lanes: {}`), so this scene is also
// the only one showing the capacity column with nothing to put in it.
const NO_STORE = { ...base, available: false, lanes: {} }

// `degrade_reason` and `stalled` are the two states the card colours and
// explains, and neither is reachable from the two baseline scenes; the fold
// control, what it opens, the 320px stack and the two store-unavailable notices
// are likewise only observable from a payload shaped for them.
const SHOTS = [
  { name: 'tasks-capacity-empty', payload: EMPTY },
  { name: 'tasks-capacity-populated', payload: POPULATED },
  { name: 'tasks-capacity-degraded', payload: DEGRADED },
  { name: 'tasks-capacity-degraded-one-lane', payload: DEGRADED_ONE_LANE },
  { name: 'tasks-capacity-stalled', payload: STALLED },
  { name: 'tasks-capacity-folded', payload: MANY },
  // The fold is a control, so its OPEN state is a second frame: the shot after
  // the click is the evidence that the held-back rows are reachable in place.
  { name: 'tasks-capacity-expanded', payload: MANY, click: 'tasks-capacity-fold' },
  { name: 'tasks-capacity-narrow', payload: POPULATED, viewport: { width: 320, height: 1200 } },
  // The FOLDED default of a busy queue at 320px — the tallest card this harness
  // renders, and the one geometry that produced an unreadable frame. Photographing
  // the narrow stack only at the shorter POPULATED payload left the pair that
  // actually breaks (narrow AND long) unphotographed.
  { name: 'tasks-capacity-narrow-folded', payload: MANY, viewport: { width: 320, height: 1200 } },
  // Two scenes mount no health badge, so each waits on an element of its own:
  // the store-less arm because every input a healthy/backlog verdict reads is a
  // zero the wire cannot fill there (nothing is stalled or degraded in this
  // payload, so no verdict survives), and the load-error arm because it renders
  // its notice in place of the queue.
  { name: 'tasks-capacity-no-store', payload: NO_STORE, ready: 'tasks-capacity-empty' },
  { name: 'tasks-capacity-error', status: 500, ready: 'tasks-capacity-error' },
]

/** The card's geometry, its title row, and what is painted over that row. */
const CARD_GEOMETRY = () => {
  const card = document.querySelector('[data-testid="tasks-capacity-card"]')
  const title = card?.querySelector('h3')
  if (!card || !title) return null
  // Measured from the TOP of the content: a scroll offset is the thing that puts
  // the sticky bar over the card, so it is zeroed before anything is read.
  for (let node = card.parentElement; node; node = node.parentElement) {
    if (/(auto|scroll)/.test(getComputedStyle(node).overflowY)) node.scrollTop = 0
  }
  window.scrollTo(0, 0)
  const box = el => {
    const r = el.getBoundingClientRect()
    return { top: r.top, bottom: r.bottom, left: r.left, right: r.right, width: r.width, height: r.height }
  }
  const t = box(title)
  const pill = card.querySelector('[data-testid="tasks-capacity-health"]')
  // WHO PAINTS ON TOP where the reader looks. A hit test rather than a rect
  // sweep: `pointer-events: none` decorative layers span the whole viewport and
  // paint nothing, while the sticky back bar takes its hits like any bar.
  const probes = [['title', t.left + 4, t.top + t.height / 2]]
  if (pill) {
    const p = box(pill)
    probes.push(['health pill', p.left + p.width / 2, p.top + p.height / 2])
  }
  const covered = probes
    .map(([what, x, y]) => [what, document.elementFromPoint(x, y)])
    .filter(([, hit]) => !hit || !card.contains(hit))
    .map(([what, hit]) => `${what} is under <${hit?.tagName?.toLowerCase() ?? 'nothing'}`
      + `${hit?.className ? ` class="${String(hit.className).slice(0, 40)}"` : ''}>`)
  // How much taller the viewport must be for the CARD to fit in it at scroll
  // offset zero. Only the card: the plane below it may overflow as far as it
  // likes, because a capture whose box is inside the viewport needs no expansion
  // and so cannot re-lay the page out. Growing to the whole plane's scroll height
  // would double the viewport for scenes that were never at risk.
  const shortBy = Math.ceil(box(card).bottom - window.innerHeight)
  return { card: box(card), title: t, hasPill: !!pill, covered, shortBy }
}

/**
 * Grow the viewport until the whole card fits, then REFUSE the shot unless the
 * card's title row is inside the captured box with nothing painted over it.
 *
 * `card.screenshot()` measures the element's box, then captures beyond the
 * viewport when the element is taller than it — and that capture re-lays the page
 * out. The scroll offset clamps, the sticky "‹ Developer" back bar (`NavBackBar`,
 * `top: 0`) lands at the top of the clip, and the frame shows the bar where the
 * title row belongs with the card's last ~150px cut off. That is how the health
 * pill reached a UX round "half cut off so I can't read it": at 320x1200 the
 * folded card is 1302px against ~1158px of scroller, and the frame LOOKED
 * plausible. A harness that can silently occlude the copy under review is worth
 * less than one that cannot lie, so the fit is what the growth buys and the
 * guard is what proves it: nothing is hidden and no style is mutated, the frame
 * is the real card.
 *
 * Only the HEIGHT grows. Width is what every responsive rule here keys on
 * (`max-[900px]`, `max-[600px]`, `sm:`), so the narrow stack a 320px scene
 * exists to photograph is exactly what it still photographs.
 */
async function frameCard(page, card, name, { pill }) {
  let geom = null
  // The shortfall is kept OUTSIDE the loop: the refusal below is reached only by
  // exhausting every round, and each round clears `geom`, so the one number an
  // operator needs -- how much more room the card wants -- lives nowhere else.
  let shortfall = null
  for (let attempt = 0; attempt < 5; attempt++) {
    geom = await page.evaluate(CARD_GEOMETRY)
    if (!geom) throw new Error(`${name}: the card and its title row must both be mounted before the shot`)
    if (geom.shortBy <= 0) break
    shortfall = geom.shortBy
    const vp = page.viewportSize()
    await page.setViewportSize({ width: vp.width, height: vp.height + geom.shortBy + 24 })
    await page.waitForTimeout(200)
    geom = null
  }
  if (!geom || geom.shortBy > 0) {
    const needs = geom?.shortBy ?? shortfall
    throw new Error(`${name}: the card still needs ${needs}px more viewport after 5 growths;`
      + ' capturing it would clip the title row')
  }
  // The pill is the copy the blind reader could not read, so its probe is not
  // optional wherever it mounts: a scene that lost the badge would otherwise
  // report "no occlusion" for a pill that is not in the frame at all.
  if (geom.hasPill !== pill) {
    throw new Error(`${name}: the health pill is ${geom.hasPill ? 'present' : 'absent'}`
      + ` where the scene expects it ${pill ? 'present' : 'absent'}`)
  }
  if (geom.covered.length > 0) {
    throw new Error(`${name}: ${geom.covered.join('; ')} — the frame would hide the copy under review`)
  }
  const t = geom.title
  const c = geom.card
  if (t.top < c.top - 1 || t.bottom > c.bottom + 1 || t.height < 8) {
    throw new Error(`${name}: the title row (${Math.round(t.top)}–${Math.round(t.bottom)})`
      + ` is not inside the captured box (${Math.round(c.top)}–${Math.round(c.bottom)})`)
  }
  const path = `${OUT}/${name}.png`
  await card.screenshot({ path })
  // The captured PNG must be the box that was just measured and cleared. A
  // mismatch means the page moved under the capture, which is the one failure
  // every check above is blind to.
  const png = readFileSync(path)
  const [width, height] = [png.readUInt32BE(16), png.readUInt32BE(20)]
  if (Math.abs(width - Math.round(c.width)) > 1 || Math.abs(height - Math.round(c.height)) > 1) {
    throw new Error(`${name}: shot is ${width}x${height} but the guarded box was`
      + ` ${Math.round(c.width)}x${Math.round(c.height)} — the page moved under the capture`)
  }
  const vp = page.viewportSize()
  console.log(`wrote ${path} ${width}x${height}`
    + ` · viewport ${vp.width}x${vp.height} · title row ${Math.round(t.top - c.top)}px into the card`
    + ` · health pill ${geom.hasPill ? 'in frame, unoccluded' : 'absent (this scene supports no verdict)'}`)
}

const { srv, base: origin } = await serveDist()
const browser = await chromium.launch()

try {
  for (const { name, payload, viewport, status, ready, click } of SHOTS) {
    const context = await browser.newContext({
      viewport: viewport ?? { width: 1280, height: 1000 },
      deviceScaleFactor: 1,
    })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      theme: 'light',
      extra: async (path, route) => {
        if (path !== '/api/tasks/summary') return false
        if (status) await route.fulfill({ status, body: '{}', contentType: 'application/json' })
        else await json(route, payload)
        return true
      },
    })
    await page.goto(`${origin}/developer?tab=system&plane=services`, { waitUntil: 'domcontentloaded' })
    const card = page.getByTestId('tasks-capacity-card')
    await card.waitFor({ timeout: 10000 })
    // The badge only mounts once the summary resolved; a frame before that is a
    // card with dashes, which would pass the harness and prove nothing.
    await page.getByTestId(ready ?? 'tasks-capacity-health').waitFor({ timeout: 15000 })
    if (click) await page.getByTestId(click).click()
    await page.waitForTimeout(300)
    // A scene that renders the badge waits on the badge itself, so `ready` being
    // set IS the scene saying it renders no pill.
    await frameCard(page, card, name, { pill: !ready })
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
