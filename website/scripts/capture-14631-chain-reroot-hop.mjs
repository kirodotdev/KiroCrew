/**
 * Screenshot harness for #14631: a chained crew whose parent has no tab.
 *
 * The parent `build-box` was never connected, so it has no switcher entry. Its
 * chained child `gpu-box` is connected and re-rooted to the top level. On main the
 * row names no hop and reads "connected"; with the fix it names `build-box` in its
 * subtitle and reads unreachable through it.
 *
 * Runs against the REAL built SPA (website/dist) with a stubbed instances API, the
 * same approach as `capture-crew-tab-stable-order.mjs`. Nothing in CI runs this
 * file; the logic is unit-tested in `src/test/remoteCrewChaining.test.ts`.
 *
 * Usage: npm run build && node scripts/capture-14631-chain-reroot-hop.mjs [outDir] [stem]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/14631-chain-reroot-hop'
const STEM = process.argv[3] || 'switcher'

const VIEWPORT = { width: 1280, height: 760 }

const crew = (id, name, sshHost, port, extra = {}) => ({
  id,
  name,
  ssh_host: sshHost,
  remote_port: 7777,
  local_port: port,
  ttl: '20h',
  remote_bin: '',
  connection_method: 'ssh',
  ssm_target: '',
  ssm_run_as: '',
  aws_profile: '',
  aws_region: '',
  was_connected: true,
  status: { instance_id: id, state: 'connected', local_port: port, remote_port: 7777 },
  ...extra,
})

const CREWS = [
  crew('devdesk', 'devdesk', 'dev-dsk-alias', 7801),
  // Never connected: no switcher entry.
  crew('build-box', 'build-box', 'build-alias', 0, {
    was_connected: false,
    status: { instance_id: 'build-box', state: 'disconnected' },
  }),
  crew('gpu-box', 'gpu-box', 'gpu-alias', 7803, {
    via_instance_id: 'build-box',
    via_remote_port: 53999,
    via_remote_id: 'gpu-box',
  }),
]

const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const SLOTS = [{
  key: 'chain-reroot-shot',
  title: 'Switching between crews',
  running: false,
  last_message: 'Connected the GPU crew.',
  messages: 2,
  agent: 'kirocrew',
  memory_mode: 'persistent',
  folder_id: '',
  modified: Math.floor(Date.now() / 1000),
  source_links: [],
  source_links_total: 0,
}]
const TRIGGER = '[aria-label^="Switch crew"]'

async function main() {
  const { srv, base } = await serveDist()
  mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()
  const extra = async (path, route) => {
    if (path === '/api/instances') {
      await json(route, { active: true, instances: CREWS, warm_set_cap: 5, sso: SSO })
      return true
    }
    if (path.startsWith('/api/instances/')) {
      await json(route, { ok: true, token: 'stub-token' })
      return true
    }
    return false
  }
  for (const theme of ['dark', 'light']) {
    const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 })
    const page = await context.newPage()
    logPageProblems(page)
    await page.route(/127\.0\.0\.1:78\d\d/, route =>
      route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }),
    )
    await stubDashboardApi(page, { theme, slots: SLOTS, extra })
    await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
    await page.waitForSelector(TRIGGER, { timeout: 20000 })
    await page.locator(`${TRIGGER}:visible`).first().click()
    await page.waitForSelector('[role="menuitemradio"]:has-text("gpu-box")', { timeout: 10000 })
    await page.waitForTimeout(300)
    const box = await page.evaluate(() => {
      const item = [...document.querySelectorAll('[role="menuitemradio"]')].find(e =>
        (e.textContent || '').includes('gpu-box'),
      )
      const menu = item?.closest('[role="menu"]')
      const r = menu?.getBoundingClientRect()
      return r ? { x: r.x, y: r.y, width: r.width, height: r.height } : null
    })
    const row = await page
      .locator('[role="menuitemradio"]:has-text("gpu-box")')
      .first()
      .textContent()
    console.log(`${theme}: gpu-box row text = ${JSON.stringify((row || '').trim())}`)
    await page.screenshot({
      path: `${OUT}/${STEM}-${theme}.png`,
      clip: box
        ? { x: Math.max(0, box.x - 8), y: 0, width: box.width + 16, height: box.y + box.height + 8 }
        : { x: 0, y: 0, width: VIEWPORT.width, height: 300 },
    })
    await context.close()
  }
  await browser.close()
  srv.close()
}

main().catch(err => {
  console.error(err)
  process.exit(1)
})
