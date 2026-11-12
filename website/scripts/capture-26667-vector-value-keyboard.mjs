/**
 * Screenshot harness: a semantic memory value in the Vector Memory card opens
 * its inline editor from the keyboard, and Escape hands focus back to it (#26667).
 *
 * Serves the REAL built SPA (website/dist) and answers /api/** from the shared
 * stub map plus the memory endpoints below. Shoots Settings > Overview > Memory in dark
 * and light: the value button focused from the keyboard, the editor opened with
 * Enter, and the value button focused again after Escape.
 *
 * Usage: SKIP_BUILD=1 node scripts/capture-26667-vector-value-keyboard.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/26667-vector-value-keyboard'
mkdirSync(OUT, { recursive: true })

const SEMANTIC = [
  { key: 'user.editor', value_json: '"vim"', confidence: 1.0, source: 'user_explicit' },
  { key: 'project.acme.status', value_json: '"shipping"', confidence: 0.9, source: 'inferred' },
]
const EDIT_NAME = 'Edit value of “user.editor”'

const focused = page => page.evaluate(() => {
  const el = document.activeElement
  if (!el || el === document.body) return 'BODY'
  return (el.getAttribute('aria-label') || el.textContent || el.tagName).trim().slice(0, 60)
})

function assert(label, ok, detail = '') {
  console.log(`${label}: ${ok ? 'OK' : 'FAIL'}${detail ? ` -- ${detail}` : ''}`)
  if (!ok) throw new Error(`${label} failed${detail ? `: ${detail}` : ''}`)
}

async function main() {
  if (!process.env.SKIP_BUILD) execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })

  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: { width: 1300, height: 900 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots: [],
      extra: async (path, route) => {
        if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' }), true
        if (path === '/api/acp-backends') return json(route, { backends: [] }), true
        if (path === '/api/memory/stores') return json(route, { stores: [{ name: 'default', is_default: true, lineage: 'v1', exists: true }] }), true
        if (path === '/api/lessons') return json(route, { lessons: [] }), true
        if (['/api/memory/preferences', '/api/memory/projects', '/api/memory/history'].includes(path)) return json(route, { content: '' }), true
        if (path === '/api/memory/retired') return json(route, { retired: [] }), true
        if (path === '/api/memory/backups') return json(route, { backups: [] }), true
        if (path === '/api/memory/carve') return json(route, { counts: {} }), true
        if (path === '/api/memory/records') return json(route, { entries: [], total: 0, has_more: false }), true
        if (path === '/api/sessions') return json(route, { sessions: [] }), true
        if (path === '/api/memory/settings') return json(route, { history_idle_hours: 3, history_max_days: 90, migrated: true }), true
        if (path === '/api/memory/stats') return json(route, { semantic_active: SEMANTIC.length, episodic_active: 0, embedded_count: 0, migrated: true }), true
        if (path === '/api/memory/embedding-status') return json(route, { provider: 'none', setup_step: 'idle' }), true
        if (path === '/api/memory/semantic') return json(route, { entries: SEMANTIC }), true
        if (path === '/api/memory/episodic') return json(route, { entries: [] }), true
        if (path === '/api/memory/events') return json(route, { events: [] }), true
        if (path.startsWith('/api/chat/slots/')) return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
        return false
      },
      localStorageEntries: {
        'mc-theme-mode': theme,
        'mc-onboarded': '1',
        'mc-lang': 'en',
        'mc-dev-mode': '1',
      },
    })

    await page.goto(`${base}/settings?tab=overview&view=memory`, { waitUntil: 'domcontentloaded' })
    const valueBtn = page.getByRole('button', { name: EDIT_NAME })
    await valueBtn.waitFor({ state: 'attached', timeout: 20000 }).catch(async err => {
      await page.screenshot({ path: `${OUT}/debug-${theme}.png`, fullPage: true })
      throw err
    })
    // Anchored on the key cell, which stays put while the value cell swaps to the editor.
    const table = page.locator('table', { has: page.getByRole('cell', { name: 'project.acme.status', exact: true }) })
    await table.scrollIntoViewIfNeeded()

    // Reach the value from the keyboard: focus the key cell's neighbour, then Tab.
    await page.getByPlaceholder('Filter by key or value…').focus()
    for (let i = 0; i < 12 && (await focused(page)) !== EDIT_NAME; i++) await page.keyboard.press('Tab')
    assert(`${theme}: Tab reaches the value button`, (await focused(page)) === EDIT_NAME, await focused(page))
    await table.screenshot({ path: `${OUT}/after-1-focused-${theme}.png` })

    await page.keyboard.press('Enter')
    const editor = page.getByRole('textbox', { name: EDIT_NAME })
    await editor.waitFor({ state: 'visible' })
    assert(`${theme}: Enter opens the editor`, (await focused(page)) === EDIT_NAME)
    await table.screenshot({ path: `${OUT}/after-2-editing-${theme}.png` })

    await page.keyboard.press('Escape')
    await editor.waitFor({ state: 'detached' })
    await page.waitForTimeout(100)
    assert(`${theme}: Escape returns focus to the value button`, (await focused(page)) === EDIT_NAME, await focused(page))
    await table.screenshot({ path: `${OUT}/after-3-closed-${theme}.png` })
    await context.close()
  }
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
