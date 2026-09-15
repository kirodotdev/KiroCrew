/**
 * Screenshot harness for the peer-session adopt error on a Sessions row.
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with every /api/** answered from fixtures (gateway-free).
 *
 * The scenario: one connected crew ("astro") advertises a live session, the
 * user clicks its row, and the adopt POST is refused with
 * `502 {code: "remote_bind_failed"}` whose sentence is a VERSION-PARITY refusal
 * -- the crew is up and answering, it just runs a different major.minor. The row
 * must show that sentence. The frame this harness exists to disprove rendered a
 * fixed "Could not reach astro to open this session." for the same response,
 * which told the user to reconnect a crew that was reachable.
 *
 * Asserts as well as shoots: the error notice must name both versions and must
 * not contain "could not reach". Pass `--expect-stale` to invert the assertion
 * and capture the BEFORE frame from a base-branch dist (`--dist <dir>`).
 *
 * Usage: node scripts/capture-adopt-error-reason.mjs [outDir] [prefix] [--dist <dir>] [--expect-stale]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const positional = process.argv.slice(2).filter((a, i, all) => !a.startsWith('--') && all[i - 1] !== '--dist')
const OUT = positional[0] || '../temp-screenshots/adopt-error-reason'
const PREFIX = positional[1] || 'after'
const distIdx = process.argv.indexOf('--dist')
const DIST = distIdx > -1 ? process.argv[distIdx + 1] : undefined
const EXPECT_STALE = process.argv.includes('--expect-stale')

mkdirSync(OUT, { recursive: true })

// The preview flag that enables the merged remote-sessions list. Mirrors
// `PREVIEW_INSTANCE_SESSIONS` in src/utils/previewFlags.ts.
const PREVIEW_INSTANCE_SESSIONS = 'mc-preview-instance-sessions'

// The backend's own sentence for a parity refusal (remote_relay.ensure_version_parity).
const REASON = 'This crew runs Kiro Crew 0.6.0 but this machine runs 0.7.0. '
  + 'A session only runs on a crew at the same major.minor version — update whichever end is behind.'

// The SSM-transport fields are not optional on the wire: the dashboard reads
// them while deciding which lifecycle actions a row may offer, and omitting
// them crashes the shell rather than degrading.
const ASTRO = {
  id: 'astro', name: 'astro', ssh_host: 'astro', remote_port: 5480, local_port: 7778,
  ttl: '20h', remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: false,
  status: { instance_id: 'astro', state: 'connected', local_port: 7778, remote_port: 5480 },
}
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }

// The peer's live session as the listing route shapes it: `row_identity` is the
// identity the ROUTE stamps on every peer row.
const PEER_ROW = {
  key: 'chat-9', row_identity: 'astro:chat-9', title: 'Draft response to Mudhar',
  last_turn_ts: new Date(Date.now() - 120_000).toISOString(),
  created: new Date(Date.now() - 30 * 86_400_000).toISOString(),
  agent: 'default',
}

// One local session, so the sidebar has an ordinary row above the peer one.
const LOCAL_SLOTS = [{
  key: 'chat-local', title: 'Release notes draft', messages: 2, running: false,
  agent: 'kirocrew', created: '2026-09-13T20:00:00Z', last_ts: new Date(Date.now() - 60_000).toISOString(), folder_id: '',
}]

async function main() {
  const { srv, base } = await serveDist(DIST)
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })
  const context = await browser.newContext({ viewport: { width: 1280, height: 820 }, deviceScaleFactor: 2 })
  const page = await context.newPage()

  let adoptBody = null
  await stubDashboardApi(page, {
    folders: [], slots: LOCAL_SLOTS,
    localStorageEntries: { [PREVIEW_INSTANCE_SESSIONS]: '1' },
    extra: async (path, route) => {
      if (path === '/api/instances') {
        await json(route, { active: true, instances: [ASTRO], warm_set_cap: 5, sso: SSO }); return true
      }
      if (path === '/api/instances/astro/chat-slots') { await json(route, [PEER_ROW]); return true }
      if (path.startsWith('/api/instances/')) { await json(route, { ok: true }); return true }
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        adoptBody = route.request().postDataJSON()
        // What chat_handlers returns when resolve_adopt_target raises RemoteTurnError.
        await json(route, { error: REASON, code: 'remote_bind_failed' }, 502); return true
      }
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

  const notice = row.locator('[data-testid="session-peer-adopt-error"]')
  await notice.waitFor({ state: 'visible', timeout: 15_000 })
  // Off the row, or the hover toolbar covers the title in the frame.
  await page.mouse.move(900, 400)
  await page.waitForTimeout(500)

  const shown = (await notice.textContent()) ?? ''
  const tooltip = await notice.locator('[title]').first().getAttribute('title').catch(() => null)
  console.log('adopt POST body:', JSON.stringify(adoptBody))
  console.log('row shows:', shown)
  console.log('row tooltip:', tooltip)
  if (!adoptBody || adoptBody.instance_id !== 'astro' || adoptBody.adopt_remote_slot !== 'chat-9') {
    throw new Error('the click did not send an adopt for astro:chat-9')
  }
  const staleCopy = /could not reach/i.test(shown)
  const namesVersions = shown.includes('0.6.0') && shown.includes('0.7.0')
  if (EXPECT_STALE) {
    if (!staleCopy) throw new Error('expected the BEFORE frame to show the fixed "could not reach" copy')
  } else {
    if (staleCopy) throw new Error('row still shows the fixed "could not reach" copy instead of the crew\'s reason')
    if (!namesVersions) throw new Error('row does not show the version-parity sentence the backend sent')
    // The row clips the sentence to one line; the clipped half must ride a tooltip.
    if (tooltip !== REASON) throw new Error(`expected the full reason as the notice tooltip, got ${JSON.stringify(tooltip)}`)
  }

  await page.screenshot({ path: `${OUT}/${PREFIX}-1-page.png` })
  const sidebar = page.locator('[data-session-row]').first().locator('xpath=ancestor::nav[1] | ancestor::aside[1]').first()
  const target = (await sidebar.count()) ? sidebar : row
  await target.screenshot({ path: `${OUT}/${PREFIX}-2-sidebar.png` })

  await browser.close()
  srv.close()
  console.log(`wrote ${OUT}/${PREFIX}-{1-page,2-sidebar}.png`)
}

main().catch(err => { console.error(err); process.exit(1) })
