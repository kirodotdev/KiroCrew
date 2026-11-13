/**
 * Screenshot harness for the Files app's Refresh folder control.
 *
 * The tree is cached and nothing pushes filesystem changes to it, so a folder
 * created after the page loaded (a `git clone`, an agent's writes) stayed
 * invisible. The path bar now carries a Refresh folder button that re-reads the
 * tree and its git status.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server, with every API call answered from fixtures. The tree endpoint answers
 * the first read with the original listing and every later read with a listing
 * that also holds `cloned-repo/`, standing in for the clone that happened on
 * disk in between. Frames: the loaded tree, the button hovered, and the tree
 * after the click. The script fails unless the click caused a second tree read
 * and the new folder is on screen.
 *
 * Usage: node scripts/capture-files-refresh-folder.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/files-refresh-folder'
mkdirSync(OUT, { recursive: true })

const ROOT = '/home/dev/workspace'
const FE_API = '/apps/file-explorer/api'

const BEFORE = [
  { name: 'notes', path: `${ROOT}/notes`, type: 'dir', children: [
    { name: 'todo.md', path: `${ROOT}/notes/todo.md`, type: 'file', size: 210, mtime: 1761700000 },
  ] },
  { name: 'README.md', path: `${ROOT}/README.md`, type: 'file', size: 402, mtime: 1761700000 },
]
const AFTER = [
  { name: 'cloned-repo', path: `${ROOT}/cloned-repo`, type: 'dir', children: [
    { name: 'pyproject.toml', path: `${ROOT}/cloned-repo/pyproject.toml`, type: 'file', size: 1204, mtime: 1761790000 },
  ] },
  ...BEFORE,
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  let treeReads = 0
  try {
    for (const theme of ['dark', 'light']) {
      treeReads = 0
      const context = await browser.newContext({ viewport: { width: 1200, height: 720 }, deviceScaleFactor: 2 })
      const page = await context.newPage()
      logPageProblems(page)
      await stubDashboardApi(page, {
        theme,
        extra: async (path, route) => {
          if (!path.startsWith(FE_API)) return false
          if (path === `${FE_API}/health`) await json(route, { allowedRoots: [ROOT], home: ROOT })
          else if (path === `${FE_API}/tree`) { treeReads++; await json(route, { entries: treeReads === 1 ? BEFORE : AFTER }) }
          else if (path === `${FE_API}/git-status`) await json(route, { repoRoot: ROOT, branch: 'main', statuses: {} })
          else if (path === `${FE_API}/resolve`) await json(route, { exists: true, type: 'dir' })
          else await json(route, { entries: [] })
          return true
        },
      })
      await page.goto(`${base}/file-explorer`)
      await page.waitForSelector('.mc-fe-tree')
      await page.getByText('README.md', { exact: true }).waitFor()
      if (await page.getByText('cloned-repo', { exact: true }).count()) throw new Error('cloned-repo shown before refresh')
      await page.screenshot({ path: `${OUT}/01-before-refresh-${theme}.png` })

      const btn = page.getByRole('button', { name: 'Refresh folder', exact: true })
      await btn.hover()
      await page.waitForTimeout(300)
      await page.locator('.mc-fe-pathbar').screenshot({ path: `${OUT}/02-refresh-button-${theme}.png` })

      await btn.click()
      await page.getByText('cloned-repo', { exact: true }).waitFor({ timeout: 10000 })
      if (treeReads < 2) throw new Error(`expected a second tree read, saw ${treeReads}`)
      await page.mouse.move(600, 650)
      await page.waitForTimeout(300)
      await page.screenshot({ path: `${OUT}/03-after-refresh-${theme}.png` })
      await context.close()
    }
    console.log(`wrote 6 screenshots to ${OUT}`)
  } finally {
    await browser.close()
    srv.close()
  }
}

await main()
