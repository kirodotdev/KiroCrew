/**
 * Screenshot evidence for #13598: focus ring on rename triggers after Enter.
 *
 * Frames (all "after Enter" — focus ring on the control that received focus):
 *   02-slot-rename-enter          ChatSidebar session row after Enter
 *   03-folder-list-rename-enter   ChatSidebar list-view folder header after Enter
 *   04-project-rename-enter       ProjectsPage title trigger after Enter
 *   05-folder-card-rename-enter   ArtifactsPage FolderCard after Enter
 *   06-library-row-rename-enter   LibraryTable FolderRow after Enter
 *   07-board-folder-rename-enter  ChatSidebar board-view folder header after Enter
 *
 * Usage: node capture/shoot-rename-focus-ring-13598.mjs [outDir]
 *
 *
 * NOTE: the SPA uses BrowserRouter (HTML5 pathname routing), so deep-links must
 * be bare pathnames like `/projects`, never `/#/projects`.
 * Frames 04–07 use fresh browser pages so each starts with clean Redux state.
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from '../scripts/lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from '../scripts/lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || '../temp-screenshots/rename-focus-ring-13598'
mkdirSync(OUT, { recursive: true })

const SLOT_KEY = 'rename-focus-slot-1'
const FOLDER_ID = 'folder-rename-focus-1'
const TASK_ID = 'task-rename-focus-1'

const SLOT = {
  key: SLOT_KEY, title: 'Rename Focus Session',
  running: false, tags: [], created: '', last_ts: '',
}
const CHAT_FOLDER = {
  id: FOLDER_ID, name: 'Rename Focus Folder', collapsed: false,
  parent_id: '', color: '', icon: '',
}
const ARTIFACT_FOLDER = {
  id: FOLDER_ID, name: 'Rename Focus Folder',
  order: 0, parent_id: '', color: '', icon: '', item_count: 0,
}
const TASK_RUN = {
  task_id: TASK_ID, name: 'Alpha Project', running: false, status: 'planned',
  tasks: 1, completed: 0, failed: 0, skipped: 0, current_task: 0,
  spec: '', spec_name: 'spec', error: '', tokens_used: 0, replan_count: 0,
  task_details: [], started_at: 0, finished_at: 0, work_dir: '', branch_name: '',
  spec_content: '', lessons_learned: [], commits: 0, original_input: '', source: 'text', groups: [],
}
// Board-view column: include_untagged so any session matches.
const BOARD_COLUMN = {
  id: 'col-board-1', name: 'Active', tag_ids: [], mode: 'any',
  include_untagged: true, order: 0, source: 'tags',
}
// Session inside the board folder so the folder appears in the board column.
const BOARD_SLOT = {
  key: 'board-folder-slot-1', title: 'Board Session',
  running: false, tags: [], created: '', last_ts: '',
  folder_id: FOLDER_ID,
}
// /api/dashboard/config that enables board columns.
const BOARD_DASHBOARD_CONFIG = {
  restore_sessions: false, restore_window_minutes: 30,
  merge_queued_messages: false, widget_density: 'more',
  social_share_enabled: true,
  tagColumnsEnabled: true,
}

const { srv, base } = await serveDist()
const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  headless: true,
  args: ['--no-sandbox'],
})

/** Screenshot helper — takes `page` explicitly so each frame can use its own. */
async function capture(page, name, fn) {
  await fn()
  await page.waitForTimeout(400)
  const p = `${OUT}/${name}.png`
  await page.screenshot({ path: p })
  console.log(`captured ${name}`)
}

// ── Frames 02–03 share a page (chat-sidebar, no routing needed) ────────────
const chatPage = await browser.newPage()
await chatPage.setViewportSize({ width: 1280, height: 800 })
logPageProblems(chatPage)

// ── Frame 02: Session slot rename — focus on the row after Enter ──────────
await stubDashboardApi(chatPage, {
  slots: [SLOT],
  extra: async (path, route) => {
    if (path === '/api/sessions') return json(route, { sessions: [] }), true
    if (/\/api\/slots\/[^/]+\/rename/.test(path)) return json(route, {}), true
  },
})
await chatPage.goto(`${base}`, { waitUntil: 'networkidle' })
await capture(chatPage, '02-slot-rename-enter', async () => {
  await chatPage.waitForSelector('[data-session-title]', { timeout: 5000 }).catch(() => {})
  const titleDiv = await chatPage.$('[data-session-title]')
  if (titleDiv) {
    await titleDiv.dblclick()
    await chatPage.waitForSelector('textarea', { timeout: 2000 }).catch(() => {})
    const ta = await chatPage.$('textarea')
    if (ta) { await ta.fill('Session Renamed'); await ta.press('Enter') }
  }
})

// ── Frame 03: List-view folder rename — focus on folder header after Enter ─
await stubDashboardApi(chatPage, {
  slots: [SLOT],
  folders: [CHAT_FOLDER],
  extra: async (path, route) => {
    if (path === '/api/sessions') return json(route, { sessions: [] }), true
    if (/\/api\/chat\/folders\//.test(path)) return json(route, { ...CHAT_FOLDER }), true
  },
})
await chatPage.goto(`${base}`, { waitUntil: 'networkidle' })
await capture(chatPage, '03-folder-list-rename-enter', async () => {
  await chatPage.waitForSelector(`[data-folder-row="${FOLDER_ID}"]`, { timeout: 5000 }).catch(() => {})
  const folderRow = await chatPage.$(`[data-folder-row="${FOLDER_ID}"]`)
  if (folderRow) {
    const nameSpan = await folderRow.$('[title]')
    if (nameSpan) {
      await nameSpan.dblclick()
      await chatPage.waitForFunction(
        (rowEl) => rowEl.querySelector('input') !== null,
        folderRow,
        { timeout: 2000 },
      ).catch(() => {})
      const input = await folderRow.$('input')
      if (input) { await input.fill('Folder Renamed'); await input.press('Enter') }
    }
  }
})

await chatPage.close()

// ── Frame 04: ProjectsPage title trigger — focused after Enter ────────────
// Fresh page. BrowserRouter: URL is /projects, NOT /#/projects.
const projectsPage = await browser.newPage()
await projectsPage.setViewportSize({ width: 1280, height: 800 })
logPageProblems(projectsPage)
await stubDashboardApi(projectsPage, {
  extra: async (path, route) => {
    if (path === '/api/sessions') return json(route, { sessions: [] }), true
    if (path === '/api/taskrunner') return json(route, { running: false, available: true, runs: [TASK_RUN] }), true
    if (path === '/api/taskrunner/refine') return json(route, { status: 'idle', text: '', error: '', input: '' }), true
    if (path.startsWith('/api/taskrunner/')) return json(route, {}), true
  },
})
await projectsPage.goto(`${base}/projects`, { waitUntil: 'networkidle' })
await capture(projectsPage, '04-project-rename-enter', async () => {
  await projectsPage.waitForTimeout(1500)
  const runBtn = await projectsPage.$('[role="button"][tabindex="0"][aria-label*="Alpha"]')
    ?? await projectsPage.$('[role="button"][tabindex="0"]')
  if (runBtn) await runBtn.click()
  await projectsPage.waitForSelector('[data-testid="project-name-trigger"]', { timeout: 3000 }).catch(() => {
    console.log('note: project-name-trigger not visible')
  })
  const trigger = await projectsPage.$('[data-testid="project-name-trigger"]')
  if (trigger) {
    await trigger.click()
    await projectsPage.waitForSelector('[data-testid="project-rename-input"]', { timeout: 2000 }).catch(() => {})
    const input = await projectsPage.$('[data-testid="project-rename-input"]')
    if (input) { await input.fill('Alpha Renamed'); await input.press('Enter') }
  }
})
await projectsPage.close()

// ── Frame 05: ArtifactsPage FolderCard — focused after Enter ─────────────
const artifactsGridPage = await browser.newPage()
await artifactsGridPage.setViewportSize({ width: 1280, height: 800 })
logPageProblems(artifactsGridPage)
await stubDashboardApi(artifactsGridPage, {
  extra: async (path, route) => {
    if (path === '/api/sessions') return json(route, { sessions: [] }), true
    if (path === '/api/artifact-folders') return json(route, { folders: [ARTIFACT_FOLDER] }), true
    if (path === '/api/artifacts') return json(route, { artifacts: [] }), true
    if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true
    if (path.startsWith('/api/artifact-folders/')) return json(route, ARTIFACT_FOLDER), true
  },
})
await artifactsGridPage.goto(`${base}/artifacts`, { waitUntil: 'networkidle' })
await capture(artifactsGridPage, '05-folder-card-rename-enter', async () => {
  await artifactsGridPage.waitForTimeout(1500)
  // Hover over the folder card to reveal the hidden "..." menu button.
  const card = await artifactsGridPage.$('[role="button"][tabindex="0"]')
  if (card) {
    await card.hover()
    await artifactsGridPage.waitForTimeout(200)
    const menuBtn = await artifactsGridPage.$('[title="Folder actions"]')
    if (menuBtn) {
      await menuBtn.click()
      await artifactsGridPage.waitForSelector('[role="menuitem"]', { timeout: 2000 }).catch(() => {})
      const items = await artifactsGridPage.$$('[role="menuitem"]')
      for (const item of items) {
        const text = await item.textContent()
        if (text && text.includes('Rename')) { await item.click(); break }
      }
      await artifactsGridPage.waitForSelector('input[aria-label="Rename folder"]', { timeout: 2000 }).catch(() => {})
      const input = await artifactsGridPage.$('input[aria-label="Rename folder"]')
      if (input) { await input.fill('Folder Renamed'); await input.press('Enter') }
    }
  }
})
await artifactsGridPage.close()

// ── Frame 06: LibraryTable FolderRow — focused after Enter ───────────────
// table view via localStorage key mc-artifacts-view=table.
const artifactsTablePage = await browser.newPage()
await artifactsTablePage.setViewportSize({ width: 1280, height: 800 })
logPageProblems(artifactsTablePage)
await stubDashboardApi(artifactsTablePage, {
  localStorageEntries: { 'mc-artifacts-view': 'table' },
  extra: async (path, route) => {
    if (path === '/api/sessions') return json(route, { sessions: [] }), true
    if (path === '/api/artifact-folders') return json(route, { folders: [ARTIFACT_FOLDER] }), true
    if (path === '/api/artifacts') return json(route, { artifacts: [] }), true
    if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true
    if (path.startsWith('/api/artifact-folders/')) return json(route, ARTIFACT_FOLDER), true
  },
})
await artifactsTablePage.goto(`${base}/artifacts`, { waitUntil: 'networkidle' })
await capture(artifactsTablePage, '06-library-row-rename-enter', async () => {
  await artifactsTablePage.waitForTimeout(1500)
  // FolderRow is a <tr tabIndex=-1>. Hover to reveal the "..." menu button.
  const folderRow = await artifactsTablePage.$('tr[tabindex="-1"]')
  if (folderRow) {
    await folderRow.hover()
    await artifactsTablePage.waitForTimeout(200)
    const menuBtn = await artifactsTablePage.$('[title="Folder actions"]')
    if (menuBtn) {
      await menuBtn.click()
      await artifactsTablePage.waitForSelector('[role="menuitem"]', { timeout: 2000 }).catch(() => {})
      const items = await artifactsTablePage.$$('[role="menuitem"]')
      for (const item of items) {
        const text = await item.textContent()
        if (text && text.includes('Rename')) { await item.click(); break }
      }
      await artifactsTablePage.waitForSelector('input[aria-label="Rename folder"]', { timeout: 2000 }).catch(() => {})
      const input = await artifactsTablePage.$('input[aria-label="Rename folder"]')
      if (input) { await input.fill('Folder Renamed'); await input.press('Enter') }
    }
  }
})
await artifactsTablePage.close()

// ── Frame 07: ChatSidebar board-view folder rename — focused after Enter ──
// Board view activates when tag columns exist AND tagColumnsEnabled = true.
// The session has folder_id so it appears inside the board folder column.
const boardPage = await browser.newPage()
await boardPage.setViewportSize({ width: 1280, height: 800 })
logPageProblems(boardPage)
await stubDashboardApi(boardPage, {
  slots: [BOARD_SLOT],
  folders: [CHAT_FOLDER],
  extra: async (path, route) => {
    if (path === '/api/sessions') return json(route, { sessions: [] }), true
    if (path === '/api/chat/tag-columns') return json(route, [BOARD_COLUMN]), true
    if (path === '/api/dashboard/config') return json(route, BOARD_DASHBOARD_CONFIG), true
    if (/\/api\/chat\/folders\//.test(path)) return json(route, { ...CHAT_FOLDER }), true
  },
})
await boardPage.goto(`${base}`, { waitUntil: 'networkidle' })
await capture(boardPage, '07-board-folder-rename-enter', async () => {
  // Wait for board view to render (tag columns must load).
  await boardPage.waitForTimeout(2000)
  // Find the board folder name span and double-click it.
  const folderSpan = await boardPage.$('[title="Double-click to rename"]')
    ?? await boardPage.$(`[data-testid="col-${BOARD_COLUMN.id}-folder-${FOLDER_ID}"] span[title]`)
  if (folderSpan) {
    await folderSpan.dblclick()
    // Wait for the inline Input to appear.
    await boardPage.waitForTimeout(500)
    const input = await boardPage.$(`[data-testid="col-${BOARD_COLUMN.id}-folder-${FOLDER_ID}"] input`)
    if (input) { await input.fill('Folder Renamed'); await input.press('Enter') }
    else {
      // Try any visible input that appeared
      const anyInput = await boardPage.$('input[class*="flex-1"]')
      if (anyInput) { await anyInput.fill('Folder Renamed'); await anyInput.press('Enter') }
    }
  }
})
await boardPage.close()

await browser.close()
await srv.close()
console.log(`\nDone. Screenshots in: ${OUT}`)
