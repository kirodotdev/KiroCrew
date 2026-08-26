/**
 * Capture one honest product screenshot for every built-in app.
 *
 * The harness serves the REAL built SPA and navigates to each app's real route.
 * Gateway calls are answered at the network boundary so the frames are
 * reproducible and never depend on the operator's data home, credentials, or
 * live services. App-specific fixtures are intentionally small: setup and empty
 * states are product UI too, and are preferable to invented populated screens.
 *
 * Usage:
 *   npm run build
 *   node scripts/capture-app-store-gallery.mjs [outputRoot]
 */
import { chromium } from 'playwright'
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const ROOT = fileURLToPath(new URL('../../', import.meta.url))
const BUILTINS = fileURLToPath(new URL('../../src/kiro_crew/apps/builtins/', import.meta.url))
const OUTPUT_ROOT = resolve(process.argv[2]
  || fileURLToPath(new URL('../public/app-assets/', import.meta.url)))
const VIEWPORT = { width: 1280, height: 800 }

// These are prior captures of the same real SPA in richer, deliberately seeded
// states. Keep them instead of replacing useful product frames with empty-state
// screenshots. They are copied byte-for-byte; no mock artwork is substituted.
const SEEDED_CAPTURES = {
  'auto-improvement': fileURLToPath(new URL('../../temp-screenshots/auto-improvement/01-dashboard.png', import.meta.url)),
  'auto-triage-pipeline': fileURLToPath(new URL('../../temp-screenshots/auto-triage-pipeline/app-01-populated.png', import.meta.url)),
  'command-bar': fileURLToPath(new URL('../../temp-screenshots/command-bar/2-command-bar-root.png', import.meta.url)),
  'crew-companion': fileURLToPath(new URL('../../temp-screenshots/crew-companion/dashboard-app-page.png', import.meta.url)),
  mochi: fileURLToPath(new URL('../public/app-assets/mochi/shot-1-gallery.png', import.meta.url)),
}

const APPS = [
  ['agent-worlds', 'agent_worlds', 'worlds', '/worlds'],
  ['auto-improvement', 'auto_improvement', 'auto-improvement', '/auto-improvement'],
  ['auto-research', 'auto_research', 'auto-research', '/auto-research'],
  ['auto-triage-pipeline', 'auto_triage_pipeline', 'auto-triage-pipeline', '/auto-triage-pipeline'],
  ['channels', 'channels', 'channels', '/channels'],
  ['code-review-sage', 'code_review_sage', 'code-review-sage', '/code-review-sage'],
  ['command-bar', 'command_bar', 'command-bar', '/chat'],
  ['crew-companion', 'crew_companion', 'crew-companion', '/crew-companion'],
  ['design-critique', 'design_critique', 'design-critique', '/design-critique'],
  ['design-tweak', 'design_tweak', 'design-tweak', '/design-tweak'],
  ['dev-fleet', 'dev_fleet', 'dev-fleet', '/dev-fleet'],
  ['file-explorer', 'file_explorer', 'file-explorer', '/file-explorer'],
  ['issue-radar', 'issue_radar', 'issue-radar', '/issue-radar'],
  ['md-notebook', 'md_notebook', 'md-notebook', '/md-notebook'],
  ['meetings', 'meetings', 'meetings', '/meetings'],
  ['mochi', 'mochi', 'mochi', '/mochi', 'shot-1-gallery.png'],
  ['ops-mission-control', 'ops_mission_control', 'ops-mission-control', '/ops-mission-control'],
  ['papyrus', 'papyrus', 'papyrus', '/papyrus'],
  ['personal-shopper', 'personal_shopper', 'personal-shopper', '/personal-shopper'],
  ['pptx-maker', 'pptx_maker', 'pptx-maker', '/pptx-maker'],
  ['projects', 'projects', 'projects', '/projects'],
  ['spec-builder', 'spec_builder', 'spec-builder', '/spec-builder'],
  ['workflows', 'workflows', 'workflows', '/workflows'],
]

const configuredDirs = new Set(APPS.map(([, dir]) => dir))
const discoveredDirs = readdirSync(BUILTINS, { withFileTypes: true })
  .filter(entry => entry.isDirectory() && existsSync(join(BUILTINS, entry.name, 'app.json')))
  .map(entry => entry.name)
const unconfiguredDirs = discoveredDirs.filter(dir => !configuredDirs.has(dir))
if (unconfiguredDirs.length > 0) {
  throw new Error(`capture list is missing built-in manifests: ${unconfiguredDirs.join(', ')}`)
}

const manifests = new Map(APPS.map(([name, dir]) => {
  const manifest = JSON.parse(readFileSync(`${BUILTINS}${dir}/app.json`, 'utf8'))
  return [name, manifest]
}))

const installed = APPS.map(([name]) => {
  const manifest = manifests.get(name)
  return {
    name,
    version: manifest.version,
    displayName: manifest.displayName,
    enabled: true,
    installedAt: '2026-08-25T12:00:00Z',
    source: 'builtin',
    origin: 'builtin',
    resources: 'gateway',
    lifecycle: 'locked',
    manifest,
  }
})

/** Stable examples for components whose empty response has a richer envelope. */
const FIXTURES = {
  '/api/apps/auto-improvement/config': { configured: false, target_url: '', branch: '' },
  '/api/apps/auto-improvement/status': { status: 'idle', findings: [], history: [] },
  '/api/apps/auto-research/questions': { questions: [], sessions: [] },
  '/api/apps/auto-triage-pipeline/runs': { runs: [] },
  '/api/apps/code-review-sage/runs': { runs: [], pool: null, reviewer: null },
  '/api/apps/code-review-sage/repos': { repos: [] },
  '/api/apps/code-review-sage/settings': { settings: {}, models: [], efforts: [], namespaces: [] },
  '/api/apps/design-critique/sessions': { sessions: [] },
  '/api/apps/dev-fleet/worktrees': { worktrees: [], needs_setup: true },
  '/apps/file-explorer/api/health': { allowedRoots: ['/workspace'], home: '/workspace' },
  '/apps/file-explorer/api/tree': { entries: [] },
  '/apps/file-explorer/api/git-status': null,
  '/api/apps/issue-radar/repos': { repos: [], configured: false },
  '/api/apps/issue-radar/overview': { issues: [], pulls: [], totals: {} },
  '/apps/md-notebook/api/health': { ok: true, features: ['trash', 'vaults', 'settings'] },
  '/apps/md-notebook/api/settings': {
    settings: { autoSync: false, autoSyncMins: 10, lastSync: null },
  },
  '/apps/md-notebook/api/vaults': { vaults: [], hasPat: false, hasGhAuth: false },
  '/api/apps/meetings/sessions': { sessions: [], active: null },
  '/api/apps/meetings/settings': { calendar_connected: false, task_provider: '' },
  '/api/apps/ops-mission-control/state': {
    incidents: [], counts: {}, providers: [], rotation: null,
    ledger: { total: 0, promoted: 0, demoted: 0 },
  },
  '/api/apps/ops-mission-control/ledger': {
    entries: [], stats: { total: 0, promoted: 0, demoted: 0 },
  },
  '/api/apps/papyrus/projects': { projects: [] },
  '/api/apps/personal-shopper/history': { items: [] },
  '/api/apps/personal-shopper/preferences': { preferences: [] },
  '/api/apps/personal-shopper/groups': { groups: [] },
  '/api/apps/pptx-maker/engine': {
    ready: true, clone: true, venv: true, pinnedTag: 'v1',
    provision: { state: 'done', log: '', elapsed: 0 },
  },
  '/api/apps/pptx-maker/deps': {
    labels: {}, present: {}, managed: {}, missing: [], hints: {},
  },
  '/api/apps/pptx-maker/assets': {
    sources: [], provisioned: {}, ready: true, tag: 'v1', state: 'done',
    log: '', elapsed: 0, perSource: {},
  },
  '/api/apps/pptx-maker/config': { deckRoot: '/workspace/decks', default: '/workspace/decks' },
  '/api/apps/pptx-maker/decks': { decks: [] },
  '/api/apps/pptx-maker/styles': { styles: [] },
  '/api/apps/pptx-maker/templates': { templates: [] },
  '/api/apps/spec-builder/specs': { specs: [] },
  '/api/apps/workflows/runs': { runs: [] },
  '/api/workflows': { workflows: [] },
  '/api/projects': { projects: [] },
  '/api/worlds': { worlds: [] },
}

function appResponse(name) {
  return installed.find(app => app.name === name)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: VIEWPORT,
    deviceScaleFactor: 1,
    serviceWorkers: 'block',
  })
  const page = await context.newPage()

  const extra = async (path, route) => {
    if (path === '/api/apps') {
      await json(route, installed)
      return true
    }
    if (path === '/api/apps/registry') {
      await json(route, { apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } })
      return true
    }
    if (Object.prototype.hasOwnProperty.call(FIXTURES, path)) {
      await json(route, FIXTURES[path])
      return true
    }
    const detail = path.match(/^\/api\/apps\/([^/]+)$/)
    if (detail) {
      const app = appResponse(decodeURIComponent(detail[1]))
      await json(route, app || { code: 'app_not_found' }, app ? 200 : 404)
      return true
    }
    return false
  }

  await stubDashboardApi(page, {
    theme: 'light',
    extra,
    localStorageEntries: {
      'kc-onboarded': '1',
      'mc-changelog-seen': '9999',
      'mc-yolo-ack': '1',
    },
  })
  logPageProblems(page)

  for (const [name, , assetDir, route, screenshotName = 'screenshot-main.png'] of APPS) {
    const outputDir = join(OUTPUT_ROOT, assetDir)
    mkdirSync(outputDir, { recursive: true })
    const output = join(outputDir, screenshotName)
    const seeded = SEEDED_CAPTURES[name]
    if (seeded && existsSync(seeded)) {
      if (seeded !== output) copyFileSync(seeded, output)
      console.log(`copied seeded real capture ${output.replace(`${ROOT}`, '')}`)
      continue
    }

    await page.goto(`${base}${route}`, { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2200)
    if (name === 'design-critique') {
      const example = page.getByRole('button', { name: /example/i }).first()
      if (await example.isVisible().catch(() => false)) {
        await example.click()
        await page.waitForTimeout(500)
      }
    }
    const visibleText = (await page.locator('body').innerText()).replace(/\s+/g, ' ').trim()
    if (visibleText.length < 40) {
      throw new Error(`${name}: capture is effectively blank (${visibleText.length} characters)`)
    }
    if (await page.getByText('Something went wrong', { exact: true }).isVisible().catch(() => false)) {
      throw new Error(`${name}: capture rendered the application error boundary`)
    }
    // Validate the live page before replacing a known-good catalog asset. A
    // failed capture must leave the previous screenshot intact.
    await page.screenshot({ path: output, animations: 'disabled' })
    console.log(`wrote ${output.replace(`${ROOT}`, '')} (${visibleText.length} visible characters)`)
  }

  await context.close()
  await browser.close()
  srv.close()
}

await main()
