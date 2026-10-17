/**
 * Screenshot harness for the slash-command autocomplete after hiding blocked
 * commands (issue #2673).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with every /api/** call answered from fixtures via Playwright route
 * interception (gateway-free). The /api/slash-commands stub mirrors the
 * backend payload contract: _SLASH_COMMANDS minus _BLOCKED_SLASH_COMMANDS.
 *
 * Asserts, then shoots:
 *  1. typing "/" opens the menu with real commands and NO blocked entry
 *     (/tangent, /quit, /exit, /q, /chat, /paste, /reply, /editor)
 *  2. typing "/tan" (the issue's repro) matches nothing — no inert /tangent
 *
 * Usage: node scripts/capture-slash-menu-blocked.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/slash-menu-blocked'
const PREFIX = process.argv[3] || 'after'

mkdirSync(OUT, { recursive: true })

// Mirrors GET /api/slash-commands on the fixed backend: the sorted
// _SLASH_COMMANDS set minus _BLOCKED_SLASH_COMMANDS, with descriptions.
const API_COMMANDS = [
  { name: '/agent', description: 'Switch or manage the active agent' },
  { name: '/changelog', description: 'Show the release changelog' },
  { name: '/clear', description: 'Clear conversation history' },
  { name: '/code', description: 'Open code intelligence tools' },
  { name: '/compact', description: 'Compact conversation to free context' },
  { name: '/context', description: 'Manage context files and token usage' },
  { name: '/experiment', description: 'Toggle experimental features' },
  { name: '/goal', description: 'Set a standing goal the agent works toward across turns' },
  { name: '/help', description: 'Show available commands' },
  { name: '/hooks', description: 'View configured context hooks' },
  { name: '/issue', description: 'Report an issue or bug' },
  { name: '/logdump', description: 'Dump session logs to a file' },
  { name: '/mcp', description: 'Show configured MCP servers' },
  { name: '/model', description: 'Show or switch the current model' },
  { name: '/prompts', description: 'List or invoke saved prompts & agent SOPs' },
  { name: '/side', description: 'Open a side conversation panel' },
  { name: '/todos', description: 'Show or manage the task list' },
  { name: '/tools', description: 'Show available tools' },
  { name: '/usage', description: 'Show billing and usage information' },
]

const BLOCKED = ['/tangent', '/quit', '/exit', '/q', '/chat', '/paste', '/reply', '/editor']

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1400, height: 900 },
    deviceScaleFactor: 2, // 12-13px menu type renders soft at 1x on GitHub
  })
  const page = await context.newPage()

  await stubDashboardApi(page, {
    slots: [{
      key: 's1', title: 'Slash menu demo', messages: 0, running: false,
      agent: 'kirocrew', mode: '', created: '2026-08-01T01:00:00Z',
      last_ts: '2026-08-04T20:00:00Z', folder_id: '',
    }],
  })
  await page.route('**/api/slash-commands', route =>
    route.fulfill({ json: API_COMMANDS }))
  logPageProblems(page)

  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2600)

  const composer = page.locator('textarea').first()
  await composer.waitFor({ state: 'visible', timeout: 15000 })

  // ── Frame 1: "/" opens the menu; assert no blocked command renders ──
  await composer.click()
  await composer.fill('/')
  const menu = page.locator('[role="listbox"]')
  await menu.first().waitFor({ state: 'visible', timeout: 10000 })
  await page.waitForTimeout(400) // let the slide-up animation settle

  const rows = (await menu.first().locator('[role="option"]').allInnerTexts())
    .map(s => s.trim().split(/\s/)[0])
  console.log('MENU COMMANDS', JSON.stringify(rows))
  for (const cmd of BLOCKED) {
    if (rows.includes(cmd)) throw new Error(`blocked command ${cmd} rendered in the menu`)
  }
  if (!rows.includes('/compact')) throw new Error('expected /compact in the menu')

  await page.screenshot({ path: `${OUT}/${PREFIX}-01-slash-menu-no-blocked.png` })
  console.log('wrote', `${OUT}/${PREFIX}-01-slash-menu-no-blocked.png`)

  // ── Frame 2: the issue's repro "/tan" — nothing matches, menu closed ──
  await composer.fill('/tan')
  await page.waitForTimeout(600)
  if (await menu.count() > 0 && await menu.first().isVisible()) {
    const leftover = (await menu.first().locator('[role="option"]').allInnerTexts()).join(',')
    throw new Error(`menu still open for /tan with rows: ${leftover}`)
  }
  console.log('/tan matches nothing — no inert /tangent suggestion')

  // Crop around the composer so the empty state is legible.
  const box = await composer.boundingBox()
  await page.screenshot({
    path: `${OUT}/${PREFIX}-02-tan-no-suggestion.png`,
    clip: {
      x: Math.max(0, box.x - 30), y: Math.max(0, box.y - 220),
      width: Math.min(1400 - Math.max(0, box.x - 30), box.width + 60),
      height: Math.min(900 - Math.max(0, box.y - 220), box.height + 260),
    },
  })
  console.log('wrote', `${OUT}/${PREFIX}-02-tan-no-suggestion.png`)

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
