/**
 * Screenshot harness for the terminal panel pop-out (issue #2004).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server and answers every /api/** call from fixtures via Playwright route
 * interception — gateway-free, no kiro-cli, no dashboard auth, no real data.
 *
 * Unlike the sibling harnesses this one installs its stubs at the CONTEXT
 * level, because the flow under test spans TWO pages: the main dashboard and
 * the popped-out terminal window (`window.open` → /popout/terminal). Page-level
 * routes/init-scripts would leave the popup unstubbed. The context init script
 * also must NOT clear localStorage (the popup adopts the tab list the main
 * window persisted there) — it only seeds theme/onboarding when absent.
 *
 * The terminal WebSocket is answered by a tiny scripted PTY: on connect it
 * pushes a title frame plus a fixture shell transcript, so xterm renders a
 * realistic, entirely synthetic session.
 *
 * Proves, in order: (1) the docked panel's tab strip with the new pop-out
 * control; (2) the popout window hosting the same tabs full-window with a
 * Return control; (3) adding a tab inside the popout; (4) the main window
 * while detached — panel suppressed, sidebar Terminal row still lit; (5) the
 * panel re-docked after Return.
 *
 * Usage: node scripts/capture-terminal-popout.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/terminal-popout'
mkdirSync(OUT, { recursive: true })

/** Per-session fixture transcripts — generic, synthetic content only. */
const TRANSCRIPTS = [
  { title: 'npm run build', lines: [
    '\x1b[1;32m$\x1b[0m npm run build',
    '',
    '\x1b[36m> website@0.0.0 build\x1b[0m',
    '\x1b[36m> vite build\x1b[0m',
    '',
    '\x1b[32m✓\x1b[0m 2841 modules transformed.',
    'dist/index.html                 \x1b[2m0.46 kB\x1b[0m',
    'dist/assets/index-Ck2rq1.css  \x1b[2m101.22 kB\x1b[0m',
    'dist/assets/index-B7xnp3.js \x1b[2m1,204.18 kB\x1b[0m',
    '\x1b[32m✓ built in 8.42s\x1b[0m',
    '\x1b[1;32m$\x1b[0m ',
  ]},
  { title: 'vitest', lines: [
    '\x1b[1;32m$\x1b[0m npx vitest run src/test/terminalPopout.test.ts',
    '',
    ' \x1b[32m✓\x1b[0m src/test/terminalPopout.test.ts \x1b[2m(4 tests)\x1b[0m \x1b[32m9ms\x1b[0m',
    '',
    ' \x1b[2mTest Files\x1b[0m  \x1b[1;32m1 passed\x1b[0m \x1b[2m(1)\x1b[0m',
    ' \x1b[2m     Tests\x1b[0m  \x1b[1;32m4 passed\x1b[0m \x1b[2m(4)\x1b[0m',
    '\x1b[1;32m$\x1b[0m ',
  ]},
  { title: 'htop', lines: [
    '\x1b[1;32m$\x1b[0m echo "Hello from the popped-out terminal"',
    'Hello from the popped-out terminal',
    '\x1b[1;32m$\x1b[0m ',
  ]},
]
let nextTranscript = 0

/** Context-level API stub (popup-safe variant of stub-dashboard-api). */
async function stubContext(context) {
  // Playwright matches the MOST RECENTLY registered WS route first, so the
  // generic swallow goes in before the terminal-specific scripted PTY.
  // Every non-terminal WS (dashboard live feed): swallow, no retry-storm.
  await context.routeWebSocket(/\/api\/ws/, () => {})
  // Scripted PTY: title frame + fixture transcript on connect; swallow input.
  await context.routeWebSocket(/\/api\/ws\/terminal\//, ws => {
    const t = TRANSCRIPTS[nextTranscript++ % TRANSCRIPTS.length]
    ws.send(JSON.stringify({ type: 'title', text: t.title }))
    ws.send(Buffer.from(t.lines.join('\r\n')))
    ws.onMessage(() => {}) // keystrokes/resize: accepted, no echo needed
  })

  await context.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'darwin', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      })
    }
    if (path === '/api/chat/folders') return json(route, [])
    if (path === '/api/chat/slots') {
      // POST = create-slot: MUST return a slot object with a key — an
      // empty-array answer puts a keyless slot in redux and crashes the shell
      // (command-palette recents maps slot.key.startsWith).
      if (route.request().method() === 'POST') {
        return json(route, { key: 'fixture-chat', title: 'New Session…', agent: 'kirocrew' })
      }
      return json(route, [])
    }
    if (path.startsWith('/api/chat/slots/')) return json(route, {})
    if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
    if (path === '/api/status') return json(route, { sessions: 0, crons: 0, lessons: 0, uptime: 120, version: '0.5.0' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro Crew', avatar: '/logo.png' })
    if (path === '/api/recent-projects') return json(route, { dirs: [] })
    if (path === '/api/agents') {
      return json(route, {
        agents: [{ name: 'kirocrew', kiro_agent: 'kirocrew', workspace: 'default', memory_store: 'default' }],
        default_agent: 'kirocrew',
      })
    }
    if (path === '/api/agents/installed') return json(route, [{ name: 'kirocrew' }])
    if (path === '/api/workspaces') return json(route, { workspaces: [{ name: 'default' }] })
    if (path === '/api/chat/agents') return json(route, [{ name: 'kirocrew', source: 'builtin' }])
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    return json(route, objectish ? {} : [])
  })

  // Seed WITHOUT clearing: the popup must see the tab list the main window
  // persisted in this same origin's localStorage.
  await context.addInitScript(() => {
    if (!localStorage.getItem('mc-theme')) localStorage.setItem('mc-theme', 'dark')
    if (!localStorage.getItem('mc-onboarded')) localStorage.setItem('mc-onboarded', '1')
    // Seed the docked panel open with two fixture tabs (the same persisted
    // shape a reload restores) — the nav rail re-renders on background polls,
    // which makes clicking the Terminal row flaky in a harness.
    if (!localStorage.getItem('mc-bottom-terminal')) {
      localStorage.setItem('mc-bottom-terminal', JSON.stringify({
        open: true, height: 320,
        tabs: [{ id: 'fixture-tab-1' }, { id: 'fixture-tab-2' }],
        activeId: 'fixture-tab-1',
      }))
    }
  })
}

const shot = (page, name) => page.screenshot({ path: `${OUT}/${name}.png` })

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2,
  })
  await stubContext(context)

  const page = await context.newPage()
  logPageProblems(page)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })

  // 1) The docked panel restores open with two fixture tabs (seeded above).
  await page.getByRole('tab').first().waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1500) // scripted PTY transcripts render
  await shot(page, '01-dock-panel-two-tabs')

  // 2) Pop out: the button releases the sockets and window.open's the popout.
  page.on('console', msg => console.log(`ALLCONSOLE[${msg.type()}]:`, msg.text().slice(0, 200)))
  page.on('dialog', d => { console.log('DIALOG:', d.message().slice(0, 200)); d.dismiss().catch(() => {}) })
  const popupPromise = context.waitForEvent('page')
  await page.getByRole('button', { name: 'Pop out to window' }).dispatchEvent('click')
  const popout = await popupPromise
  logPageProblems(popout)
  await popout.waitForLoadState('domcontentloaded')
  await popout.setViewportSize({ width: 1000, height: 700 })
  await popout.getByRole('tab').first().waitFor({ state: 'visible', timeout: 15000 })
  await popout.waitForTimeout(1200) // reconnect + transcript replay
  await shot(popout, '02-popout-window')

  // 3) Multi-tab inside the popout.
  await popout.getByRole('button', { name: 'New terminal' }).dispatchEvent('click')
  await popout.waitForTimeout(1200)
  await shot(popout, '03-popout-third-tab')

  // 4) Main window while detached: panel suppressed, sidebar row still lit.
  await page.waitForTimeout(600) // BroadcastChannel announce
  await shot(page, '04-main-window-while-detached')

  // 5) Return: panel re-docks in the main window with all three tabs.
  await popout.getByRole('button', { name: /Return to main window/ }).dispatchEvent('click')
  await page.getByRole('tab').first().waitFor({ state: 'visible', timeout: 15000 })
  await page.waitForTimeout(1500) // stale-prune + reconnect replay
  await shot(page, '05-returned-to-dock')

  await browser.close()
  srv.close()
  console.log(`wrote 5 screenshots to ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
