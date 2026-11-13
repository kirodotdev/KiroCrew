/**
 * Screenshot harness: closing an edit-in-place field with Enter or Escape
 * hands focus back to the control that opened it, on the five surfaces fixed
 * for #26635 -- the Files tab rename, the artifact title rename, the knowledge
 * item tag editor, the knowledge source rename, and Mochi's Save as Preset.
 *
 * Real built SPA (website/dist or $DIST) behind the shared loopback static
 * server, every /api/** call answered from fixtures (gateway-free). Each scene
 * opens its field, types, presses the key, and reads document.activeElement.
 *
 * MODE=before (a build of main) records where focus lands; MODE=after
 * (default) asserts it lands on the opener. THEME picks dark (default) or
 * light. SCENES narrows the run (comma list of scene names).
 *
 * Usage: DIST=<dist dir> MODE=after THEME=dark node scripts/capture-26635-inline-edit-focus.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/26635'
const MODE = process.env.MODE || 'after'
const THEME = process.env.THEME || 'dark'
const ONLY = process.env.SCENES ? new Set(process.env.SCENES.split(',')) : null
mkdirSync(OUT, { recursive: true })

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` -- ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

/** What holds focus: its aria-label, title, or text; BODY when nothing does. */
const focused = (page) => page.evaluate(() => {
  const el = document.activeElement
  if (!el || el === document.body) return 'BODY'
  return (el.getAttribute('aria-label') || el.getAttribute('title') || el.textContent || el.tagName).trim().slice(0, 60)
})

// ── fixtures ────────────────────────────────────────────────────────────────

const FE_ROOT = '/home/dev/workspace'
const FE_API = '/apps/file-explorer/api'
const FE_TREE = [
  { name: 'notes', path: `${FE_ROOT}/notes`, type: 'dir', children: [] },
  { name: 'README.md', path: `${FE_ROOT}/README.md`, type: 'file', size: 402, mtime: 1761700000 },
]

const ARTIFACT = {
  slug: 'release-checklist', name: 'Release checklist', kind: 'markdown', source: 'chat',
  description: '', tags: ['release'], version: 3, pinned: false,
  created_at: '2026-10-01T10:00:00.000000+00:00', updated_at: '2026-10-08T21:00:00.000000+00:00',
  content: '# Release checklist\n\n- [x] Freeze main\n- [ ] Cut the release branch\n',
}

const KITEM = {
  id: 'k1', title: 'Ledger design notes', item_type: 'design_doc', status: 'active',
  content: 'The ledger keeps every entry.', tags: 'ops,ledger', namespace: 'default', source_id: 's1',
  created_at: '2026-10-01T00:00:00Z', updated_at: '2026-10-08T00:00:00Z',
}
const KSOURCES = [
  { id: 's1', name: 'Work notes', source_type: 'local_file', uri: '/home/dev/notes.md', sync_status: 'synced', item_count: 4, properties: '{}' },
  { id: 's2', name: 'Design docs', source_type: 'local_file', uri: '/home/dev/design.md', sync_status: 'synced', item_count: 9, properties: '{}' },
]

function knowledgeStub(state) {
  return async (path, route) => {
    if (!path.startsWith('/api/knowledge')) return false
    const method = route.request().method()
    const rest = path.slice('/api/knowledge'.length)
    if (method === 'PATCH') { state.writes.push(rest); await json(route, { ok: true }); return true }
    if (rest === '/sources') await json(route, KSOURCES)
    else if (rest === '/config') await json(route, { enabled: true, supported_formats: ['md', 'txt'], folder_picker: false })
    else if (rest === '/namespaces') await json(route, [])
    else if (rest === '/stats') await json(route, { items: 1, entities: 0, relations: 0, sources: 2, embeddings: { enabled: false } })
    else if (rest.startsWith('/items/k1/related')) await json(route, [])
    else if (rest.startsWith('/items/k1')) await json(route, KITEM)
    else if (rest.startsWith('/items')) await json(route, { items: [KITEM], total: 1 })
    else if (rest.startsWith('/source-counts')) await json(route, { counts: { s1: 1 }, total: 1 })
    else await json(route, {})
    return true
  }
}

// ── scenes ──────────────────────────────────────────────────────────────────
// Each scene: open(page, state) drives to the closed-field state after `key`,
// and returns { expected, clip } -- the focus label it should land on and the
// region to photograph.

const SCENES = {
  async tabs(page, state, key) {
    await stubDashboardApi(page, {
      theme: THEME,
      extra: async (path, route) => {
        if (!path.startsWith(FE_API)) return false
        if (path === `${FE_API}/health`) await json(route, { allowedRoots: [FE_ROOT], home: FE_ROOT })
        else if (path === `${FE_API}/tree`) await json(route, { entries: FE_TREE })
        else if (path === `${FE_API}/git-status`) await json(route, { repoRoot: FE_ROOT, branch: 'main', statuses: {} })
        else if (path === `${FE_API}/resolve`) await json(route, { exists: true, type: 'dir' })
        else await json(route, { entries: [] })
        return true
      },
    })
    await page.goto(`${state.base}/file-explorer`)
    await page.waitForSelector('.mc-fe-tree')
    const tab = page.locator('.mc-fe-tab-folder').first()
    // The rename opens on double-click only, so the opener is the tab itself.
    await tab.dblclick()
    const input = page.getByRole('textbox', { name: 'Rename workspace tab' })
    await input.waitFor()
    await page.keyboard.press('ControlOrMeta+a')
    await page.keyboard.type(key === 'Enter' ? 'Workspace' : 'Discarded')
    await page.keyboard.press(key)
    await input.waitFor({ state: 'detached' })
    if ((await tab.textContent()).includes('Workspace')) state.writes.push('Workspace')
    const tabTitle = await tab.getAttribute('title')
    return { expected: tabTitle, clip: await clipAround(page, tab, 360, 56) }
  },

  async artifact(page, state, key) {
    await stubDashboardApi(page, {
      theme: THEME,
      extra: async (path, route) => {
        if (path === '/api/artifact-folders') return json(route, { folders: [] }), true
        const m = /^\/api\/artifacts\/([^/]+)(\/.*)?$/.exec(path)
        if (!m || decodeURIComponent(m[1]) !== ARTIFACT.slug) return false
        const rest = m[2] || ''
        if (rest === '/versions') return json(route, { slug: ARTIFACT.slug, versions: [1, 2, 3] }), true
        if (rest === '/events') return json(route, { slug: ARTIFACT.slug, events: [] }), true
        if (rest === '/comments') return json(route, { comments: [] }), true
        if (rest === '/upstream-status') return json(route, {}), true
        if (rest === '' && route.request().method() !== 'GET') {
          state.writes.push(route.request().postDataJSON())
          state.name = 'Launch checklist'
          return json(route, { ...ARTIFACT, name: state.name }), true
        }
        if (rest === '') return json(route, { ...ARTIFACT, name: state.name || ARTIFACT.name }), true
        return false
      },
    })
    await page.goto(`${state.base}/artifacts/${ARTIFACT.slug}`, { waitUntil: 'domcontentloaded' })
    const title = page.getByRole('button', { name: /^Release checklist/ })
    await title.waitFor({ timeout: 15_000 })
    await page.waitForTimeout(800)
    await title.focus()
    await page.keyboard.press('Enter')
    const input = page.getByRole('textbox', { name: 'Artifact name' })
    await input.waitFor()
    await page.keyboard.press('ControlOrMeta+a')
    await page.keyboard.type(key === 'Enter' ? 'Launch checklist' : 'Discarded')
    await page.keyboard.press(key)
    await input.waitFor({ state: 'detached' })
    await page.waitForTimeout(500)
    return { expected: key === 'Enter' ? 'Launch checklist' : 'Release checklist', clip: await clipAround(page, page.getByRole('button', { name: /checklist$/ }).first(), 520, 90) }
  },

  async tags(page, state, key) {
    await stubDashboardApi(page, { theme: THEME, extra: knowledgeStub(state) })
    await page.goto(`${state.base}/knowledge`, { waitUntil: 'networkidle' })
    await page.getByText('Ledger design notes').first().click()
    const edit = page.getByRole('button', { name: 'edit', exact: true })
    await edit.waitFor({ timeout: 15_000 })
    await page.waitForTimeout(500)
    await edit.focus()
    await page.keyboard.press('Enter')
    const input = page.getByRole('textbox', { name: 'Comma-separated tags' })
    await input.waitFor()
    await page.keyboard.press('ControlOrMeta+a')
    await page.keyboard.type(key === 'Enter' ? 'ops, ledger, design' : 'discarded')
    await page.keyboard.press(key)
    await input.waitFor({ state: 'detached' })
    // Escape must close only the tag field, not the whole detail view.
    await edit.waitFor()
    return { expected: 'edit', clip: await clipAround(page, edit, 340, 70) }
  },

  async sources(page, state, key) {
    await stubDashboardApi(page, { theme: THEME, extra: knowledgeStub(state) })
    await page.goto(`${state.base}/knowledge`, { waitUntil: 'networkidle' })
    await page.waitForTimeout(800)
    await page.getByRole('button', { name: /^sources$/i }).first().click()
    await page.getByText('Design docs').first().waitFor({ timeout: 15_000 })
    const row = page.locator('.group\\/name', { hasText: 'Design docs' })
    const rename = row.getByRole('button', { name: 'Rename source' })
    await rename.focus()
    await page.keyboard.press('Enter')
    const input = page.getByRole('textbox', { name: 'Source name' })
    await input.waitFor()
    await page.keyboard.press('ControlOrMeta+a')
    await page.keyboard.type(key === 'Enter' ? 'Design notes' : 'Discarded')
    await page.keyboard.press(key)
    await input.waitFor({ state: 'detached' })
    await page.waitForTimeout(300)
    const target = page.locator('.group\\/name', { hasText: 'Design docs' })
    return { expected: 'Rename source', clip: await clipAround(page, target, 420, 70), check: () => target.getByRole('button', { name: 'Rename source' }).evaluate((el) => el === document.activeElement) }
  },

  async mochi(page, state, key) {
    await stubDashboardApi(page, {
      theme: THEME,
      extra: async (path, route) => {
        if (!path.startsWith('/api/apps/mochi/')) return false
        const method = route.request().method()
        if (path === '/api/apps/mochi/packs') await json(route, { packs: [] })
        else if (path === '/api/apps/mochi/settings' && method === 'POST') {
          const body = route.request().postDataJSON()
          if (Array.isArray(body.customPresets)) { state.writes.push(body.customPresets); state.customPresets = body.customPresets }
          await json(route, { customPresets: state.customPresets || [] })
        } else if (path === '/api/apps/mochi/settings') await json(route, { customPresets: state.customPresets || [], colorMaps: {} })
        else await json(route, {})
        return true
      },
    })
    await page.goto(`${state.base}/src/apps/mochi/avatar.html`, { waitUntil: 'domcontentloaded' })
    // The color customizer lives in the built-in Mochi pack's detail panel.
    await page.getByText('Mochi', { exact: true }).first().click()
    await page.getByRole('button', { name: /Customize/i }).click()
    const trigger = page.getByRole('button', { name: 'Save as Preset', exact: true })
    await trigger.waitFor({ timeout: 15_000 })
    await page.waitForTimeout(500)
    await trigger.focus()
    await page.keyboard.press('Enter')
    const input = page.getByRole('textbox', { name: 'Preset name' })
    await input.waitFor()
    await page.keyboard.type(key === 'Enter' ? 'Sunset' : 'Discarded')
    await page.keyboard.press(key)
    await input.waitFor({ state: 'detached' })
    await page.waitForTimeout(300)
    const reset = page.getByRole('button', { name: 'Reset', exact: true })
    return { expected: 'Save as Preset', clip: await clipAround(page, reset, 380, 60) }
  },
}

/** A clip around `loc`: `width` wide (0 = the element's own), padded by `pad`. */
async function clipAround(page, loc, width, height) {
  await loc.scrollIntoViewIfNeeded()
  const box = await loc.boundingBox()
  const x = Math.max(0, box.x - 16)
  const y = Math.max(0, box.y - (height - box.height) / 2)
  return { x, y, width: width || box.width + 32, height: Math.max(height, box.height + 16) }
}

const { srv, base } = await serveDist(process.env.DIST || undefined)
const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })
let failed = 0
try {
  for (const [name, scene] of Object.entries(SCENES)) {
    if (ONLY && !ONLY.has(name)) continue
    for (const key of ['Enter', 'Escape']) {
      const state = { base, writes: [] }
      const context = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 1 })
      const page = await context.newPage()
      logPageProblems(page)
      try {
        const { expected, clip, check } = await scene(page, state, key)
        await page.waitForTimeout(300)
        const got = await focused(page)
        const onOpener = check ? await check() : got === expected
        if (MODE === 'before') console.log(`before ${name} ${key}: focus on ${got}`)
        else assert(`${name} ${key}: focus on ${expected}`, onOpener, got)
        if (key === 'Escape') assert(`${name} ${key}: nothing written`, state.writes.length === 0, JSON.stringify(state.writes))
        else assert(`${name} ${key}: saved once`, state.writes.length === 1, JSON.stringify(state.writes))
        await page.mouse.move(1270, 790)
        await page.screenshot({ path: `${OUT}/${MODE}-${name}-${key.toLowerCase()}-${THEME}.png`, clip })
      } catch (err) {
        failed += 1
        console.log(`${name} ${key}: ERROR ${err.message.split('\n')[0]}`)
        await page.screenshot({ path: `${OUT}/error-${MODE}-${name}-${key.toLowerCase()}-${THEME}.png` })
      }
      await context.close()
    }
  }
} finally {
  await browser.close()
  srv.close()
}
if (failed) { console.log(`${failed} scene run(s) failed`); process.exit(1) }
