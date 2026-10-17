/**
 * Capture + regression harness for "new chat in a folder whose project_dir no
 * longer exists" (#8229): the backend refuses the folder scope with HTTP 400
 * "Not a directory", createSlot rolls the session back, and (this PR) the
 * sidebar now surfaces an inline notice under the folder row naming the stale
 * path — instead of only console.error.
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback server with
 * /api/** answered by the shared fixture stub. Only the network is stubbed:
 * the create POST succeeds, the follow-up project-scope POST returns the same
 * 400 body the real handler emits, and the rollback DELETE is accepted.
 *
 * Photographs the flow AND asserts what matters, exiting non-zero on failure:
 *   1. The + button fires the create, project POST is refused, rollback fires.
 *   2. The inline notice renders under the folder row with role="alert",
 *      naming the folder's resolved project path.
 *   3. The ✕ dismisses the notice.
 *
 * Usage: node scripts/capture-folder-create-stale-dir.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-create-stale-project-dir'
const VIEW = { width: 1400, height: 900 }
const FID = 'fwork'
const STALE = '/home/user/projects/renamed-away/webapp'

mkdirSync(OUT, { recursive: true })

const now = Math.floor(Date.now() / 1000)

const folders = [
  { id: FID, name: 'Work', order: 0, project_dir: STALE },
]

const slots = [{
  key: 'chat-w1', title: 'Sprint planning', running: false, last_message: '',
  messages: 3, agent: 'kirocrew', memory_mode: 'persistent', project: '',
  folder_id: FID, modified: now, tags: [], source_links: [], source_links_total: 0,
}]

/** Wire log, so the flow is asserted rather than eyeballed. */
const calls = []

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ args: ['--no-sandbox'] })
  const results = []
  const record = (name, pass, note = '') => {
    results.push({ name, pass, note })
    console.log(`${pass ? 'PASS' : 'FAIL'}  ${name}${note ? ` — ${note}` : ''}`)
  }

  const extra = async (path, route) => {
    const method = route.request().method()
    if (path === '/api/chat/slots' && method === 'POST') {
      calls.push('create')
      await json(route, {
        key: 'chat-new', title: '', running: false, messages: 0,
        agent: 'kirocrew', folder_id: FID, modified: now, tags: [],
      })
      return true
    }
    // The folder carries a project_dir, so createSlot follows up with the
    // scope POST — refuse it exactly the way api_chat_slot_project does when
    // os.path.isdir fails.
    if (path === '/api/chat/slots/chat-new/project' && method === 'POST') {
      calls.push('project')
      await json(route, { error: 'Not a directory' }, 400)
      return true
    }
    // createSlot's rollback: an unscoped session must not be published.
    if (path === '/api/chat/slots/chat-new' && method === 'DELETE') {
      calls.push('rollback')
      await json(route, { ok: true })
      return true
    }
    return false
  }

  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, { folders, slots, extra })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForSelector(`[data-folder-drop="${FID}"]`, { timeout: 12000 })
  await page.waitForTimeout(500)

  const shot = (name) => page.screenshot({ path: `${OUT}/${name}.png` })

  await shot('01-folder-before-create')

  // The + button reveals on hover over the folder header.
  const header = page.locator(`[data-folder-drop="${FID}"]`).first()
  await header.hover()
  const plus = page.getByLabel('New chat in Work').first()
  await plus.click()

  const notice = page.locator(`[data-testid="folder-create-error-${FID}"]`)
  await notice.waitFor({ timeout: 12000 })
  await page.waitForTimeout(300)
  await shot('02-inline-notice-stale-project-dir')

  const text = (await notice.textContent()) ?? ''
  record('create → refused → rolled back', ['create', 'project', 'rollback'].every(c => calls.includes(c)), calls.join(','))
  record('notice names the stale path', text.includes(STALE), text.slice(0, 120))
  record('notice is an alert', (await notice.getAttribute('role')) === 'alert')
  // UX round: the stale-directory case offers a direct Folder settings action.
  record('Folder settings remedy link renders', (await page.locator(`[data-testid="folder-create-error-settings-${FID}"]`).count()) === 1)

  // ErrorNotice's own dismiss affordance (components.errorNotice.dismiss).
  await notice.getByLabel('Dismiss').click()
  await page.waitForTimeout(200)
  record('✕ dismisses the notice', (await notice.count()) === 0)
  await shot('03-notice-dismissed')

  await browser.close()
  srv.close()
  const failed = results.filter(r => !r.pass)
  if (failed.length) {
    console.error(`\n${failed.length} assertion(s) failed`)
    process.exit(1)
  }
  console.log('\nall assertions passed; screenshots in', OUT)
}

main().catch(err => { console.error(err); process.exit(1) })
