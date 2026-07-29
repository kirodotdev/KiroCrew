/**
 * Screenshot harness for the folder-collapse affordance in the chat sidebar.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * and answers every /api/** call from fixtures via Playwright route interception
 * (gateway-free — no kiro-cli, no live backend). Seeds three folders so every
 * state of the FolderGlyph toggle is visible in one frame:
 *   - "🚀 Kiro"  expanded  → open-folder glyph (emoji drops, no flat face)
 *   - "🎨 Design" collapsed → closed-folder glyph WITH the custom emoji overlaid
 *   - "Infra"     collapsed → closed-folder glyph, plain (no emoji)
 * The point of the change is the delta (the rotating chevron is gone), so run
 * this against the branch (after) and against origin/main (before).
 *
 * Usage: node scripts/capture-folder-glyph.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/folder-glyph'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

const folders = [
  { id: 'f1', name: 'Kiro', icon: '🚀', order: 0, collapsed: false },
  { id: 'f2', name: 'Design', icon: '🎨', order: 1, collapsed: true },
  { id: 'f3', name: 'Infra', order: 2, collapsed: true },
]

const slot = (key, title, folder_id, last_ts, running = false) => ({
  key, title, messages: 4, running, agent: 'kirocrew',
  created: '2026-07-20T01:00:00Z', last_ts, folder_id,
})

// f1 is expanded, so its children are visible under the open glyph.
const slots = [
  slot('s1', 'Replace collapse chevron', 'f1', '2026-07-29T20:00:00Z'),
  slot('s2', 'Auto-update install flow', 'f1', '2026-07-29T18:30:00Z'),
  slot('s3', 'Tips Kit T1 analyzer', 'f1', '2026-07-29T16:00:00Z'),
  slot('s4', 'StyledSelect retirement', 'f2', '2026-07-28T12:00:00Z'),
  slot('s5', 'App Store revamp', 'f2', '2026-07-28T10:00:00Z'),
  slot('s6', 'Linux CDN links', 'f3', '2026-07-27T09:00:00Z'),
  slot('s7', 'Notification bridge RFC', '', '2026-07-29T21:00:00Z'),
  slot('s8', 'Weixin QR render fix', '', '2026-07-29T14:00:00Z'),
]

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 950 },
    deviceScaleFactor: 2, // 12-13px sidebar type renders soft at 1x on GitHub
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'darwin', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      })
    }
    if (path === '/api/chat/folders') return json(route, folders)
    if (path === '/api/chat/slots') return json(route, slots)
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/status') return json(route, { sessions: slots.length, crons: 0, lessons: 0, uptime: 120, version: '0.5.0' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/recent-projects') return json(route, { dirs: [] })
    if (path === '/api/dashboard/config') return json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' })
    if (path === '/api/agents' || path === '/api/chat/agents') return json(route, [{ name: 'kirocrew', source: 'builtin' }, { name: 'oncall', source: 'aim' }])
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

  await page.addInitScript(() => {
    localStorage.clear()
    localStorage.setItem('mc-theme', 'dark')
    localStorage.setItem('mc-onboarded', '1')
  })

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  // Crop to the session/folder panel (the second column) so the folder rows
  // and their glyphs fill the frame. Derive the box from the folder rows.
  async function shot(name) {
    const f1 = page.locator('[data-testid="folder-collapse-f1"]')
    const box = (await f1.count()) ? await f1.first().boundingBox() : null
    // The panel's left edge sits just left of the glyph; give it a generous
    // fixed width so long session titles are not clipped.
    const x = box ? Math.max(0, box.x - 44) : 470
    await page.screenshot({
      path: `${OUT}/${name}.png`,
      clip: { x, y: 118, width: Math.min(1400 - x, 360), height: 820 },
    })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  await shot(`${PREFIX}-01-folders-mixed-state`)

  // Optional alignment probe: log the left edges of the folder name and the
  // first session's text so the folder-header padding can be tuned to line
  // them up. Enable with MEASURE=1.
  if (process.env.MEASURE) {
    const m = await page.evaluate(() => {
      const glyph = document.querySelector('[data-testid="folder-collapse-f1"]')
      const btn = glyph && glyph.closest('button')
      const name = btn && Array.from(btn.querySelectorAll('span')).find(s => s.textContent.trim() === 'Kiro')
      const sess = document.querySelector('.session-agent-label')
      const left = el => (el ? Math.round(el.getBoundingClientRect().left * 100) / 100 : null)
      return { glyphLeft: left(glyph), nameLeft: left(name), sessionTextLeft: left(sess) }
    })
    console.log('MEASURE', JSON.stringify(m))
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
