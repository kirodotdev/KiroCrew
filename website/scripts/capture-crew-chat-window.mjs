/**
 * Screenshot harness for the crew chat window (a peer-owned session opened from
 * its sidebar row).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with every /api/** answered from fixtures (gateway-free). The peer's
 * replies arrive through the hub proxy route, already redacted, exactly as the
 * hub serves them.
 *
 * Asserts as well as shoots: clicking the crew row must open the window without
 * creating a local slot, the window must show the peer's running turn and its
 * pending approval, and Approve must reach the PEER's approve route with the
 * row id its strict check needs.
 *
 * Usage: node scripts/capture-crew-chat-window.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-chat-window'
const PREFIX = process.argv[3] || 'after'
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
  key: 'chat-9', row_identity: 'astro:chat-9', title: 'Rotate the deploy keys',
  last_turn_ts: new Date(Date.now() - 60_000).toISOString(), running: true, pending_approval: true, agent: 'default',
}
const PEER_SLOT = {
  ...PEER_ROW, interrupted: false, pending_approval: true,
  pending_approval_info: { origin: 'native', request_id: '7', request_mid: 'm-7', tool: 'shell', tool_input: 'aws iam list-access-keys --user-name deploy', tool_purpose: 'List the deploy user\'s access keys so the old one can be rotated' },
}
// What the hub proxy returns: the planted key is already a placeholder.
const PEER_DETAIL = {
  key: 'chat-9', title: PEER_ROW.title, running: true,
  messages: [
    { role: 'user', content: 'List the deploy user\'s access keys, then rotate the old one.', ts: 't1' },
    { role: 'assistant', content: 'The current key is `[REDACTED:aws-access-key]`. I will list the keys first.', ts: 't2' },
  ],
}
const LOCAL_SLOTS = [{
  key: 'chat-local', title: 'Release notes draft', messages: 2, running: false,
  agent: 'kirocrew', created: '2026-09-13T20:00:00Z', last_ts: new Date(Date.now() - 120_000).toISOString(), folder_id: '',
}]

async function main() {
  const { srv, base } = await serveDist()
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.addInitScript(() => {
    // The peer feed stays quiet in the frame; a stub keeps it from erroring.
    window.EventSource = class { addEventListener() {} close() {} }
  })

  let approveBody = null
  let localCreate = false
  await stubDashboardApi(page, {
    folders: [], slots: LOCAL_SLOTS,
    localStorageEntries: { [PREVIEW_INSTANCE_SESSIONS]: '1' },
    extra: async (path, route) => {
      const method = route.request().method()
      if (path === '/api/instances') { await json(route, { active: true, instances: [ASTRO], warm_set_cap: 5, sso: SSO }); return true }
      if (path === '/api/instances/astro/chat-slots') { await json(route, [PEER_ROW]); return true }
      if (path === '/api/instances/astro/capabilities') { await json(route, { instance_id: 'astro', version: '0.9.0', local_version: '0.9.0', version_match: true, agents: [], workspaces: [] }); return true }
      if (path === '/api/instances/astro/proxy/api/chat/slots') { await json(route, [PEER_SLOT]); return true }
      if (path === '/api/instances/astro/proxy/api/chat/slots/chat-9') { await json(route, PEER_DETAIL); return true }
      if (path === '/api/instances/astro/proxy/api/chat/slots/chat-9/approve') {
        approveBody = route.request().postDataJSON(); await json(route, { ok: true }); return true
      }
      // Auto-connect warms the crew from this reply (connectInstanceInto).
      if (path === '/api/instances/astro/connect' || path === '/api/instances/astro/status') {
        await json(route, { ...ASTRO.status, token: 'tok' }); return true
      }
      if (path.startsWith('/api/instances/')) { await json(route, { ok: true }); return true }
      if (path === '/api/chat/slots' && method === 'POST') { localCreate = true; await json(route, { key: 'x' }); return true }
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
  await page.locator('[data-testid="crew-window-approval"]').waitFor({ state: 'visible', timeout: 15_000 })
  await page.mouse.move(900, 60)
  await page.waitForTimeout(400)
  const text = (await win.textContent()) ?? ''
  if (localCreate) throw new Error('opening the crew row created a local slot')
  if (!text.includes('[REDACTED:aws-access-key]')) throw new Error('the window did not render the redacted peer text')
  // A pending approval is the running state's one label; the window's own
  // header, not the local page header, must be what the user sees on top.
  if (await page.locator('[data-testid="crew-window-running"]').isVisible()) throw new Error('two running labels beside a pending approval')
  const top = await page.evaluate(() => {
    const r = document.querySelector('[data-testid="crew-chat-window"]').getBoundingClientRect()
    return document.elementFromPoint(r.left + 40, r.top + 18)?.closest('[data-testid="crew-chat-window"]') != null
  })
  if (!top) throw new Error('the page header covers the crew window header')
  const layout = await page.evaluate(() => {
    const win = document.querySelector('[data-testid="crew-chat-window"]').getBoundingClientRect()
    const box = document.querySelector('[data-testid="crew-chat-window"] textarea').getBoundingClientRect()
    const first = document.querySelector('[data-testid="crew-window-user"]').getBoundingClientRect()
    return { gapToBottom: win.bottom - box.bottom, composerTop: box.top, firstRowTop: first.top }
  })
  console.log('layout', JSON.stringify(layout))
  if (layout.gapToBottom > 40 || layout.firstRowTop > layout.composerTop) throw new Error('the composer is not docked at the bottom')
  await page.screenshot({ path: `${OUT}/${PREFIX}-1-window.png` })

  await page.getByRole('button', { name: 'Approve' }).click()
  await page.waitForTimeout(500)
  console.log('approve body:', JSON.stringify(approveBody))
  if (!approveBody || approveBody.request_mid !== 'm-7' || approveBody.origin !== 'native') {
    throw new Error('Approve did not reach the peer route with the row id')
  }
  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-1-window.png`)
}

main().catch(e => { console.error(e); process.exit(1) })
