/**
 * Screenshot harness: Rename from an Artifacts folder card's ⋯ menu keeps the
 * inline name field open, focused, with the name selected, until Enter, Escape
 * or a click away.
 *
 * Real built SPA behind the shared loopback static server, every /api/** call
 * answered from fixtures (gateway-free). The folder PATCH is STATEFUL, so the
 * refetch after a save shows the new name.
 *
 * MODE=before (the build without the fix) only records what Rename leaves on
 * screen; MODE=after (default) asserts the field holds focus with the name
 * selected, by mouse and by keyboard, that Enter saves and that Escape writes
 * nothing. THEME picks dark (default) or light.
 *
 * Usage: DIST=<dist dir> MODE=after THEME=dark node scripts/capture-26491-folder-menu-rename.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/26491'
const MODE = process.env.MODE || 'after'
const THEME = process.env.THEME || 'dark'
const VIEW = { width: 1440, height: 900 }
mkdirSync(OUT, { recursive: true })

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` — ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

const artifact = (slug, name, overrides = {}) => ({
  slug, name, kind: 'markdown', source: 'chat', pinned: false, description: '',
  tags: [], version: 1, folder_id: '',
  created_at: '2026-10-28T10:00:00.000000+00:00',
  updated_at: '2026-10-30T21:00:00.000000+00:00',
  ...overrides,
})

const { srv, base } = await serveDist(process.env.DIST || undefined)
// A mise-managed node exports its own lib/node on LD_LIBRARY_PATH, which the
// browser child inherits; point it at the system path (same as the siblings).
const browser = await chromium.launch({ env: { ...process.env, LD_LIBRARY_PATH: '/usr/lib64' } })

async function openLibrary() {
  const artifacts = [
    artifact('q3-report', 'Quarterly report', { folder_id: 'f-ops', content: '# Q3' }),
    artifact('release-notes', 'Release notes', { content: '# Notes' }),
  ]
  const folders = [
    { id: 'f-ops', name: 'Ops notes', parent_id: '', order: 0, item_count: 1, color: '#3b82f6' },
    { id: 'f-later', name: 'Later', parent_id: '', order: 1, item_count: 0 },
  ]
  const patches = []
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 1 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme: THEME,
    extra: async (path, route) => {
      const method = route.request().method()
      if (path === '/api/artifacts') return json(route, { artifacts }), true
      if (path === '/api/artifacts/session-docs') return json(route, { docs: [] }), true
      if (path === '/api/artifact-folders' && method === 'GET') return json(route, { folders: folders.map(f => ({ ...f })) }), true
      const patch = /^\/api\/artifact-folders\/([^/]+)$/.exec(path)
      if (patch && method === 'PATCH') {
        const body = route.request().postDataJSON()
        patches.push({ id: decodeURIComponent(patch[1]), body })
        const f = folders.find(x => x.id === decodeURIComponent(patch[1]))
        if (f && body.name) f.name = body.name
        return json(route, {}), true
      }
      const detail = /^\/api\/artifacts\/([^/]+)$/.exec(path)
      if (detail) {
        const a = artifacts.find(x => x.slug === decodeURIComponent(detail[1]))
        if (a) return json(route, a), true
        await route.fulfill({ status: 404, contentType: 'application/json', body: '{}' })
        return true
      }
      if (path.startsWith('/api/artifacts/')) return json(route, {}), true
      return false
    },
  })
  await page.goto(`${base}/artifacts`, { waitUntil: 'domcontentloaded' })
  await page.getByRole('button', { name: 'Open folder Ops notes' }).waitFor({ timeout: 15_000 })
  return { context, page, patches }
}

/** The field's focus and selection, read in the page. */
const fieldState = (page) => page.evaluate(() => {
  const el = document.querySelector('input[aria-label="Rename folder"]')
  return el
    ? { present: true, focused: document.activeElement === el, value: el.value, sel: [el.selectionStart, el.selectionEnd] }
    : { present: false, focused: false }
})

/** The folder grid row, padded, so the card and its field fill the frame. */
async function shootCards(page, name) {
  const card = page.getByRole('button', { name: /^Open folder / }).first()
  const b = await card.boundingBox()
  const clip = { x: Math.max(0, b.x - 24), y: Math.max(0, b.y - 24), width: Math.min(VIEW.width, 2 * b.width + 84), height: b.height + 72 }
  await page.screenshot({ path: `${OUT}/${name}-${THEME}.png`, clip })
}

async function pickRename(page, via) {
  const trigger = page.getByRole('button', { name: 'Actions for folder Ops notes' })
  if (via === 'mouse') {
    await page.getByRole('button', { name: 'Open folder Ops notes' }).hover()
    await trigger.click()
    await page.getByRole('menuitem', { name: /Rename/ }).click()
  } else {
    await trigger.focus()
    await page.keyboard.press('Enter')
    await page.getByRole('menuitem', { name: /Rename/ }).waitFor()
    await page.getByRole('menuitem', { name: /Rename/ }).focus()
    await page.keyboard.press('Enter')
  }
  await page.getByRole('menu').waitFor({ state: 'detached' })
  // Past the menu's own close and its focus hand-back.
  await page.waitForTimeout(400)
}

try {
  if (MODE === 'before') {
    const { context, page, patches } = await openLibrary()
    await pickRename(page, 'mouse')
    const s = await fieldState(page)
    console.log(`before: field ${JSON.stringify(s)}, patches ${patches.length}`)
    await shootCards(page, 'before-rename-click')
    await context.close()
  } else {
    for (const via of ['mouse', 'keyboard']) {
      const { context, page, patches } = await openLibrary()
      await pickRename(page, via)
      const s = await fieldState(page)
      assert(`${via}: field open`, s.present)
      assert(`${via}: field focused`, s.focused)
      assert(`${via}: name selected`, s.value === 'Ops notes' && s.sel[0] === 0 && s.sel[1] === 9, JSON.stringify(s))
      if (via === 'mouse') await shootCards(page, 'after-rename-open')
      await page.keyboard.type('Runbooks')
      if (via === 'mouse') await shootCards(page, 'after-typed')
      await page.keyboard.press('Enter')
      await page.getByRole('button', { name: 'Open folder Runbooks' }).waitFor({ timeout: 5000 })
      assert(`${via}: Enter saved`, patches.length === 1 && patches[0].body.name === 'Runbooks', JSON.stringify(patches))
      if (via === 'mouse') await shootCards(page, 'after-saved')
      await context.close()
    }
    const { context, page, patches } = await openLibrary()
    await pickRename(page, 'mouse')
    await page.keyboard.type('Scratch')
    await page.keyboard.press('Escape')
    await page.getByRole('button', { name: 'Open folder Ops notes' }).waitFor()
    assert('Escape: field closed', !(await fieldState(page)).present)
    assert('Escape: nothing written', patches.length === 0, JSON.stringify(patches))
    await context.close()
  }
} finally {
  await browser.close()
  srv.close()
}
