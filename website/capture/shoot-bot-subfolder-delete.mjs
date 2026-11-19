/**
 * Screenshot harness for the Delete folder item on an empty folder inside a
 * Bot's home folder. Component render, not the live page.
 *
 * Serves capture/bot-subfolder-delete.tsx with a programmatic Vite dev server on
 * loopback, answers `/api/chat/folders` (and, for the board frames,
 * `/api/chat/tag-columns`) with fixed data, and photographs, per theme:
 *   rest-<theme>     the list view at rest;
 *   after-<theme>    the list view with the empty sub-folder's ⋯ menu open;
 *   confirm-<theme>  the delete confirmation that menu item opens;
 *   board-<theme>    a board column with the same menu open;
 *   research-menu-<theme>  'Research' with its own menu open, for comparison.
 * It also asserts what the frames must show: a menu on the empty sub-folder
 * holding exactly one item, no menu on the Bot's home or on the sub-folder that
 * holds an archived session, a visible `0` count on the empty sub-folder's row
 * (at rest in both views, and with its menu open in the board view), the same
 * count state as 'Research' when each list row has its menu open (the row's
 * floating action cluster stands over the count on every folder, so the
 * research-menu-<theme> frame shows that), and a confirm dialog naming the
 * folder. The run exits non-zero on a mismatch.
 *
 * Usage (from website/): node capture/shoot-bot-subfolder-delete.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'
import { stubDashboardApi, logPageProblems } from '../scripts/lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/bot-subfolder-delete')
mkdirSync(OUT, { recursive: true })

const HOME = 'f-radar'
const EMPTY = 'f-pr-review'
const ARCHIVED = 'f-triage'
const FOLDERS = [
  { id: HOME, name: 'Radar', order: 0, collapsed: false, mate_home: true, owner_app: 'member:member-radar-0123', history_count: 0 },
  { id: EMPTY, name: 'PR review', order: 0, parent_id: HOME, owner_app: 'member:member-radar-0123', history_count: 0 },
  { id: ARCHIVED, name: 'Triage', order: 1, parent_id: HOME, owner_app: 'member:member-radar-0123', history_count: 2 },
  { id: 'f-research', name: 'Research', order: 1, history_count: 0 },
]
const COL = 'col-idle'
const COLUMNS = [{ id: COL, name: '', tag_ids: [], mode: 'any', order: 0, source: 'state', state_key: 'idle' }]

let failed = false
const check = (label, ok, detail) => { console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` -- ${detail}` : ''}`); if (!ok) failed = true }

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({ executablePath: chromiumExecutable(), env: { ...process.env, LD_LIBRARY_PATH: '' } })
const page = await browser.newPage({ viewport: { width: 440, height: 360 }, deviceScaleFactor: 2 })
page.on('pageerror', e => { console.log(`FAIL pageerror -- ${e.message}`); failed = true })

let theme = 'dark'
let wantColumns = false
await stubDashboardApi(page, {
  theme: 'dark',
  folders: FOLDERS,
  preserveStorage: true,
  // The dev server also serves `/src/api/*.ts` under a matching path; only gateway paths are stubbed.
  extra: async (path, route) => {
    if (path === '/api/theme/boot') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ mode: theme, theme: '' }) })
      return true
    }
    if (path === '/api/chat/tag-columns') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(wantColumns ? COLUMNS : []) })
      return true
    }
    if (path.startsWith('/api/')) return false
    await route.continue()
    return true
  },
})
logPageProblems(page)

/**
 * The row's count as a reader sees it: the numeric leaf span's text, and
 * whether the row's action cluster (the element holding the ⋯ trigger) is
 * painted over it. Painted means a box that overlaps the count's and an
 * effective opacity above zero. A hit test cannot answer this: the cluster at
 * rest is opacity 0 yet still hit-testable, and an open Radix menu turns off
 * pointer events on the page. `null` when the row draws no count.
 */
const visibleCount = (rowSelector, triggerSelector) => page.evaluate(([sel, trig]) => {
  const row = document.querySelector(sel)
  if (!row) return null
  const span = [...row.querySelectorAll('span')].find(s => s.children.length === 0 && /^\d+$/.test(s.textContent.trim()))
  if (!span) return null
  const a = span.getBoundingClientRect()
  let cluster = row.querySelector(trig)
  while (cluster && cluster.parentElement !== row) cluster = cluster.parentElement
  let covered = false
  if (cluster && !cluster.contains(span)) {
    const b = cluster.getBoundingClientRect()
    let opacity = 1
    for (let n = cluster; n && n !== document.body; n = n.parentElement) opacity *= Number(getComputedStyle(n).opacity)
    covered = opacity > 0.01 && b.left < a.right && b.right > a.left && b.top < a.bottom && b.bottom > a.top
  }
  return { text: span.textContent.trim(), covered }
}, [rowSelector, triggerSelector])

const checkCount = async (label, rowSelector, triggerSelector) => {
  const c = await visibleCount(rowSelector, triggerSelector)
  check(`${label}: the empty sub-folder's row shows a visible 0`, !!c && c.text === '0' && !c.covered, JSON.stringify(c))
}
// Let the cluster's opacity transition finish before reading it.
const settle = () => page.waitForTimeout(400)

for (const t of ['dark', 'light']) {
  theme = t
  wantColumns = false
  // ── List view ──
  await page.goto(`${base}/capture/bot-subfolder-delete.html?theme=${t}`)
  const trigger = page.locator(`[data-testid="folder-menu-${EMPTY}"]`)
  await trigger.waitFor({ timeout: 20000 })
  check(`${t}: no menu on the Bot's home`, (await page.locator(`[data-testid="folder-menu-${HOME}"]`).count()) === 0)
  check(`${t}: no menu on the sub-folder with archived sessions`, (await page.locator(`[data-testid="folder-menu-${ARCHIVED}"]`).count()) === 0)
  check(`${t}: no new-chat button on the empty sub-folder`, (await page.locator(`[data-testid="folder-new-chat-${EMPTY}"]`).count()) === 0)
  await settle()
  await checkCount(`${t} list at rest`, `[data-folder-row="${EMPTY}"]`, `[data-testid="folder-menu-${EMPTY}"]`)
  const rest = `${OUT}/rest-${t}.png`
  await page.screenshot({ path: rest, animations: 'disabled' })
  console.log('wrote', rest)

  await page.locator(`[data-folder-row="${EMPTY}"]`).hover()
  await trigger.click()
  const menu = page.getByRole('menu')
  await menu.waitFor({ timeout: 5000 })
  check(`${t}: the menu holds one item`, (await menu.getByRole('menuitem').count()) === 1)
  check(`${t}: that item is Delete folder`, (await menu.locator(`[data-testid="folder-delete-${EMPTY}"]`).count()) === 1)
  // With the menu open, the list row's floating action cluster stands where the
  // count is, on every folder row (main's hover cluster). The frame is held to
  // what an ordinary empty folder ('Research') shows in the same state.
  await settle()
  const openBot = await visibleCount(`[data-folder-row="${EMPTY}"]`, `[data-testid="folder-menu-${EMPTY}"]`)
  const path = `${OUT}/after-${t}.png`
  await page.screenshot({ path, animations: 'disabled' })
  console.log('wrote', path)

  // ── The confirmation the Delete folder item opens ──
  await menu.locator(`[data-testid="folder-delete-${EMPTY}"]`).click()
  const dialog = page.getByRole('alertdialog').or(page.getByRole('dialog'))
  await dialog.first().waitFor({ timeout: 5000 })
  check(`${t}: the confirm dialog names PR review`, (await dialog.first().textContent())?.includes('PR review') === true)
  const confirm = `${OUT}/confirm-${t}.png`
  await page.screenshot({ path: confirm, animations: 'disabled' })
  console.log('wrote', confirm)
  await page.keyboard.press('Escape')
  await page.getByRole('alertdialog').or(page.getByRole('dialog')).first().waitFor({ state: 'detached', timeout: 5000 }).catch(() => {})

  // The ordinary empty folder with its own menu open, for the parity check.
  await page.locator('[data-folder-row="f-research"]').hover()
  await page.locator('[data-testid="folder-menu-f-research"]').click()
  await page.getByRole('menu').waitFor({ timeout: 5000 })
  await settle()
  const openPlain = await visibleCount('[data-folder-row="f-research"]', '[data-testid="folder-menu-f-research"]')
  check(`${t} list, menu open: the empty sub-folder's count shows as Research's does`,
    !!openBot && !!openPlain && openBot.text === '0' && openPlain.text === '0' && openBot.covered === openPlain.covered,
    `PR review ${JSON.stringify(openBot)}, Research ${JSON.stringify(openPlain)}`)
  const plainShot = `${OUT}/research-menu-${t}.png`
  await page.screenshot({ path: plainShot, animations: 'disabled' })
  console.log('wrote', plainShot)
  await page.keyboard.press('Escape')

  // ── Board view ──
  wantColumns = true
  await page.goto(`${base}/capture/bot-subfolder-delete.html?theme=${t}&lane=board`)
  const boardRow = `[data-board-folder-row="${COL}:${EMPTY}"]`
  const boardTrigger = page.locator(`[data-testid="col-${COL}-folder-${EMPTY}-menu"]`)
  await boardTrigger.waitFor({ timeout: 20000 })
  check(`${t} board: no menu on the Bot's home`, (await page.locator(`[data-testid="col-${COL}-folder-${HOME}-menu"]`).count()) === 0)
  check(`${t} board: no new-chat button on the empty sub-folder`, (await page.locator(`[data-testid="col-${COL}-folder-${EMPTY}-new-chat"]`).count()) === 0)
  await settle()
  await checkCount(`${t} board at rest`, boardRow, `[data-testid="col-${COL}-folder-${EMPTY}-menu"]`)
  await page.locator(boardRow).hover()
  await boardTrigger.click()
  const boardMenu = page.getByRole('menu')
  await boardMenu.waitFor({ timeout: 5000 })
  check(`${t} board: the menu holds one item`, (await boardMenu.getByRole('menuitem').count()) === 1)
  check(`${t} board: that item is Delete folder`, (await boardMenu.locator(`[data-testid="col-${COL}-folder-${EMPTY}-delete"]`).count()) === 1)
  await settle()
  await checkCount(`${t} board, menu open`, boardRow, `[data-testid="col-${COL}-folder-${EMPTY}-menu"]`)
  const boardShot = `${OUT}/board-${t}.png`
  await page.screenshot({ path: boardShot, animations: 'disabled' })
  console.log('wrote', boardShot)
}

await browser.close()
await server.close()
process.exit(failed ? 1 : 0)
