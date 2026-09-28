/**
 * Screenshot evidence — the project directory picker can cross Windows drives.
 *
 * Runs the REAL built SPA (website/dist) with every /api/** call answered from
 * fixtures (repo's standard evidence harness, same boot as
 * capture-folder-modal.mjs). `/api/browse-dirs` answers what a Windows gateway
 * answers: a drive root with `parent: ""`, and the mounted drives behind
 * `?drives=1`.
 *
 * Frames (New folder -> Browse), each the full viewport: the picker portals
 * ABOVE the folder modal, so a modal-element shot crops its header away (UX
 * review on #11424 could not see the Back arrow it was asked to judge).
 *   1-drive-root-back  — at C:\ the Back control is present (it used to vanish)
 *   2-drive-list       — Back lists C:\ / D:\ / E:\, path input empty, Select disabled
 *   3-other-drive      — D:\ chosen from the list, its folders listed
 *   4-drives-failed    — `?drives=1` fails: the ErrorNotice, rows still usable
 *   5-folder-failed    — a folder listing fails (403): its notice, long path wrapped
 *   6-typed-path-failed — from the drive list, a typed `Z:\` fails (404): the no-path notice
 *   7-chat-page-handoff — ChatPage's project chooser (`errorHandoff`): the same failure
 *                         with the “Ask the agent” hand-off in the notice
 *
 * Usage: node scripts/capture-windows-drives-evidence.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/windows-drives-picker'
mkdirSync(OUT, { recursive: true })

const DRIVES = { path: '', parent: '', dirs: ['C:\\', 'D:\\', 'E:\\'].map(d => ({ name: d, path: d })) }
let drivesFail = false
const TREE = {
  'C:\\': { path: 'C:\\', parent: '', dirs: ['Program Files', 'Users', 'Windows'].map(n => ({ name: n, path: `C:\\${n}` })) },
  'D:\\': { path: 'D:\\', parent: '', dirs: ['backups', 'games', 'work'].map(n => ({ name: n, path: `D:\\${n}` })) },
  'D:\\work': { path: 'D:\\work', parent: 'D:\\', dirs: ['kirocrew', 'notes'].map(n => ({ name: n, path: `D:\\work\\${n}` })) },
}

const folders = [
  { id: 'f1', name: 'Kiro', icon: '🚀', order: 0, collapsed: false, project_dir: 'C:\\Users\\me\\KiroCrew' },
]
const slots = [
  { key: 's1', title: 'Windows drives', messages: 4, running: false, agent: 'kirocrew', created: '2026-07-20T01:00:00Z', last_ts: '2026-08-01T20:00:00Z', folder_id: 'f1' },
]
const agentsRoster = { agents: [{ name: 'kirocrew', source: 'builtin' }], default_agent: 'kirocrew' }

const { srv, base: BASE } = await serveDist()
const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1400, height: 1000 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()
await stubDashboardApi(page, {
  folders, slots,
  extra: async (path, route) => {
    if (path === '/api/agents' || path === '/api/chat/agents') { await json(route, agentsRoster); return true }
    if (path !== '/api/browse-dirs') return false
    const u = new URL(route.request().url())
    if (u.searchParams.get('drives') === '1') {
      if (drivesFail) { await json(route, { error: 'Access denied' }, 503); return true }
      await json(route, DRIVES); return true
    }
    const p = u.searchParams.get('path') || 'C:\\'
    if (p.startsWith('D:\\work\\kirocrew')) { await json(route, { error: 'Access denied' }, 403); return true }
    if (/^Z:/i.test(p)) { await json(route, { error: 'Not a directory', path: p }, 400); return true }
    await json(route, TREE[p] ?? { path: p, parent: 'C:\\', dirs: [] })
    return true
  },
})
logPageProblems(page)

await page.goto(BASE + '/chat', { waitUntil: 'domcontentloaded' })
await page.waitForTimeout(2600)

// New folder -> Browse
await page.click('[aria-label="More create options"]')
await page.click('text=New folder')
await page.getByTestId('folder-config-browse').click()
const combo = page.getByRole('combobox', { name: 'Project directory path' })
await combo.waitFor()
await page.waitForTimeout(500)

const shot = async (name) => {
  await page.screenshot({ path: `${OUT}/${name}.png` })
  console.log(`wrote ${OUT}/${name}.png  input=${JSON.stringify(await combo.inputValue())}`)
}

await shot('1-drive-root-back')
await page.getByTitle('All drives', { exact: true }).click()
await page.getByRole('option', { name: /D:\\/ }).waitFor()
await page.waitForTimeout(300)
await shot('2-drive-list')
await page.getByRole('option', { name: /D:\\/ }).click()
await page.getByRole('option', { name: /work/ }).waitFor()
await page.waitForTimeout(300)
await shot('3-other-drive')

// Back to the drive list, then Back again from another drive root with the
// listing failing: the notice appears and the D:\ rows stay usable.
drivesFail = true
await page.getByTitle('All drives', { exact: true }).click()
await page.getByTestId('pp-drives-error').waitFor()
await page.waitForTimeout(300)
await shot('4-drives-failed')

// A folder listing that fails: drill to D:\work, then into a folder the stub
// refuses. The notice names the listing still on screen.
drivesFail = false
await page.getByRole('option', { name: /work/ }).click()
await page.getByRole('option', { name: /kirocrew/ }).waitFor()
await page.getByRole('option', { name: /kirocrew/ }).click()
await page.getByTestId('pp-listing-error').waitFor()
await page.waitForTimeout(300)
await shot('5-folder-failed')

// From the drive list (no path on screen), type a drive that does not exist:
// the auto-drill fails and the no-path notice appears.
await page.getByRole('button', { name: 'Back' }).filter({ hasText: 'Back' }).click()  // the picker's own Back (the app shell has a disabled one too) -> D:\
await page.getByTitle('All drives', { exact: true }).click()          // -> drive list
await page.getByRole('option', { name: /E:\\/ }).waitFor()
await combo.fill('Z:\\')
await page.getByTestId('pp-listing-error').waitFor()
await page.waitForTimeout(300)
await shot('6-typed-path-failed')

// ChatPage's own project chooser opts into the agent hand-off. Close the
// folder dialog, open the picker from the input bar's project chip, and make
// a listing fail there.
await page.keyboard.press('Escape')                      // close the picker popover
await page.keyboard.press('Escape')                      // close the folder dialog
await page.getByRole('dialog').waitFor({ state: 'detached' }).catch(() => {})
await page.getByRole('button', { name: /^(Select project|Project:)/ }).first().click()
const chatCombo = page.getByRole('combobox', { name: 'Project directory path' })
await chatCombo.waitFor()
await page.getByRole('option', { name: /Users/ }).waitFor()
drivesFail = true
await page.getByTitle('All drives', { exact: true }).click()
await page.getByRole('button', { name: /Ask the agent/i }).waitFor()
await page.waitForTimeout(300)
await page.screenshot({ path: `${OUT}/7-chat-page-handoff.png` })
console.log(`wrote ${OUT}/7-chat-page-handoff.png  input=${JSON.stringify(await chatCombo.inputValue())}`)

await ctx.close()
await browser.close()
srv.close()
