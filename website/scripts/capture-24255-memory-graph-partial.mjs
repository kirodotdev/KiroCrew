/**
 * Screenshot harness: the memory graph says when the server capped it.
 *
 * Serves the REAL built SPA (website/dist) and answers /api/** from the shared stub
 * map. The scene-specific stub is `GET /api/memory/graph`: a handful of nodes with
 * `partial` set (the semantic population was capped), and once with nothing
 * partial, which draws no notice. Shoots Developer > Memory in dark and light.
 *
 * Usage: node scripts/capture-24255-memory-graph-partial.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/24255-memory-graph-partial'
const PREFIX = process.argv[3] || 'after'

const NODES = [
  { id: 'p1', label: 'Prefers concise answers', group: 'preference', title: 'Prefers concise answers' },
  { id: 'j1', label: 'acme', group: 'project', title: 'acme' },
  { id: 's1', label: 'project.acme.status', group: 'semantic', title: 'project.acme.status = shipping' },
  { id: 's2', label: 'user.editor', group: 'semantic', title: 'user.editor = vim' },
  { id: 'l1', label: 'Run the related tests only', group: 'lesson', title: 'Run the related tests only' },
]
const EDGES = [{ from: 's1', to: 'j1' }]

mkdirSync(OUT, { recursive: true })

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }

  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })

  for (const [theme, partial] of [['dark', true], ['light', true], ['dark', false]]) {
    const context = await browser.newContext({ viewport: { width: 1300, height: 860 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots: [],
      // `return json(...), true` -- the comma marks the request handled.
      extra: async (path, route) => {
        if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' }), true
        if (path === '/api/acp-backends') return json(route, { backends: [] }), true
        if (path === '/api/memory/graph') {
          return json(route, { nodes: NODES, edges: EDGES, partial: { semantic: partial, lessons: false } }), true
        }
        if (path.startsWith('/api/chat/slots/')) {
          return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
        }
        return false
      },
    })
    await page.addInitScript(t => {
      localStorage.setItem('mc-theme-mode', t)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-lang', 'en')
      localStorage.setItem('mc-dev-mode', '1')
    }, theme)

    await page.goto(`${base}/developer?tab=memory`, { waitUntil: 'domcontentloaded' })
    await page.getByLabel('Search memory nodes').waitFor({ state: 'visible', timeout: 20000 })
    if (partial) await page.getByTestId('memory-graph-partial').waitFor({ state: 'visible', timeout: 15000 })
    await page.waitForTimeout(800) // let the one-shot layout paint the canvas
    const out = `${OUT}/${PREFIX}-${partial ? 'partial' : 'complete'}-${theme}.png`
    await page.screenshot({ path: out })
    console.log('wrote', out)
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
