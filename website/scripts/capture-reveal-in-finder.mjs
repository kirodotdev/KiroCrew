/**
 * Screenshot harness for the file viewer's reveal action, whose label names the
 * gateway host's own file manager (Finder / File Explorer / generic).
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route
 * interception (gateway-free — no kiro-cli, no live backend). The file-explorer
 * app has its own API prefix, so those endpoints are supplied through the
 * shared stub's `extra` hook.
 *
 * The action lives in the viewer bar's overflow menu (the row caps at two peer
 * buttons), so each capture opens the menu first. It also asserts the click
 * reaches POST /api/reveal with the open file's path, so the frame is evidence of
 * a wired control rather than of a rendered glyph.
 *
 * Usage: node scripts/capture-reveal-in-finder.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/reveal-in-finder'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const ROOT = '/Users/kyle/workspace/kirocrew'
const FILE = `${ROOT}/README.md`
const FE_API = '/apps/file-explorer/api'

const ENTRIES = [
  { name: 'src', path: `${ROOT}/src`, type: 'dir', children: [
    { name: 'index.ts', path: `${ROOT}/src/index.ts`, type: 'file', size: 812, mtime: 1755000000 },
  ] },
  { name: 'README.md', path: FILE, type: 'file', size: 402, mtime: 1755100000 },
  { name: 'pyproject.toml', path: `${ROOT}/pyproject.toml`, type: 'file', size: 1204, mtime: 1755000000 },
]

const CONTENT = [
  '# Kiro Crew',
  '',
  'The autonomous agent management layer: persistent memory, scheduled jobs,',
  'background subagents and multi-session orchestration.',
  '',
  '## Install',
  '',
  '    pipx install kirocrew',
  '',
].join('\n')

/**
 * Answer the file-explorer app's own endpoints.
 *
 * The shared stub awaits this hook and treats a truthy result as "handled", so
 * each branch must await its own fulfil and return `true` — returning the
 * fulfil promise resolves to `undefined` and the request is then answered twice.
 */
async function fileExplorerRoutes(path, route) {
  if (!path.startsWith(FE_API)) return false
  if (path === `${FE_API}/health`) await json(route, { allowedRoots: [ROOT], home: ROOT })
  else if (path === `${FE_API}/tree`) await json(route, { entries: ENTRIES })
  else if (path === `${FE_API}/read`) {
    await json(route, {
      size: 402, mtime: 1755100000, mime: 'text/markdown',
      encoding: 'utf-8', content: CONTENT,
    })
  }
  else if (path === `${FE_API}/git-status`) await json(route, { repoRoot: ROOT, branch: 'main', statuses: {} })
  else if (path === `${FE_API}/resolve`) await json(route, { exists: true, type: 'dir' })
  else await json(route, { entries: [] })
  return true
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const revealed = []

  // The label follows the GATEWAY's platform, so the platform is a fixture axis
  // and not just a theme: a macOS gateway names Finder, everything else (and an
  // unknown platform) keeps the neutral wording.
  const cases = [
    { theme: 'dark', platform: 'darwin', label: 'Open in Finder', name: 'mac-dark' },
    { theme: 'light', platform: 'darwin', label: 'Open in Finder', name: 'mac-light' },
    { theme: 'dark', platform: 'win32', label: 'Open in File Explorer', name: 'windows-dark' },
    { theme: 'dark', platform: 'linux', label: 'Show in file manager', name: 'linux-dark' },
  ]

  for (const c of cases) {
    const context = await browser.newContext({
      viewport: { width: 1400, height: 900 },
      deviceScaleFactor: 2, // 11-12px toolbar type renders soft at 1x
    })
    const page = await context.newPage()
    await stubDashboardApi(page, {
      theme: c.theme,
      extra: async (path, route) => {
        if (path === '/api/reveal') {
          // Record what the menu row asked for, then answer as a desktop host
          // would so the success path is what the frame shows.
          revealed.push(route.request().postDataJSON())
          await json(route, { ok: true })
          return true
        }
        if (path === '/api/kiro-prerequisite') {
          // Same shape as the shared stub's, with the platform under test.
          await json(route, {
            platform: c.platform, installed: true, authenticated: true, ready: true,
            initial_setup_complete: true, can_auto_install: false, can_login: false,
            repair_required: false, docs_url: '', setup_allowed: false,
          })
          return true
        }
        return fileExplorerRoutes(path, route)
      },
    })
    logPageProblems(page)

    await page.goto(`${base}/file-explorer`)
    await page.waitForSelector('.mc-fe-tree')
    await page.getByText('README.md', { exact: true }).click()
    await page.waitForSelector('.mc-fe-viewer-filename')

    // The action row caps at two controls, so the reveal lives in the overflow —
    // the frame has to show the menu open or it shows nothing of the feature.
    await page.getByRole('button', { name: 'More options' }).click()
    const reveal = page.getByRole('menuitem', { name: c.label })
    await reveal.waitFor()
    await page.screenshot({ path: `${OUT}/${PREFIX}-01-file-viewer-${c.name}.png` })
    await page.locator('.mc-fe-viewer-bar').screenshot({ path: `${OUT}/${PREFIX}-02-viewer-bar-${c.name}.png` })

    await reveal.click()
    await page.waitForFunction(() => true)
    await context.close()
  }

  await browser.close()
  srv.close()

  // The frame is only evidence if the control is wired: prove each click became
  // a reveal request for the open file.
  const paths = revealed.map((b) => b?.path)
  const ok = paths.length === cases.length && paths.every((p) => p === FILE)
  console.log(`POST /api/reveal received: ${JSON.stringify(revealed)}`)
  if (!ok) {
    console.error(`expected ${cases.length} reveal requests for ${FILE}, got ${JSON.stringify(paths)}`)
    process.exit(1)
  }
  console.log(`wrote ${cases.length * 2} screenshots to ${OUT}`)
}

main().catch((err) => { console.error(err); process.exit(1) })
