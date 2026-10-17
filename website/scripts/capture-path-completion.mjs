/**
 * Screenshot harness + behavior check for the composer's `./` PATH COMPLETION
 * (#1504).
 *
 * Typing a token that starts with `./` or `../` opens the file picker scoped to
 * the directory the token names, and Tab/Enter accepts the highlighted entry as
 * plain relative-path text. This photographs the four states a reviewer cannot
 * get from a diff — menu open on `./`, a directory accepted (before/after), and
 * the no-match empty state — and ASSERTS each one against the REAL built SPA
 * (website/dist) so a green run cannot be a screenshot of an error boundary.
 *
 * Nothing in CI runs this file; the CI-enforced half of the behaviour is
 * website/src/test/ChatInput.pathTrigger.test.tsx and test/test_path_complete.py.
 *
 * Usage: node scripts/capture-path-completion.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/path-completion'
const SLOT = 'chat-paths'
const PROJECT = '/home/user/workspace/sprint-42'

mkdirSync(OUT, { recursive: true })

const slots = [{
  key: SLOT,
  title: 'Release notes',
  running: false,
  last_message: 'Ready when you are.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  project: PROJECT,
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]

const detail = {
  running: false,
  has_more: false,
  total: 2,
  queue: [],
  project: PROJECT,
  messages: [
    { role: 'user', ts: Date.now() / 1000 - 600, content: 'Where do the release notes live?' },
    { role: 'assistant', ts: Date.now() / 1000 - 590, content: 'Under the notes directory in this project.' },
  ],
}

const MTIME = Math.floor(Date.now() / 1000) - 3600
/** `parent` is the on-disk directory the row lives in, so the secondary line the
 *  picker renders is the entry's real absolute path, as the endpoint returns. */
const row = (parent, name, kind, size = 0) => ({ path: `${parent}/${name}`, name, kind, size, mtime: MTIME })

/** One directory level per prefix, the way /api/path-complete answers. */
const LEVELS = {
  './': [
    row(PROJECT, 'notes', 'dir'),
    row(PROJECT, 'src', 'dir'),
    row(PROJECT, 'README.md', 'file', 2048),
    row(PROJECT, 'package.json', 'file', 812),
  ],
  './notes/': [
    row(`${PROJECT}/notes`, 'adr', 'dir'),
    row(`${PROJECT}/notes`, 'weekly-recap.md', 'file', 4096),
    row(`${PROJECT}/notes`, 'setup.md', 'file', 1536),
  ],
  // A leading `../` run that comes back into the project resolves, so the
  // endpoint answers it exactly like `./`.
  '../sprint-42/': [
    row(PROJECT, 'notes', 'dir'),
    row(PROJECT, 'src', 'dir'),
    row(PROJECT, 'README.md', 'file', 2048),
    row(PROJECT, 'package.json', 'file', 812),
  ],
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    deviceScaleFactor: 2,
    locale: 'en-US',
  })

  const asked = []

  const extra = async (path, route) => {
    const url = new URL(route.request().url())
    if (url.pathname === '/api/path-complete') {
      const dir = url.searchParams.get('dir') || ''
      const q = (url.searchParams.get('q') || '').toLowerCase()
      asked.push(`${dir}|${q}`)
      const level = LEVELS[dir]
      if (!level) {
        // What the endpoint answers for a token that resolved out of the project:
        // no rows, and the one fact the picker cannot work out for itself.
        if (dir.startsWith('..')) { await json(route, { results: [], root: '', outside: true }); return true }
        await json(route, { results: [], root: PROJECT })
        return true
      }
      const results = q ? level.filter(r => r.name.toLowerCase().startsWith(q)) : level
      await json(route, { results, root: PROJECT })
      return true
    }
    if (url.pathname === '/api/file-search') { await json(route, { results: [], root: PROJECT }); return true }
    if (url.pathname.startsWith('/api/chat/slots/')) { await json(route, detail); return true }
    return false
  }

  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { slots, theme: 'dark', extra })
  await page.addInitScript(slot => { localStorage.setItem('mc-active-slot', slot) }, SLOT)
  await page.goto(base + '/', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2000)

  const composer = page.locator('textarea').first()
  const menu = page.locator('[role="listbox"]')

  // 1 — the menu open on a bare `./`: one directory level, directories first.
  await composer.click()
  // pressSequentially drives real keydown/input events, so the trigger
  // detection and the picker open path both run.
  await composer.pressSequentially('summarize ./', { delay: 15 })
  await menu.getByText('notes/', { exact: true }).waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/1-menu-open-on-dot-slash.png` })
  console.log('wrote', `${OUT}/1-menu-open-on-dot-slash.png`)

  // 2 — before: narrowed to the directory about to be accepted.
  await composer.pressSequentially('no', { delay: 15 })
  await menu.getByText('notes/', { exact: true }).waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/2-before-accepting-a-directory.png` })
  console.log('wrote', `${OUT}/2-before-accepting-a-directory.png`)

  // 3 — after Tab: the token completed to `./notes/` and the menu re-opened on
  // the new level, so the next segment can be typed.
  await composer.press('Tab')
  await menu.getByText('weekly-recap.md', { exact: true }).waitFor({ timeout: 10000 })
  const afterTab = await composer.inputValue()
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/3-after-tab-directory-completed.png` })
  console.log('wrote', `${OUT}/3-after-tab-directory-completed.png`)

  // 4 — the no-match empty state in path mode.
  await composer.pressSequentially('zz', { delay: 15 })
  await menu.getByText(/No matching files/i).waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/4-no-match-empty-state.png` })
  console.log('wrote', `${OUT}/4-no-match-empty-state.png`)

  // 5 — accepting a file inserts a plain relative path, not an @-mention.
  await composer.press('Backspace')
  await composer.press('Backspace')
  await composer.pressSequentially('wee', { delay: 15 })
  await menu.getByText('weekly-recap.md', { exact: true }).waitFor({ timeout: 10000 })
  await composer.press('Enter')
  await page.waitForTimeout(600)
  const afterEnter = await composer.inputValue()
  await page.screenshot({ path: `${OUT}/5-file-completed-as-relative-path.png` })
  console.log('wrote', `${OUT}/5-file-completed-as-relative-path.png`)

  // 6 — a `../` token that leaves the project: its own empty state, because
  // "no matching files" would be false about a directory that has files.
  await composer.fill('')
  await composer.pressSequentially('read ../', { delay: 15 })
  await menu.getByText(/ends up outside the project/i).waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/6-outside-the-project.png` })
  console.log('wrote', `${OUT}/6-outside-the-project.png`)

  // 7 — the same `../` run coming back in: it lists, like any other token.
  await composer.pressSequentially('sprint-42/', { delay: 15 })
  await menu.getByText('src/', { exact: true }).waitFor({ timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/7-parent-run-back-inside.png` })
  console.log('wrote', `${OUT}/7-parent-run-back-inside.png`)

  console.log({ afterTab, afterEnter, asked })
  await browser.close()
  srv.close()

  const problems = []
  if (afterTab !== 'summarize ./notes/') problems.push(`Tab did not complete the directory: ${JSON.stringify(afterTab)}`)
  if (afterEnter !== 'summarize ./notes/weekly-recap.md ') problems.push(`Enter did not insert the relative path: ${JSON.stringify(afterEnter)}`)
  if (!asked.includes('./notes/|')) problems.push('the sub-directory level was never requested')
  if (!asked.includes('../sprint-42/|')) problems.push('the re-entering parent run was never requested')
  if (problems.length) {
    problems.forEach(p => console.error('FAIL:', p))
    process.exit(1)
  }
  console.log('PASS: ./ completion opens, scopes per token, and inserts plain relative paths')
}

main().catch(err => { console.error(err); process.exit(1) })
