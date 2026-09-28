/**
 * Capture + regression harness: the two drag-move surfaces that were still silent.
 *
 * Session drags in the chat sidebar already show the MoveUndoBar; this drives
 * the two surfaces issue #4626 covers — (1) the ARTIFACTS LIBRARY, where a card
 * dragged into a folder (and a folder nested into a folder) completed with no
 * confirmation and no undo, and (2) FOLDER-INTO-FOLDER moves in the chat
 * sidebar. Real built SPA (website/dist), /api/** stubbed, REAL pointer events
 * (dnd-kit's sensors are pointer-based, so there is no synthetic shortcut).
 *
 * It asserts what the screenshots are supposed to evidence, and exits non-zero
 * when any of it stops being true:
 *   1. Before a drag there is no bar.
 *   2. An artifact drop moves the artifact (PATCH observed) AND the bar names
 *      the destination; Undo posts the ORIGINAL folder back and retires it.
 *   3. A folder-nest drop in the library arms the same bar.
 *   4. A folder dragged between folders in the chat sidebar arms the bar, and
 *      Undo restores the previous parent.
 *
 * Folder GETs are STATEFUL (the PATCH mutates the fixture): the move mutation
 * invalidates the folder query, and a refetch frozen on the pre-move list
 * would retire a live offer — hiding exactly what these frames evidence.
 *
 * Usage: node scripts/capture-artifact-move-undo.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/artifact-folder-move-undo'
const VIEW = { width: 1500, height: 950 }
mkdirSync(OUT, { recursive: true })

const ARCHIVE = 'folder-archive'
const LATER = 'folder-later'
const CHILD = 'folder-child'
const SLUG = 'quarterly-report'

const artifact = (slug, name, overrides = {}) => ({
  slug, name, kind: 'markdown', source: 'chat', pinned: false, description: '',
  tags: [], version: 2, folder_id: '',
  created_at: '2026-07-30T10:00:00.000000+00:00',
  updated_at: '2026-08-01T21:00:00.000000+00:00',
  ...overrides,
})

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` — ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

const { srv, base } = await serveDist()
// A mise-managed node exports its own lib/node on LD_LIBRARY_PATH, which the
// browser child inherits and then dies resolving libstdc++; point it at the
// system path (harmless otherwise). Same note as the sibling harnesses.
const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })

/** Real dnd-kit drag from one element's center to another's. Multi-step moves
 *  are required, not cosmetic: the sensor only activates past a distance
 *  constraint, and collision detection reads pointer coordinates per move. */
async function drag(page, fromLocator, toLocator, { toHeaderBand = false } = {}) {
  const from = await fromLocator.boundingBox()
  const to = await toLocator.boundingBox()
  const sx = from.x + from.width / 2
  const sy = from.y + from.height / 2
  const tx = to.x + to.width / 2
  // A sidebar folder's droppable spans its whole block; the nest gesture is
  // resolved in the HEADER BAND, so aim near the top rather than the center.
  const ty = toHeaderBand ? to.y + Math.min(14, to.height / 2) : to.y + to.height / 2
  await page.mouse.move(sx, sy)
  await page.mouse.down()
  await page.mouse.move(sx + 9, sy + 5, { steps: 4 })
  await page.waitForTimeout(150)
  for (let i = 1; i <= 12; i++) {
    await page.mouse.move(sx + ((tx - sx) * i) / 12, sy + ((ty - sy) * i) / 12)
    await page.waitForTimeout(35)
  }
  await page.waitForTimeout(200)
  await page.mouse.up()
}

/** Open the ARTIFACTS LIBRARY with stateful folder fixtures. */
async function openLibrary(theme) {
  const artifacts = [
    artifact(SLUG, 'Quarterly report', { description: 'Q3 numbers and narrative' }),
    artifact('cr-queue', 'CR Queue', { description: 'Hourly snapshot of the review queue', kind: 'widget' }),
  ]
  const libFolders = [
    { id: ARCHIVE, name: 'Archive', parent_id: '', order: 0, item_count: 0 },
    { id: LATER, name: 'Later', parent_id: '', order: 1, item_count: 0 },
  ]
  const patches = []
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme,
    extra: async (path, route) => {
      const method = route.request().method()
      if (path === '/api/artifacts') return json(route, { artifacts }), true
      if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true
      if (path === '/api/artifact-folders' && method === 'GET') return json(route, { folders: libFolders.map(f => ({ ...f })) }), true
      const folderPatch = /^\/api\/artifact-folders\/([^/]+)$/.exec(path)
      if (folderPatch && method === 'PATCH') {
        const body = route.request().postDataJSON()
        patches.push({ kind: 'folder', id: decodeURIComponent(folderPatch[1]), body })
        const f = libFolders.find(x => x.id === decodeURIComponent(folderPatch[1]))
        if (f && body.parent_id !== undefined) f.parent_id = body.parent_id
        return json(route, {}), true
      }
      const move = /^\/api\/artifacts\/([^/]+)\/folder$/.exec(path)
      if (move && method === 'PATCH') {
        const body = route.request().postDataJSON()
        patches.push({ kind: 'artifact', slug: decodeURIComponent(move[1]), body })
        const a = artifacts.find(x => x.slug === decodeURIComponent(move[1]))
        if (a) {
          // Keep item_count truthful: the product invalidates
          // ['artifact-folders'] on settle and the refetched counts move with
          // the artifact — a fixture frozen at 0 would put a "0 artifacts"
          // card right above a bar asserting "Moved to" that folder.
          const from = libFolders.find(f => f.id === a.folder_id)
          const to = libFolders.find(f => f.id === body.folder_id)
          if (from) from.item_count = Math.max(0, (from.item_count || 0) - 1)
          if (to) to.item_count = (to.item_count || 0) + 1
          a.folder_id = body.folder_id
        }
        return json(route, {}), true
      }
      const detail = /^\/api\/artifacts\/([^/]+)$/.exec(path)
      if (detail) {
        const a = artifacts.find(x => x.slug === decodeURIComponent(detail[1]))
        if (a) return json(route, a), true
        // A wrong-slug fetch must FAIL, not silently answer with another
        // artifact — that is exactly the kind of miss this harness exists to catch.
        await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
        return true
      }
      if (path.startsWith('/api/artifacts/')) return json(route, {}), true
      return false
    },
  })
  await page.goto(`${base}/artifacts`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  return { context, page, patches }
}

/** Open the CHAT sidebar with a nested folder to re-parent. */
async function openChat(theme) {
  const iso = min => new Date(Date.now() - min * 60_000).toISOString()
  const slot = (key, title, min, folder = '') => ({
    key, title, running: false, messages: 6, agent: 'kirocrew',
    memory_mode: 'persistent', folder_id: folder, last_ts: iso(min),
    last_turn_ts: iso(min), created: iso(min + 200),
  })
  const chatFolders = [
    { id: ARCHIVE, name: 'Archive', order: 0, collapsed: false },
    { id: LATER, name: 'Later', order: 1, collapsed: false },
    { id: CHILD, name: 'Child', order: 0, parent_id: ARCHIVE, collapsed: false },
  ]
  const patches = []
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots: [
      slot('chat-1', 'Session move undo bar', 3),
      slot('chat-2', 'i18n catalog parity', 26),
    ],
    theme,
    extra: async (path, route) => {
      const method = route.request().method()
      if (path === '/api/chat/folders' && method === 'GET') return json(route, chatFolders.map(f => ({ ...f }))), true
      const m = /^\/api\/chat\/folders\/([^/]+)$/.exec(path)
      if (m && method === 'PATCH') {
        const body = route.request().postDataJSON()
        patches.push({ id: decodeURIComponent(m[1]), body })
        const f = chatFolders.find(x => x.id === decodeURIComponent(m[1]))
        if (f && body.parent_id !== undefined) f.parent_id = body.parent_id
        return json(route, {}), true
      }
      return false
    },
  })
  await page.goto(`${base}/chat`, { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(3000)
  return { context, page, patches }
}

try {
  // ── 1. Artifact card → folder ─────────────────────────────────────────────
  {
    const { context, page, patches } = await openLibrary('light')
    const bar = page.getByTestId('session-move-undo')
    const card = page.getByText('Quarterly report', { exact: true }).first()
    await card.waitFor({ timeout: 15_000 })
    assert('1 before: no undo bar', (await bar.count()) === 0)
    await page.screenshot({ path: `${OUT}/1-library-before.png` })

    await drag(page, card, page.getByText('Archive', { exact: true }).first())
    await bar.waitFor({ timeout: 5_000 })
    const barText = (await bar.innerText()).replace(/\s+/g, ' ').trim()
    assert('2 artifact drop: bar names the destination', /Archive/.test(barText), barText)
    assert('2 artifact drop: move PATCHed', patches.some(p => p.kind === 'artifact' && p.body.folder_id === ARCHIVE))
    await page.screenshot({ path: `${OUT}/2-artifact-drop.png` })

    await page.getByTestId('session-move-undo-button').click()
    await page.waitForTimeout(600)
    assert('3 undo: bar retired', (await bar.count()) === 0)
    assert('3 undo: original folder posted back', patches.some(p => p.kind === 'artifact' && p.body.folder_id === ''))
    await page.screenshot({ path: `${OUT}/3-artifact-undo.png` })

    // ── 2. Folder nested into folder (same library) ─────────────────────────
    await drag(page, page.getByText('Later', { exact: true }).first(), page.getByText('Archive', { exact: true }).first())
    await bar.waitFor({ timeout: 5_000 })
    const nestText = (await bar.innerText()).replace(/\s+/g, ' ').trim()
    assert('4 folder nest: bar names the destination', /Archive/.test(nestText), nestText)
    assert('4 folder nest: re-parent PATCHed', patches.some(p => p.kind === 'folder' && p.id === LATER && p.body.parent_id === ARCHIVE))
    await page.screenshot({ path: `${OUT}/4-folder-nest.png` })
    await context.close()
  }

  // ── 3. Chat sidebar: folder re-parent ─────────────────────────────────────
  {
    const { context, page, patches } = await openChat('light')
    const bar = page.getByTestId('session-move-undo')
    const childRow = page.getByText('Child', { exact: true }).first()
    await childRow.waitFor({ timeout: 15_000 })
    assert('5 before: no undo bar', (await bar.count()) === 0)

    await drag(page, childRow, page.locator(`[data-folder-drop="${LATER}"]`).first(), { toHeaderBand: true })
    await bar.waitFor({ timeout: 5_000 })
    const text = (await bar.innerText()).replace(/\s+/g, ' ').trim()
    assert('5 sidebar folder move: bar names the destination', /Later/.test(text), text)
    assert('5 sidebar folder move: re-parent PATCHed', patches.some(p => p.id === CHILD && p.body.parent_id === LATER))
    await page.screenshot({ path: `${OUT}/5-sidebar-folder-move.png` })

    await page.getByTestId('session-move-undo-button').click()
    await page.waitForTimeout(600)
    assert('6 sidebar undo: bar retired', (await bar.count()) === 0)
    assert('6 sidebar undo: previous parent restored', patches.some(p => p.id === CHILD && p.body.parent_id === ARCHIVE))
    await page.screenshot({ path: `${OUT}/6-sidebar-undo.png` })
    await context.close()
  }

  // ── 4. Dark theme: every surface in the bar is a theme token ──────────────
  {
    const { context, page } = await openLibrary('dark')
    const bar = page.getByTestId('session-move-undo')
    const card = page.getByText('Quarterly report', { exact: true }).first()
    await card.waitFor({ timeout: 15_000 })
    await drag(page, card, page.getByText('Archive', { exact: true }).first())
    await bar.waitFor({ timeout: 5_000 })
    assert('7 dark: bar names the destination', /Archive/.test(await bar.innerText()))
    await page.screenshot({ path: `${OUT}/7-artifact-drop-dark.png` })
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
console.log('captures written to', OUT)
