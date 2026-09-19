/**
 * Screenshot harness for the Git panel's capped-listing and unavailable states.
 *
 * Runs the REAL built SPA (website/dist) with /api/** answered from fixtures,
 * reusing capture-footer-git-status.mjs's approach: the git endpoints are
 * fixtured per state and `dashboard.auto_open_git_panel` opens the panel
 * without a click.
 *
 * Three frames, one per state the panel must tell apart:
 *   clean-pill       repo: true, files: []          -> green "clean" pill
 *   capped-listing   500 files + truncated: true    -> "500+ uncommitted" + note
 *   filter-refused   503 git_status_filter_refused  -> ONE titled notice, no pill
 *   unreadable       503 ... cause=unreadable       -> same notice, other cause
 *   divergent        503 outage + 503 log refusal   -> TWO notices, refresh live
 *   status-outage    503 git_status_unavailable     -> the generic outage notice
 *
 * The clean frame is also the BEFORE frame for the refusal: the endpoint used
 * to answer a filter-driver refusal with the same `{repo: true, files: []}` a
 * clean tree returns, so this frame is what an LFS-filtered repo rendered.
 * That the clean and refused frames now differ is the whole change.
 *
 * Usage: node scripts/capture-git-panel-degraded-states.mjs <outDir>
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'
import { json } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/git-panel-degraded-states'
const SLOT = 'chat-git-degraded'
const PROJECT = '/home/user/workspace/demo-service'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Git panel status states',
  running: false,
  last_message: 'A refused status no longer reads as a clean tree.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 1,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 60, content: 'The Git panel says clean but my tree is not.' },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'The status read was refused, and the panel reported that refusal as an empty file list. It now names the cause instead.' },
  ],
}

const h = await openTranscriptHarness({
  slot: SLOT,
  project: PROJECT,
  slots,
  detail,
  viewport: { width: 1280, height: 800 },
  deviceScaleFactor: 1,
})

const commits = [
  { sha: 'e19c2ab', message: 'local work', author: 'Demo', date: new Date(Date.now() - 3600e3).toISOString(), isHead: true },
  { sha: '4f6d1c0', message: 'init', author: 'Demo', date: new Date(Date.now() - 86400e3).toISOString(), isHead: false },
]

// Mutable so one browser context serves every state: each frame sets `state`
// and reloads rather than booting a fresh harness.
let state = 'clean'

const dashCfg = { auto_open_git_panel: true }
await h.page.route(/\/api\/dashboard\/config$/, async route => json(route, dashCfg))

await h.page.route(/\/api\/project\/git/, async route => {
  const path = new URL(route.request().url()).pathname
  if (path === '/api/project/git/status') {
    if (state === 'refused') {
      return json(
        route,
        {
          error: 'Checks are off for this repository: its Git config declares a filter driver, so they are refused by policy.',
          code: 'git_status_filter_refused',
          cause: 'declared',
        },
        503,
      )
    }
    if (state === 'unreadable') {
      return json(
        route,
        {
          error: 'Checks are off for this repository: its Git config could not be read, so they are refused by policy.',
          code: 'git_status_filter_refused',
          cause: 'unreadable',
        },
        503,
      )
    }
    if (state === 'outage' || state === 'divergent') {
      return json(
        route,
        { error: "Couldn't read the repository status.", code: 'git_status_unavailable' },
        503,
      )
    }
    if (state === 'capped') {
      return json(route, {
        repo: true,
        repoRoot: PROJECT,
        branch: 'mainline',
        truncated: true,
        files: Array.from({ length: 500 }, (_, i) => ({
          path: `src/generated/module_${String(i).padStart(4, '0')}.py`,
          status: 'M',
          staged: false,
          additions: 3,
          deletions: 1,
        })),
      })
    }
    // clean
    return json(route, { repo: true, repoRoot: PROJECT, branch: 'mainline', files: [] })
  }
  if (path === '/api/project/git/log') {
    if (state === 'unreadable') {
      return json(
        route,
        {
          error: 'History is off for this repository: its Git config could not be read, so this check is refused by policy.',
          code: 'git_log_filter_refused',
          cause: 'unreadable',
        },
        503,
      )
    }
    if (state === 'refused' || state === 'divergent') {
      return json(
        route,
        {
          error: 'History is off for this repository: its Git config declares a filter driver, so this check is refused by policy.',
          code: 'git_log_filter_refused',
          cause: 'declared',
        },
        503,
      )
    }
    return json(route, { repo: true, commits })
  }
  return json(route, { repo: true, repoRoot: PROJECT, branch: 'mainline' })
})

/** Shoot the Git panel, clipped to it, after asserting the state is on screen. */
async function shoot(name, waitFor) {
  await h.load('dark', { selector: 'textarea', settle: 1500 })
  await h.page.waitForSelector(waitFor, { timeout: 15000 })
  await h.page.waitForTimeout(600)
  // The panel's own header carries the branch label and the pill; clip to the
  // panel so the frame is about the panel rather than the whole dashboard.
  const panel = h.page.locator('[data-testid="git-panel-status-error"]').first()
  const anchor = (await panel.count()) > 0 ? panel : h.page.locator('textarea')
  await anchor.first().waitFor({ timeout: 15000 })
  await h.page.screenshot({ path: join(OUT, `${name}.png`) })
  console.log('SHOT', name)
}

// 1. Clean tree: the green pill is correct here and must survive. This frame
//    doubles as the BEFORE for a filter-driver refusal, which used to render
//    exactly this.
state = 'clean'
await shoot('git-panel-1-clean-pill', 'text=clean')

// 2. Capped listing: the pill says 500+ and the Changes header carries the
//    note. Assert the bare total is NOT also rendered (one claim per number).
state = 'capped'
await h.load('dark', { selector: 'textarea', settle: 1500 })
await h.page.waitForSelector('[data-testid="git-panel-truncated"]', { timeout: 15000 })
await h.page.waitForTimeout(600)
const pill = await h.page.locator('text=500+ uncommitted').first().textContent()
const note = await h.page.locator('[data-testid="git-panel-truncated"]').first().textContent()
console.log('CAPPED', JSON.stringify({ pill, note }))
await h.page.screenshot({ path: join(OUT, 'git-panel-2-capped-listing.png') })
console.log('SHOT git-panel-2-capped-listing')

// 3. Filter-driver refusal: no pill at all, and ONE titled ErrorNotice naming
//    the cause, with the Ask-the-agent hand-off. An earlier revision rendered
//    this as a muted non-error line; GPT blocked that on the
//    `errors-use-error-notice` rule, since the string comes from a 503
//    {error, code} body. The header refresh control is INERT here.
//    Note the fixture `error` strings above are never rendered -- the panel
//    keys on `code` and shows localized copy -- but they are kept in step with
//    the backend's real sentences so this file cannot be read as evidence for
//    wording that no longer ships.
state = 'refused'
await h.load('dark', { selector: 'textarea', settle: 1500 })
await h.page.waitForSelector('[data-testid="git-panel-filter-refused"]', { timeout: 20000 })
await h.page.waitForTimeout(600)
// Count the notices ACTUALLY rendered, not the outage testIds: those are absent
// by construction here, so counting them proves nothing about this frame. The
// claim worth proving is that ONE notice renders and it is the titled refusal.
console.log('REFUSED', JSON.stringify({
  title: (await h.page.locator('[data-testid="git-panel-filter-refused"] strong').first().textContent())?.trim(),
  message: (await h.page.locator('[data-testid="git-panel-filter-refused"]').first().innerText())?.trim(),
  totalNotices: await h.page.locator('[data-testid^="git-panel-"][data-testid$="-error"], [data-testid="git-panel-filter-refused"]').count(),
}))
await h.page.screenshot({ path: join(OUT, 'git-panel-3-filter-refused.png') })
console.log('SHOT git-panel-3-filter-refused')

// 4. A genuine outage, for contrast: the retryable red notice the refusal no
//    longer borrows.
state = 'outage'
await h.load('dark', { selector: 'textarea', settle: 1500 })
await h.page.waitForSelector('[data-testid="git-panel-status-error"]', { timeout: 20000 })
await h.page.waitForTimeout(600)
// The outage notice carries NO title for a coded failure (the localized string
// is the whole message), so read the title count rather than its text: waiting
// on an element that is absent by design times the capture out.
// `titles` must read 0: the outage notice is untitled on purpose, so the frame
// disagrees with the code the moment someone gives it a title again.
console.log('OUTAGE', JSON.stringify({
  titles: await h.page.locator('[data-testid="git-panel-status-error"] strong').count(),
  message: (await h.page.locator('[data-testid="git-panel-status-error"]').first().innerText())?.trim(),
  refusalNotices: await h.page.locator('[data-testid="git-panel-filter-refused"]').count(),
}))
await h.page.screenshot({ path: join(OUT, 'git-panel-4-status-outage.png') })
console.log('SHOT git-panel-4-status-outage')

// 5. The OTHER refusal cause. The guard refuses on two different facts and the
//    body says which, so this frame is the one that shows the `unreadable`
//    sentence a reader actually sees -- the four frames above all carry the
//    `declared` copy. It also has no permanence clause and keeps refresh LIVE:
//    a config that could not be read can become readable, and the status route
//    re-polls every 5 s.
state = 'unreadable'
await h.load('dark', { selector: 'textarea', settle: 1500 })
await h.page.waitForSelector('[data-testid="git-panel-filter-refused"]', { timeout: 20000 })
await h.page.waitForTimeout(600)
console.log('UNREADABLE', JSON.stringify({
  title: (await h.page.locator('[data-testid="git-panel-filter-refused"] strong').first().textContent())?.trim(),
  message: (await h.page.locator('[data-testid="git-panel-filter-refused"]').first().innerText())?.trim(),
  slashedRefresh: await h.page.locator('header .lucide-refresh-cw-off, .lucide-refresh-cw-off').count(),
}))
await h.page.screenshot({ path: join(OUT, 'git-panel-5-unreadable-config.png') })
console.log('SHOT git-panel-5-unreadable-config')

// 6. Two real faults at once: a corrupt HEAD fails the status route on its own
//    terms while the log route still refuses on config. The panel deliberately
//    does NOT coalesce here -- collapsing would hide a true condition behind a
//    sibling's copy -- so this frame is the only one showing the stacked
//    arrangement, with the refusal titled, the outage untitled, and refresh live
//    because one route can still recover.
state = 'divergent'
await h.load('dark', { selector: 'textarea', settle: 1500 })
await h.page.waitForSelector('[data-testid="git-panel-log-error"]', { timeout: 20000 })
await h.page.waitForTimeout(600)
console.log('DIVERGENT', JSON.stringify({
  statusTitles: await h.page.locator('[data-testid="git-panel-status-error"] strong').count(),
  logTitle: (await h.page.locator('[data-testid="git-panel-log-error"] strong').first().textContent())?.trim(),
  coalesced: await h.page.locator('[data-testid="git-panel-filter-refused"]').count(),
  notices: await h.page.locator('[data-testid^="git-panel-"][data-testid$="-error"], [data-testid="git-panel-filter-refused"]').count(),
  slashedRefresh: await h.page.locator('.lucide-refresh-cw-off').count(),
}))
await h.page.screenshot({ path: join(OUT, 'git-panel-6-divergent-failures.png') })
console.log('SHOT git-panel-6-divergent-failures')

await h.close()
console.log('DONE', OUT)
