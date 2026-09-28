/**
 * Real-browser evidence for the Git panel's SCOPED filter refusal (#12080).
 *
 * `capture-git-panel-degraded-states.mjs` (#11982) shoots the states that
 * existed when the refusal had one title and one sentence per cause. This shoots
 * what changed: the title now names the half the refusal accounts for, it
 * carries permanence, and the two stacked notices' hand-offs name their own
 * half. Those are claims about composited text and about pixels, which is what a
 * DOM assertion cannot carry -- the prior defect in this family was found by a
 * reader looking at three frames and calling all three crossed-out.
 *
 * Runs the REAL built SPA (website/dist) with /api/** answered from fixtures,
 * the same approach the sibling harness uses. `dashboard.auto_open_git_panel`
 * opens the panel without a click.
 *
 * Frames:
 *   1 coalesced-declared    both routes refuse on a declared driver -> both
 *                           halves named, refresh INERT
 *   2 coalesced-unreadable  both refuse on an unreadable config -> "right now"
 *                           title, refresh LIVE
 *   3 divergent-log         status outage + log refusal -> two notices, the
 *                           refusal named on history only, the outage marking
 *                           itself a separate problem, distinct hand-off labels
 *   4 divergent-status      status refusal + log outage -> the mirror, named on
 *                           changes only
 *   5 partial-healthy-log   status refuses while the log route is HEALTHY -> one
 *                           notice named on changes only, commit list rendering
 *                           underneath it
 *   6 glyph-inert           header close-up, declared cause
 *   7 glyph-live            header close-up, unreadable cause, for comparison
 *
 * The frames cannot lie: every frame ASSERTS the strings it is evidence for are
 * present and that the title of a WIDER scope is absent, and the two glyph
 * frames assert the disabled state, the stroke weight and the opacity class.
 * Any failure exits non-zero and the PNGs are not citable.
 *
 * Usage: node scripts/capture-git-panel-refusal-scope.mjs <outDir>
 */
import { mkdirSync } from 'node:fs'
import { join } from 'node:path'
import { openTranscriptHarness } from './lib/transcript-harness.mjs'
import { json } from './lib/boot-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/git-panel-refusal-scope'
const SLOT = 'chat-git-refusal-scope'
const PROJECT = '/home/user/workspace/demo-service'

mkdirSync(OUT, { recursive: true })

/** The shipped English copy, so a frame cannot disagree with the catalog. */
const COPY = {
  titleBoth: "Kiro can't show changes or history for this repository",
  titleChanges: "Kiro can't show changes for this repository",
  titleHistory: "Kiro can't show history for this repository",
  titleBothNow: "Kiro can't show changes or history right now",
  titleChangesNow: "Kiro can't show changes right now",
  titleHistoryNow: "Kiro can't show history right now",
  declared: 'Git LFS or another conversion program is set up',
  // The cause sentence, not the retry cue: the cue is deliberately NOT in
  // the shared body, because two of its three surfaces have no refresh
  // control. What tells the two causes apart is the cause itself.
  unreadable: "Kiro couldn't read this repository's Git config.",
  retryCue: 'Refresh to try again.',
  separate: 'A separate problem',
  askChanges: 'Ask the agent about the changes',
  askHistory: 'Ask the agent about the history',
}

const slots = [{
  key: SLOT,
  title: 'Git panel refusal scope',
  running: false,
  last_message: 'A refusal now names the half it is reporting on.',
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
    { role: 'user', ts: Date.now() / 1000 - 60, content: 'Why does the panel say history is off when the commits are right there?' },
    { role: 'assistant', ts: Date.now() / 1000 - 30, content: 'It was claiming both halves whichever route had refused. The refusal now names only the half it accounts for.' },
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

/**
 * Per-state routing for the two endpoints, as [statusBody, logBody] where a
 * body is either a 503 {error, code, cause} or a 200 payload. Kept as data so a
 * frame's fixture is readable beside the claim it supports.
 */
const REFUSE_STATUS = cause => ({
  error: 'Checks are off for this repository: refused by policy.',
  code: 'git_status_filter_refused',
  cause,
})
const REFUSE_LOG = cause => ({
  error: 'History is off for this repository: refused by policy.',
  code: 'git_log_filter_refused',
  cause,
})
const STATUS_OUTAGE = { error: "Couldn't read the repository status.", code: 'git_status_unavailable' }
// Deliberately NOT the localized `log_failed` sentence: the log notice renders
// that title ABOVE the backend's own message, so a fixture echoing it puts the
// same words on the frame twice and reads as a duplication defect the code does
// not have.
const LOG_OUTAGE = { error: 'git log exited 128: bad object HEAD', code: 'git_log_unavailable' }
const HEALTHY_LOG = { repo: true, commits }

const STATES = {
  'coalesced-declared': [REFUSE_STATUS('declared'), REFUSE_LOG('declared')],
  'coalesced-unreadable': [REFUSE_STATUS('unreadable'), REFUSE_LOG('unreadable')],
  'divergent-log': [STATUS_OUTAGE, REFUSE_LOG('declared')],
  'divergent-status': [REFUSE_STATUS('declared'), LOG_OUTAGE],
  'partial-healthy-log': [REFUSE_STATUS('declared'), HEALTHY_LOG],
  // The unreadable cause at the two NARROW scopes. Those two titles are the
  // only ones a frame set built from the states above never reaches, and they
  // are the pair a reader has to be able to tell from the permanent case while
  // a sibling notice or a live list sits beside them.
  'divergent-log-unreadable': [STATUS_OUTAGE, REFUSE_LOG('unreadable')],
  'partial-healthy-log-unreadable': [REFUSE_STATUS('unreadable'), HEALTHY_LOG],
}

let state = 'coalesced-declared'
let failures = 0

const dashCfg = { auto_open_git_panel: true }
await h.page.route(/\/api\/dashboard\/config$/, async route => json(route, dashCfg))

await h.page.route(/\/api\/project\/git/, async route => {
  const path = new URL(route.request().url()).pathname
  const [statusBody, logBody] = STATES[state]
  if (path === '/api/project/git/status') {
    return statusBody.code ? json(route, statusBody, 503) : json(route, statusBody)
  }
  if (path === '/api/project/git/log') {
    return logBody.code ? json(route, logBody, 503) : json(route, logBody)
  }
  return json(route, { repo: true, repoRoot: PROJECT, branch: 'mainline' })
})

/** Assert a string IS on the panel; count a miss rather than throwing, so every
 *  frame is attempted and the run reports all of them at once. */
function must(name, haystack, needle, present = true) {
  const has = haystack.includes(needle)
  if (has !== present) {
    console.error(`[${name}] ${present ? 'MISSING' : 'UNEXPECTED'}: ${JSON.stringify(needle)}`)
    failures++
  }
}

const NOTICES = '[data-testid="git-panel-filter-refused"], [data-testid="git-panel-status-error"], [data-testid="git-panel-log-error"]'

async function load(waitFor, theme = 'dark') {
  await h.load(theme, { selector: 'textarea', settle: 1500 })
  await h.page.waitForSelector(waitFor, { timeout: 20000 })
  await h.page.waitForTimeout(600)
}

/** Panel text as the viewer reads it: every notice, composited. */
async function panelText() {
  const parts = await h.page.locator(NOTICES).allInnerTexts()
  return parts.join('\n')
}

// 1. Both routes refuse on a declared driver. This is the only state where the
//    refusal legitimately claims both halves, and the refresh control is inert.
state = 'coalesced-declared'
await load('[data-testid="git-panel-filter-refused"]')
{
  const text = await panelText()
  must(state, text, COPY.titleBoth)
  must(state, text, COPY.declared)
  must(state, text, COPY.unreadable, false)
  console.log(state, JSON.stringify({ notices: await h.page.locator(NOTICES).count(), text }))
  await h.page.screenshot({ path: join(OUT, 'git-scope-1-coalesced-declared.png') })
  console.log('SHOT git-scope-1-coalesced-declared')
}

// 2. Same scope, other cause. The title says "right now" and the body invites a
//    refresh, because an unreadable config can become readable.
state = 'coalesced-unreadable'
await load('[data-testid="git-panel-filter-refused"]')
{
  const text = await panelText()
  must(state, text, COPY.titleBothNow)
  must(state, text, COPY.unreadable)
  must(state, text, COPY.titleBoth, false)
  console.log(state, JSON.stringify({ text }))
  await h.page.screenshot({ path: join(OUT, 'git-scope-2-coalesced-unreadable.png') })
  console.log('SHOT git-scope-2-coalesced-unreadable')
}

// 3. The divergent state the issue was filed for: the status route fails on its
//    own terms while the log route refuses. The refusal names HISTORY only, the
//    outage marks itself a separate problem, and the two hand-offs differ.
state = 'divergent-log'
await load('[data-testid="git-panel-log-error"]')
{
  const text = await panelText()
  must(state, text, COPY.titleHistory)
  must(state, text, COPY.titleBoth, false)
  must(state, text, COPY.separate)
  must(state, text, COPY.askChanges)
  must(state, text, COPY.askHistory)
  console.log(state, JSON.stringify({ notices: await h.page.locator(NOTICES).count(), text }))
  await h.page.screenshot({ path: join(OUT, 'git-scope-3-divergent-log-refusal.png') })
  console.log('SHOT git-scope-3-divergent-log-refusal')
}

// 4. The mirror. The status route refuses and the log route fails for its own
//    reason, so the refusal names CHANGES only.
state = 'divergent-status'
await load('[data-testid="git-panel-status-error"]')
{
  const text = await panelText()
  must(state, text, COPY.titleChanges)
  must(state, text, COPY.titleBoth, false)
  console.log(state, JSON.stringify({ notices: await h.page.locator(NOTICES).count(), text }))
  await h.page.screenshot({ path: join(OUT, 'git-scope-4-divergent-status-refusal.png') })
  console.log('SHOT git-scope-4-divergent-status-refusal')
}

// 5. The state no notice covered before: one route refuses and the other is
//    HEALTHY, so the commit list renders underneath. A refusal claiming history
//    here denies something on screen, which is why the frame must show both.
state = 'partial-healthy-log'
await load('[data-testid="git-panel-filter-refused"]')
{
  const text = await panelText()
  must(state, text, COPY.titleChanges)
  must(state, text, COPY.titleBoth, false)
  const shas = await h.page.locator('text=e19c2ab').count()
  if (shas < 1) {
    console.error(`[${state}] the healthy commit list is not on the frame, so it proves nothing`)
    failures++
  }
  console.log(state, JSON.stringify({ commitRows: shas, text }))
  await h.page.screenshot({ path: join(OUT, 'git-scope-5-partial-healthy-log.png') })
  console.log('SHOT git-scope-5-partial-healthy-log')
}

// 6. The unreadable cause in the divergent state: history only, and "right now"
//    rather than the permanent wording, beside a sibling notice that owns the
//    other half.
state = 'divergent-log-unreadable'
await load('[data-testid="git-panel-log-error"]')
{
  const text = await panelText()
  must(state, text, COPY.titleHistoryNow)
  must(state, text, COPY.unreadable)
  must(state, text, COPY.titleHistory, false)
  must(state, text, COPY.titleBothNow, false)
  console.log(state, JSON.stringify({ notices: await h.page.locator(NOTICES).count(), text }))
  await h.page.screenshot({ path: join(OUT, 'git-scope-6-divergent-log-unreadable.png') })
  console.log('SHOT git-scope-6-divergent-log-unreadable')
}

// 7. The unreadable cause beside a HEALTHY log route: changes only, "right now",
//    with the commit list rendering under it.
state = 'partial-healthy-log-unreadable'
await load('[data-testid="git-panel-filter-refused"]')
{
  const text = await panelText()
  must(state, text, COPY.titleChangesNow)
  must(state, text, COPY.unreadable)
  must(state, text, COPY.titleBothNow, false)
  // Only the refusal renders here, so the retry cue must be absent: it
  // belongs to the panel-only status string, never to the shared body.
  must(state, text, COPY.retryCue, false)
  const shas = await h.page.locator('text=e19c2ab').count()
  if (shas < 1) {
    console.error(`[${state}] the healthy commit list is not on the frame, so it proves nothing`)
    failures++
  }
  console.log(state, JSON.stringify({ commitRows: shas, text }))
  await h.page.screenshot({ path: join(OUT, 'git-scope-7-partial-healthy-log-unreadable.png') })
  console.log('SHOT git-scope-7-partial-healthy-log-unreadable')
}

/**
 * The refused panel header beside its notice, once per cause.
 *
 * Item 5 is a judgement about pixels: at `size={13}` the slash that separates
 * the inert control from the live one is the first thing a fade erases, and a
 * reader shown three frames called all three crossed-out. So the frame has to
 * carry the control at the density a reader meets it in AND the copy it sits
 * above, which is what makes the pair comparable. Clipped to the panel column
 * from the tab strip down through the notice, computed from the button's own box
 * so it cannot drift with layout.
 */
async function glyphFrame(name, { inert }) {
  const glyph = inert ? '.lucide-refresh-cw-off' : '.lucide-refresh-cw'
  const button = h.page.locator(`button:has(${glyph})`).first()
  await button.waitFor({ timeout: 20000 })
  const box = await button.boundingBox()
  if (!box) {
    console.error(`[${name}] the refresh button has no box`)
    failures++
    return
  }
  const disabled = await button.isDisabled()
  const cls = (await button.getAttribute('class')) || ''
  const stroke = await button.locator(glyph).getAttribute('stroke-width')
  if (disabled !== inert) {
    console.error(`[${name}] disabled=${disabled}, expected ${inert}`)
    failures++
  }
  if (inert && (stroke !== '2.5' || !cls.includes('opacity-60') || cls.includes('opacity-40'))) {
    console.error(`[${name}] inert treatment wrong: stroke=${stroke} class=${cls}`)
    failures++
  }
  console.log(name, JSON.stringify({ disabled, stroke, opacity60: cls.includes('opacity-60') }))
  await h.page.screenshot({
    path: join(OUT, `${name}.png`),
    clip: {
      x: Math.max(0, box.x - 430),
      y: Math.max(0, box.y - 58),
      width: box.width + 460,
      height: 330,
    },
  })
  console.log('SHOT', name)
}

state = 'coalesced-declared'
await load('[data-testid="git-panel-filter-refused"]')
await glyphFrame('git-scope-8-refused-header-inert', { inert: true })

state = 'coalesced-unreadable'
await load('[data-testid="git-panel-filter-refused"]')
await glyphFrame('git-scope-9-unreadable-header-live', { inert: false })

// The same inert control in LIGHT theme. The claim item 5 rests on is about
// contrast between a muted glyph and what is behind it, and a theme changes
// exactly that, so verifying it on one theme verifies half of it.
state = 'coalesced-declared'
await load('[data-testid="git-panel-filter-refused"]', 'light')
await glyphFrame('git-scope-10-refused-header-inert-light', { inert: true })

await h.close()
if (failures > 0) {
  console.error(`FAILED: ${failures} assertion failure(s) -- the frames are not citable`)
  process.exit(1)
}
console.log('DONE', OUT)
