/**
 * Screenshot harness for the file-tree row context menu's new Download row
 * (issue #9665). House pattern of website/scripts/capture-pierre-files-tab.mjs:
 * the REAL built SPA (website/dist) behind the in-process static server, every
 * /api/** answered from fixtures via Playwright route interception --
 * gateway-free, no kiro-cli, no token. The client code under test is unmodified.
 *
 * Lives under website/capture/ (the documented harness location; the scripts
 * are git-tracked). It writes frames into `temp-screenshots/`, which is
 * GITIGNORED on purpose: PR review evidence is attached to the pull request
 * with `gh pr edit --attach`, never committed to the tree. It reuses the
 * serve-dist + stub-dashboard-api libs that ship under website/scripts/lib/.
 *
 * Frames:
 *   20-tree-download-file    right-clicking a FILE row: Add to chat + Download.
 *   21-tree-nodownload-dir   right-clicking a DIRECTORY row: Add to chat only,
 *                            no Download (the ask is file rows only).
 *   22-tree-download-refused clicking Download on a file the credential scan
 *                            flags: /api/file-download answers 400 and the tree
 *                            surfaces a refusal notice, not bytes. NOTE: this
 *                            frame stubs the endpoint's 400 to exercise the
 *                            CLIENT's 400-handling branch -- it is the handler,
 *                            not a live scanner run.
 *   23-viewer-download-refused
 *                            the MarkdownPanel viewer's own Download on a
 *                            flagged file: the SAME downloadFileToDisk, so the
 *                            same credential refusal surfaces through the
 *                            panel's notice on a second surface.
 *
 * The tree renders inside a `<file-tree-container>` shadow root, so the row menu
 * is opened with a real right-click at the row's measured screen coordinates
 * (Pierre binds its own contextmenu handler); it renders `TreeContextMenu` into
 * its own `role="menu"` slot.
 *
 * Usage (from website/): node capture/shoot-tree-download-menu.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync, readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { serveDist } from '../scripts/lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from '../scripts/lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/tree-download-menu'
const PROJECT = resolve(dirname(fileURLToPath(import.meta.url)), '../..')
const SLOT = 'chat-tree-download'
const MAX_EDGE = 2000
const MIN_MBPP = 15

mkdirSync(OUT, { recursive: true })

const TREE_PATHS = [
  'README.md',
  'package.json',
  'docs/architecture/side-panel.md',
  'website/src/pierre/PierreWorkspaceTreeImpl.tsx',
]

/** The file whose Download the refusal frame flags. In the tree so its row
 *  exists; /api/file-download answers 400 for it, standing in for a positive
 *  credential-scan verdict. */
const FLAGGED_ROW = 'package.json'

/** A markdown file opened in the viewer for frame 23 (the viewer's own Download
 *  refusal). Its content is served by /api/file-read; the body names itself so
 *  the wait has a landmark. */
const MD_REL = 'docs/architecture/side-panel.md'
const MD_PATH = `${resolve(dirname(fileURLToPath(import.meta.url)), '../..')}/${MD_REL}`
const MD_CONTENT = '# Side panel\n\nThis file is flagged in this fixture so the viewer Download exercises the credential-scan refusal path.\n'


const slots = [{
  key: SLOT, title: 'Tree download', running: false, last_message: 'Tree download',
  messages: 1, agent: 'kirocrew', memory_mode: 'persistent', project: PROJECT,
  modified: Math.floor(Date.now() / 1000), source_links: [], source_links_total: 0,
}]
const slotDetail = { running: false, has_more: false, total: 0, queue: [], messages: [] }

const FILES_TAB = { id: 'files', kind: 'files', title: 'Files' }
const bucket = (tabs, activeId) => JSON.stringify({ activeId, tabs })

function pngSize(path) {
  const b = readFileSync(path)
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) }
}

async function main() {
  const { srv, base } = await serveDist()
  const executablePath = chromiumExecutable()
  console.log('chromium:', executablePath || '(playwright default)')
  const browser = await chromium.launch({ executablePath })
  const context = await browser.newContext({ viewport: { width: 1200, height: 820 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  const extra = async (path, route) => {
    if (path === '/api/chat/slots') return json(route, slots), true
    if (/^\/api\/chat\/slots\/[^/]+/.test(path)) return json(route, slotDetail), true
    if (path === '/api/project/tree') return json(route, { root: PROJECT, paths: TREE_PATHS, repo: true, truncated: false }), true
    if (path === '/api/project/git/status') return json(route, { repo: true, repoRoot: PROJECT, branch: 'main', ahead: 0, behind: 0, files: [] }), true
    if (path === '/api/project/git') return json(route, { path: PROJECT, repo: true, repoRoot: PROJECT, branch: 'main', detached: false, head: 'a1b2c3d' }), true
    if (path === '/api/project/git/log') return json(route, { repo: true, commits: [] }), true
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] }), true
    // Cold-tab hydration reads file-read as TEXT (the shared map's catch-all
    // would hand it JSON []); serve the MD body for the viewer frame.
    if (path === '/api/file-read') {
      const q = new URL(route.request().url()).searchParams.get('path') || ''
      return route.fulfill(q === MD_PATH || q.endsWith(MD_REL)
        ? { status: 200, contentType: 'text/plain; charset=utf-8', body: MD_CONTENT }
        : { status: 404, contentType: 'text/plain', body: 'not found' }), true
    }
    // The credential-gate refusal: /api/file-download aborts a flagged file
    // with 400 {error: content redacted}. Answering 400 here drives the
    // client's own 400-handling branch (downloadFileToDisk -> onError -> the
    // tree's ErrorNotice). This is the HANDLER, not a live scanner run.
    if (path === '/api/file-download') {
      return route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'file content was redacted; download aborted', code: 'content_redacted' }) }), true
    }
    return false
  }

  await stubDashboardApi(page, { slots, extra })
  logPageProblems(page)

  const wrote = []
  function record(file, note) {
    const { w, h } = pngSize(file)
    const bytes = readFileSync(file).length
    const mbpp = Math.round((bytes * 1000) / (w * h))
    const over = w > MAX_EDGE || h > MAX_EDGE
    const blank = mbpp < MIN_MBPP
    console.log(`wrote ${file}  ${w}x${h}  ${bytes}B  ${mbpp} mB/px${over ? '  OVER' : ''}${blank ? '  BLANK' : ''}  ${note}`)
    wrote.push({ file, over, blank })
    if (blank) throw new Error(`frame ${file}: ${mbpp} mB/px below ${MIN_MBPP} blank floor`)
    if (over) throw new Error(`frame ${file}: over ${MAX_EDGE}px`)
  }

  async function load() {
    await page.addInitScript(([slot, tabsJson, project]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot-chat', slot)
      localStorage.setItem('mc-activity-open:' + slot, 'true')
      localStorage.setItem('mc-panel-tabs:' + slot, tabsJson)
      localStorage.setItem('mc-files-rail-open', '1')
      localStorage.setItem('mc-files-rail-w', '360')
      localStorage.setItem('mc-side-panel-width', '560')
      localStorage.setItem('mc-git-panel-opened:' + slot + ':' + project, '1')
      localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }))
    }, [SLOT, bucket([FILES_TAB], 'files'), PROJECT])
    await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  /** Like load(), but with a file tab open and active so the MarkdownPanel
   *  viewer is mounted (frame 23). */
  async function loadWithFile() {
    const fileTab = { id: `file:${MD_PATH}`, kind: 'file', title: 'side-panel.md', path: MD_PATH, slot: SLOT }
    await page.addInitScript(([slot, tabsJson, project]) => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-active-slot-chat', slot)
      localStorage.setItem('mc-activity-open:' + slot, 'true')
      localStorage.setItem('mc-panel-tabs:' + slot, tabsJson)
      localStorage.setItem('mc-files-rail-open', '1')
      localStorage.setItem('mc-files-rail-w', '300')
      localStorage.setItem('mc-side-panel-width', '640')
      localStorage.setItem('mc-file-linenums', '1')
      localStorage.setItem('kirocrew:comment-hint-dismissed', '1')
      localStorage.setItem('mc-git-panel-opened:' + slot + ':' + project, '1')
      localStorage.setItem('mc-chat-config', JSON.stringify({ pinLastPrompt: false, streamMode: 'immediate' }))
    }, [SLOT, bucket([FILES_TAB, fileTab], `file:${MD_PATH}`), PROJECT])
    await page.goto(base + '/?sid=' + encodeURIComponent(SLOT), { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  const panel = () => page.locator('div:has(> .side-panel-strip)').last()
  const waitTreeText = async (name) => page.waitForFunction(
    n => (document.querySelector('file-tree-container')?.shadowRoot?.textContent ?? '').replace(/…/g, '').includes(n),
    name, { timeout: 20000 })

  /** Screen-space center of the row whose label (truncation markers dropped)
   *  contains `label`, measured inside the shadow root. */
  const rowCenter = (label) => page.evaluate((lbl) => {
    const root = document.querySelector('file-tree-container')?.shadowRoot
    if (!root) return null
    const norm = s => (s || '').replace(/…/g, '').replace(/\s+/g, '')
    const candidates = [...root.querySelectorAll('*')].filter(r => norm(r.textContent).includes(lbl))
    if (!candidates.length) return { notFound: true, sample: norm(root.textContent).slice(0, 120) }
    candidates.sort((a, b) => a.textContent.length - b.textContent.length)
    const rect = candidates[0].getBoundingClientRect()
    return { x: Math.round(rect.x + rect.width / 2), y: Math.round(rect.y + rect.height / 2) }
  }, label)

  const openRowMenu = async (label) => {
    const c = await rowCenter(label)
    if (!c || c.notFound) return { ok: false, c }
    await page.mouse.move(c.x, c.y)
    await page.waitForTimeout(150)
    await page.mouse.click(c.x, c.y, { button: 'right' })
    return { ok: true, c }
  }

  await load()
  await panel().waitFor({ state: 'visible', timeout: 20000 })
  await waitTreeText('README.md')
  await page.waitForTimeout(1200)

  // ── Frame 20: FILE row → Add to chat + Download ─────────────────────────────
  let r = await openRowMenu('README.md')
  console.log('openRowMenu(README.md)', JSON.stringify(r))
  await page.locator('[role="menu"]').first().waitFor({ state: 'visible', timeout: 8000 })
  await page.waitForTimeout(400)
  {
    const rows = await page.locator('[role="menu"] [role="menuitem"]').allInnerTexts()
    console.log('DIAG file-menu rows', JSON.stringify(rows))
    const hasDownload = rows.some(t => /Download/.test(t))
    const hasAdd = rows.some(t => /Add to chat/.test(t))
    if (!hasDownload || !hasAdd) throw new Error(`frame 20: expected Add to chat + Download, got ${JSON.stringify(rows)}`)
    await page.locator('[role="menu"]').first().screenshot({ path: `${OUT}/20-tree-download-file.png` })
    record(`${OUT}/20-tree-download-file.png`, `rows=${JSON.stringify(rows)}`)
  }
  await page.keyboard.press('Escape')
  await page.waitForTimeout(300)

  // ── Frame 21: DIRECTORY row → Add to chat only, no Download ─────────────────
  r = await openRowMenu('docs')
  console.log('openRowMenu(docs)', JSON.stringify(r))
  await page.locator('[role="menu"]').first().waitFor({ state: 'visible', timeout: 8000 })
  await page.waitForTimeout(400)
  {
    const rows = await page.locator('[role="menu"] [role="menuitem"]').allInnerTexts()
    console.log('DIAG dir-menu rows', JSON.stringify(rows))
    const hasDownload = rows.some(t => /Download/.test(t))
    const hasAdd = rows.some(t => /Add to chat/.test(t))
    if (hasDownload) throw new Error(`frame 21: a directory row must NOT show Download, got ${JSON.stringify(rows)}`)
    if (!hasAdd) throw new Error(`frame 21: expected Add to chat on a directory row, got ${JSON.stringify(rows)}`)
    await page.locator('[role="menu"]').first().screenshot({ path: `${OUT}/21-tree-nodownload-dir.png` })
    record(`${OUT}/21-tree-nodownload-dir.png`, `rows=${JSON.stringify(rows)}`)
  }
  await page.keyboard.press('Escape')
  await page.waitForTimeout(300)

  // ── Frame 22: Download REFUSED — the gate's 400 surfaces, no bytes ─────────
  r = await openRowMenu(FLAGGED_ROW)
  console.log('openRowMenu(' + FLAGGED_ROW + ')', JSON.stringify(r))
  await page.locator('[role="menu"]').first().waitFor({ state: 'visible', timeout: 8000 })
  await page.getByRole('menuitem', { name: 'Download' }).first().click()
  // The tree's own ErrorNotice (testid workspace-tree-action-error) is where a
  // refusal lands — it outlives the menu, which the click closes.
  const notice = page.locator('[data-testid="workspace-tree-action-error"]')
  await notice.waitFor({ state: 'visible', timeout: 8000 })
  const noticeText = (await notice.innerText()).trim()
  console.log('DIAG refusal notice', JSON.stringify(noticeText))
  if (!/credential scan/i.test(noticeText)) throw new Error(`frame 22: expected a credential-scan refusal notice, got ${JSON.stringify(noticeText)}`)
  await page.waitForTimeout(300)
  await panel().screenshot({ path: `${OUT}/22-tree-download-refused.png` })
  record(`${OUT}/22-tree-download-refused.png`, `notice=${JSON.stringify(noticeText)}`)

  // ── Frame 23: the VIEWER's Download refused — same message, other surface ──
  // The MarkdownPanel viewer's own Download (overflow → Download) calls the SAME
  // downloadFileToDisk; a flagged file's 400 (code content_redacted) surfaces
  // the credential message through the panel's ErrorNotice. Reload with a file
  // tab open so the viewer is mounted.
  await loadWithFile()
  const pv = panel()
  await pv.waitFor({ state: 'visible', timeout: 20000 })
  await page.getByText('flagged in this fixture', { exact: false }).first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1000)
  await page.locator('[data-testid="markdown-panel-more-options"]').first().click()
  await page.locator('[role="menu"]').first().waitFor({ state: 'visible', timeout: 10000 })
  await page.getByRole('menuitem', { name: 'Download' }).first().click()
  const vnotice = pv.getByText('flagged by the credential scan', { exact: false })
  await vnotice.waitFor({ state: 'visible', timeout: 8000 })
  const vtext = (await vnotice.innerText()).trim()
  console.log('DIAG viewer refusal notice', JSON.stringify(vtext))
  await page.waitForTimeout(300)
  await pv.screenshot({ path: `${OUT}/23-viewer-download-refused.png` })
  record(`${OUT}/23-viewer-download-refused.png`, `notice=${JSON.stringify(vtext)}`)

  console.log('\n── SUMMARY ─────────────────────────────')
  const bad = wrote.filter(w => w.over || w.blank)
  console.log(bad.length ? `FAIL ${bad.length}` : `all ${wrote.length} frames ok`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
