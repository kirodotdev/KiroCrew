/**
 * Screenshot harness for #42094: while one "Share across chats" approval is in
 * flight, its button shows a spinner and "Sharing…"; the other blocks stay
 * answerable.
 *
 * Serves the REAL built SPA (website/dist) and answers /api/** from the shared
 * stub map. The stub POST never answers, so the shot holds the in-flight state.
 *
 * Usage: node scripts/capture-42094-mcp-approve-busy.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { chromiumExecutable } from './lib/chromium-executable.mjs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/42094-mcp-approve-busy'
const PREFIX = process.argv[3] || 'after'
const NAMES = ['artifactory-mcp', 'builder-mcp', 'grasp-mcp']

const server = name => ({ name, command: name, status: 'ok', source: 'agent', enabled: true, tools: [] })
const refusal = name => ({
  reason: 'changed_needs_reapproval', complete: true, expected_launch: `${name}:shown`,
  commands: [[`/home/me/.toolbox/bin/${name}`]], approved_commands: [[`/home/me/.toolbox/bin/${name}`]],
  envs: [['PATH=/home/me/.toolbox/bin:/usr/bin:/home/linuxbrew/.linuxbrew/bin']],
  approved_envs: [['PATH=/home/me/.toolbox/bin:/usr/bin']],
})
const STATUS = {
  enabled: true, stub: NAMES, stub_count: NAMES.length, running: true, ping_ok: true, supported: true,
  share_refused: Object.fromEntries(NAMES.map(n => [n, 'changed_needs_reapproval'])),
  launch_refused: Object.fromEntries(NAMES.map(n => [n, refusal(n)])),
}
const MANAGED = NAMES.map(name => ({
  name, stub: true, can_stub: true, in_allowlist: true, entry_poolable: false,
  agents: ['kirocrew'], transport: 'stdio', denylisted: false,
}))

mkdirSync(OUT, { recursive: true })

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ executablePath: chromiumExecutable() })

  for (const [theme, lang] of [['dark', 'en'], ['dark', 'zh-CN'], ['light', 'en']]) {
    const context = await browser.newContext({ viewport: { width: 1300, height: 860 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await stubDashboardApi(page, {
      slots: [],
      localStorageEntries: { 'mc-theme-mode': theme, 'mc-onboarded': '1', 'mc-lang': lang, 'mc-developer-mode': '1' },
      extra: async (path, route) => {
        if (path === '/api/theme/boot') return json(route, { mode: theme, theme: '' }), true
        if (path === '/api/mcp' || path === '/api/mcp/probe') return json(route, NAMES.map(server)), true
        if (path === '/api/acp-backends') return json(route, { backends: [] }), true
        if (path === '/api/mcp-gateway/status') return json(route, STATUS), true
        if (path === '/api/mcp-gateway/servers') return json(route, { servers: MANAGED }), true
        // Held open: the approval is still in flight when the shot is taken.
        if (path === '/api/mcp-gateway/servers/stub') return new Promise(() => {}), true
        if (path.startsWith('/api/chat/slots/')) {
          return json(route, { running: false, has_more: false, total: 0, queue: [], messages: [] }), true
        }
        return false
      },
    })
    await page.goto(`${base}/settings/developer/mcp-pool`, { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: /Review and approve|查看并批准/ }).click({ timeout: 15000 })
    const dialog = page.getByRole('dialog')
    await dialog.waitFor({ state: 'visible', timeout: 15000 })
    await dialog.locator('section').first().locator('button').last().click()
    await dialog.locator('section').first().locator('button[disabled] .animate-spin').waitFor({ state: 'visible', timeout: 15000 })
    const out = `${OUT}/${PREFIX}-approving-${lang}-${theme}.png`
    await page.screenshot({ path: out })
    console.log('wrote', out)
    await context.close()
  }
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
