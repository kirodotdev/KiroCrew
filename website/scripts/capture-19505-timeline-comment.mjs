/**
 * Screenshot harness for the Issue Radar timeline comment box (#19505).
 *
 * Serves the REAL built SPA (website/dist) and answers /api/** from the shared boot stub,
 * with the Issue Radar routes layered on through its `extra` hook. Only the network is
 * stubbed, so the box, the header bar's composer and the panes are the production code.
 * Each scene asserts the state it shows before it writes a frame, so no still can
 * contradict the diff. The same run checks the behaviour a still cannot show, in the
 * browser with real input events: a hit-tested click on Clear, Escape keeping the draft
 * and still reaching `window`, and words typed during a post surviving it.
 *
 * Stills:
 *   01-issue-pane-box           the issue pane, writable repo, box empty above the timeline
 *   02-pr-pane-box              the pull-request pane, with the header bar's Comment button
 *   03-pr-pane-both-composers   the header bar's composer open above the box
 *   04-issue-post-in-flight     a post in flight: spinner, box still editable
 *   05-issue-post-refused       a refused post: the reason under the kept draft
 *
 * Builds nothing: serve-dist serves what is on disk, so run `npm run build` first.
 *
 * Usage: node scripts/capture-19505-timeline-comment.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/19505-timeline-comment'
mkdirSync(OUT, { recursive: true })

const REPO = { owner: 'kirodotdev', repo: 'Kiro', provider: 'github', host: 'github.com' }
const REPOS = { repos: [{ ...REPO, enabled: true, permissions: { push: true, triage: true } }] }
const ISSUE = 4438
const PULL = 4451
const WEB = 'https://github.com/kirodotdev/Kiro'
const TITLE = 'Answer from the detail timeline'
const BODY = 'The timeline is where the conversation is, but a reader cannot add to it from there.'
const WHEN = '2026-10-12T08:00:00Z'
const NO_REACTIONS = { total: 0, plus1: 0, minus1: 0, laugh: 0, hooray: 0, confused: 0, heart: 0, rocket: 0, eyes: 0 }
const LABELS = [{ name: 'enhancement', color: 'a2eeef', description: 'New feature or request' }]
const TIMELINE = [
  { kind: 'comment', actor: 'maria-s', author_association: 'MEMBER', created_at: '2026-10-12T09:10:00Z',
    body: 'I can reproduce this. The pane has no way to answer.' },
  { kind: 'comment', actor: 'sam-k', author_association: 'CONTRIBUTOR', created_at: '2026-10-12T11:40:00Z',
    body: 'Same here, I reply on the forge.' },
]
const ROW = {
  title: TITLE, labels: ['enhancement'], author: 'raymond', author_association: 'MEMBER',
  created_at: WHEN, updated_at: '2026-10-12T11:40:00Z', assignees: [],
}
const issueRow = { ...ROW, number: ISSUE, url: `${WEB}/issues/${ISSUE}`, state: 'open', comments: 2, body: BODY }
const pullRow = {
  ...ROW, number: PULL, url: `${WEB}/pull/${PULL}`, state: 'open', draft: false, merged_at: null,
  requested_reviewers: [], base: 'main', head: 'feat/timeline-comment', head_sha: 'abc1234',
}
const issueDetail = () => ({
  ...REPO, number: ISSUE, from_cache: false, timeline: TIMELINE,
  detail: {
    ...issueRow, body: BODY, state_reason: null, closed_at: null, closed_by: null, locked: false,
    comments: 2, labels: LABELS, milestone: null, reactions: NO_REACTIONS,
  },
})
const pullDetail = () => ({
  ...REPO, number: PULL, from_cache: false, timeline: TIMELINE, checks: [],
  detail: {
    ...pullRow, body: BODY, merged: false, closed_at: null, merged_by: null, comments: 2,
    review_comments: 0, commits: 2, additions: 120, deletions: 8, changed_files: 5, mergeable: true,
    mergeable_state: 'clean', labels: LABELS, milestone: null,
  },
})

/** What the comment POST does in the current scene: `refuse`, or hold the response until `release()`. */
const post = { mode: 'hold', hits: [], release: () => {} }

const routes = (path, route) => {
  const get = (suffix, body) => path.endsWith(`/issue-radar/${suffix}`) && json(route, body).then(() => true)
  if (path.endsWith('/comment')) {
    post.hits.push(path)
    if (post.mode === 'refuse') {
      return json(route, { error: 'Resource not accessible by integration', code: 'provider_forbidden' }, 403).then(() => true)
    }
    return new Promise((done) => { post.release = () => json(route, { ...REPO, number: ISSUE, comment_id: 1, id: 1, url: null }).then(() => done(true)) })
  }
  return get('repos', REPOS) || get('me', { login: 'owner', provider: 'github', host: 'github.com' })
    || get('labels', { ...REPO, from_cache: true, labels: LABELS })
    || get('members', { ...REPO, members: [], source: 'collaborators', from_cache: true })
    || get('settings', { ...REPO, settings: { triage_labels: [], unlabeled_is_untriaged: true, good_first_issue_labels: [], notify_on_new_issue: false, revision: 1 } })
    || get('issues', { ...REPO, state: 'open', from_cache: true, issues: [issueRow] })
    || get('pulls', { ...REPO, state: 'open', from_cache: true, bulk_max: 50, pulls: [pullRow] })
    || get('issue', issueDetail()) || get('pull', pullDetail())
    || get('issue-ai', { ...REPO, number: ISSUE, summary: 'Readers cannot answer from the timeline.', suggested_labels: [], generated_at: WHEN, from_cache: true })
    || get('pull-ai', { ...REPO, number: PULL, summary: 'Adds a comment box to the detail timeline.', generated_at: WHEN, from_cache: true })
    || get('deps', { schema: 1, edges: [], nodes: {}, truncated: false })
    || get('pull/runs', { ...REPO, number: PULL, runs: [] })
}

const { srv, base } = await serveDist()
const browser = await chromium.launch({ executablePath: chromiumExecutable() })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 2 })
let failed = false
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'OK  ' : 'FAIL'} ${name}${detail ? ` -- ${detail}` : ''}`)
  if (!ok) failed = true
}

/** A fresh page on the pane under test, selected through the persisted UI state. */
async function open(pane, mode = 'hold') {
  post.mode = mode
  post.hits = []
  const page = await context.newPage()
  logPageProblems(page)
  const issues = pane === 'issue'
  await stubDashboardApi(page, {
    extra: routes,
    localStorageEntries: {
      'kc:issue-radar:active-repo': JSON.stringify({ owner: REPO.owner, repo: REPO.repo }),
      'kc:issue-radar:ui-state': JSON.stringify(issues
        ? { mainView: 'issues', selectedIssue: ISSUE, stateFilter: 'open' }
        : { mainView: 'pulls', selectedPull: PULL, prStateFilter: 'open' }),
    },
  })
  await page.goto(`${base}/issue-radar`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('textbox', { name: 'Add a timeline comment', exact: true }).waitFor({ timeout: 20_000 })
  await page.getByText(TIMELINE[0].body).first().waitFor()
  await page.addStyleTag({ content: '*, *::before, *::after { animation: none !important; transition: none !important; }' })
  return page
}
const timelineBox = (page) => page.getByRole('textbox', { name: 'Add a timeline comment', exact: true })
const boxButton = (page, name) => timelineBox(page).locator('xpath=..').getByRole('button', { name, exact: true })
/** The detail pane: the nearest ancestor of the box's `main` that also holds the title, so the
 *  frame takes in the header bar (and the composer it opens) above the timeline. */
async function shoot(page, name) {
  const pane = await page.locator('xpath=//main[.//textarea[@aria-label="Add a timeline comment"]]/ancestor::*[.//h1][1]').last().boundingBox()
  const clip = { x: pane.x, y: pane.y, width: Math.min(pane.width, 1000), height: Math.min(pane.height, 1000) }
  await page.screenshot({ path: `${OUT}/${name}.png`, clip })
  console.log('wrote', `${OUT}/${name}.png`, `${Math.round(clip.width)}x${Math.round(clip.height)} css px at ${Math.round(clip.x)},${Math.round(clip.y)}`)
}
/** True when `a` comes before `b` in document order. */
const precedes = async (a, b) => a.evaluate((x, y) => Boolean(x.compareDocumentPosition(y) & Node.DOCUMENT_POSITION_FOLLOWING), await b.elementHandle())

// 01 / 02: the box in its resting state on each pane.
for (const [pane, still] of [['issue', '01-issue-pane-box'], ['pr', '02-pr-pane-box']]) {
  const page = await open(pane)
  // The timeline is newest first, so the first card is the later of the two comments.
  check(`${pane}: the box sits above the first timeline card`, await precedes(timelineBox(page), page.getByText(TIMELINE[1].body).first()))
  check(`${pane}: Comment and Clear are both offered, both idle on an empty box`,
    await boxButton(page, 'Comment').isDisabled() && await boxButton(page, 'Clear').isDisabled())
  await shoot(page, still)
  await page.close()
}

// 03: the header bar's composer open on the pull-request pane, with the box below it.
{
  const page = await open('pr')
  await page.getByRole('button', { name: 'Comment', exact: true }).first().click()
  // The header composer and the box carry different names, so each is addressed by its own.
  const header = page.getByRole('textbox', { name: 'Add a comment', exact: true })
  await header.waitFor()
  const [top, bottom] = [await header.boundingBox(), await timelineBox(page).boundingBox()]
  check('pr: both comment composers are on screen at once, under different names', await header.count() === 1 && await timelineBox(page).count() === 1)
  check('pr: the header composer is above the box and they do not overlap', top.y + top.height <= bottom.y, `${Math.round(top.y)}+${Math.round(top.height)} <= ${Math.round(bottom.y)}`)
  await shoot(page, '03-pr-pane-both-composers')
  await page.close()
}

// 04: a post in flight. The box stays editable, and words typed meanwhile survive the post.
{
  const page = await open('issue', 'hold')
  const box = timelineBox(page)
  await box.fill('Thanks, confirmed. I will pick this up today.')
  await page.keyboard.press('Control+Enter')
  await page.waitForFunction(() => document.querySelector('main button svg.animate-spin'))
  check('issue: the post went to the issue route', post.hits.length === 1 && post.hits[0].endsWith('/issue/comment'), post.hits.join())
  check('issue: the box is still editable while the post is in flight', await box.isEditable())
  await box.focus()
  await page.keyboard.press('End')
  await page.keyboard.type(' I will add a test as well.')
  await shoot(page, '04-issue-post-in-flight')
  post.release()
  await page.waitForFunction(() => !document.querySelector('main button svg.animate-spin'))
  check('issue: words typed during the post survive it', (await box.inputValue()).endsWith('I will add a test as well.'), await box.inputValue())
  await page.close()
}

// 05: a refused post keeps the draft and says why. Clear then empties both, by a real click.
{
  const page = await open('issue', 'refuse')
  const box = timelineBox(page)
  await box.fill('This will be refused by the forge.')
  await boxButton(page, 'Comment').click()
  await page.getByText(/Resource not accessible by integration/).first().waitFor()
  check('issue: the refused post keeps the draft', await box.inputValue() === 'This will be refused by the forge.')
  await shoot(page, '05-issue-post-refused')

  // Escape leaves the box, keeps the text, and is not consumed: window still sees it.
  await page.evaluate(() => { window.__escapes = 0; window.addEventListener('keydown', (e) => { if (e.key === 'Escape') window.__escapes += 1 }) })
  await box.focus()
  await page.keyboard.press('Escape')
  check('issue: Escape keeps the draft', await box.inputValue() === 'This will be refused by the forge.')
  check('issue: Escape leaves the box', await page.evaluate(() => document.activeElement?.tagName !== 'TEXTAREA'))
  check('issue: Escape still reaches window', await page.evaluate(() => window.__escapes) === 1)

  // A real mouse click at the button's centre, after checking nothing covers that point.
  const clear = boxButton(page, 'Clear')
  const r = await clear.boundingBox()
  const [x, y] = [r.x + r.width / 2, r.y + r.height / 2]
  check('issue: the point a click lands on is the Clear button', await page.evaluate(([px, py]) => document.elementFromPoint(px, py)?.closest('button')?.textContent?.trim() === 'Clear', [x, y]))
  await page.mouse.click(x, y)
  check('issue: Clear empties the box', await box.inputValue() === '')
  check('issue: Clear removes the failure notice', await page.getByText(/Resource not accessible by integration/).count() === 0)
  await page.close()
}

await context.close()
await browser.close()
srv.close()
if (failed) {
  console.error('one or more scenes did not render the expected state')
  process.exit(1)
}
console.log('done ->', OUT)
