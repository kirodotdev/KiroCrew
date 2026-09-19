/**
 * Screenshots for the Projects (thin Project bundles) page.
 *
 * Drives the isolated capture entry (website/capture/project-bundles.html),
 * which mounts the REAL ProjectBundlesPage. Every REAL /api call is answered
 * by page.route on the pathname; the Project payloads MATCH the backend
 * (src/kiro_crew/dashboard/handlers_project.py `_project_payload`) field for
 * field. Every frame ASSERTS its state before writing, so a frame cannot
 * document the wrong state:
 *   fe-01-empty       no Projects -> the empty-state card
 *   fe-02-list        two healthy Projects (repo-backed + source-less)
 *   fe-03-detail      payments-platform detail: repo, sessions, the "New chat"
 *                     start (the dashboard's one term for creating a slot), no
 *                     card for anything the manifest does not carry (no MCP,
 *                     no memory)
 *   fe-04-review      review_stale: Review-needed badge, stale files, disabled start,
 *                     the "Review files" button that opens the dialog with
 *                     "Pull updates" beside it, and a declared source not yet
 *                     cloned wearing "Pending review" with what lifts it
 *   fe-05-unavailable sources_unavailable: badge, the notice that leads with the
 *                     edit to make (project.yaml by its full path), the missing
 *                     source id in the notice AND marked "Source unavailable"
 *                     inside the Repositories card -- one term for one state
 *   fe-06-list-light  the two-Project list in light theme
 *   fe-07-review-dialog the digest-bound review dialog: each file's path, status
 *                     and whole content, a removed entry, three unreadable entries
 *                     (a link out of the tree, a file that is not text, a file the
 *                     credential redactor changed) each with its path and a copy
 *                     button, withholding "Accept these changes" with the "Ask the
 *                     agent" hand-off beside the blocked note
 *   fe-08-review-dialog-clean the same dialog with only acceptable entries, so
 *                     the primary action is live
 *   fe-09-add-form    the Add-existing form open: Folder or Git URL, submit, Cancel
 *   fe-10-create-form the Create form open: Project name, Project folder
 *   fe-11-local-copy  detail scrolled to the Local copy card with "Pull updates"
 *                     in its header and the "Remove from Kiro Crew" danger
 *                     action in view
 *   fe-12-remove-confirm the Remove confirm dialog, naming the Project, what
 *                     removal keeps and what happens to its sessions (history
 *                     kept, work stopped; new chats outside the project)
 *   fe-13-sidebar-create-menu the chat sidebar's create menu with its Projects
 *                     section: one row per healthy Project, then Manage projects;
 *                     shot after the open animation settles, on an opaque surface
 *   fe-14-composer-chip the composer chip on a Project-bound session: the
 *                     Project's name, visibly inert (muted, faded, not-allowed
 *                     cursor, aria-disabled), and the explanation bubble that
 *                     a CLICK opens (plus a -closeup clip of the composer,
 *                     since the chip is small)
 *   fe-15-nav-rail    the nav rail with the Projects entry, active
 *   fe-16-sidebar-create-menu-paused the create menu with a review-stale
 *                     Project: its row disabled, naming "Review needed", the
 *                     why as its tooltip; the healthy row still live; Manage
 *                     projects present
 *   fe-17-review-dialog-copy-failed the review dialog after a copy that did
 *                     not reach the clipboard: the shared error notice under
 *                     the path, no tick, the entry still readable
 *   fe-18-pull-diverged the Local copy card after a pull the server refused
 *                     with the structured project_checkout_diverged body: one
 *                     line per diverged checkout with its detail (the remedy
 *                     says "pull updates again", never "sync"), then
 *                     "Updated: bundle, web" for what did fast-forward
 *   fe-19-remove-cleanup-pending the list after a removal that forgot the
 *                     registration but could not delete every on-disk root: the
 *                     Project gone from the list, the notice above it naming
 *                     the Project and every leftover path, with the hand-off
 *   fe-20-pull-partial the Local copy card after a 200 pull whose response
 *                     lists `unavailable_sources`: the outcome names the source
 *                     that could not be fetched through the shared error notice
 *                     with the hand-off (never the plain "Updates pulled."
 *                     line), and the refetched Project wears Source unavailable
 *
 * fe-01..fe-12 and fe-17..fe-20 drive the isolated entry. fe-13..fe-16 drive the SHIPPED SPA
 * root (index.html -> main.tsx -> App) against the same route interception, so
 * the sidebar, composer and rail are the production tree, not a re-mount:
 * a session bound to the Project via `project_id` is served by the slot
 * endpoints, and the rest of the shell's boot reads are answered with the
 * shapes their consumers iterate.
 *
 * Usage:
 *   npx vite --host 127.0.0.1 --port 6832 --strictPort   # in another shell
 *   node scripts/capture-project-bundles.mjs http://127.0.0.1:6832 <evidence dir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'

const BASE = process.argv[2] || 'http://127.0.0.1:6832'
const OUT = process.argv[3] || '../temp-screenshots/project-bundles'
mkdirSync(OUT, { recursive: true })

// ── Fixtures: shaped exactly like `_project_payload` output ──────────────────
// A repo-backed, healthy Project. `workspace_source` names the primary source
// id (the checkout that supplies the working dir), matching the backend.
const PAYMENTS_ID = '5f2c9a71-8e0d-4b3a-9c14-7d6e2f0a1b33'
const PRIMARY_SOURCE_ID = 'payments-api-1a2b3c4d'
const INFRA_SOURCE_ID = 'payments-infra-3f9a1c2b'

const paymentsSources = [
  { id: PRIMARY_SOURCE_ID, type: 'repo', url: 'https://github.com/acme/payments-api', default_branch: 'main', role: 'primary' },
]
// A secondary reference repo that is unavailable in fe-05: the notice names its
// id and the Repositories card row carries the same "Source unavailable" badge
// the header does. `status` is the per-row state the payload carries.
const paymentsSourcesWithInfra = [
  { ...paymentsSources[0], status: 'healthy' },
  { id: INFRA_SOURCE_ID, type: 'repo', url: 'https://github.com/acme/payments-infra', default_branch: 'main', role: 'reference', status: 'unavailable' },
]
// fe-04: the review-stale Project also declares a source it has not cloned --
// the clone waits on the review that names it -- so its row is `pending`.
const paymentsSourcesWithPending = [
  { ...paymentsSources[0], status: 'healthy' },
  { id: INFRA_SOURCE_ID, type: 'repo', url: 'https://github.com/acme/payments-infra', default_branch: 'main', role: 'reference', status: 'pending' },
]
const paymentsRegistrations = [
  { origin: 'managed_git', path: '~/.kiro/crew/projects/managed/' + PAYMENTS_ID + '/bundle', syncable: true },
]
const paymentsSessions = [
  { key: 'sess-onboard-4821', title: 'Add idempotency keys to charge intents', messages: 34, running: false, live: false },
  { key: 'sess-refund-1190', title: 'Refund webhook replay audit', messages: 12, running: true, live: true },
]

function payments(health, sources = paymentsSources) {
  return {
    id: PAYMENTS_ID,
    name: 'payments-platform',
    description: 'Charge, refund and webhook services for the payments platform.',
    workspace_source: PRIMARY_SOURCE_ID,
    sources,
    registrations: paymentsRegistrations,
    health,
    sessions: paymentsSessions,
  }
}

const docsSite = {
  id: 'a1d47e90-3c22-49f5-8b6e-0f9c1a2b4d55',
  name: 'docs-site',
  description: 'Public documentation site.',
  workspace_source: 'self',
  sources: [],
  registrations: [{ origin: 'local', path: '~/projects/docs-site', syncable: false }],
  health: { status: 'healthy', code: 'project_healthy' },
  sessions: [],
}

const HEALTHY = { status: 'healthy', code: 'project_healthy' }
const STALE_FILES = ['.kiro/settings/mcp.json', '.kiro/agents/payments.md', '.kiro/hooks/legacy.json', '.kiro/skills/deploy/run.sh', '.kiro/skills/deploy/assets/badge.png', '.kiro/hooks/deploy-notify.json']
const REVIEW_STALE = { status: 'review_stale', code: 'project_review_stale', stale_files: STALE_FILES }

// GET /api/project-bundles/{id}/review — shaped like the digest-bound preview
// contract: digest + files[{path, status}] plus the WHOLE `content` for
// added/changed and a `reason` for unreadable entries. There is no partial
// display: an entry is shown whole or is unreadable.
const MCP_JSON = [
  '{',
  '  "mcpServers": {',
  '    "atlassian": {',
  '      "command": "npx",',
  '      "args": ["-y", "mcp-atlassian"],',
  '      "env": { "ATLASSIAN_SITE": "acme" }',
  '    }',
  '  }',
  '}',
  '',
].join('\n')
const AGENT_MD = [
  '# payments agent',
  '',
  'Reconciles refund webhooks against charge intents and drafts the daily ledger note.',
  '',
  '## Tools',
  '- atlassian (PAY board)',
  '',
].join('\n')
const REVIEW_PREVIEW_BLOCKED = {
  digest: 'sha256:9c1f0a7d2e4b8f6a',
  files: [
    { path: '.kiro/settings/mcp.json', status: 'changed', content: MCP_JSON },
    { path: '.kiro/agents/payments.md', status: 'added', content: AGENT_MD },
    { path: '.kiro/hooks/legacy.json', status: 'removed' },
    { path: '.kiro/skills/deploy/run.sh', status: 'unreadable', reason: 'link-outside-root' },
    { path: '.kiro/skills/deploy/assets/badge.png', status: 'unreadable', reason: 'binary' },
    // The credential redactor changed what would be displayed, so the bytes
    // on screen would not be the bytes accepted: reviewed outside the dashboard.
    { path: '.kiro/hooks/deploy-notify.json', status: 'unreadable', reason: 'redacted' },
  ],
}
const REVIEW_PREVIEW_CLEAN = {
  digest: 'sha256:5e2a9b0c7d1f3e8a',
  files: REVIEW_PREVIEW_BLOCKED.files.slice(0, 3),
}
const SOURCES_UNAVAILABLE = { status: 'sources_unavailable', code: 'project_sources_unavailable', unavailable_sources: [INFRA_SOURCE_ID] }
// POST .../sync refused: the bundle and the `web` source fast-forwarded, the
// primary source has local commits. Nothing that moved is unwound.
const SYNC_DIVERGED = {
  error: 'project_checkout_diverged',
  code: 'project_checkout_diverged',
  project_id: PAYMENTS_ID,
  advanced: ['bundle', 'web'],
  diverged: [{ checkout: PRIMARY_SOURCE_ID, detail: 'local-commits' }],
}
// POST .../sync answered 200 with a partial pull: the bundle and the primary
// fast-forwarded, the infra reference could not be fetched. The payload is the
// Project as the pull left it (health sources_unavailable, the infra row
// `unavailable`) plus the top-level list of ids the dashboard names.
const SYNC_PARTIAL = {
  ...payments(SOURCES_UNAVAILABLE, paymentsSourcesWithInfra),
  unavailable_sources: [INFRA_SOURCE_ID],
}
// DELETE .../{id} succeeded (the registration is gone) but the two on-disk roots
// could not be deleted: the server lists them, relative to projects/.
const REMOVE_CLEANUP_PENDING = { ok: true, id: PAYMENTS_ID, cleanup_pending: [`managed/${PAYMENTS_ID}`, `state/${PAYMENTS_ID}`] }

const browser = await chromium.launch()
let failed = false

function check(name, ok, detail) {
  console.log(`${name}: ${ok ? 'OK' : 'MISMATCH'} ${detail}`)
  if (!ok) failed = true
  return ok
}

/** Open the page with the given Project list and initial route/theme.
 *  Gateway-free: answer every REAL /api call. Predicate on the pathname so a
 *  glob does not swallow vite-served source modules. Array-shaped endpoints
 *  answer [] ({} crashes their .map consumers). */
async function open(projects, { theme = 'dark', route = '/project-bundles', reviewPreview = null, clipboardFails = false, sync = null, remove = null } = {}) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  // fe-17: a clipboard that refuses on BOTH layers, the way a plain-HTTP
  // remote gateway or a refused clipboard-write permission behaves, so the
  // copy button's failure path is the one photographed.
  if (clipboardFails) {
    await page.addInitScript(() => {
      Object.defineProperty(navigator, 'clipboard', {
        configurable: true,
        value: { writeText: () => Promise.reject(new DOMException('denied', 'NotAllowedError')) },
      })
      document.execCommand = () => false
    })
  }
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route2 => {
    const path = new URL(route2.request().url()).pathname
    if (path === '/api/project-bundles') {
      return route2.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ projects }) })
    }
    if (reviewPreview && path === `/api/project-bundles/${PAYMENTS_ID}/review` && route2.request().method() === 'GET') {
      return route2.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(reviewPreview) })
    }
    if (sync && path === `/api/project-bundles/${PAYMENTS_ID}/sync` && route2.request().method() === 'POST') {
      // A 200 pull is followed by a list refetch: answer it with the Project
      // as the pull left it, so the page shows the state the response named.
      if (sync.status === 200) projects = projects.map(project => project.id === PAYMENTS_ID ? payments(sync.body.health, sync.body.sources) : project)
      return route2.fulfill({ status: sync.status, contentType: 'application/json', body: JSON.stringify(sync.body) })
    }
    if (remove && path === `/api/project-bundles/${PAYMENTS_ID}` && route2.request().method() === 'DELETE') {
      // The list is re-read after the removal: answer it without the Project.
      projects = projects.filter(project => project.id !== PAYMENTS_ID)
      return route2.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(remove) })
    }
    const isList = /commands|skills|agents|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route2.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  const url = `${BASE}/capture/project-bundles.html?theme=${theme}&route=${encodeURIComponent(route)}`
  await page.goto(url)
  await page.waitForSelector('[data-capture-root]')
  return page
}

// fe-01 — empty list
{
  const page = await open([])
  await page.getByTestId('project-bundles-empty').waitFor()
  const rows = await page.locator('[data-project-id]').count()
  check('fe-01 empty no rows', rows === 0, `rows=${rows}`)
  const empty = await page.getByTestId('project-bundles-empty').isVisible()
  check('fe-01 empty state visible', empty, 'empty-state card shown')
  const addLabel = await page.getByRole('button', { name: 'Add existing project' }).isVisible()
  check('fe-01 add button label', addLabel, 'header action reads "Add existing project"')
  await page.screenshot({ path: `${OUT}/fe-01-empty.png` })
  await page.close()
}

// fe-02 — two healthy Projects
{
  const page = await open([payments(HEALTHY), docsSite])
  await page.locator('[data-project-id]').first().waitFor()
  const rows = await page.locator('[data-project-id]').count()
  check('fe-02 two rows', rows === 2, `rows=${rows}`)
  const healthyBadges = await page.getByText('Healthy', { exact: true }).count()
  check('fe-02 both healthy', healthyBadges === 2, `healthy-badges=${healthyBadges}`)
  const names = (await page.locator('[data-project-id]').allTextContents()).join(' | ')
  check('fe-02 names present', /payments-platform/.test(names) && /docs-site/.test(names), 'both project names rendered')
  const addLabel = await page.getByRole('button', { name: 'Add existing project' }).isVisible()
  check('fe-02 add button label', addLabel, 'header action reads "Add existing project"')
  await page.screenshot({ path: `${OUT}/fe-02-list.png` })
  await page.close()
}

// fe-03 — payments-platform detail (healthy)
{
  const page = await open([payments(HEALTHY)], { route: `/project-bundles?project=${PAYMENTS_ID}` })
  await page.getByRole('heading', { name: 'payments-platform' }).waitFor()
  const headerHealthy = await page.getByText('Healthy', { exact: true }).count()
  check('fe-03 header healthy badge', headerHealthy >= 1, `healthy=${headerHealthy}`)
  // The manifest carries no MCP and no memory key, so the detail shows no
  // card for them: nothing is declared that the Project does not act on.
  const declared = await page.getByText('Declared context', { exact: true }).count()
  check('fe-03 no declared-context card', declared === 0, `"Declared context" count=${declared}`)
  const mcpRow = await page.getByText(/MCP servers|Project memory/).count()
  check('fe-03 no mcp/memory rows', mcpRow === 0, `mcp/memory text count=${mcpRow}`)
  const localCopy = await page.getByText('Local copy', { exact: true }).isVisible()
  check('fe-03 local copy card follows repositories', localCopy, 'Local copy card present')
  const repoUrl = await page.getByText('https://github.com/acme/payments-api').isVisible()
  check('fe-03 repository url', repoUrl, 'primary repo url shown')
  const s1 = await page.getByText('Add idempotency keys to charge intents').isVisible()
  const s2 = await page.getByText('Refund webhook replay audit').isVisible()
  check('fe-03 two sessions listed', s1 && s2, 'both session titles rendered')
  const counted = await page.getByText('34 messages', { exact: true }).isVisible()
  check('fe-03 session row says what it counts', counted, '"34 messages", not a bare number')
  const startDisabled = await page.getByRole('button', { name: 'New chat' }).isDisabled()
  check('fe-03 new-chat enabled (healthy)', startDisabled === false, `disabled=${startDisabled}`)
  const oldStart = await page.getByRole('button', { name: /New session/ }).count()
  check('fe-03 one term for creating a slot', oldStart === 0, `"New session" buttons=${oldStart}; the start reads "New chat"`)
  // One label for the one pull action, on the shared Project's Local copy card.
  const pulls = await page.getByRole('button', { name: 'Pull updates' }).count()
  check('fe-03 pull updates on the card', pulls === 1, `Pull updates buttons=${pulls}`)
  const oldLabels = await page.getByRole('button', { name: /^(Sync project|Retry sync)$/ }).count()
  check('fe-03 no sync/retry labels', oldLabels === 0, `Sync project / Retry sync buttons=${oldLabels}`)
  await page.screenshot({ path: `${OUT}/fe-03-detail.png` })
  await page.close()
}

// fe-04 — review_stale
{
  const page = await open([payments(REVIEW_STALE, paymentsSourcesWithPending)], { route: `/project-bundles?project=${PAYMENTS_ID}` })
  await page.getByRole('heading', { name: 'payments-platform' }).waitFor()
  const badge = await page.getByText('Review needed', { exact: true }).isVisible()
  check('fe-04 review-needed badge', badge, 'Review needed badge shown')
  let listed = 0
  for (const file of STALE_FILES) if (await page.getByText(file, { exact: true }).isVisible()) listed += 1
  check('fe-04 stale files listed', listed === STALE_FILES.length, `listed=${listed}/${STALE_FILES.length} (any .kiro/ depth)`)
  const reviewBtn = page.getByRole('button', { name: 'Review files' })
  check('fe-04 review button', await reviewBtn.isVisible(), 'Review files button visible; accepting is not one click')
  // "Pull updates" stands in the same action row as "Review files": every
  // unreadable remedy ends in "pull updates", so the pull is where the copy points.
  const actionRow = reviewBtn.locator('xpath=..')
  const pullBeside = await actionRow.getByRole('button', { name: 'Pull updates' }).count()
  check('fe-04 pull beside review', pullBeside === 1, `Pull updates in the Review files row (count=${pullBeside})`)
  const oldLabels = await page.getByRole('button', { name: /^(Sync project|Retry sync)$/ }).count()
  check('fe-04 no sync/retry labels', oldLabels === 0, `Sync project / Retry sync buttons=${oldLabels}`)
  // The declared-but-not-cloned source: a muted "Pending review" badge on its
  // row and the line saying when the clone happens; the healthy row is bare.
  const pendingRow = page.getByTestId(`project-source-${INFRA_SOURCE_ID}`)
  check('fe-04 pending badge on the declared row', (await pendingRow.getByText('Pending review', { exact: true }).count()) === 1, `Pending review badge in the ${INFRA_SOURCE_ID} row`)
  check('fe-04 pending helper', await pendingRow.getByText('Cloned after you accept the review.', { exact: true }).isVisible(), 'the row says the clone waits on the acceptance')
  check('fe-04 pending row not called unavailable', (await pendingRow.getByText('Source unavailable', { exact: true }).count()) === 0, 'no "Source unavailable" on a pending row')
  const primaryRow = page.getByTestId(`project-source-${PRIMARY_SOURCE_ID}`)
  check('fe-04 healthy row bare', (await primaryRow.getByText(/Pending review|Source unavailable/).count()) === 0, 'the primary row carries no state badge')
  const pendingPaint = await pendingRow.getByText('Pending review', { exact: true }).evaluate(el => getComputedStyle(el).color)
  const errPaint = await page.getByText('Review needed', { exact: true }).evaluate(el => getComputedStyle(el).color)
  check('fe-04 pending badge muted', pendingPaint !== errPaint, `pending badge color ${pendingPaint} differs from the warn badge ${errPaint}`)
  const startDisabled = await page.getByRole('button', { name: 'New chat' }).isDisabled()
  check('fe-04 new-chat disabled', startDisabled === true, `disabled=${startDisabled}`)
  // Taller frame: the banner with its action row and the Repositories card
  // with the pending row must be in the same picture.
  await page.setViewportSize({ width: 1440, height: 1240 })
  await page.screenshot({ path: `${OUT}/fe-04-review.png` })
  await page.close()
}

// fe-05 — sources_unavailable
{
  const page = await open([payments(SOURCES_UNAVAILABLE, paymentsSourcesWithInfra)], { route: `/project-bundles?project=${PAYMENTS_ID}` })
  await page.getByRole('heading', { name: 'payments-platform' }).waitFor()
  const headerBadge = await page.locator('h2 + span', { hasText: 'Source unavailable' }).count()
  check('fe-05 source-unavailable badge', headerBadge === 1, `Source unavailable badge beside the heading (count=${headerBadge})`)
  const idInNotice = await page.locator('li', { hasText: INFRA_SOURCE_ID }).count()
  check('fe-05 missing source id listed', idInNotice === 1, `notice lists ${INFRA_SOURCE_ID} (count=${idInNotice})`)
  const notice = await page.getByRole('alert').first().textContent()
  check('fe-05 notice leads with the edit', /^Edit ~\/\.kiro\/crew\/projects\/managed\/[^ ]*\/project\.yaml to fix the sources listed below, then pull updates\./.test((notice ?? '').trim()), 'first sentence is the edit, naming project.yaml by its full path')
  check('fe-05 notice says what the pause costs', /New sessions are paused until every source is available\./.test(notice ?? ''), 'the pause and its end are named')
  check('fe-05 no Manifest label, no cloned', !/Manifest:|cloned/.test(notice ?? ''), 'neither "Manifest:" nor "cloned" in the notice')
  const infraRow = page.getByTestId(`project-source-${INFRA_SOURCE_ID}`)
  const idInRow = await infraRow.getByText(INFRA_SOURCE_ID).count()
  check('fe-05 source id in the repositories card', idInRow >= 1, `repositories row names ${INFRA_SOURCE_ID}`)
  const rowBadge = await infraRow.getByText('Source unavailable', { exact: true }).count()
  check('fe-05 one term on the repo row', rowBadge === 1, `Source unavailable badge inside the infra row (count=${rowBadge})`)
  const cloneFailed = await page.getByText('Clone failed', { exact: true }).count()
  check('fe-05 no second term', cloneFailed === 0, `"Clone failed" anywhere on the page (count=${cloneFailed})`)
  const apiRow = page.getByTestId(`project-source-${PRIMARY_SOURCE_ID}`)
  const apiBadge = await apiRow.getByText(/Source unavailable|Pending review/).count()
  check('fe-05 healthy repo row unmarked', apiBadge === 0, `primary row carries no state badge (count=${apiBadge})`)
  const pullInBanner = await page.getByRole('alert').first().locator('xpath=..').getByRole('button', { name: 'Pull updates' }).count()
  check('fe-05 pull updates under the notice', pullInBanner === 1, `Pull updates beside the notice (count=${pullInBanner})`)
  const startDisabled = await page.getByRole('button', { name: 'New chat' }).isDisabled()
  check('fe-05 new-chat disabled', startDisabled === true, `disabled=${startDisabled}`)
  // Taller frame: the banner naming the id and the Repositories row wearing
  // the badge must both be in the same picture.
  await page.setViewportSize({ width: 1440, height: 1240 })
  await page.screenshot({ path: `${OUT}/fe-05-unavailable.png` })
  await page.close()
}

// fe-06 — the two-Project list in light theme
{
  const page = await open([payments(HEALTHY), docsSite], { theme: 'light' })
  await page.locator('[data-project-id]').first().waitFor()
  const rows = await page.locator('[data-project-id]').count()
  check('fe-06 two rows (light)', rows === 2, `rows=${rows}`)
  const themeAttr = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
  check('fe-06 light theme', themeAttr === 'kiro-light', `data-theme=${themeAttr}`)
  const addLabel = await page.getByRole('button', { name: 'Add existing project' }).isVisible()
  check('fe-06 add button label', addLabel, 'header action reads "Add existing project"')
  await page.screenshot({ path: `${OUT}/fe-06-list-light.png` })
  await page.close()
}

/** Open the review-stale detail and click through to the review dialog. */
async function openReviewDialog(reviewPreview, options = {}) {
  const page = await open([payments(REVIEW_STALE)], { route: `/project-bundles?project=${PAYMENTS_ID}`, reviewPreview, ...options })
  await page.getByRole('heading', { name: 'payments-platform' }).waitFor()
  await page.getByRole('button', { name: 'Review files' }).click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor()
  await dialog.getByTestId('project-review-file').first().waitFor()
  return { page, dialog }
}

// fe-07 — the review dialog with never-acceptable entries
{
  const { page, dialog } = await openReviewDialog(REVIEW_PREVIEW_BLOCKED)
  const title = await dialog.getByText('Review changed files in payments-platform').isVisible()
  check('fe-07 dialog title', title, 'changed-files title (not the first-review one)')
  const rows = await dialog.getByTestId('project-review-file').count()
  check('fe-07 six files', rows === 6, `rows=${rows}`)
  const mcpContent = await dialog.getByLabel('Content of .kiro/settings/mcp.json').textContent()
  check('fe-07 mcp.json content shown whole', mcpContent === MCP_JSON, 'the bytes being accepted are on screen, all of them')
  const agentContent = await dialog.getByLabel('Content of .kiro/agents/payments.md').textContent()
  check('fe-07 agent content shown whole', agentContent === AGENT_MD, 'markdown agent body on screen, all of it')
  const partial = await dialog.getByText(/Only the first part of this file/).count()
  check('fe-07 no partial-display note', partial === 0, `partial-note count=${partial}`)
  const removed = await dialog.getByText('This file was removed. Accepting records that it is gone.').isVisible()
  check('fe-07 removed entry explained', removed, 'removed entry has no content and says why')
  const link = await dialog.getByText(/link that points outside the Project/).isVisible()
  check('fe-07 link entry explained', link, 'link-outside-root reason rendered')
  const binary = await dialog.getByText(/This file is not text, so its content cannot be shown or accepted\. Replace it with a text file inside the Project, pull updates, and review again\./).isVisible()
  check('fe-07 binary entry explained', binary, 'binary reason rendered, ending in pull-updates-and-review')
  const dialogText = (await dialog.textContent()) ?? ''
  check('fe-07 one verb in the dialog', !/\bsync/i.test(dialogText), 'no "sync" anywhere in the review dialog copy')
  const redacted = await dialog.getByText('Contains a value the dashboard must redact, so it cannot be shown whole. Review this file outside the dashboard.').isVisible()
  check('fe-07 redacted entry explained', redacted, 'redacted reason rendered: no content, review outside the dashboard')
  const redactedRow = dialog.getByTestId('project-review-file').nth(5)
  check('fe-07 redacted entry shows no content', (await redactedRow.getByRole('region').count()) === 0, 'no content region under the redacted entry')
  // Each unreadable entry hands over its path: a line of its own plus a
  // labelled copy button, so the remedy is actionable.
  const fixPaths = dialog.getByTestId('project-review-fix-path')
  const fixCount = await fixPaths.count()
  check('fe-07 fix-path lines', fixCount === 3, `fix-path lines=${fixCount}`)
  const fixTexts = await fixPaths.allTextContents()
  check('fe-07 fix-path names all three files', /\.kiro\/skills\/deploy\/run\.sh/.test(fixTexts[0] ?? '') && /badge\.png/.test(fixTexts[1] ?? '') && /deploy-notify\.json/.test(fixTexts[2] ?? ''), `paths=${JSON.stringify(fixTexts)}`)
  const copyButtons = await dialog.getByRole('button', { name: 'Copy path' }).count()
  check('fe-07 copy-path buttons', copyButtons === 3, `Copy path buttons=${copyButtons}`)
  const blockedNotice = dialog.getByTestId('project-review-blocked')
  const blockedNote = await blockedNotice.textContent()
  check('fe-07 blocked note', /3 entries cannot be accepted until they are fixed/.test(blockedNote ?? ''), `note=${JSON.stringify(blockedNote)}`)
  // The withheld accept is not terminal: the hand-off stands beside the note.
  const blockedHandoff = await blockedNotice.getByRole('button', { name: 'Ask the agent' }).count()
  check('fe-07 hand-off beside the blocked note', blockedHandoff === 1, `Ask the agent inside the blocked notice (count=${blockedHandoff})`)
  const acceptDisabled = await dialog.getByRole('button', { name: 'Accept these changes' }).isDisabled()
  check('fe-07 accept withheld', acceptDisabled === true, `disabled=${acceptDisabled}`)
  const statuses = await dialog.getByTestId('project-review-file').evaluateAll(rows =>
    rows.map(row => row.querySelector('span.rounded-full')?.textContent))
  check('fe-07 status badges', JSON.stringify(statuses) === JSON.stringify(['Changed', 'Added', 'Removed', 'Unreadable', 'Unreadable', 'Unreadable']), `statuses=${JSON.stringify(statuses)}`)
  // The frame documents the withheld accept: scroll the body so the
  // unreadable entries and the blocked note sit above the disabled button.
  await dialog.getByTestId('project-review-blocked').scrollIntoViewIfNeeded()
  await page.screenshot({ path: `${OUT}/fe-07-review-dialog.png` })
  await page.close()
}

// fe-08 — the review dialog with only acceptable entries
{
  const { page, dialog } = await openReviewDialog(REVIEW_PREVIEW_CLEAN)
  const rows = await dialog.getByTestId('project-review-file').count()
  check('fe-08 three files', rows === 3, `rows=${rows}`)
  const blocked = await dialog.getByTestId('project-review-blocked').count()
  check('fe-08 no blocked note', blocked === 0, `blocked-note count=${blocked}`)
  const acceptDisabled = await dialog.getByRole('button', { name: 'Accept these changes' }).isDisabled()
  check('fe-08 accept live', acceptDisabled === false, `disabled=${acceptDisabled}`)
  await page.screenshot({ path: `${OUT}/fe-08-review-dialog-clean.png` })
  await page.close()
}

/** A freshly mounted Card eases in; a frame shot mid-fade documents a dimmed
 *  form. Wait until the element's opacity has settled at 1. */
async function settled(locator) {
  await locator.evaluate(el => new Promise(resolve => {
    const tick = () => (getComputedStyle(el).opacity === '1' ? resolve() : requestAnimationFrame(tick))
    tick()
  }))
}

// fe-09 — the Add-existing form
{
  const page = await open([payments(HEALTHY), docsSite])
  await page.locator('[data-project-id]').first().waitFor()
  await page.getByRole('button', { name: 'Add existing project' }).click()
  const source = page.getByLabel('Folder or Git URL')
  check('fe-09 source field', await source.isVisible(), 'labelled "Folder or Git URL" input')
  const form = source.locator('xpath=ancestor::form')
  await settled(form.locator('xpath=ancestor::*[contains(@class, "animate-rise")][1]'))
  const submit = form.getByRole('button', { name: 'Add existing project' })
  check('fe-09 submit present', await submit.isVisible(), 'form submit reads "Add existing project"')
  check('fe-09 submit withheld while empty', await submit.isDisabled(), `disabled=${await submit.isDisabled()} with an empty field`)
  check('fe-09 cancel present', await form.getByRole('button', { name: 'Cancel' }).isVisible(), 'Cancel beside the submit')
  const help = await page.getByText('Register an existing Project folder, or clone one from a Git URL.').isVisible()
  check('fe-09 form help', help, 'the form says what it accepts')
  await page.screenshot({ path: `${OUT}/fe-09-add-form.png` })
  await page.close()
}

// fe-10 — the Create form
{
  const page = await open([payments(HEALTHY), docsSite])
  await page.locator('[data-project-id]').first().waitFor()
  await page.getByRole('button', { name: 'Create project' }).click()
  const name = page.getByLabel('Project name')
  const folder = page.getByLabel('Project folder')
  check('fe-10 name field', await name.isVisible(), 'labelled "Project name" input')
  check('fe-10 folder field', await folder.isVisible(), 'labelled "Project folder" input')
  const form = name.locator('xpath=ancestor::form')
  await settled(form.locator('xpath=ancestor::*[contains(@class, "animate-rise")][1]'))
  const submit = form.getByRole('button', { name: 'Create project' })
  check('fe-10 submit present', await submit.isVisible(), 'form submit reads "Create project"')
  check('fe-10 submit withheld while empty', await submit.isDisabled(), `disabled=${await submit.isDisabled()} with empty fields`)
  check('fe-10 cancel present', await form.getByRole('button', { name: 'Cancel' }).isVisible(), 'Cancel beside the submit')
  await page.screenshot({ path: `${OUT}/fe-10-create-form.png` })
  await page.close()
}

// fe-11 — detail scrolled to the Local copy card
{
  const page = await open([payments(HEALTHY)], { route: `/project-bundles?project=${PAYMENTS_ID}` })
  await page.getByRole('heading', { name: 'payments-platform' }).waitFor()
  const remove = page.getByRole('button', { name: 'Remove from Kiro Crew' })
  await remove.scrollIntoViewIfNeeded()
  check('fe-11 local copy card', await page.getByText('Local copy', { exact: true }).isVisible(), 'Local copy card in view')
  check('fe-11 project id shown', await page.getByText(PAYMENTS_ID, { exact: true }).isVisible(), 'Project ID on the card')
  check('fe-11 registration path', await page.getByText(paymentsRegistrations[0].path, { exact: true }).isVisible(), 'the bundle path on disk')
  check('fe-11 pull on the card', await page.getByRole('button', { name: 'Pull updates' }).isVisible(), 'Pull updates in the card header')
  check('fe-11 no sync label', (await page.getByRole('button', { name: /^(Sync project|Retry sync)$/ }).count()) === 0, 'no "Sync project" / "Retry sync" on the page')
  check('fe-11 remove visible', await remove.isVisible(), '"Remove from Kiro Crew" in view')
  const danger = await remove.evaluate(el => getComputedStyle(el).color)
  const body = await page.evaluate(() => getComputedStyle(document.body).color)
  check('fe-11 remove reads as danger', danger !== body, `button color ${danger} differs from body text ${body}`)
  await page.screenshot({ path: `${OUT}/fe-11-local-copy.png` })
  await page.close()
}

// fe-12 — the Remove confirm dialog
{
  const page = await open([payments(HEALTHY)], { route: `/project-bundles?project=${PAYMENTS_ID}` })
  await page.getByRole('heading', { name: 'payments-platform' }).waitFor()
  const remove = page.getByRole('button', { name: 'Remove from Kiro Crew' })
  await remove.scrollIntoViewIfNeeded()
  await remove.click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor()
  check('fe-12 title names the project', await dialog.getByText('Remove payments-platform?').isVisible(), 'confirm title carries the Project name')
  check('fe-12 body says what stays and what sessions do', await dialog.getByText('Folders you added stay on disk. Kiro Crew removes only storage it created for this project. Its sessions keep their history but stop working; start new chats outside this project.').isVisible(), 'body names what removal keeps, that sessions stop working, and the way out: new chats outside the project')
  check('fe-12 confirm repeats the trigger', await dialog.getByRole('button', { name: 'Remove from Kiro Crew' }).isVisible(), 'the confirm reads "Remove from Kiro Crew", the label that opened it')
  check('fe-12 no bare confirm label', await dialog.getByRole('button', { name: 'Remove project' }).count() === 0, 'no "Remove project" button in the dialog')
  check('fe-12 cancel action', await dialog.getByRole('button', { name: 'Cancel' }).isVisible(), 'Cancel beside it')
  await page.screenshot({ path: `${OUT}/fe-12-remove-confirm.png` })
  await page.close()
}

// fe-18 — a pull the server refused: the structured diverged body
{
  const page = await open([payments(HEALTHY, [paymentsSources[0], { id: 'web', type: 'repo', url: 'https://github.com/acme/payments-web', default_branch: 'main', role: 'reference' }])], { route: `/project-bundles?project=${PAYMENTS_ID}`, sync: { status: 409, body: SYNC_DIVERGED } })
  await page.getByRole('heading', { name: 'payments-platform' }).waitFor()
  const pull = page.getByRole('button', { name: 'Pull updates' })
  await pull.scrollIntoViewIfNeeded()
  await pull.click()
  const outcome = page.getByTestId('project-sync-outcome-local')
  await outcome.waitFor()
  const alert = outcome.getByRole('alert')
  const text = (await alert.textContent()) ?? ''
  check('fe-18 diverged entry with its detail', /has commits the shared repository does not, so the pull could not fast-forward it and changed nothing/.test(text), 'the local-commits line')
  check('fe-18 remedy uses the one verb', /then pull updates again\./.test(text) && !/\bsync/i.test(text), 'the remedy says "pull updates again", never "sync"')
  check('fe-18 diverged entry names its checkout', new RegExp(`Checkout: ${PRIMARY_SOURCE_ID}`).test(text), `Checkout: ${PRIMARY_SOURCE_ID}`)
  check('fe-18 what did move, last', /Checkout: [^\n]+\n\nUpdated: bundle, web/.test(text), '"Updated: bundle, web" after the refusal')
  check('fe-18 no raw status', !/HTTP 409|project_checkout_diverged/.test(text), 'no raw code or status in the notice')
  check('fe-18 hand-off', (await alert.getByRole('button', { name: 'Ask the agent' }).count()) === 1, 'Ask the agent beside the refusal')
  check('fe-18 not called pulled', (await page.getByText('Updates pulled.').count()) === 0, 'no "Updates pulled." line')
  await alert.scrollIntoViewIfNeeded()
  await page.screenshot({ path: `${OUT}/fe-18-pull-diverged.png` })
  await page.close()
}

// fe-19 — a removal that forgot the registration but left files behind
{
  const page = await open([payments(HEALTHY), docsSite], { route: `/project-bundles?project=${PAYMENTS_ID}`, remove: REMOVE_CLEANUP_PENDING })
  await page.getByRole('heading', { name: 'payments-platform' }).waitFor()
  const remove = page.getByRole('button', { name: 'Remove from Kiro Crew' })
  await remove.scrollIntoViewIfNeeded()
  await remove.click()
  const dialog = page.getByRole('dialog')
  await dialog.waitFor()
  await dialog.getByRole('button', { name: 'Remove from Kiro Crew' }).click()
  const notice = page.getByTestId('project-remove-cleanup-pending')
  await notice.waitFor()
  await page.locator('[data-project-id]').first().waitFor()
  const rows = await page.locator('[data-project-id]').count()
  check('fe-19 project gone from the list', rows === 1 && !/payments-platform/.test((await page.locator('[data-project-id]').allTextContents()).join(' ')), `rows=${rows}, docs-site remains`)
  const text = (await notice.textContent()) ?? ''
  check('fe-19 notice is the shared alert', (await notice.getAttribute('role')) === 'alert', `role=${await notice.getAttribute('role')}`)
  check('fe-19 notice names the project', /payments-platform/.test(text), 'the removed Project is named')
  check('fe-19 notice says removed, files left', /Removed from Kiro Crew\. Some files could not be deleted\./.test(text), 'the lead sentence')
  check('fe-19 notice lists every leftover path', REMOVE_CLEANUP_PENDING.cleanup_pending.every(path => text.includes(path)), `paths=${JSON.stringify(REMOVE_CLEANUP_PENDING.cleanup_pending)}`)
  check('fe-19 hand-off', (await notice.getByRole('button', { name: 'Ask the agent' }).count()) === 1, 'Ask the agent in the notice')
  check('fe-19 dismissable', (await notice.getByRole('button', { name: 'Dismiss' }).count()) === 1, 'a Dismiss control')
  const noticeBox = await notice.boundingBox()
  const rowBox = await page.locator('[data-project-id]').first().boundingBox()
  check('fe-19 notice above the list', !!noticeBox && !!rowBox && noticeBox.y + noticeBox.height <= rowBox.y, `notice bottom ${noticeBox?.y + noticeBox?.height} <= first row top ${rowBox?.y}`)
  await page.screenshot({ path: `${OUT}/fe-19-remove-cleanup-pending.png` })
  await page.close()
}

// fe-20 — a 200 pull that could not fetch every source
{
  const page = await open([payments(HEALTHY, paymentsSourcesWithInfra.map(source => ({ ...source, status: 'healthy' })))], { route: `/project-bundles?project=${PAYMENTS_ID}`, sync: { status: 200, body: SYNC_PARTIAL } })
  await page.getByRole('heading', { name: 'payments-platform' }).waitFor()
  check('fe-20 healthy before the pull', (await page.locator('h2 + span', { hasText: 'Healthy' }).count()) === 1, 'Healthy badge beside the heading before the pull')
  // Healthy before the pull: the one control is the Local copy card's.
  const pull = page.getByRole('button', { name: 'Pull updates' })
  await pull.scrollIntoViewIfNeeded()
  await pull.click()
  const outcome = page.getByTestId('project-sync-outcome-local')
  await outcome.waitFor()
  const partial = outcome.getByTestId('project-sync-partial-local')
  await partial.waitFor()
  const text = (await partial.textContent()) ?? ''
  check('fe-20 partial notice is the shared alert', (await partial.getAttribute('role')) === 'alert', `role=${await partial.getAttribute('role')}`)
  check('fe-20 partial notice names the count and the id', new RegExp(`Updates pulled; 1 source could not be fetched: ${INFRA_SOURCE_ID}`).test(text), `notice=${JSON.stringify(text)}`)
  check('fe-20 hand-off', (await partial.getByRole('button', { name: 'Ask the agent' }).count()) === 1, 'Ask the agent beside the partial pull')
  check('fe-20 not a plain success', (await outcome.getByRole('status').count()) === 0 && (await page.getByText('Updates pulled.', { exact: true }).count()) === 0, 'no "Updates pulled." status line')
  check('fe-20 no raw code', !/project_sources_unavailable|HTTP/.test(text), 'no raw code or status in the notice')
  // The refetched Project wears the state the response named: the header
  // badge and the infra row both say Source unavailable, the primary row is bare.
  await page.locator('h2 + span', { hasText: 'Source unavailable' }).waitFor()
  const infraRow = page.getByTestId(`project-source-${INFRA_SOURCE_ID}`)
  check('fe-20 refetched row marked', (await infraRow.getByText('Source unavailable', { exact: true }).count()) === 1, `Source unavailable badge inside the ${INFRA_SOURCE_ID} row`)
  check('fe-20 primary row bare', (await page.getByTestId(`project-source-${PRIMARY_SOURCE_ID}`).getByText(/Source unavailable|Pending review/).count()) === 0, 'the primary row carries no state badge')
  // The outcome stays under the card that was clicked; the banner that the
  // refetch brought in carries its own Pull control, not the outcome.
  check('fe-20 outcome anchored to the card', (await page.getByTestId('project-sync-outcome-sources').count()) === 0, 'no outcome under the banner')
  check('fe-20 two pull controls after the refetch', (await page.getByRole('button', { name: 'Pull updates' }).count()) === 2, 'the banner and the card each carry Pull updates')
  await partial.scrollIntoViewIfNeeded()
  await page.screenshot({ path: `${OUT}/fe-20-pull-partial.png` })
  await page.close()
}

// ── The shipped shell: sidebar, composer, rail ───────────────────────────────
// A session bound to the Project, as the slot endpoints report it: the slot
// carries `project_id` and the Project's working directory, which is what the
// composer chip and the sidebar read. Same fixture on the list and the detail
// endpoint, since the page refetches the slot on mount.
const PROJECT_SLOT = {
  key: paymentsSessions[0].key,
  title: paymentsSessions[0].title,
  messages: 2,
  running: false,
  project: paymentsRegistrations[0].path,
  project_id: PAYMENTS_ID,
}
const PROJECT_SLOT_MESSAGES = [
  { role: 'user', content: 'Where do charge intents get their idempotency key today?', cls: '', ts: '2026-09-17T09:12:00Z' },
  { role: 'assistant', content: 'In `services/charges/intents.py` — the key is minted per request and never persisted, which is the gap.', cls: '', ts: '2026-09-17T09:12:20Z' },
]

/** Open the production SPA at `route` with every boot read answered in the
 *  shape its consumer iterates, onboarding recorded as done, and the WebSocket
 *  left to fail (the shell tolerates a closed socket; it polls instead). */
async function openShell(projects, route) {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1 })
  await page.addInitScript(() => {
    for (const key of ['mc-onboarded', 'mc-import-onboarded', 'mc-privacy-acked']) localStorage.setItem(key, '1')
    localStorage.setItem('mc-theme', 'dark')
  })
  const json = (route2, body) => route2.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })
  await page.route(u => new URL(u).pathname.startsWith('/api/'), route2 => {
    const path = new URL(route2.request().url()).pathname
    if (path === '/api/project-bundles') return json(route2, { projects })
    if (path === '/api/chat/slots') return json(route2, [PROJECT_SLOT])
    if (path === `/api/chat/slots/${PROJECT_SLOT.key}`) return json(route2, { ...PROJECT_SLOT, has_more: false, total: PROJECT_SLOT_MESSAGES.length, messages: PROJECT_SLOT_MESSAGES })
    if (path === '/api/theme/boot') return json(route2, { mode: 'dark', color: 'kiro', onboarded: true, import_onboarded: true, privacy_acked: true })
    if (path.startsWith('/api/kiro-prerequisite')) return json(route2, { ready: true, installed: true, authenticated: true, initial_setup_complete: true })
    if (path === '/api/instances') return json(route2, { instances: [] })
    if (path === '/api/apps' || path === '/api/apps/registry') return json(route2, { apps: [] })
    if (/\/api\/(approvals|terminal\/sessions|themes|chat\/(tags|pins|folders|tag-columns))$/.test(path)) return json(route2, [])
    const isList = /commands|skills|agents$|sessions|files|history|models|artifacts|folders|slots$/.test(path)
    return route2.fulfill({ status: 200, contentType: 'application/json', body: isList ? '[]' : '{}' })
  })
  await page.goto(`${BASE}${route}`)
  const nav = page.getByRole('navigation', { name: 'Main navigation' })
  await nav.waitFor()
  return { page, nav }
}

/** Open the create menu and wait until it has finished opening: Radix marks
 *  the content `data-state=open` at once, but the entry animation (fade-in,
 *  zoom) still runs, and a frame shot inside it shows the session list
 *  through a half-transparent menu. Wait for every animation on the content
 *  to finish and its opacity to settle at 1, then prove the surface itself
 *  is opaque — the token it paints with, not the animation, is what keeps the
 *  rows behind it from showing through. */
async function openCreateMenu(page, tag) {
  await page.getByRole('button', { name: 'More create options' }).click()
  const menu = page.getByRole('menu')
  await menu.waitFor()
  await page.locator('[role="menu"][data-state="open"]').waitFor()
  await menu.evaluate(async el => {
    await Promise.all(el.getAnimations({ subtree: true }).map(a => a.finished.catch(() => {})))
    await new Promise(resolve => {
      const tick = () => (getComputedStyle(el).opacity === '1' ? resolve() : requestAnimationFrame(tick))
      tick()
    })
  })
  const paint = await menu.evaluate(el => {
    const cs = getComputedStyle(el)
    // Chromium serializes a token painted through color-mix as
    // `color(srgb r g b)` (or `… / a` when translucent); a plain token comes
    // back as `rgb()`/`rgba()`. No alpha term in either form means opaque.
    const bg = cs.backgroundColor
    const rgba = bg.match(/^rgba?\([^)]*?(?:,\s*([\d.]+))?\)$/)
    const srgb = bg.match(/^color\(srgb [\d.]+ [\d.]+ [\d.]+(?: \/ ([\d.%]+))?\)$/)
    const term = rgba ? rgba[1] : srgb ? srgb[1] : undefined
    const alpha = !rgba && !srgb ? NaN : term === undefined ? 1 : term.endsWith('%') ? Number(term.slice(0, -1)) / 100 : Number(term)
    return { state: el.getAttribute('data-state'), opacity: cs.opacity, bg, alpha, animations: el.getAnimations({ subtree: true }).filter(a => a.playState === 'running').length }
  })
  check(`${tag} menu open and settled`, paint.state === 'open' && paint.opacity === '1' && paint.animations === 0, `data-state=${paint.state} opacity=${paint.opacity} running-animations=${paint.animations}`)
  check(`${tag} menu surface opaque`, paint.alpha === 1, `background=${paint.bg} alpha=${paint.alpha}`)
  return menu
}

// fe-13 — the sidebar's create menu, Projects section
{
  const { page } = await openShell([payments(HEALTHY), docsSite], `/chat?sid=${PROJECT_SLOT.key}`)
  await page.getByText('Where do charge intents get their idempotency key today?').waitFor()
  const menu = await openCreateMenu(page, 'fe-13')
  check('fe-13 projects section label', await menu.getByText('Projects', { exact: true }).isVisible(), 'a "Projects" group label in the menu')
  const rows = menu.getByRole('menuitem', { name: /^(payments-platform|docs-site) — / })
  const rowCount = await rows.count()
  check('fe-13 one row per healthy project', rowCount === 2, `project rows=${rowCount}`)
  const rowText = (await rows.allTextContents()).join(' | ')
  check('fe-13 rows carry name and path', /payments-platform/.test(rowText) && /docs-site/.test(rowText) && /~\/projects\/docs-site/.test(rowText), `rows=${JSON.stringify(rowText)}`)
  check('fe-13 manage projects', await menu.getByRole('menuitem', { name: 'Manage projects' }).isVisible(), '"Manage projects" closes the section')
  check('fe-13 plain new chat first', await menu.getByRole('menuitem', { name: 'New chat' }).isVisible(), 'the ordinary New chat stays in the menu')
  await page.screenshot({ path: `${OUT}/fe-13-sidebar-create-menu.png` })
  await page.close()
}

// fe-14 — the composer chip on a Project-bound session
{
  const { page } = await openShell([payments(HEALTHY)], `/chat?sid=${PROJECT_SLOT.key}`)
  await page.getByText('Where do charge intents get their idempotency key today?').waitFor()
  const explanation = "Project: payments-platform. This session works inside this Project's files and can't switch."
  const chip = page.getByRole('button', { name: explanation })
  await chip.waitFor()
  check('fe-14 chip label', (await chip.textContent())?.trim() === 'payments-platform', `label=${JSON.stringify(await chip.textContent())}`)
  // Inert by aria, not by the native attribute: a natively disabled button
  // takes no click, and the click is how the explanation is reached.
  check('fe-14 chip aria-disabled', (await chip.getAttribute('aria-disabled')) === 'true', `aria-disabled=${await chip.getAttribute('aria-disabled')}`)
  check('fe-14 chip not natively disabled', (await chip.getAttribute('disabled')) === null, 'no disabled attribute (it must stay focusable and clickable)')
  check('fe-14 chip disabled to Playwright', await chip.isDisabled(), `isDisabled=${await chip.isDisabled()} (aria-disabled counts)`)
  // Visibly inert: muted text at reduced opacity, the not-allowed cursor,
  // measured on the painted element, not read off a class list.
  const paint = await chip.evaluate(el => {
    const cs = getComputedStyle(el)
    return { opacity: cs.opacity, cursor: cs.cursor, color: cs.color }
  })
  check('fe-14 chip faded', Number(paint.opacity) < 1, `opacity=${paint.opacity}`)
  check('fe-14 chip not-allowed cursor', paint.cursor === 'not-allowed', `cursor=${paint.cursor}`)
  const bodyColor = await page.evaluate(() => getComputedStyle(document.body).color)
  check('fe-14 chip muted text', paint.color !== bodyColor, `chip color ${paint.color} differs from body text ${bodyColor}`)
  const hoverGeometry = await chip.evaluate(el => [...el.classList].filter(c => /hover:(scale|-?translate)/.test(c)))
  check('fe-14 no hover scale/translate', hoverGeometry.length === 0, `hover geometry classes=${JSON.stringify(hoverGeometry)}`)
  // The explanation is reached by a CLICK, not only by hover: the bubble is
  // a real tooltip element, described-by from the chip, painted headless.
  const noTip = await page.getByRole('tooltip').count()
  check('fe-14 no bubble before the click', noTip === 0, `tooltips=${noTip}`)
  await chip.click({ force: true })
  const tip = page.getByRole('tooltip')
  await tip.waitFor()
  check('fe-14 click opens the explanation', (await tip.textContent())?.trim() === explanation, `tooltip=${JSON.stringify(await tip.textContent())}`)
  check('fe-14 bubble described-by', (await chip.getAttribute('aria-describedby')) === (await tip.getAttribute('id')), 'aria-describedby resolves to the bubble')
  const tipBox = await tip.boundingBox()
  check('fe-14 bubble painted', !!tipBox && tipBox.width > 0 && tipBox.height > 0, `bubble box=${JSON.stringify(tipBox)}`)
  await page.screenshot({ path: `${OUT}/fe-14-composer-chip.png` })
  // The chip is one small control at the foot of a 1440x900 shell; a second
  // frame clips the composer so the label, its inert paint and the bubble
  // the click opened are legible together.
  const box = await chip.boundingBox()
  await page.screenshot({
    path: `${OUT}/fe-14-composer-chip-closeup.png`,
    clip: { x: Math.max(0, box.x - 140), y: Math.max(0, box.y - 130), width: 900, height: 170 },
  })
  await page.close()
}

// fe-15 — the nav rail with the Projects entry
{
  const { page, nav } = await openShell([payments(HEALTHY), docsSite], '/project-bundles')
  await page.locator('[data-project-id]').first().waitFor()
  const entry = nav.locator('[data-onboarding-nav="project-bundles"]')
  check('fe-15 projects entry', await entry.isVisible(), 'Projects row in the rail')
  check('fe-15 entry label', (await entry.textContent())?.trim() === 'Projects', `label=${JSON.stringify(await entry.textContent())}`)
  const active = await entry.evaluate(el => el.classList.contains('nav-active'))
  check('fe-15 entry active on its route', active, `nav-active=${active}`)
  const title = await page.getByTestId('page-title').textContent()
  check('fe-15 page behind it', title === 'Projects', `page title=${JSON.stringify(title)}; the entry opens the Projects page`)
  await page.screenshot({ path: `${OUT}/fe-15-nav-rail.png` })
  await page.close()
}

// fe-16 — the sidebar's create menu with a paused (review-stale) Project
{
  const { page } = await openShell([payments(REVIEW_STALE), docsSite], `/chat?sid=${PROJECT_SLOT.key}`)
  await page.getByText('Where do charge intents get their idempotency key today?').waitFor()
  const menu = await openCreateMenu(page, 'fe-16')
  check('fe-16 projects section label', await menu.getByText('Projects', { exact: true }).isVisible(), 'a "Projects" group label in the menu')
  const paused = menu.getByTestId('new-chat-project-review_stale')
  check('fe-16 paused row listed', await paused.isVisible(), 'the review-stale Project is listed, not dropped')
  check('fe-16 paused row disabled', (await paused.getAttribute('aria-disabled')) === 'true', `aria-disabled=${await paused.getAttribute('aria-disabled')}`)
  const pausedText = await paused.textContent()
  check('fe-16 paused row names the state', /payments-platform/.test(pausedText ?? '') && /Review needed/.test(pausedText ?? ''), `row=${JSON.stringify(pausedText)}`)
  const why = await paused.getAttribute('title')
  check('fe-16 paused row says why', /waiting for your review/.test(why ?? ''), `title=${JSON.stringify(why)}`)
  check('fe-16 paused row accessible name', /^payments-platform — Review needed\. /.test((await paused.getAttribute('aria-label')) ?? ''), 'aria-label carries state and reason')
  const pausedPaint = await paused.evaluate(el => { const cs = getComputedStyle(el); return { opacity: cs.opacity, cursor: cs.cursor, pointer: cs.pointerEvents } })
  check('fe-16 paused row faded', Number(pausedPaint.opacity) < 1, `opacity=${pausedPaint.opacity}`)
  check('fe-16 paused row reachable by pointer', pausedPaint.pointer !== 'none' && pausedPaint.cursor === 'not-allowed', `pointer-events=${pausedPaint.pointer} cursor=${pausedPaint.cursor} (the tooltip needs the pointer)`)
  const healthyRow = menu.getByRole('menuitem', { name: /^docs-site — / })
  check('fe-16 healthy row live', (await healthyRow.count()) === 1 && (await healthyRow.getAttribute('aria-disabled')) === null, 'docs-site stays a live row')
  check('fe-16 manage projects', await menu.getByRole('menuitem', { name: 'Manage projects' }).isVisible(), '"Manage projects" present with a paused Project')
  await paused.hover()
  await page.screenshot({ path: `${OUT}/fe-16-sidebar-create-menu-paused.png` })
  await page.close()
}

// fe-17 — the review dialog after a copy that did not reach the clipboard
{
  const { page, dialog } = await openReviewDialog(REVIEW_PREVIEW_BLOCKED, { clipboardFails: true })
  const rows = dialog.getByTestId('project-review-file')
  const linkRow = rows.nth(3)
  check('fe-17 link entry', /run\.sh/.test((await linkRow.textContent()) ?? ''), 'the fourth row is the link-outside-root entry')
  const before = await dialog.getByTestId('project-review-copy-failed').count()
  check('fe-17 no notice before the copy', before === 0, `notices=${before}`)
  await linkRow.getByRole('button', { name: 'Copy path' }).click()
  const notice = linkRow.getByTestId('project-review-copy-failed')
  await notice.waitFor()
  check('fe-17 notice is the shared alert', (await notice.getAttribute('role')) === 'alert', `role=${await notice.getAttribute('role')}`)
  check('fe-17 notice says what to do', /The path could not be copied\. Select it above and copy it by hand\./.test((await notice.textContent()) ?? ''), `notice=${JSON.stringify(await notice.textContent())}`)
  check('fe-17 no hand-off in the notice', (await notice.getByRole('button', { name: 'Ask the agent' }).count()) === 0, 'the remedy is in the notice; nothing for the agent')
  check('fe-17 no tick over an unchanged clipboard', (await linkRow.getByRole('button', { name: 'Path copied' }).count()) === 0, 'no "Path copied"')
  check('fe-17 button says it failed', (await linkRow.getByRole('button', { name: 'Copy failed' }).count()) === 1, 'the button announces the failure too')
  // The entry stays readable above the notice: reason, path, copy affordance.
  check('fe-17 reason still shown', await linkRow.getByText(/link that points outside the Project/).isVisible(), 'reason line intact')
  check('fe-17 path still shown', await linkRow.getByTestId('project-review-fix-path').getByText('.kiro/skills/deploy/run.sh').isVisible(), 'path line intact')
  const otherNotices = await dialog.getByTestId('project-review-copy-failed').count()
  check('fe-17 one notice, under its own entry', otherNotices === 1, `notices=${otherNotices}`)
  await notice.scrollIntoViewIfNeeded()
  await page.screenshot({ path: `${OUT}/fe-17-review-dialog-copy-failed.png` })
  await page.close()
}

await browser.close()
if (failed) {
  console.error('CAPTURE FAILED: at least one frame did not match its asserted state')
  process.exit(1)
}
console.log('all frames verified')
