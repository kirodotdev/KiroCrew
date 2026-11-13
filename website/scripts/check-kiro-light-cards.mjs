/**
 * Gate: no surface may vanish in kiro-light because it has no edge of its own.
 *
 * `kiro-light` paints the canvas `#ffffff`, the same value as `--card` (and
 * `--bg-elevated`), so a borderless `bg-card` box on the page draws nothing in
 * that one theme while every other theme shows a card. The rule is written in
 * docs/theming-contract.md ("A card must carry its own edge"). A class-attribute
 * grep cannot enforce it: an edge can come from an inline `style`, a parent's
 * `divide-*`, or a ring/shadow utility, and an opaque cover that matches its
 * parent in every theme is not a card at all. So this renders the real built SPA
 * (website/dist) over loopback with every /api/** call answered from fixtures,
 * opens each route in kiro-light, and asks the computed styles.
 *
 * For every painted element (non-transparent background, at least 16x16 px) the
 * probe composites its background over the nearest painted ancestors, then:
 *
 *  - in kiro-light it must be indistinguishable from what is behind it (every
 *    sRGB channel within MIN_DELTA), AND
 *  - in a reference theme (`light`, the default palette of the same polarity
 *    where `--card` and `--bg` differ, and `kiro-dark`) it must be distinct, AND
 *  - in kiro-light it must have no visible edge: no border side, painting
 *    `box-shadow` layer (Tailwind's `ring-*` is a box-shadow) or outline whose
 *    colour differs from the backdrop.
 *
 * Such an element is a card in every reference theme and nothing in kiro-light:
 * it is reported as `route  selector` and the script exits 1. Surfaces equal to
 * their parent in every theme (opaque covers, redundant fills) are not reported,
 * because their look does not depend on the theme.
 *
 * The reference themes are applied by switching `data-theme` / `data-mode` on
 * the root in place, with transitions and animations off, so both readings come
 * from the same DOM. Every route must render: a route whose page shows an error
 * boundary, or whose main pane holds no text, fails the gate rather than
 * passing with nothing checked.
 *
 * A deliberate exception goes in EXEMPT below with the reason, keyed on a CSS
 * selector the element matches; an exemption that matches nothing fails the gate
 * so the list cannot rot.
 *
 * Usage: node scripts/check-kiro-light-cards.mjs [--dist <dir>] [--json <file>]
 */
import { chromium } from 'playwright'
import { writeFileSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, makeFixedApi, handleBootRoute } from './lib/boot-api.mjs'

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
delete process.env.LD_LIBRARY_PATH

const args = process.argv.slice(2)
const argValue = name => {
  const i = args.indexOf(name)
  return i >= 0 ? args[i + 1] : undefined
}
const DIST = argValue('--dist')
const JSON_OUT = argValue('--json')

const PROJECT = '/home/user/workspace/KiroCrew'
const SLOT = 'chat-kiro-light-cards'
const VIEWPORT = { width: 1440, height: 900 }
/** Max per-channel sRGB difference (0-255) that still reads as the same colour. */
const MIN_DELTA = 3
/** Smallest box (px, both sides) the probe inspects; below it is glyph-sized. */
const MIN_SIDE = 16
const REFERENCE_THEMES = [
  { theme: 'light', mode: 'light' },
  { theme: 'kiro-dark', mode: 'dark' },
]

/**
 * The routes the gate renders. `ready` is text the page shows once it has
 * rendered past its loading state, so a blank or crashed page cannot pass.
 */
const ROUTES = [
  { path: '/', name: 'chat', ready: 'Reply in the composer' },
  { path: '/settings/overview', name: 'settings-overview' },
  { path: '/settings/chat', name: 'settings-chat' },
  { path: '/settings/display/theme', name: 'settings-display-theme' },
  { path: '/settings/security', name: 'settings-security' },
  { path: '/schedule', name: 'schedule' },
  { path: '/artifacts', name: 'artifacts' },
  { path: '/capabilities?tab=knowledge', name: 'knowledge' },
  { path: '/apps/library', name: 'apps-library' },
  { path: '/members', name: 'members' },
  // Built-in app pages: one per entry in src/apps/builtinRegistry.ts. Each one
  // whose page reads a shape the generic `[]` / `{}` fallback cannot satisfy has
  // its answers in APP_FIXTURES below, so it renders its real page rather than
  // an error boundary or a "could not reach the backend" notice.
  { path: '/worlds', name: 'app-agent-worlds', ready: 'Kiro Haunt' },
  { path: '/channels', name: 'app-channels', ready: 'No channels yet' },
  { path: '/auto-improvement', name: 'app-auto-improvement', ready: 'Measures a repository before it changes it' },
  { path: '/auto-research', name: 'app-auto-research', ready: 'Run autonomous research' },
  { path: '/aws-control', name: 'app-aws-control', ready: 'sandbox-dev' },
  { path: '/file-explorer', name: 'app-file-explorer', ready: 'README.md' },
  { path: '/code-review-sage', name: 'app-code-review-sage', ready: 'Pick a repo to see its pull requests' },
  { path: '/workflows', name: 'app-workflows', ready: 'Run a workflow to see its phases' },
  { path: '/dev-fleet', name: 'app-dev-fleet', ready: 'Manage the git worktrees' },
  { path: '/issue-radar', name: 'app-issue-radar', ready: 'Welcome to Issue Radar' },
  { path: '/meetings', name: 'app-meetings', ready: 'Live notes, diagrams, and action items' },
  { path: '/papyrus', name: 'app-papyrus', ready: 'LaTeX papers with a live PDF preview' },
  { path: '/pptx-maker', name: 'app-pptx-maker', ready: 'Quarterly review' },
  { path: '/ops-mission-control', name: 'app-ops-mission-control', ready: 'Checkout latency above budget' },
  { path: '/design-critique', name: 'app-design-critique', ready: 'Point me at a screen' },
  { path: '/crew-companion', name: 'app-crew-companion', ready: 'Set your break rhythm' },
  { path: '/projects', name: 'app-task-runner', ready: 'No runs yet' },
  { path: '/md-notebook', name: 'app-md-notebook', ready: 'Release checklist' },
  { path: '/mochi', name: 'app-mochi', ready: 'Write the release notes' },
  { path: '/spec-builder', name: 'app-spec-builder', ready: 'Plan your next feature with a spec' },
  { path: '/personal-shopper', name: 'app-personal-shopper', ready: 'Your life-problem advisor' },
  { path: '/design-tweak', name: 'app-design-tweak', ready: 'Point, describe, and watch the code catch up' },
  { path: '/project-scaffolder', name: 'app-project-scaffolder', ready: 'Scan a project directory' },
]

/**
 * Deliberate borderless surfaces: `{ selector, why }`. Each must match at least
 * one reported element on some route, or the gate fails as stale.
 */
const EXEMPT = []

/** Planted on every route: `edge` is the box's own edge CSS, `reported` whether the probe must flag it. */
const CANARIES = [
  { name: 'bare', edge: 'border:none', reported: true },
  { name: 'shadow-reset', edge: 'border:none;box-shadow:0 0 0 0 transparent,0 0 #0000', reported: true },
  { name: 'edged', edge: 'border:1px solid var(--border)', reported: false },
  { name: 'ringed', edge: 'border:none;box-shadow:0 0 0 1px var(--border)', reported: false },
]

const t0 = Math.floor(Date.now() / 1000) - 600
const slots = [{
  key: SLOT, title: 'kiro-light card sweep', running: false, unread: 0,
  messages: 2, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const detail = {
  running: false, has_more: false, total: 2, key: SLOT, title: 'kiro-light card sweep', queue: [],
  messages: [
    { role: 'user', ts: t0, content: 'Summarise the open review comments.' },
    { role: 'assistant', ts: t0 + 8, content: 'Two comments are open. Reply in the composer when you want me to address them.' },
  ],
}

/**
 * Per-app answers, keyed by exact pathname. The generic fallback answers `[]` or
 * `{}`, which some app pages index straight into (`.messages.sent`,
 * `.entries.forEach`, `.provision.state`) and so crash on. Each fixture carries
 * one populated row where the page has a list, so the gate also checks the
 * surface a row is drawn on, not only the empty state.
 */
const ACC = '111122223333'
const nullSummary = { storage: null, sites: null, tasks: null, costMonthToDate: null }
const NOTE_PATH = 'release-checklist.md'
const INCIDENT = {
  incident_id: 'inc-1', status: 'investigating', operating_mode: 'observe', claimed_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z', slot_key: '', slack_thread_ts: '', ledger_matches: [], diagnosis: '',
  proposed_action: null, resolution: '',
  signal: {
    id: 'sig-1', source: 'cloudwatch', title: 'Checkout latency above budget', severity: 'warning', state: 'firing',
    fired_at: '2026-01-01T00:00:00Z', resource: 'checkout-api', url: '', labels: {}, fingerprint: 'fp-1', provider_key: 'cloudwatch',
  },
}
const APP_FIXTURES = new Map([
  ['/api/apps/aws-control/accounts', {
    accounts: [{
      account: ACC, name: 'sandbox-dev', health: 'ok', summary: nullSummary,
      profiles: [{ name: 'sandbox-dev', region: 'us-west-2', kind: 'sso', identityOk: true, account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Dev/dev`, detail: '', default: true }],
    }],
    totals: { accounts: 1, profiles: 1, profilesHealthy: 1 },
    generatedAt: '2026-01-01T00:00:00Z',
  }],
  ['/api/apps/aws-control/profiles/available', { profiles: [], registeredCount: 1, max: 10, supported: true }],
  ['/api/aws/consent', {
    service: 's3', serviceLabel: 'Amazon S3', profile: 'sandbox-dev', credentialSource: 'profile sandbox-dev',
    region: 'us-west-2', account: ACC, arn: `arn:aws:sts::${ACC}:assumed-role/Dev/dev`, identityResolved: true,
    identityDetail: '', granted: true, reason: '', revokedOnAccountChange: false,
    grant: { account: ACC, region: 'us-west-2', profile: 'sandbox-dev', granted_at: '2026-01-01T00:00:00Z' },
  }],
  ['/api/apps/aws-control/costs/' + ACC, { fresh: true, monthToDate: 12.5, projected: 30, currency: 'USD', byService: [], fetchedAt: '2026-01-01T00:00:00Z' }],
  ['/api/apps/aws-control/drive/' + ACC, {
    exists: true, bucket: `kirocrew-drive-${ACC}-usw2`, region: 'us-west-2',
    usage: { bytes: 0, objects: 0, sections: { drive: { objects: 0, bytes: 0 }, library: { objects: 0, bytes: 0 }, backup: { objects: 0, bytes: 0 } } },
  }],
  ['/api/apps/aws-control/shares', { checked: true, shares: [] }],
  ['/apps/file-explorer/api/health', { allowedRoots: [PROJECT], home: '/home/user' }],
  ['/apps/file-explorer/api/tree', { entries: [
    { name: 'src', path: `${PROJECT}/src`, type: 'dir' },
    { name: 'README.md', path: `${PROJECT}/README.md`, type: 'file', size: 2048, mtime: 1767225600 },
  ] }],
  ['/apps/file-explorer/api/git-status', null],
  ['/api/apps/pptx-maker/engine', { ready: true, clone: true, venv: true, pinnedTag: 'v1.0.0', provision: { state: 'done', log: '', elapsed: 0 } }],
  ['/api/apps/pptx-maker/deps', { labels: {}, present: {}, managed: {}, missing: [], hints: {} }],
  ['/api/apps/pptx-maker/decks', { decks: [{ deckId: 'quarterly-review', name: 'Quarterly review', slideCount: 6, thumbnailUrl: null, pptxUrl: null, brief: '' }] }],
  ['/api/apps/pptx-maker/deck', {
    deckId: 'quarterly-review', name: 'Quarterly review', defsUrl: null, pptxUrl: null, dirPath: '/home/user/decks/quarterly-review',
    pptxPath: null, specs: { brief: 'Results and plans for the quarter.' }, updatedAt: {}, slides: [],
  }],
  ['/api/apps/pptx-maker/styles', { styles: [] }],
  ['/api/apps/pptx-maker/templates', { templates: [] }],
  ['/api/apps/ops-mission-control/state', {
    incidents: [INCIDENT], counts: { investigating: 1 }, providers: [], webhook_queue: 0,
    rotation: { on_shift: false, who: '', until: '', unknown: false, roster: null },
    ledger: { total: 0, verified: 0, high_confidence: 0, total_uses: 0, proven: 0, demoted: 0 },
  }],
  ['/api/apps/ops-mission-control/incidents', { incidents: [INCIDENT] }],
  ['/api/apps/ops-mission-control/ledger', { entries: [], stats: { total: 0, verified: 0, high_confidence: 0, total_uses: 0, proven: 0, demoted: 0 } }],
  ['/api/apps/mochi/pet-state', { state: 'idle', mood: 'calm' }],
  ['/api/apps/mochi/stats', {
    firstLaunch: '2026-01-01T00:00:00Z', streak: 3, lastActiveDate: '2026-01-03', companionSeconds: 3600,
    messages: { sent: 12, received: 14 }, walkSteps: 0, screenshots: 0, peeks: 0, drags: 0, thinkingSeconds: 60,
    latestActiveTime: '18:00', earliestActiveTime: '09:00', moods: {}, longestChat: 6,
    busiestDay: { date: '2026-01-02', messages: 9 }, lastMemoryHour: 0, celebratedMilestones: [],
  }],
  ['/api/apps/mochi/settings', { petInstance: 'self', mode: 'quiet', catPreset: null, allowMcpServers: false, name: '' }],
  ['/api/apps/mochi/watchlist', { items: [] }],
  ['/api/apps/mochi/pinned', { pins: [] }],
  ['/api/apps/mochi/plan', { tasks: [{ id: 't1', type: 'remind', action: 'Write the release notes', done: false }], narrative: 'Write the release notes', mood: 'calm' }],
  ['/api/apps/mochi/activity', { entries: [] }],
  ['/api/apps/mochi/manifest', { version: '1.0.0' }],
  ['/apps/md-notebook/api/health', { ok: true, features: [
    'trash', 'move', 'createdAt', 'attach', 'changes', 'saveGuard', 'forget', 'pat', 'newNote', 'duplicate',
    'localOnly', 'autoCommit', 'trashOpen', 'knowledge', 'pickFolder', 'settings', 'autoSyncLoop',
  ] }],
  ['/apps/md-notebook/api/vaults', { vaults: [{
    id: 'local-notes', name: 'My Notes', repo: '', branch: 'main', localPath: '/home/user/notes', readOnly: false,
    external: true, localOnly: true, knowledge: false, knowledgeSourceId: null,
  }], hasPat: false, hasGhAuth: false }],
  ['/apps/md-notebook/api/settings', { settings: { autoSync: false, autoSyncMins: 10, lastSync: {} } }],
  ['/apps/md-notebook/api/notes', { notes: [{ path: NOTE_PATH, title: 'Release checklist', mtime: 1767225600, size: 120, createdAt: 1767225600 }] }],
  ['/apps/md-notebook/api/changes', { rev: 1, changed: [], watching: true }],
])

/** Runs in the page: returns every element that is a card in a reference theme and nothing in kiro-light. */
function probe({ references, minDelta, minSide }) {
  const root = document.documentElement
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = 1
  const ctx = canvas.getContext('2d', { willReadFrequently: true })
  const cache = new Map()
  // Canvas normalises any CSS colour syntax (rgb, color(srgb), oklch, color-mix
  // results) to unpremultiplied sRGB bytes.
  const rgba = str => {
    if (!str || str === 'transparent') return { r: 0, g: 0, b: 0, a: 0 }
    if (cache.has(str)) return cache.get(str)
    ctx.clearRect(0, 0, 1, 1)
    ctx.fillStyle = '#000'
    ctx.fillStyle = str
    ctx.fillRect(0, 0, 1, 1)
    const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data
    const c = { r, g, b, a: a / 255 }
    cache.set(str, c)
    return c
  }
  const over = (top, under) => ({
    r: top.r * top.a + under.r * (1 - top.a),
    g: top.g * top.a + under.g * (1 - top.a),
    b: top.b * top.a + under.b * (1 - top.a),
    a: 1,
  })
  const delta = (x, y) => Math.max(Math.abs(x.r - y.r), Math.abs(x.g - y.g), Math.abs(x.b - y.b))
  const backdrop = el => {
    const layers = []
    for (let p = el.parentElement; p; p = p.parentElement) {
      const c = rgba(getComputedStyle(p).backgroundColor)
      if (c.a > 0) layers.push(c)
      if (c.a >= 1) break
    }
    let acc = { r: 255, g: 255, b: 255, a: 1 }
    for (let i = layers.length - 1; i >= 0; i--) acc = over(layers[i], acc)
    return acc
  }
  const shown = el => {
    for (let p = el; p && p !== root; p = p.parentElement) {
      const s = getComputedStyle(p)
      if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) === 0) return false
    }
    return true
  }
  const hasEdge = (s, under) => {
    for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
      if (parseFloat(s[`border${side}Width`]) > 0 && s[`border${side}Style`] !== 'none') {
        const bc = rgba(s[`border${side}Color`])
        if (bc.a > 0 && delta(over(bc, under), under) >= minDelta) return true
      }
    }
    if (shadowPaints(s.boxShadow, under)) return true
    if (s.outlineStyle === 'none' || !(parseFloat(s.outlineWidth) > 0)) return false
    const oc = rgba(s.outlineColor)
    return oc.a > 0 && delta(over(oc, under), under) >= minDelta
  }
  // `shadow-none` / `ring-0` compute to transparent, zero-sized layers rather
  // than `none`, so each layer must both have a colour that shows against the
  // backdrop and a non-zero offset, blur or spread before it counts as an edge.
  const shadowPaints = (value, under) => {
    if (!value || value === 'none') return false
    const layers = []
    let depth = 0
    let start = 0
    for (let i = 0; i < value.length; i++) {
      if (value[i] === '(') depth++
      else if (value[i] === ')') depth--
      else if (value[i] === ',' && depth === 0) { layers.push(value.slice(start, i)); start = i + 1 }
    }
    layers.push(value.slice(start))
    return layers.some(layer => {
      const colour = layer.match(/[a-z-]+\([^()]*(?:\([^()]*\)[^()]*)*\)|#[0-9a-f]+/i)
      const c = rgba(colour ? colour[0] : 'currentcolor')
      if (c.a === 0 || delta(over(c, under), under) < minDelta) return false
      const lengths = (colour ? layer.replace(colour[0], '') : layer).match(/-?[\d.]+px/g) || []
      return lengths.some(len => parseFloat(len) !== 0)
    })
  }
  const describe = el => {
    const parts = []
    for (let p = el; p && p !== document.body && parts.length < 4; p = p.parentElement) {
      let part = p.tagName.toLowerCase()
      if (p.id) part += `#${p.id}`
      const testid = p.getAttribute('data-testid')
      if (testid) part += `[data-testid="${testid}"]`
      const cls = typeof p.className === 'string' ? p.className.trim().split(/\s+/).filter(Boolean).slice(0, 4) : []
      if (cls.length) part += '.' + cls.join('.')
      parts.unshift(part)
    }
    return parts.join(' > ')
  }

  const candidates = []
  for (const el of document.body.querySelectorAll('*')) {
    const r = el.getBoundingClientRect()
    if (r.width < minSide || r.height < minSide) continue
    const s = getComputedStyle(el)
    const own = rgba(s.backgroundColor)
    if (own.a === 0) continue
    if (!shown(el)) continue
    const under = backdrop(el)
    if (delta(over(own, under), under) >= minDelta) continue
    if (hasEdge(s, under)) continue
    candidates.push(el)
  }

  const saved = { theme: root.dataset.theme, mode: root.dataset.mode }
  const distinctSomewhere = new Map(candidates.map(el => [el, []]))
  for (const ref of references) {
    root.dataset.theme = ref.theme
    root.dataset.mode = ref.mode
    for (const el of candidates) {
      const own = rgba(getComputedStyle(el).backgroundColor)
      const under = backdrop(el)
      if (delta(over(own, under), under) >= minDelta) distinctSomewhere.get(el).push(ref.theme)
    }
  }
  root.dataset.theme = saved.theme
  root.dataset.mode = saved.mode

  return candidates
    .filter(el => distinctSomewhere.get(el).length)
    .map(el => {
      const r = el.getBoundingClientRect()
      return {
        selector: describe(el),
        size: `${Math.round(r.width)}x${Math.round(r.height)}`,
        background: getComputedStyle(el).backgroundColor,
        distinctIn: distinctSomewhere.get(el),
        el,
      }
    })
}

const { srv, base } = await serveDist(DIST)
const browser = await chromium.launch()
const fixedApi = makeFixedApi(PROJECT)
const failures = []
const report = []
const exemptHits = new Map(EXEMPT.map(e => [e.selector, 0]))

for (const route of ROUTES) {
  const context = await browser.newContext({ viewport: VIEWPORT })
  const page = await context.newPage()
  const pageErrors = []
  page.on('pageerror', e => pageErrors.push(String(e).slice(0, 300)))
  await page.routeWebSocket(/\/api\/ws/, () => {})
  await page.route('**/api/**', r => {
    const path = new URL(r.request().url()).pathname
    if (path === '/api/chat/slots') return json(r, slots)
    if (path.startsWith('/api/chat/slots/')) return json(r, detail)
    if (path === '/api/theme/boot') return json(r, { mode: 'light', color: 'kiro', theme: '' })
    if (APP_FIXTURES.has(path)) return json(r, APP_FIXTURES.get(path))
    return handleBootRoute(r, path, { project: PROJECT, theme: 'light', fixedApi })
  })
  await page.addInitScript(slot => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'light')
    localStorage.setItem('mc-color-theme', 'kiro')
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-active-slot-chat', slot)
  }, SLOT)
  await page.goto(base + route.path, { waitUntil: 'domcontentloaded' })
  try {
    await page.waitForFunction(
      ready => {
        const main = document.querySelector('main') || document.body
        const text = main.innerText || ''
        return ready ? text.includes(ready) : text.trim().length > 40
      },
      route.ready,
      { timeout: 20000 },
    )
  } catch {
    failures.push(`${route.name}: page never rendered${route.ready ? ` "${route.ready}"` : ' its main pane'}`)
    await context.close()
    continue
  }
  await page.addStyleTag({ content: '*,*::before,*::after{transition:none!important;animation:none!important}' })
  await page.waitForTimeout(800)

  const theme = await page.evaluate(() => document.documentElement.dataset.theme)
  if (theme !== 'kiro-light') {
    failures.push(`${route.name}: data-theme is ${theme}, not kiro-light`)
    await context.close()
    continue
  }
  const crashed = await page.evaluate(() => /Something went wrong/i.test(document.body.innerText))
  if (crashed) failures.push(`${route.name}: an error boundary rendered`)
  if (pageErrors.length) failures.push(`${route.name}: page error: ${pageErrors[0]}`)

  // Planted controls prove the probe is live on this route: a bare `--card`
  // box and one carrying the transparent zero-size shadow layers `shadow-none`
  // computes to, which MUST both be reported, and the same box with a border,
  // which must NOT be. Without them a probe that matched nothing would pass.
  await page.evaluate(canaries => {
    canaries.forEach(([name, edge], i) => {
      const el = document.createElement('div')
      el.setAttribute('data-kiro-light-canary', name)
      el.style.cssText = `position:fixed;left:${8 + i * 132}px;bottom:8px;width:120px;height:60px;background:var(--card);${edge};z-index:2147483647`
      document.body.appendChild(el)
    })
  }, CANARIES.map(c => [c.name, c.edge]))
  const handle = await page.evaluateHandle(probe, { references: REFERENCE_THEMES, minDelta: MIN_DELTA, minSide: MIN_SIDE })
  const all = await handle.evaluate((list, exempt) => list.map(h => ({
    selector: h.selector, size: h.size, background: h.background, distinctIn: h.distinctIn,
    canary: h.el.getAttribute('data-kiro-light-canary'),
    exemptBy: exempt.find(sel => h.el.matches(sel)) || null,
  })), EXEMPT.map(e => e.selector))
  await handle.dispose()
  for (const c of CANARIES) {
    const reported = all.some(h => h.canary === c.name)
    if (reported !== c.reported) {
      failures.push(`${route.name}: the probe ${reported ? 'reported' : 'missed'} the planted ${c.name} card`)
    }
  }
  const hits = all.filter(h => !h.canary)

  for (const hit of hits) {
    if (hit.exemptBy) {
      exemptHits.set(hit.exemptBy, exemptHits.get(hit.exemptBy) + 1)
      continue
    }
    failures.push(`${route.name}  ${hit.selector}  (${hit.size}, ${hit.background}, a card in ${hit.distinctIn.join(' + ')})`)
  }
  report.push({ route: route.path, hits })
  console.log(`${hits.filter(h => !h.exemptBy).length ? 'FAIL' : 'ok  '} ${route.name} (${hits.length} borderless surface(s), ${hits.filter(h => h.exemptBy).length} exempt)`)
  await context.close()
}

for (const [selector, n] of exemptHits) {
  if (!n) failures.push(`stale exemption: "${selector}" matched nothing on any route`)
}

await browser.close()
srv.close()
if (JSON_OUT) writeFileSync(JSON_OUT, JSON.stringify(report, null, 2))

if (failures.length) {
  console.error(`\n${failures.length} problem(s):`)
  for (const f of failures) console.error(`  ${f}`)
  console.error('\nA card must carry its own edge (website/docs/theming-contract.md): give it a border-border, ring-1 ring-border or shadow-*, or add a reasoned EXEMPT entry.')
  process.exit(1)
}
console.log(`\nkiro-light card gate: ${ROUTES.length} routes clean`)
