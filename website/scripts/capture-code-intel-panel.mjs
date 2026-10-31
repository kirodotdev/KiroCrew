/**
 * Capture code intel's Store screenshot: its Language servers tab in the chat
 * side panel, beside a conversation.
 *
 * Runs the REAL built SPA (website/dist) and the app's REAL built tab bundle
 * (code_intel/ui/dist/panel.mjs) behind the shared loopback static server. Only
 * the network is answered from fixtures: two running servers, one detected
 * project with no server installed, and the outline of one Python file, so the
 * frame shows the tab's sections populated without a gateway or language
 * servers on the capturing machine.
 *
 * Usage:
 *   npm run build
 *   node scripts/capture-code-intel-panel.mjs [outputPath]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const BUILTIN = fileURLToPath(new URL('../../src/kiro_crew/apps/builtins/code_intel/', import.meta.url))
const OUTPUT = resolve(process.argv[2]
  || fileURLToPath(new URL('../public/app-assets/code-intel/screenshot-main.png', import.meta.url)))
const VIEWPORT = { width: 1280, height: 800 }
const SLOT = 'code-intel-shot'
const PROJECT = '/workspace/billing-service'

const manifest = JSON.parse(readFileSync(`${BUILTIN}app.json`, 'utf8'))
const bundle = readFileSync(`${BUILTIN}ui/dist/panel.mjs`)

const app = {
  name: 'code-intel',
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

const slots = [{
  key: SLOT,
  title: 'Trace the invoice rounding bug',
  running: false,
  messages: 4,
  agent: 'kirocrew',
  modified: Math.floor(Date.now() / 1000),
  last_ts: '2026-08-25T12:00:00Z',
  folder_id: '',
  project: PROJECT,
}]

const server = (name, root, command, extra = {}) => ({
  server: name,
  root,
  folders: [],
  state: 'ready',
  error: '',
  stderr: '',
  pid: 4242,
  command,
  uptimeSecs: 1260,
  ageSecs: 1264,
  idleSecs: 40,
  shutdownInSecs: 1760,
  retryInSecs: null,
  notices: [],
  ...extra,
})

const POOL = {
  servers: {
    servers: [
      server('python', PROJECT, 'pyright-langserver --stdio'),
      server('typescript', `${PROJECT}/web`, 'typescript-language-server --stdio', { pid: 4310, uptimeSecs: 410 }),
    ],
    maxServers: 8,
    idleShutdownSecs: 1800,
  },
  detect: {
    directory: PROJECT,
    projects: [
      { server: 'python', root: PROJECT, files: 214, available: true, command: 'pyright-langserver', install: [], refused: [], state: 'ready' },
      { server: 'typescript', root: `${PROJECT}/web`, files: 96, available: true, command: 'typescript-language-server', install: [], refused: [], state: 'ready' },
      { server: 'go', root: `${PROJECT}/tools/exporter`, files: 12, available: false, command: '', install: ['gopls'], refused: [], state: 'stopped' },
    ],
    truncated: false,
  },
  symbols: {
    server: 'python',
    truncated: false,
    symbols: [
      { name: 'Invoice', kind: 'class', detail: '', container: '', depth: 0, line: 14, character: 0 },
      { name: '__init__', kind: 'method', detail: '', container: 'Invoice', depth: 1, line: 21, character: 4 },
      { name: 'add_line', kind: 'method', detail: '', container: 'Invoice', depth: 1, line: 33, character: 4 },
      { name: 'subtotal', kind: 'property', detail: '', container: 'Invoice', depth: 1, line: 41, character: 4 },
      { name: 'total', kind: 'method', detail: '', container: 'Invoice', depth: 1, line: 48, character: 4 },
      { name: 'round_half_even', kind: 'function', detail: '', container: '', depth: 0, line: 62, character: 0 },
    ],
  },
}

const DIRS = {
  [PROJECT]: {
    dirs: ['billing', 'tests', 'web'],
    files: ['pyproject.toml', 'README.md'],
  },
  [`${PROJECT}/billing`]: {
    dirs: [],
    files: ['__init__.py', 'invoice.py', 'ledger.py', 'tax.py'],
  },
}

function listing(path) {
  const entry = DIRS[path] || { dirs: [], files: [] }
  return {
    path,
    parent: path.replace(/\/[^/]+$/, ''),
    dirs: entry.dirs.map(name => ({ name, path: `${path}/${name}`, mtime: 0 })),
    files: entry.files.map(name => ({ name, path: `${path}/${name}`, mtime: 0 })),
  }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 1, serviceWorkers: 'block' })
  const page = await context.newPage()

  await page.route('**/apps/code-intel/ui/**', route => route.fulfill({
    status: 200,
    contentType: 'text/javascript',
    body: bundle,
  }))

  const extra = async (path, route) => {
    if (path === '/api/apps') return json(route, [app]).then(() => true)
    if (path === '/api/apps/code-intel') return json(route, app).then(() => true)
    if (path === '/api/apps/registry') {
      return json(route, { apps: [], serverPlatform: { os: 'linux', arch: 'x86_64' } }).then(() => true)
    }
    if (path === '/api/apps/code-intel/pool') {
      const { op } = route.request().postDataJSON() || {}
      if (!(op in POOL)) return json(route, { error: `no fixture for ${op}` }, 404).then(() => true)
      return json(route, POOL[op]).then(() => true)
    }
    if (path === '/api/browse-files') {
      const target = new URL(route.request().url()).searchParams.get('path') || PROJECT
      return json(route, listing(target)).then(() => true)
    }
    return false
  }

  const kind = `app:code-intel:lsp`
  await stubDashboardApi(page, {
    theme: 'light',
    slots,
    extra,
    localStorageEntries: {
      'kc-onboarded': '1',
      'mc-changelog-seen': '9999',
      'mc-yolo-ack': '1',
      'mc-privacy-notice-v1': '1',
      'mc-active-slot': SLOT,
      [`mc-activity-open:${SLOT}`]: 'true',
      'mc-side-panel-width': '460',
      [`mc-panel-tabs:${SLOT}`]: JSON.stringify({
        tabs: [
          { id: 'files', kind: 'files', title: 'Files' },
          { id: kind, kind, title: 'Language servers', appName: 'code-intel', appTabId: 'lsp', slot: SLOT },
        ],
        activeId: kind,
      }),
    },
  })
  logPageProblems(page)

  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
  const panel = page.getByTestId('code-intel-panel')
  await panel.waitFor({ state: 'visible', timeout: 20000 })
  // Open billing/invoice.py's outline so the frame shows symbols, not only files.
  await panel.getByRole('button', { name: 'billing', exact: true }).click()
  await panel.getByRole('button', { name: /invoice\.py/ }).first().click()
  await panel.getByText('round_half_even').waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(600)

  if (await page.getByText('Something went wrong', { exact: true }).isVisible().catch(() => false)) {
    throw new Error('capture rendered the application error boundary')
  }
  mkdirSync(dirname(OUTPUT), { recursive: true })
  await page.screenshot({ path: OUTPUT, animations: 'disabled' })
  console.log(`wrote ${OUTPUT}`)

  await context.close()
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
