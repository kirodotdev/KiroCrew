/**
 * Evidence harness for the Tagging queue's label `reason` following the dashboard
 * language.
 *
 * Runs the REAL built SPA gateway-free on the shared helpers -- `serveDist()` for
 * the loopback static server (ephemeral port, so a second run of this harness
 * cannot collide with the first) and `stubDashboardApi()` for the boot endpoints.
 * Only the Issue Radar routes this surface needs are supplied here, through the
 * stub's `extra` hook.
 *
 * WHAT A SCREENSHOT CAN AND CANNOT SHOW HERE, STATED UP FRONT. The `reason` is
 * rendered ONLY into a native `title` attribute (`UntaggedIssueCard.tsx`), and a
 * native tooltip is drawn by the OS, not the page -- Playwright cannot capture it
 * at any hover or zoom. Two consequences shape this harness:
 *
 *   * it takes ONE screenshot, of the queue, which shows the surface and its
 *     localized chrome -- the frame the tooltip belongs to. A second frame for the
 *     English scene would be byte-identical, since the only difference is inside an
 *     attribute; an earlier revision committed both and they shared a blob hash,
 *     claiming a visual contrast that cannot exist.
 *   * it READS BACK the `title` attribute under BOTH languages and prints them,
 *     which is where the contrast actually lives.
 *
 * Both scenes run against a German dashboard (`mc-lang`), differing only in the
 * language of the `reason` the backend returns -- which is exactly the axis the
 * fix changes.
 *
 * Usage: node scripts/capture-tagging-reason-language.mjs <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/tagging-reason-language'
mkdirSync(OUT, { recursive: true })

const REF = { owner: 'acme', repo: 'widget', provider: 'github', host: 'github.com' }
const REPOS = { repos: [{ ...REF, enabled: true, permissions: { push: true, triage: true } }] }

const LABELS = [
  { name: 'bug', color: 'd73a4a', description: "Something isn't working" },
  { name: 'enhancement', color: 'a2eeef', description: 'New feature or request' },
]

// Two untagged issues, so the queue is a queue rather than a single card.
const ISSUES = [
  { number: 314, title: 'Uploading a 2GB file stalls at 99% and never finishes', url: '#', labels: [], comments: 4, created_at: '2026-08-24T00:00:00Z', updated_at: '2026-08-28T00:00:00Z', author: 'ada', assignees: [] },
  { number: 318, title: 'Add a keyboard shortcut for the command palette', url: '#', labels: [], comments: 1, created_at: '2026-08-26T00:00:00Z', updated_at: '2026-08-27T00:00:00Z', author: 'grace', assignees: [] },
]

/** The `reason` prose, in the two languages this is about. */
const REASONS = {
  localized: {
    '314': [{ name: 'bug', reason: 'meldet einen Upload, der bei 99% stehen bleibt' }],
    '318': [{ name: 'enhancement', reason: 'fordert ein neues Tastenkuerzel an' }],
  },
  english: {
    '314': [{ name: 'bug', reason: 'reports an upload stalling at 99% and never finishing' }],
    '318': [{ name: 'enhancement', reason: 'requests a new keyboard shortcut' }],
  },
}

let suggestions = REASONS.localized

/** The Issue Radar routes this surface reads. Everything else is the shared stub's.
 *
 *  Returns `true` when it handled the route -- the stub falls through to its own
 *  fixtures on a falsy result, and `json()` resolves to undefined, so awaiting it
 *  without returning true would double-fulfill and throw. */
const issueRadarRoutes = async (path, route) => {
  const serve = async (body) => { await json(route, body); return true }
  if (path.endsWith('/issue-radar/repos')) return serve(REPOS)
  if (path.endsWith('/issue-radar/me')) return serve({ login: 'owner', ...REF })
  if (path.endsWith('/issue-radar/labels')) return serve({ ...REF, from_cache: true, labels: LABELS })
  if (path.endsWith('/issue-radar/members')) {
    return serve({ ...REF, members: [{ login: 'ada', role: 'admin' }], source: 'collaborators', from_cache: true })
  }
  if (path.endsWith('/issue-radar/settings')) {
    return serve({ ...REF, settings: { triage_labels: [], unlabeled_is_untriaged: true, good_first_issue_labels: [], notify_on_new_issue: false, revision: 1 } })
  }
  if (path.endsWith('/issue-radar/issues')) return serve({ ...REF, state: 'open', from_cache: true, issues: ISSUES })
  if (path.endsWith('/issue-radar/pulls')) return serve({ ...REF, state: 'open', from_cache: true, bulk_max: 50, pulls: [] })
  // The route under test: the queue plus whatever suggestions are servable for it.
  if (path.endsWith('/issue-radar/tagging')) {
    return serve({
      ...REF,
      issues: ISSUES,
      untagged: ISSUES.map(i => i.number),
      open_count: ISSUES.length,
      batch_size: 20,
      suggestions,
      label_counts: {},
      titles: {},
      generated_at: '2026-08-30T00:00:00Z',
      recommendations: [],
    })
  }
  if (path.endsWith('/issue-radar/deps')) return serve({ schema: 1, edges: [], nodes: {} })
  if (path.endsWith('/issue-radar/recent-repos')) return serve({ repos: [] })
  return false
}

const { srv, base } = await serveDist()
const browser = await chromium.launch()
const context = await browser.newContext({ viewport: { width: 1440, height: 900 } })

/** Open the app on a German dashboard, landed on the Tagging queue. */
async function open() {
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: 'light',
    extra: issueRadarRoutes,
    // Seeded through the stub rather than our own init script: Playwright does
    // not order separately registered init scripts, so seeding ourselves would
    // race the stub's storage clear.
    localStorageEntries: {
      // Synchronous first paint in the chosen language (see i18n/detect.ts).
      'mc-lang': 'de',
      'kc:issue-radar:active-repo': JSON.stringify(REF),
      'kc:issue-radar:ui-state': JSON.stringify({
        mainView: 'dashboard', dashboardTab: 'tagging', stateFilter: 'open',
      }),
    },
  })
  await page.goto(base + '/issue-radar', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)
  return page
}

/** The `title` a screenshot cannot show. Read from the DOM instead. */
const tooltips = (page) => page.evaluate(() =>
  Array.from(document.querySelectorAll('[title]'))
    .map(el => el.getAttribute('title') || '')
    .filter(t => /99%|keyboard|Tastenkuerzel|stehen/.test(t)))

// ── The frame. ONE screenshot, deliberately: the reason lives only in a `title`,
// so the localized and English scenes render byte-identical pixels -- an earlier
// revision committed both and they had the same blob hash, which claimed a visual
// contrast that cannot exist. What the frame is evidence of is the surface and its
// localized chrome; the contrast lives in the attribute reads below.
suggestions = REASONS.localized
let page = await open()
await page.getByText('#314').first().waitFor({ timeout: 15000 })
await page.screenshot({ path: `${OUT}/tag-01-queue.png` })
console.log('LOCALIZED tooltips:', JSON.stringify(await tooltips(page), null, 1))
await page.close()

// ── The defect, read from the DOM. Same German dashboard, reason still English --
// what an unsteered prompt (or a cache written before the switch) produces. No
// second screenshot: it would be the same pixels.
suggestions = REASONS.english
page = await open()
await page.getByText('#314').first().waitFor({ timeout: 15000 })
console.log('ENGLISH tooltips:', JSON.stringify(await tooltips(page), null, 1))

await context.close(); await browser.close(); srv.close()
console.log('done ->', OUT)
