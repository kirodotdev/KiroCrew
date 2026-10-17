/**
 * Screenshot harness for a peer turn being stopped in the crew chat window.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with every /api/** answered from fixtures (gateway-free). The peer's
 * slot row reports a stop in flight: `soft_pending` (a cooperative stop) and
 * then `killing` (a force stop). A third frame has the running peer turn
 * compacting its context with no stop in flight.
 *
 * With an `after` prefix it asserts as well as shoots: the footer under the
 * transcript draws the stop in the danger colour, and the window carries one
 * Stopping line, not one in the footer and a second above the composer. In the
 * compacting frame the footer carries the one Compacting line and the composer
 * shows no compaction hint of its own.
 *
 * Usage: node scripts/capture-crew-window-stop-state.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-window-stop-state'
const PREFIX = process.argv[3] || 'after'
const ASSERT = PREFIX.startsWith('after')
mkdirSync(OUT, { recursive: true })

const PREVIEW_INSTANCE_SESSIONS = 'mc-preview-instance-sessions'
const ASTRO = {
  id: 'astro', name: 'astro', ssh_host: 'astro', remote_port: 5480, local_port: 7778,
  ttl: '20h', remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: true,
  status: { instance_id: 'astro', state: 'connected', local_port: 7778, remote_port: 5480 },
}
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const PEER_ROW = {
  key: 'chat-9', row_identity: 'astro:chat-9', title: 'Rebuild the search index',
  last_turn_ts: new Date(Date.now() - 60_000).toISOString(), running: true, agent: 'default',
}
const PEER_DETAIL = {
  key: 'chat-9', title: PEER_ROW.title, running: true,
  messages: [
    { role: 'user', content: 'Rebuild the search index for the docs site.', ts: 't1' },
    { role: 'assistant', content: 'Reading the index config, then rebuilding each shard.', ts: 't2' },
  ],
}
const LOCAL_SLOTS = [{
  key: 'chat-local', title: 'Release notes draft', messages: 2, running: false,
  agent: 'kirocrew', created: '2026-09-13T20:00:00Z', last_ts: new Date(Date.now() - 120_000).toISOString(), folder_id: '',
}]

async function shoot(browser, base, stopState, label) {
  const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.addInitScript(() => {
    // The peer feed stays quiet in the frame; a stub keeps it from erroring.
    window.EventSource = class { addEventListener() {} close() {} }
  })
  const compacting = stopState === 'compacting'
  const peerSlot = compacting
    ? { ...PEER_ROW, interrupted: false, stopping: false, stop_state: 'idle', compacting: true }
    : { ...PEER_ROW, interrupted: false, stopping: true, stop_state: stopState }
  await stubDashboardApi(page, {
    folders: [], slots: LOCAL_SLOTS,
    localStorageEntries: { [PREVIEW_INSTANCE_SESSIONS]: '1' },
    extra: async (path, route) => {
      if (path === '/api/instances') { await json(route, { active: true, instances: [ASTRO], warm_set_cap: 5, sso: SSO }); return true }
      if (path === '/api/instances/astro/chat-slots') { await json(route, [PEER_ROW]); return true }
      if (path === '/api/instances/astro/capabilities') { await json(route, { instance_id: 'astro', version: '0.9.0', local_version: '0.9.0', version_match: true, agents: [], workspaces: [] }); return true }
      if (path === '/api/instances/astro/proxy/api/chat/slots') { await json(route, [peerSlot]); return true }
      if (path === '/api/instances/astro/proxy/api/chat/slots/chat-9') { await json(route, PEER_DETAIL); return true }
      if (path === '/api/instances/astro/connect' || path === '/api/instances/astro/status') {
        await json(route, { ...ASTRO.status, token: 'tok' }); return true
      }
      if (path === '/api/instances/astro/approval-state') { await json(route, { yolo: false }); return true }
      if (path.startsWith('/api/instances/')) { await json(route, { ok: true }); return true }
      if (path === '/api/chat/slots/chat-local') {
        await json(route, { messages: [{ role: 'user', content: 'draft', ts: '2026-09-13T20:00:00Z', meta: { mid: 'l-1' } }], has_more: false, total: 1 })
        return true
      }
      return false
    },
  })
  logPageProblems(page)
  page.on('pageerror', e => console.log('PAGEERROR', e.message))

  await page.goto(`${base}/chat?sid=chat-local`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[aria-label="Chat messages"]', { timeout: 20_000 })
  const row = page.locator('[data-session-row="astro:chat-9"]').first()
  await row.waitFor({ state: 'visible', timeout: 15_000 })
  await row.click()
  const win = page.locator('[data-testid="crew-chat-window"]')
  await win.waitFor({ state: 'visible', timeout: 15_000 })
  await win.locator('[data-testid="chat-footer"]').waitFor({ state: 'visible', timeout: 15_000 })
  await page.mouse.move(900, 60)
  await page.waitForTimeout(600)

  if (ASSERT && compacting) {
    const compactingLines = await win.getByText('Compacting…', { exact: true }).count()
    if (compactingLines !== 1) throw new Error(`compacting: ${compactingLines} Compacting lines in the window, expected 1`)
    const footerLines = await win.locator('[data-testid="chat-footer"]').getByText('Compacting…', { exact: true }).count()
    if (footerLines !== 1) throw new Error('compacting: the Compacting line is not in the footer')
    const composerHints = await win.locator('[data-testid="compacting-indicator"]').count()
    if (composerHints !== 0) throw new Error(`compacting: the composer shows ${composerHints} compaction hint(s) beside the footer line`)
  } else if (ASSERT) {
    const footerLine = win.locator('[data-testid="chat-footer"] span').first()
    const cls = (await footerLine.getAttribute('class')) ?? ''
    if (!cls.includes('text-danger')) throw new Error(`${stopState}: the footer line is not drawn in the danger colour (${cls})`)
    const stoppingLines = await win.getByText('Stopping…', { exact: true }).count()
    const want = stopState === 'soft_pending' ? 1 : 0
    if (stoppingLines !== want) throw new Error(`${stopState}: ${stoppingLines} Stopping lines in the window, expected ${want}`)
  }
  const file = `${OUT}/${PREFIX}-${label}.png`
  await page.screenshot({ path: file })
  console.log(`wrote ${file}`)
  await context.close()
}

async function main() {
  const { srv, base } = await serveDist()
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  await shoot(browser, base, 'soft_pending', '1-soft-pending')
  await shoot(browser, base, 'killing', '2-killing')
  await shoot(browser, base, 'compacting', '3-compacting')
  await browser.close()
  srv.close()
}

main().catch(e => { console.error(e); process.exit(1) })
