/**
 * Screenshot harness for #23928: the crew chat window's composer dock has the
 * local page's shape -- a bare box floating over the transcript, no band, border
 * or padding of its own -- so the air around the composer is the composer's own.
 *
 * Runs the REAL built SPA behind the shared in-process static server with every
 * /api/** answered from fixtures (gateway-free), opens a peer row into the crew
 * window, shoots it, then measures the dock: its height must be the input area's
 * height (nothing of the dock's own around the box), the notices column must
 * take no height while it has nothing to say, and the dock root must paint no
 * background or top border of its own. The screenshot is written BEFORE the
 * assertions, so a failing run still leaves the frame to look at.
 *
 * Usage: [DIST=<dist dir>] node scripts/capture-23928-crew-window-dock.mjs [outDir] [prefix]
 * `DIST` points the server at another build (the pre-fix frame comes from main's).
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/crew-window-dock'
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
  last_turn_ts: new Date(Date.now() - 60_000).toISOString(), running: false, pending_approval: false, agent: 'default',
}
const PEER_SLOT = { ...PEER_ROW, interrupted: false, pending_approval: false, pending_approval_info: null }
const PEER_DETAIL = {
  key: 'chat-9', title: PEER_ROW.title, running: false,
  messages: [
    { role: 'user', content: 'List the deploy user\'s access keys, then rotate the old one.', ts: 't1' },
    { role: 'assistant', content: 'Cycle 7: the PR is down to its last checks. Backend shards 5 to 8 are running, and PR Readiness is waiting on them. None of the 100 checks have failed.', ts: 't2' },
    { role: 'assistant', content: 'Cycle 8: unchanged. Backend shards 5 to 8 are still running, and PR Readiness is waiting on them. Nothing has failed.', ts: 't3' },
    { role: 'assistant', content: 'Cycle 9: still waiting on backend test shards 5 to 8; they have been running for about 10 minutes. Nothing has failed.', ts: 't4' },
  ],
}
const LOCAL_SLOTS = [{
  key: 'chat-local', title: 'Release notes draft', messages: 2, running: false,
  agent: 'kirocrew', created: '2026-09-13T20:00:00Z', last_ts: new Date(Date.now() - 120_000).toISOString(), folder_id: '',
}]

async function main() {
  const { srv, base } = await serveDist(process.env.DIST || undefined)
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  const page = await context.newPage()
  await page.addInitScript(() => {
    // The peer feed stays quiet in the frame; a stub keeps it from erroring.
    window.EventSource = class { addEventListener() {} close() {} }
  })

  await stubDashboardApi(page, {
    folders: [], slots: LOCAL_SLOTS,
    localStorageEntries: { [PREVIEW_INSTANCE_SESSIONS]: '1' },
    extra: async (path, route) => {
      if (path === '/api/instances') { await json(route, { active: true, instances: [ASTRO], warm_set_cap: 5, sso: SSO }); return true }
      if (path === '/api/instances/astro/chat-slots') { await json(route, [PEER_ROW]); return true }
      if (path === '/api/instances/astro/capabilities') { await json(route, { instance_id: 'astro', version: '0.9.0', local_version: '0.9.0', version_match: true, agents: [], workspaces: [] }); return true }
      if (path === '/api/instances/astro/proxy/api/chat/slots') { await json(route, [PEER_SLOT]); return true }
      if (path === '/api/instances/astro/proxy/api/chat/slots/chat-9') { await json(route, PEER_DETAIL); return true }
      // The approval-mode picker's read; unanswered it would draw its inline
      // notice above the box, and the quiet scene is about an EMPTY column.
      if (path === '/api/instances/astro/approval-state') { await json(route, { yolo: false }); return true }
      // The second scene's send: the hub says the peer is unreachable, so the
      // window draws its own notice in the column above the box.
      if (path === '/api/instances/astro/proxy/api/chat' && route.request().method() === 'POST') {
        await route.fulfill({ status: 502, contentType: 'application/json', body: JSON.stringify({ error: 'peer unreachable', code: 'proxy_peer_unreachable' }) }); return true
      }
      // Auto-connect warms the crew from this reply (connectInstanceInto).
      if (path === '/api/instances/astro/connect' || path === '/api/instances/astro/status') {
        await json(route, { ...ASTRO.status, token: 'tok' }); return true
      }
      if (path.startsWith('/api/instances/')) { await json(route, { ok: true }); return true }
      if (path === '/api/chat/slots/chat-local') {
        await json(route, { messages: [{ role: 'user', content: 'draft', ts: '2026-09-13T20:00:00Z', meta: { mid: 'l-1' } }], has_more: false, total: 1 })
        return true
      }
      return false
    },
  })
  logPageProblems(page)

  await page.goto(`${base}/chat?sid=chat-local`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[aria-label="Chat messages"]', { timeout: 20_000 })

  const row = page.locator('[data-session-row="astro:chat-9"]').first()
  await row.waitFor({ state: 'visible', timeout: 15_000 })
  await row.click()
  const win = page.locator('[data-testid="crew-chat-window"]')
  await win.waitFor({ state: 'visible', timeout: 15_000 })
  await win.locator('textarea').waitFor({ state: 'visible', timeout: 15_000 })
  await page.mouse.move(900, 60)
  await page.waitForTimeout(600)

  const shot = `${OUT}/${PREFIX}-1-window.png`
  await page.screenshot({ path: shot })
  console.log(`wrote ${shot}`)

  // The dock is the last positioned box in the window; before the fix it was a
  // Glass band carrying its own border and padding, after it is the bare box
  // the local page uses. Measure whichever is there by the input area it holds.
  const m = await page.evaluate(() => {
    const win = document.querySelector('[data-testid="crew-chat-window"]')
    const input = win.querySelector('.input-area')
    const dock = win.querySelector('[data-testid="crew-window-dock"]') || input.closest('[class*="absolute"]')
    const cs = getComputedStyle(dock)
    const d = dock.getBoundingClientRect(), i = input.getBoundingClientRect()
    const notices = win.querySelector('[data-testid="crew-window-notices"]')
    return {
      dockHeight: Math.round(d.height), inputHeight: Math.round(i.height),
      above: Math.round(i.top - d.top), below: Math.round(d.bottom - i.bottom),
      borderTop: cs.borderTopWidth, background: cs.backgroundColor,
      noticesHeight: notices ? Math.round(notices.getBoundingClientRect().height) : null,
    }
  })
  console.log('dock', JSON.stringify(m))

  // Scene 2: the column is still where a notice lands. A send the hub cannot
  // deliver draws the window's unreachable notice above the box.
  await win.locator('textarea').fill('Rotate it now.')
  await page.keyboard.press('Enter')
  const notice = page.locator('[data-testid="crew-window-unreachable"]')
  await notice.waitFor({ state: 'visible', timeout: 10_000 })
  await page.waitForTimeout(300)
  const shot2 = `${OUT}/${PREFIX}-2-notice.png`
  await page.screenshot({ path: shot2 })
  console.log(`wrote ${shot2}`)
  // In the column, and on a glass pane of its own: the column floats over the
  // scrolling transcript, so a notice with no material behind it would paint
  // straight onto the rows passing under it.
  const inColumn = await page.evaluate(() => {
    const col = document.querySelector('[data-testid="crew-window-notices"]')
    const notice = document.querySelector('[data-testid="crew-window-unreachable"]')
    return !!col && !!notice && col.contains(notice) && col.getBoundingClientRect().height > 0
      && !!notice.closest('[class*="glass-"]') && col.contains(notice.closest('[class*="glass-"]'))
  })
  await browser.close()
  srv.close()

  if (m.above !== 0 || m.below !== 0) throw new Error(`the dock adds air of its own around the input area: ${m.above}px above, ${m.below}px below`)
  if (m.borderTop !== '0px') throw new Error(`the dock draws a top border of its own: ${m.borderTop}`)
  if (!/rgba\(0, 0, 0, 0\)|transparent/.test(m.background)) throw new Error(`the dock paints a band of its own: ${m.background}`)
  if (m.noticesHeight !== 0) throw new Error(`the empty notices column takes height: ${m.noticesHeight}px`)
  if (!inColumn) throw new Error('the unreachable notice did not render on a glass pane in the notices column above the box')
}

main().catch(e => { console.error(e); process.exit(1) })
