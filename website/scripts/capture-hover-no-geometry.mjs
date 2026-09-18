/**
 * Screenshot harness + geometry check for "hover is a HOVER" (fix/hover-no-row-scale).
 *
 * The branch removed hover-triggered scale/translate from 22 call sites: a rail
 * row, a Badge, a StatCard, a colour swatch and friends no longer change SIZE OR
 * POSITION under the cursor. They still change COLOUR, and press feedback
 * (whileTap / active:scale-95) was deliberately kept.
 *
 * Why a real browser rather than a unit test: jsdom mocks framer-motion
 * wholesale, so `whileHover={{ scale: 1.02 }}` renders as nothing at all there,
 * and jsdom computes no layout — `getBoundingClientRect()` returns zeros and CSS
 * `hover:scale-110` is never applied because there is no hover engine and no
 * transform resolution. A vitest assertion on this diff can therefore only
 * confirm that a className string no longer contains a token, which is the same
 * evidence as reading the diff. The claim under review is geometric, so it needs
 * a real compositor, a real pointer, and a measured rect.
 *
 * What the scene table covers:
 *   - shared treatments: rail row + Apps overflow, Badge, StatCard, Slider;
 *   - changed call sites: Display, schedule, comments, Issue Radar suggestion
 *     and crew stat, jump-to-bottom, Restart, memory record, AWS metric caption,
 *     Folder settings colour/tag controls, session colours, Tag manager,
 *     Library colour/no-colour controls, and Crew session colour;
 *   - both FeaturedSpotlight image render paths: full and compact collection;
 *   - the rail-row press scene is a negative control proving press feedback
 *     remains. SessionColorPicker is intentionally absent because it has no
 *     non-test importer and therefore no real running-app surface to capture.
 *
 * What makes a frame invalid — every one of these exits non-zero rather than
 * writing a PNG:
 *   - the app never rendered the surface (blank page, error boundary, stub gap);
 *   - the surface was still moving BEFORE the hover (async data reflowing the
 *     page), which would make any delta unattributable;
 *   - the rect moved during or after the hover beyond EPSILON;
 *   - the hover affordance did NOT appear where one is claimed (otherwise
 *     "geometry unchanged" is equally satisfied by a dead element);
 *   - the press control did NOT move (a mis-aimed pointer would otherwise read
 *     as "press feedback removed too").
 *
 * Usage:
 *   npm run build            # serveDist() serves website/dist, not src
 *   node scripts/capture-hover-no-geometry.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, rmSync } from 'node:fs'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { makeExtra as makeIssueCrewExtra, seedState as issueCrewSeedState } from './lib/issue-radar-crews-fixtures.mjs'

/**
 * Allowed rect drift, in CSS pixels.
 *
 * 0.25px is half a DEVICE pixel at the deviceScaleFactor 2 these frames are
 * captured at, so anything under it cannot be rendered, let alone seen — it is
 * sub-pixel rounding in the compositor, not motion. It is also far below every
 * effect this branch removed, so the assertion is not merely tight, it is
 * decisive in both directions:
 *   rail row   202px x 1.02  -> +4.04px wide, x -2.02px
 *   badge       ~85px x 1.05 -> +4.2px wide
 *   swatch       28px x 1.10 -> +2.8px wide
 *   stat card   -translate-y-0.5 -> y -2.0px
 * The smallest of those is 8x the epsilon, so a restored hover transform cannot
 * hide under it, and sub-pixel noise cannot fail the run.
 */
const EPSILON = 0.25

/** Press feedback must be VISIBLE, not just non-zero: 0.97 on a 202px row is ~6px. */
const PRESS_MIN_DELTA = 1.5

const OUT = process.argv[2]
  || (process.env.KIROCREW_SCRATCH ? `${process.env.KIROCREW_SCRATCH}/evidence` : '../temp-screenshots/hover-no-geometry')

// Without a slot the chat route's own shell throws on an undefined field and the
// ErrorBoundary replaces the whole app, rail included — so the rail scenes need
// one even though chat itself is not the subject.
const SLOTS = [
  {
    key: 's1',
    title: 'Hover geometry evidence',
    messages: 2,
    running: false,
    agent: 'kirocrew',
    mode: '',
    created: '2026-09-06T01:00:00Z',
    last_ts: '2026-09-06T04:00:00Z',
    folder_id: '',
  },
]

const SCHEDULE_JOBS = [{
  id: 'geometry-probe',
  name: 'Geometry probe',
  schedule: '0 9 * * 1',
  cron_expr: '0 9 * * 1',
  timezone: 'UTC',
  message: 'Capture hover geometry.',
  enabled: true,
  agent: 'kirocrew',
  created_ts: 1_757_376_000,
  next_run_ts: 1_757_980_800,
}]

const COMMENT_ARTIFACT = {
  slug: 'hover-evidence',
  name: 'Hover evidence',
  kind: 'markdown',
  source: 'chat',
  description: 'Real anchored-comment fixture for hover evidence.',
  tags: ['evidence'],
  version: 1,
  pinned: false,
  created_at: '2026-09-01T00:00:00Z',
  updated_at: '2026-09-01T00:00:00Z',
  content: 'Doc body\n\nThe comment bubble anchors this sentence in the real artifact body.\n',
}

const COMMENT_FIXTURE = {
  id: 'hover-comment',
  origin: 'local',
  scope: 'private',
  author: 'reviewer',
  is_agent: false,
  body: 'Keep the gutter bubble still while it paints.',
  anchor: { quote: 'comment bubble', prefix: 'The ', suffix: ' anchors', version_number: 1 },
  thread_id: 'hover-comment',
  status: 'open',
  sync_state: 'local_only',
  created_at: '2026-09-01T01:00:00Z',
  updated_at: '2026-09-01T01:00:00Z',
}

const ISSUE_REPO = {
  // The gate's kirodotdev/KiroCrew slug exemption cannot see this one, because
  // owner and repo are separate fields here rather than one joined string.
  owner: 'kirodotdev', repo: 'KiroCrew', provider: 'github', host: 'github.com', // brand-ok: repo slug, not prose
}
const ISSUE = {
  number: 11099,
  title: 'Hover geometry should stay still',
  body: 'Hovering should paint without moving the target.',
  url: 'https://github.com/kirodotdev/KiroCrew/issues/11099',
  labels: ['bug'], comments: 1, state: 'open', author: 'reviewer', assignees: [],
  created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-02T00:00:00Z',
}
const ISSUE_LABELS = [
  { name: 'bug', color: 'd73a4a', description: 'Something is not working' },
  { name: 'needs-triage', color: 'fbca04', description: 'Needs triage' },
]

const CHAT_FOLDERS = [
  { id: 'f1', name: 'Evidence', icon: '📷', order: 0, collapsed: false, color: '#6366f1', tags: ['t1'] },
]
const CHAT_TAGS = [
  { id: 't1', name: 'Evidence', color: '#22c55e', status: false, order: 0 },
  { id: 't2', name: 'Review', color: '#f59e0b', status: true, order: 1 },
]

const CHAT_MESSAGES = Array.from({ length: 36 }, (_, i) => ({
  role: i % 2 ? 'assistant' : 'user',
  ts: `2026-09-06T0${Math.floor(i / 12) + 1}:${String(i % 12).padStart(2, '0')}:00Z`,
  content: `${i % 2 ? 'Measured answer' : 'Geometry question'} ${i + 1}. ${'A real transcript row makes the scroll control observable. '.repeat(5)}`,
}))
const CHAT_DETAIL = {
  running: false,
  has_more: false,
  total: CHAT_MESSAGES.length,
  queue: [],
  messages: CHAT_MESSAGES,
  context_pct: 72,
  context_used_tokens: 144_000,
  context_window_tokens: 200_000,
}

const MEMORY_RECORD = {
  kind: 'fact', id: 'hover-record', key: 'hover.record', text: 'A saved memory card paints without lifting.',
  value_json: '"A saved memory card paints without lifting."', source: 'user_explicit',
  revision: '1111111111111111111111111111111111111111111111111111111111111111',
  updated_at: '2026-09-01T00:00:00Z', metadata: {},
}

const NAV_APPS = Array.from({ length: 18 }, (_, i) => ({
  name: `hover-nav-${i}`,
  displayName: `Hover Nav ${i}`,
  enabled: true,
  origin: 'builtin',
  manifest: {
    name: `hover-nav-${i}`,
    displayName: `Hover Nav ${i}`,
    ui: { pages: [{ route: `/apps/hover-nav-${i}`, label: `Hover Nav ${i}`, icon: 'Package' }] },
  },
}))

const STORE_APPS = [
  { name: 'hover-main', displayName: 'Hover Main', description: 'Full hero treatment.', author: 'Kiro Crew', version: '1.0.0', tags: ['developer-tools'], heroImage: '/hover-main.svg', featured: 1, origin: 'registry', provenance: 'official', verified: true },
  { name: 'hover-compact-a', displayName: 'Hover Compact A', description: 'Compact collection member.', author: 'Kiro Crew', version: '1.0.0', tags: ['developer-tools'], origin: 'registry', provenance: 'official', verified: true },
  { name: 'hover-compact-b', displayName: 'Hover Compact B', description: 'Compact collection member.', author: 'Kiro Crew', version: '1.0.0', tags: ['developer-tools'], origin: 'registry', provenance: 'official', verified: true },
  { name: 'hover-row-peer', displayName: 'Hover Row Peer', description: 'Second row card.', author: 'Kiro Crew', version: '1.0.0', tags: ['developer-tools'], origin: 'registry', provenance: 'official', verified: true },
]
const STORE_SECTIONS = [
  { form: 'full', items: [{ type: 'app', appRefs: ['hover-main'], artwork: { url: 'https://apps.crew.kiro.dev/hover-main.svg', alt: 'Full hero evidence' } }] },
  { form: 'row', items: [
    { type: 'collection', appRefs: ['hover-compact-a', 'hover-compact-b'], title: 'Compact hover collection', artwork: { url: 'https://apps.crew.kiro.dev/hover-compact.svg', alt: 'Compact collection evidence' } },
    { type: 'app', appRefs: ['hover-row-peer'], artwork: { url: 'https://apps.crew.kiro.dev/hover-peer.svg', alt: 'Peer row evidence' } },
  ] },
]

const AWS_ACCOUNTS = {
  supported: true,
  accounts: [{
    account: '111122223333', name: 'evidence', health: 'ok',
    profiles: [{ name: 'evidence-readonly', kind: 'sso', region: 'us-west-2', account: '111122223333', default: true, identityOk: true }],
  }],
  totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
}
const AWS_DRIVE = {
  exists: true, bucket: 'hover-evidence', region: 'us-west-2',
  usage: {
    bytes: 1024, objects: 1,
    sections: {
      drive: { objects: 1, bytes: 1024 },
      library: { objects: 0, bytes: 0 },
      backup: { objects: 0, bytes: 0 },
    },
  },
}

const done = async (route, body) => { await json(route, body); return true }

const chatUiApi = async (path, route) => {
  if (path === '/api/chat/tags') return done(route, CHAT_TAGS)
  if (path === '/api/chat/tag-columns') return done(route, [])
  if (path === '/api/chat/slots/s1/autocompact') {
    return done(route, { pct: 75, global_pct: 70, min: 5, max: 90 })
  }
  if (path === '/api/chat/slots/s1') return done(route, CHAT_DETAIL)
  return false
}

const memoryApi = async (path, route) => {
  if (path === '/api/memory/stores') {
    return done(route, {
      active: 'default',
      stores: [{ name: 'default', is_default: true, exists: true, lineage: 'v1', memory_version: 1, semantic_count: 1, episodic_count: 0, lessons_count: 0 }],
    })
  }
  if (path === '/api/memory/settings') return done(route, { history_idle_hours: 3, history_max_days: 90, migrated: true })
  if (path === '/api/memory/stats') return done(route, { semantic_active: 1, episodic_active: 0, embedded_count: 1, migrated: true, entries: 1, size_bytes: 1024, provider: 'local' })
  if (path === '/api/memory/embedding-status') return done(route, { enabled: true, provider: 'local', model_available: true, server_healthy: true, setup_step: 'done', model_id: 'all-MiniLM-L6-v2', model_dim: 384, model_source: 'default', model_path: '', reembed: { step: 'idle', done: 0, total: 0, error: '' } })
  if (path === '/api/memory/semantic') return done(route, { entries: [] })
  if (path === '/api/memory/preferences' || path === '/api/memory/projects' || path === '/api/memory/history') return done(route, { content: '' })
  if (path === '/api/memory/records') return done(route, { entries: [MEMORY_RECORD], total: 1, has_more: false })
  if (path === '/api/lessons') return done(route, { lessons: [] })
  if (path === '/api/memory/retired') return done(route, { retired: [] })
  if (path === '/api/memory/backups') return done(route, { backups: [] })
  return false
}

const navAppsApi = async (path, route) => {
  if (path === '/api/apps') return done(route, NAV_APPS)
  return false
}

const appStoreApi = async (path, route) => {
  if (path === '/api/apps/registry') {
    return done(route, {
      apps: STORE_APPS,
      categoryOrder: [],
      editorialSections: STORE_SECTIONS,
      serverPlatform: { os: 'linux', arch: 'x64' },
    })
  }
  if (path === '/api/apps/registries') return done(route, { registries: [] })
  if (path === '/api/apps') return done(route, [])
  return false
}

const awsApi = async (path, route) => {
  const root = '/api/apps/aws-control'
  const app = path.startsWith(root) ? path.slice(root.length) : ''
  if (app === '/accounts') return done(route, AWS_ACCOUNTS)
  if (path === '/api/aws/consent') {
    const service = new URL(route.request().url()).searchParams.get('service') || 's3'
    return done(route, { service, granted: true, region: 'us-west-2', credentialSource: 'profile evidence-readonly', account: '111122223333', identityResolved: true, revokedOnAccountChange: false, grant: { account: '111122223333', region: 'us-west-2', profile: 'evidence-readonly', granted_at: '2026-09-01T00:00:00Z' } })
  }
  if (/^\/drive\/[^/]+$/.test(app)) return done(route, AWS_DRIVE)
  if (/^\/drive\/[^/]+\/list$/.test(app)) return done(route, { folders: [], files: [] })
  if (/^\/costs\/[^/]+$/.test(app)) return done(route, { monthToDate: 1.25, currency: 'USD', fetchedAt: '2026-09-01T00:00:00Z', fresh: true, consentMissing: false })
  if (/^\/shares(\/[^/]+)?$/.test(app)) return done(route, { shares: [] })
  if (/^\/backup\/[^/]+$/.test(app)) return done(route, { nightly: false, runs: {}, remote: null, jobs: {} })
  if (app === '/profiles/available') return done(route, { supported: true, profiles: [], max: 20 })
  return false
}

const issueCrewApi = makeIssueCrewExtra(json)

const installHoverArt = page => page.route('**/hover-*.svg', route => route.fulfill({
  status: 200,
  contentType: 'image/svg+xml',
  body: '<svg xmlns="http://www.w3.org/2000/svg" width="1600" height="900"><defs><linearGradient id="g"><stop stop-color="#7c3aed"/><stop offset="1" stop-color="#2563eb"/></linearGradient></defs><rect width="1600" height="900" fill="url(#g)"/><circle cx="800" cy="450" r="180" fill="#fff" opacity=".22"/></svg>',
}))

const openFolderConfig = async page => {
  await page.locator('[aria-label="More create options"]').click()
  await page.getByText('New folder', { exact: true }).click()
}

const openTagManagerPalette = async page => {
  await page.locator('.sidebar button[aria-label="More options"]').first().click()
  await page.getByText(/Manage tags/).first().click()
  await page.locator('[data-testid="tag-color-t1"]').click()
}

const openSessionMenu = async page => {
  const row = page.locator('[data-slot-key="s1"]')
  await row.hover()
  await row.locator('button[aria-label="More options"]').click()
}

const openArtifactFolderMenu = async page => {
  const card = page.getByRole('button', { name: /Open folder Evidence/ })
  await card.hover()
  await page.getByRole('button', { name: /Actions for folder Evidence/ }).click()
}

const scheduleApi = async (path, route) => {
  if (path === '/api/crons') { await json(route, { jobs: SCHEDULE_JOBS }); return true }
  if (path === '/api/cron-folders') { await json(route, []); return true }
  if (path === '/api/crons/history') { await json(route, { runs: [] }); return true }
  if (path === '/api/models') { await json(route, []); return true }
  return false
}

const artifactApi = async (path, route) => {
  if (path === '/api/artifact-folders') {
    await json(route, { folders: [{ id: 'artifact-evidence', name: 'Evidence', icon: '📷', color: '#6366f1', parent_id: '', item_count: 0, order: 0 }] })
    return true
  }
  if (path === '/api/artifacts/session-docs') { await json(route, { docs: [] }); return true }
  if (path === '/api/artifacts') { await json(route, { artifacts: [COMMENT_ARTIFACT] }); return true }
  const match = /^\/api\/artifacts\/hover-evidence(\/.*)?$/.exec(path)
  if (!match) return false
  const rest = match[1] || ''
  if (rest === '') { await json(route, COMMENT_ARTIFACT); return true }
  if (rest === '/versions') { await json(route, { versions: [1] }); return true }
  if (rest === '/events') { await json(route, { events: [] }); return true }
  if (rest === '/comments') { await json(route, { comments: [COMMENT_FIXTURE] }); return true }
  if (rest === '/upstream-status') { await json(route, {}); return true }
  return false
}

const issueRadarApi = async (path, route) => {
  const root = '/api/apps/issue-radar'
  if (path === `${root}/repos`) {
    await json(route, { repos: [{ ...ISSUE_REPO, enabled: true, permissions: { push: true, triage: true } }] })
    return true
  }
  if (path === `${root}/me`) { await json(route, { ...ISSUE_REPO, login: 'owner' }); return true }
  if (path === `${root}/labels`) { await json(route, { ...ISSUE_REPO, from_cache: true, labels: ISSUE_LABELS }); return true }
  if (path === `${root}/members`) { await json(route, { ...ISSUE_REPO, members: [], source: 'collaborators', from_cache: true }); return true }
  if (path === `${root}/settings`) {
    await json(route, { ...ISSUE_REPO, settings: { triage_labels: [], unlabeled_is_untriaged: true, good_first_issue_labels: [], notify_on_new_issue: false, revision: 1 } })
    return true
  }
  if (path === `${root}/issues`) { await json(route, { ...ISSUE_REPO, state: 'open', from_cache: true, issues: [ISSUE] }); return true }
  if (path === `${root}/pulls`) { await json(route, { ...ISSUE_REPO, state: 'open', from_cache: true, bulk_max: 50, pulls: [] }); return true }
  if (path === `${root}/pulls/search`) { await json(route, { ...ISSUE_REPO, pulls: [] }); return true }
  if (path === `${root}/issue`) {
    await json(route, {
      ...ISSUE_REPO, number: ISSUE.number, from_cache: true,
      detail: {
        ...ISSUE, state_reason: null, author_association: 'MEMBER', closed_at: null,
        closed_by: null, locked: false,
        labels: [{ name: 'bug', color: 'd73a4a', description: '' }],
        milestone: null, reactions: null,
      },
      timeline: [],
    })
    return true
  }
  if (path === `${root}/issue-ai`) {
    await json(route, {
      ...ISSUE_REPO, number: ISSUE.number, summary: 'The target should paint without moving.',
      suggested_labels: [{ name: 'needs-triage', reason: 'The hover affordance needs review.' }],
      from_cache: true,
    })
    return true
  }
  if (path === `${root}/deps`) { await json(route, { schema: 1, edges: [], nodes: {} }); return true }
  if (path === `${root}/crews`) { await json(route, { ...ISSUE_REPO, crews: [], counts: {} }); return true }
  if (path === `${root}/recent-repos`) { await json(route, { repos: [] }); return true }
  return false
}

const SCENES = [
  {
    name: '01-nav-row-hovered',
    url: '/chat',
    selector: '.nav-item[data-onboarding-nav="schedule"]',
    claim: 'a rail row paints without moving or resizing',
    genericAffordance: true,
    pad: 28,
  },
  {
    name: '02-nav-row-pressed',
    url: '/chat',
    selector: '.nav-item[data-onboarding-nav="schedule"]',
    claim: 'NEGATIVE CONTROL — pressing the rail row still scales it down',
    press: true,
    pad: 28,
  },
  {
    // Scene 01 hovers an INACTIVE row, so it cannot show this one: the rail's
    // hover utilities live on the inactive branch, and the ACTIVE row's only
    // hover cue on main was the whileHover scale this change removes. The row
    // stays clickable when selected (it toggles the sidebar), so it needs its
    // own frame rather than riding along on the inactive row's.
    name: '25-nav-row-active-hovered',
    url: '/chat',
    selector: '.nav-item.nav-active',
    claim: 'the SELECTED rail row still paints on hover without moving or resizing',
    genericAffordance: true,
    pad: 28,
  },
  {
    name: '03-nav-toggle-hovered',
    url: '/chat',
    selector: 'button[title^="Show "][title*=" more app"]',
    claim: 'the Apps overflow toggle paints without changing geometry',
    genericAffordance: true,
    extra: navAppsApi,
    pad: 28,
  },
  {
    name: '04-badge-hovered',
    url: '/connections',
    selector: 'span[class*="rounded-full"][class*="font-mono"]',
    claim: 'a non-interactive Badge no longer grows under the cursor',
    // Badge deliberately has no hover paint: adding one would recreate the
    // false affordance this treatment removes.
    pad: 16,
  },
  {
    name: '05-stat-card-hovered',
    url: '/settings/overview',
    selector: '.stat-accent',
    claim: 'a shared StatCard paints without lifting',
    genericAffordance: true,
    pad: 18,
  },
  {
    name: '06-display-swatch-hovered',
    url: '/settings/display',
    selector: 'button[aria-label="Color 1"]',
    claim: 'a Display colour swatch paints without growing while selected scale remains static',
    genericAffordance: true,
    pad: 46,
  },
  {
    name: '07-week-grid-dot-hovered',
    url: '/schedule',
    selector: 'button[aria-label^="Geometry probe at"]',
    claim: 'a WeekGrid schedule dot paints without growing',
    genericAffordance: true,
    extra: scheduleApi,
    prepare: page => page.getByRole('button', { name: /calendar/i }).click(),
    pad: 52,
  },
  {
    name: '08-comment-bubble-hovered',
    url: '/artifacts/hover-evidence',
    selector: '.mc-cmt-bubble',
    claim: 'an anchored-comment bubble paints without growing or moving',
    genericAffordance: true,
    extra: artifactApi,
    prepare: async page => {
      await page.getByText(COMMENT_FIXTURE.body, { exact: true }).click()
    },
    settleMs: 2300,
    pad: 52,
  },
  {
    name: '09-issue-suggestion-chip-hovered',
    url: '/issue-radar',
    selector: 'button[title*="needs-triage"]',
    claim: 'an Issue Radar AI suggestion chip paints without lifting',
    genericAffordance: true,
    extra: issueRadarApi,
    localStorageEntries: {
      'kc:issue-radar:active-repo': JSON.stringify(ISSUE_REPO),
      'kc:issue-radar:ui-state': JSON.stringify({ mainView: 'issues', stateFilter: 'open', selectedIssue: ISSUE.number }),
    },
    settleMs: 2300,
    pad: 36,
  },
  {
    name: '10-jump-to-bottom-hovered',
    url: '/chat?sid=s1',
    selector: 'button[aria-label="Scroll to bottom"]',
    claim: 'the jump-to-bottom control paints without growing',
    genericAffordance: true,
    extra: chatUiApi,
    prepare: async page => {
      const scroller = page.locator('.chat-container')
      await scroller.evaluate(el => { el.scrollTop = 0; el.dispatchEvent(new Event('scroll', { bubbles: true })) })
      await page.locator('button[aria-label="Scroll to bottom"]').waitFor({ state: 'visible' })
    },
    pad: 40,
  },
  {
    name: '11-slider-knob-hovered',
    url: '/chat?sid=s1',
    selector: '[role="slider"] > div[aria-hidden][class*="absolute"] > div',
    claim: 'the shared Slider knob paints without growing',
    genericAffordance: true,
    extra: chatUiApi,
    prepare: async page => {
      await page.getByLabel('Context usage').click()
      await page.getByRole('slider', { name: 'Auto-compact threshold' }).waitFor({ state: 'visible' })
    },
    pad: 52,
  },
  {
    name: '12-restart-button-hovered',
    url: '/capabilities',
    selector: 'button[class*="from-accent"][class*="accent-hover"]',
    claim: 'the Restart button keeps its glow without lifting',
    genericAffordance: true,
    pad: 36,
  },
  {
    name: '13-memory-record-card-hovered',
    url: '/settings/overview?view=memory',
    selector: '[data-testid="memory-records-editor"] .card-glow:has(input[type="checkbox"])',
    claim: 'a saved Memory record card paints without lifting',
    genericAffordance: true,
    extra: memoryApi,
    prepare: async page => {
      await page.getByText('Edit saved memories', { exact: true }).click()
    },
    settleMs: 2200,
    pad: 24,
  },
  {
    name: '14-aws-stat-caption-hovered',
    url: '/aws-control/overview',
    selector: '[data-testid="overview-stat-accounts-sub"]',
    hoverSelector: '[data-testid="overview-stat-accounts"]',
    paintSelector: '[data-testid="overview-stat-accounts"]',
    claim: 'an AWS metric sub-caption stays still while its owning card paints',
    genericAffordance: true,
    extra: awsApi,
    settleMs: 2200,
    pad: 34,
  },
  {
    name: '15-issue-crew-stat-hovered',
    url: '/issue-radar',
    selector: '[data-testid="stat-open-items"]',
    claim: 'an Issue Radar crew stat block paints without geometric motion',
    genericAffordance: true,
    extra: issueCrewApi,
    localStorageEntries: issueCrewSeedState({ crewView: { kind: 'crew', id: 'c_7f3a01' }, crewFilter: 'all' }),
    settleMs: 2300,
    pad: 30,
  },
  {
    name: '16-folder-color-swatch-hovered',
    url: '/chat',
    selector: '[data-testid="folder-config-color-reset"] + button',
    claim: 'a Folder settings colour swatch paints without growing',
    genericAffordance: true,
    extra: chatUiApi,
    folders: CHAT_FOLDERS,
    prepare: openFolderConfig,
    pad: 38,
  },
  {
    name: '17-folder-tag-chip-hovered',
    url: '/chat',
    selector: '[data-testid="folder-config-tag-t1"]',
    claim: 'a Folder settings tag chip paints without growing',
    genericAffordance: true,
    extra: chatUiApi,
    folders: CHAT_FOLDERS,
    prepare: openFolderConfig,
    pad: 34,
  },
  {
    name: '18-session-color-swatch-hovered',
    url: '/chat',
    selector: '[role="menu"] button[class*="w-4"][class*="h-4"]:nth-of-type(2)',
    claim: 'a session-actions colour swatch paints without growing',
    genericAffordance: true,
    extra: chatUiApi,
    folders: CHAT_FOLDERS,
    prepare: openSessionMenu,
    pad: 42,
  },
  {
    name: '19-tag-manager-swatch-hovered',
    url: '/chat',
    selector: '[data-testid^="tag-color-t1-"]',
    claim: 'a Tag manager palette swatch paints without growing',
    genericAffordance: true,
    extra: chatUiApi,
    folders: CHAT_FOLDERS,
    prepare: openTagManagerPalette,
    pad: 42,
  },
  {
    name: '20-library-color-swatch-hovered',
    url: '/artifacts',
    selector: '[role="radiogroup"] button:first-child',
    claim: 'a Library folder colour swatch paints without growing',
    genericAffordance: true,
    extra: artifactApi,
    prepare: openArtifactFolderMenu,
    pad: 44,
  },
  {
    name: '21-library-no-color-hovered',
    url: '/artifacts',
    selector: '[role="radiogroup"] button:last-child',
    claim: 'the Library no-colour swatch border tints without growing',
    genericAffordance: true,
    extra: artifactApi,
    prepare: openArtifactFolderMenu,
    pad: 44,
  },
  {
    name: '22-app-hero-hovered',
    url: '/apps',
    selector: 'img[src$="/hover-main.svg"]',
    hoverSelector: 'div[role="button"]:has(img[src$="/hover-main.svg"])',
    paintSelector: 'div[role="button"]:has(img[src$="/hover-main.svg"])',
    claim: 'a full FeaturedSpotlight hero stays still while its card paints',
    genericAffordance: true,
    extra: appStoreApi,
    setupRoutes: installHoverArt,
    settleMs: 2200,
    pad: 12,
  },
  {
    name: '23-app-compact-hero-hovered',
    url: '/apps',
    selector: 'img[src$="/hover-compact.svg"]',
    hoverSelector: 'div[role="button"]:has(img[src$="/hover-compact.svg"])',
    paintSelector: 'div[role="button"]:has(img[src$="/hover-compact.svg"])',
    claim: 'a compact collection hero stays still while its card paints',
    genericAffordance: true,
    extra: appStoreApi,
    setupRoutes: installHoverArt,
    settleMs: 2200,
    pad: 12,
  },
  {
    name: '24-crew-color-swatch-hovered',
    url: '/capabilities',
    selector: '[role="dialog"] button[class*="h-5"][class*="w-5"]',
    claim: 'a Crew session-colour swatch paints without growing',
    genericAffordance: true,
    prepare: async page => {
      await page.locator('[data-testid="new-crew"]').click()
    },
    pad: 38,
  },
]

mkdirSync(OUT, { recursive: true })

let failed = 0
let failedScenes = 0
let assertions = 0
let passedAssertions = 0
let frames = 0
let sceneFailed = 0
let sceneWrote = false
const check = (label, ok, detail) => {
  assertions += 1
  if (ok) passedAssertions += 1
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`)
  if (!ok) { failed += 1; sceneFailed += 1 }
  return ok
}

const PAINT_PROPERTIES = [
  'backgroundColor', 'borderTopColor', 'borderBottomColor', 'borderLeftColor',
  'color', 'filter', 'opacity', 'boxShadow', 'outlineColor',
]

/** Resolve a scene's real surface, including sandboxed artifact content. */
const locatorFor = (page, scene, selector) => scene.frameSelector
  ? page.frameLocator(scene.frameSelector).locator(selector).first()
  : page.locator(selector).first()
const targetFor = (page, scene) => locatorFor(page, scene, scene.selector)
const hoverTargetFor = (page, scene) => locatorFor(page, scene, scene.hoverSelector ?? scene.selector)
const paintTargetFor = (page, scene) => locatorFor(page, scene, scene.paintSelector ?? scene.selector)

/** Rect + computed paint properties in page coordinates. */
const probeLocator = async target => {
  const rect = await target.boundingBox().catch(() => null)
  if (!rect) return null
  const paint = await target.evaluate(el => {
    const cs = getComputedStyle(el)
    return {
      backgroundColor: cs.backgroundColor,
      borderTopColor: cs.borderTopColor,
      borderBottomColor: cs.borderBottomColor,
      borderLeftColor: cs.borderLeftColor,
      color: cs.color,
      filter: cs.filter,
      opacity: cs.opacity,
      boxShadow: cs.boxShadow,
      outlineColor: cs.outlineColor,
      transform: cs.transform,
      hovered: el.matches(':hover'),
    }
  })
  return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, ...paint }
}
const probe = (page, scene) => probeLocator(targetFor(page, scene))
const probeHoverTarget = (page, scene) => probeLocator(hoverTargetFor(page, scene))
const probePaintTarget = (page, scene) => probeLocator(paintTargetFor(page, scene))

const worstDrift = (a, b) => Math.max(
  Math.abs(a.x - b.x), Math.abs(a.y - b.y),
  Math.abs(a.width - b.width), Math.abs(a.height - b.height),
)

const fmt = r => `x=${r.x.toFixed(2)} y=${r.y.toFixed(2)} w=${r.width.toFixed(2)} h=${r.height.toFixed(2)}`

/**
 * Sample the rect repeatedly over a window and return the reading that deviates
 * MOST from `ref`.
 *
 * A single post-hover measurement would pass a surface that scaled up and
 * settled back, and "wait until it stops moving" would pass one that moved and
 * stayed moved for less than the settle window. Taking the worst reading across
 * the whole window means ANY transform, transient or persistent, fails — which
 * is the property the branch actually claims.
 */
async function worstOver(page, scene, ref, ms, step = 60) {
  let worst = ref
  let drift = 0
  for (let waited = 0; waited < ms; waited += step) {
    await page.waitForTimeout(step)
    const now = await probe(page, scene)
    if (!now) return { now: null, drift: Infinity }
    const d = worstDrift(ref, now)
    if (d >= drift) { drift = d; worst = now }
  }
  return { now: worst, drift }
}

async function openScene(browser, scene) {
  const ctx = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 2,
    colorScheme: 'dark',
  })
  const page = await ctx.newPage()
  logPageProblems(page)
  if (scene.setupRoutes) await scene.setupRoutes(page)
  let sandboxHtml = ''
  if (scene.sandboxDoc) {
    await page.route('**/sandbox-doc/**', route => route.fulfill({
      status: 200,
      contentType: 'text/html; charset=utf-8',
      body: sandboxHtml,
    }))
  }
  const sceneApi = async (path, route) => {
    if (scene.sandboxDoc && path === '/api/sandbox-doc') {
      sandboxHtml = JSON.parse(route.request().postData() || '{}').html || ''
      await json(route, { url: '/sandbox-doc/hover-evidence' })
      return true
    }
    return scene.extra ? scene.extra(path, route) : false
  }
  await stubDashboardApi(page, {
    folders: scene.folders ?? [],
    slots: scene.slots ?? SLOTS,
    // Seeded through the stub's own init script: a second addInitScript would
    // race its localStorage.clear(). mc-nav '0' keeps the rail EXPANDED, which
    // matters — the old whileHover was skipped when collapsed, so a collapsed
    // rail would make scene 1 vacuous.
    localStorageEntries: {
      'mc-color-theme': 'kiro-dark',
      'mc-privacy-notice-v1': '1',
      'mc-nav': '0',
      ...scene.localStorageEntries,
    },
    extra: sceneApi,
  })
  await page.goto(base + scene.url, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => document.documentElement.getAttribute('data-theme') === 'kiro-dark',
    undefined,
    { timeout: 20000 },
  )
  if (scene.prepare) await scene.prepare(page)
  const target = targetFor(page, scene)
  await target.waitFor({
    state: 'visible', timeout: scene.selectorTimeout ?? 20000,
  }).catch(() => {})
  // Bring the surface into view BEFORE anything is measured. locator.hover() scrolls
  // an off-screen target itself, and that scroll moves the rect by hundreds of
  // pixels — a real detection (the Display swatches sit ~2000px down) but the
  // wrong signal: it would fail the geometry assertion for a reason that has
  // nothing to do with a transform.
  if (await target.isVisible().catch(() => false)) {
    await target.scrollIntoViewIfNeeded()
  }
  // Park the pointer far from every surface: an accidental hover at rest would
  // make the "at rest" baseline already-hovered and the comparison meaningless.
  await page.mouse.move(1390, 930)
  // Entry animations (animate-rise / stagger) and the first data round trip both
  // move layout; measure only once they are done.
  await page.waitForTimeout(scene.settleMs ?? 1500)
  return { ctx, page }
}

const VIEWPORT = { width: 1400, height: 940 }

/**
 * Clip a padded box around the element, so a frame shows it plus its neighbours.
 * Clamped to the viewport: Playwright throws "Clipped area is ... outside the
 * resulting image" for a box that runs past the edge, which would abort the run
 * on a scene whose assertions had all passed.
 */
const clipFor = (rect, pad) => {
  const x = Math.max(0, Math.round(rect.x - pad))
  const y = Math.max(0, Math.round(rect.y - pad))
  return {
    x,
    y,
    width: Math.min(Math.round(rect.width + pad * 2), VIEWPORT.width - x),
    height: Math.min(Math.round(rect.height + pad * 2), VIEWPORT.height - y),
  }
}

/** The only screenshot path: failed scenes cannot leave new or stale evidence. */
async function writeEvidence(page, scene, rest) {
  const path = `${OUT}/${scene.name}.png`
  if (sceneFailed) {
    rmSync(path, { force: true })
    console.log(`  REFUSED ${scene.name}: ${sceneFailed} failed assertion(s); no PNG written`)
    return false
  }
  try {
    await page.screenshot({ path, clip: clipFor(rest, scene.pad) })
    frames += 1
    sceneWrote = true
    console.log(`  ${scene.name} -> ${scene.claim}`)
    return true
  } catch (err) {
    rmSync(path, { force: true })
    check(`${scene.name}: evidence frame written`, false, String(err))
    return false
  }
}

const { srv, base } = await serveDist()
const browser = await chromium.launch({ args: ['--no-sandbox'] })

try {
  for (const scene of SCENES) {
    sceneFailed = 0
    sceneWrote = false
    rmSync(`${OUT}/${scene.name}.png`, { force: true })
    let ctx
    try {
      const opened = await openScene(browser, scene)
      ctx = opened.ctx
      const { page } = opened
      const rest = await probe(page, scene)
      const missingDetail = rest ? scene.selector : await page.evaluate(sel => ({
        selector: sel,
        title: document.title,
        body: document.body.innerText.replace(/\s+/g, ' ').slice(0, 240),
        commentOverlays: document.querySelectorAll('.mc-cmt-overlay').length,
        commentRects: document.querySelectorAll('.mc-cmt-rect').length,
        markdown: document.querySelector('.msg-content')?.textContent?.replace(/\s+/g, ' ').slice(0, 180) ?? null,
      }), scene.selector).then(v => JSON.stringify(v))
      if (!check(`${scene.name}: surface rendered`, !!rest, missingDetail)) continue
      const hoverRest = await probeHoverTarget(page, scene)
      if (!check(`${scene.name}: hover target rendered`, !!hoverRest, scene.hoverSelector ?? scene.selector)) continue
      const paintRest = await probePaintTarget(page, scene)
      if (!check(`${scene.name}: paint target rendered`, !!paintRest, scene.paintSelector ?? scene.selector)) continue
      if (!check(`${scene.name}: not already hovered at rest`, !hoverRest.hovered)) continue

      // Is the page itself still? A surface that drifts on its own would make
      // any post-hover delta unattributable, so refuse rather than measure it.
      const settle = await worstOver(page, scene, rest, 300)
      if (!check(`${scene.name}: geometry is stable at rest`,
        settle.drift <= EPSILON, `drift ${settle.drift.toFixed(3)}px, ${fmt(rest)}`)) continue

      if (scene.press) {
        const box = await targetFor(page, scene).boundingBox()
        await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
        await page.mouse.down()
        const held = await worstOver(page, scene, rest, 600)
        check(`${scene.name}: pressed row DOES change geometry (press feedback kept)`,
          held.drift >= PRESS_MIN_DELTA,
          `drift ${held.drift.toFixed(3)}px (needs >= ${PRESS_MIN_DELTA}), rest ${fmt(rest)} -> held ${fmt(held.now)}`)
        // The direction matters: whileTap SHRINKS. A press that grew the row
        // would satisfy a bare "it moved" check while being a different bug.
        check(`${scene.name}: press shrinks rather than grows`,
          held.now.width < rest.width - EPSILON,
          `w ${rest.width.toFixed(2)} -> ${held.now.width.toFixed(2)}`)
        await writeEvidence(page, scene, rest)
        await page.mouse.up()
        continue
      }

      await hoverTargetFor(page, scene).hover()
      const after = await worstOver(page, scene, rest, 700)
      if (!check(`${scene.name}: still present while hovered`, !!after.now)) continue
      // `continue` rather than a bare check: a frame of a surface that DID move is
      // a screenshot of the bug, and shipping it as evidence of the fix is worse
      // than having no frame at all.
      if (!check(`${scene.name}: hovered geometry unchanged (<= ${EPSILON}px)`,
        after.drift <= EPSILON,
        `worst drift ${after.drift.toFixed(3)}px, rest ${fmt(rest)} -> hovered ${fmt(after.now)}, transform ${after.now.transform}`)) continue

      const liveHover = await probeHoverTarget(page, scene)
      if (!check(`${scene.name}: hover target still present`, !!liveHover)) continue
      check(`${scene.name}: pointer really is on the hover target`, liveHover.hovered,
        `:hover=${liveHover.hovered}`)
      if (scene.genericAffordance) {
        const livePaint = await probePaintTarget(page, scene)
        if (!check(`${scene.name}: paint target still present`, !!livePaint)) continue
        const changed = PAINT_PROPERTIES.filter(prop => livePaint[prop] !== paintRest[prop])
        check(`${scene.name}: hover still paints (at least one generic paint property changes)`,
          changed.length > 0,
          changed.length
            ? changed.map(prop => `${prop}: ${paintRest[prop]} -> ${livePaint[prop]}`).join('; ')
            : PAINT_PROPERTIES.map(prop => `${prop}=${paintRest[prop]}`).join(', '))
      }

      await writeEvidence(page, scene, rest)
    } catch (err) {
      check(`${scene.name}: scene completed`, false, String(err))
      rmSync(`${OUT}/${scene.name}.png`, { force: true })
    } finally {
      if (!sceneWrote && sceneFailed === 0) {
        check(`${scene.name}: evidence frame written`, false, 'scene ended without a frame')
      }
      if (sceneFailed > 0) {
        failedScenes += 1
        console.log(`SCENE FAIL ${scene.name} — ${sceneFailed} failed assertion(s), no PNG`)
      } else {
        console.log(`SCENE PASS ${scene.name} — ${OUT}/${scene.name}.png`)
      }
      await ctx?.close()
    }
  }
} finally {
  await browser.close()
  srv.close()
}

console.log(`\n${passedAssertions}/${assertions} assertions passed; ${frames}/${SCENES.length} frames written; ${failedScenes} scene(s) failed`)
console.log(`Evidence directory: ${OUT}`)
if (failed) {
  console.error(`${failed} assertion(s) failed — failed scenes wrote no frames; passing-scene frames remain valid`)
  process.exit(1)
}
