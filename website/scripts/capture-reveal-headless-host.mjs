/**
 * Screenshot harness: a path chip's "Show in file manager" on a gateway host that
 * has no file manager to open.
 *
 * The browser is direct-local (a loopback peer with no forwarding headers, which
 * is also what an `ssh -L` tunnel looks like), so `/api/dashboard/branding`
 * answers `direct_local: true`. The host itself has no desktop launcher, so
 * `/api/reveal` can only answer the clipboard fallback, `{ ok: true, copy }`,
 * which is what the real handler answers on a headless Linux host.
 *
 * Two chips are right-clicked, and for each the harness screenshots the menu,
 * clicks the file-manager row when the menu offers one, and screenshots the
 * result:
 *  - a folder named in the USER's message, whose bubble wires no in-app opener;
 *  - a folder named in the ASSISTANT's reply, whose bubble opens folders in the
 *    side panel.
 *
 * `SCENARIO` picks what the gateway reports and answers:
 *  - `headless` (default): `desktop_launcher: false`, and a reveal is copied;
 *  - `launch-failed`: `desktop_launcher: true`, but the launcher does not start,
 *    so the reveal is still copied;
 *  - `desktop`: `desktop_launcher: true`, and the reveal opens a file manager.
 *
 * Runs the REAL built SPA behind the shared loopback static server with every
 * /api/** call answered from fixtures (gateway-free). `DIST` selects the build.
 *
 * Usage: DIST=<dist dir> SCENARIO=<scenario> node scripts/capture-reveal-headless-host.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/reveal-headless-host'
const PREFIX = process.argv[3] || 'shot'
const SCENARIO = process.env.SCENARIO || 'headless'
const ACTIVE = 'chat-a'
const USER_DIR = '/home/user/project/drafts'
const REPLY_DIR = '/home/user/project/reports'
const REPLY_FILE = '/home/user/project/reports/summary.md'

mkdirSync(OUT, { recursive: true })
const now = Math.floor(Date.now() / 1000)

const slots = [{
  key: ACTIVE, title: 'Tidy the weekly reports', running: false, messages: 2,
  agent: 'kirocrew', modified: now, last_ts: '2026-10-20T00:10:00Z', folder_id: '',
  last_message: 'Moved the draft.',
}]

const detail = {
  running: false, has_more: false, total: 2, queue: [],
  messages: [
    { role: 'user', content: `Please tidy up \`${USER_DIR}\` before Friday.`, ts: now - 600 },
    {
      role: 'assistant',
      content: `Done. The drafts now live in \`${REPLY_DIR}\`, and \`${REPLY_FILE}\` lists them.`,
      ts: now - 540,
    },
  ],
}

const launcher = SCENARIO !== 'headless'
const reveals = []
const extra = (path, route) => {
  if (path === '/api/kiro-prerequisite') {
    // The gateway in the report is a Linux host, so the reveal row reads
    // "Show in file manager" rather than "Open in Finder".
    return json(route, {
      platform: 'Linux', installed: true, authenticated: true, ready: true,
      initial_setup_complete: true, can_auto_install: false, can_login: false,
      repair_required: false, docs_url: '', setup_allowed: false,
      operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
    }), true
  }
  if (path === '/api/dashboard/branding') {
    return json(route, {
      bot_name: 'Kiro Crew', avatar: '/logo.png', direct_local: true, desktop_launcher: launcher,
    }), true
  }
  if (path.startsWith('/api/chat/slots/')) return json(route, detail), true
  if (path === '/api/file-kinds') {
    const { paths = [] } = JSON.parse(route.request().postData() || '{}')
    const kinds = {}
    for (const p of paths) {
      if (p === USER_DIR || p === REPLY_DIR) kinds[p] = 'dir'
      else if (p === REPLY_FILE) kinds[p] = 'file'
    }
    return json(route, { kinds }), true
  }
  if (path === '/api/reveal') {
    const body = JSON.parse(route.request().postData() || '{}')
    reveals.push(body)
    // What api_reveal_path answers: the clipboard fallback when the host has no
    // launcher or the launcher did not start, and a plain ok when it opened.
    return json(route, SCENARIO === 'desktop' ? { ok: true } : { ok: true, copy: body.path }), true
  }
  return false
}

/** A clip around the chip and its menu, so the shot reads at review size. */
async function clipAround(page, chip) {
  const vp = page.viewportSize()
  const a = await chip.boundingBox()
  const menu = page.locator('[role="menu"]').first()
  const b = (await menu.count()) ? await menu.boundingBox() : null
  const left = Math.max(0, Math.min(a.x, b ? b.x : a.x) - 260)
  const top = Math.max(0, Math.min(a.y, b ? b.y : a.y) - 90)
  const right = Math.min(vp.width, Math.max(a.x + a.width, b ? b.x + b.width : 0) + 60)
  const bottom = Math.min(vp.height, Math.max(a.y + a.height, b ? b.y + b.height : 0) + 50)
  return { x: left, y: top, width: right - left, height: bottom - top }
}

async function menuRows(page) {
  return page.locator('[role="menu"] [role="menuitem"], [role="menu"] [role="alert"]')
    .evaluateAll(els => els.map(e => (e.getAttribute('aria-label') || e.textContent || '').trim()))
}

async function main() {
  const { srv, base } = await serveDist(process.env.DIST || undefined)
  const browser = await chromium.launch()
  const context = await browser.newContext({ viewport: { width: 1400, height: 820 }, deviceScaleFactor: 2 })
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    slots, theme: 'dark', extra,
    localStorageEntries: { 'mc-active-slot': ACTIVE, 'mc-privacy-notice-v1': '1', 'mc-sidebar-pinned': 'true' },
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })

  const report = []
  for (const [label, dir] of [['user-bubble', USER_DIR], ['assistant-reply', REPLY_DIR]]) {
    await page.evaluate(() => navigator.clipboard.writeText('(clipboard before the click)'))
    const chip = page.locator(`code[data-path="${dir}"]`).first()
    await chip.waitFor({ state: 'visible', timeout: 20000 })
    await chip.scrollIntoViewIfNeeded()
    await chip.click({ button: 'right' })
    await page.locator('[role="menu"]').first().waitFor({ state: 'visible', timeout: 10000 })
    await page.waitForTimeout(400)
    const rows = await menuRows(page)
    await page.screenshot({ path: `${OUT}/${PREFIX}-${label}-menu.png`, clip: await clipAround(page, chip) })
    const row = page.getByRole('menuitem', { name: 'Show in file manager' })
    const offered = (await row.count()) > 0
    let after = null
    if (offered) {
      const before = reveals.length
      await row.click()
      await page.waitForTimeout(700)
      after = {
        reveal_calls: reveals.slice(before),
        clipboard: await page.evaluate(() => navigator.clipboard.readText()),
        rows: await menuRows(page),
      }
      await page.screenshot({ path: `${OUT}/${PREFIX}-${label}-after-click.png`, clip: await clipAround(page, chip) })
    }
    report.push({ chip: label, path: dir, menu: rows, file_manager_row: offered, after_click: after })
    await page.keyboard.press('Escape')
    await page.waitForTimeout(300)
  }
  console.log(JSON.stringify({ scenario: SCENARIO, report }, null, 1))
  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
