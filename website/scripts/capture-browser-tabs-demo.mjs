/**
 * Evidence capture for the SIDE PANEL browser-tab strip restyle: renders the
 * real built SPA with a stubbed API, opens the right panel with seeded tabs,
 * and crops the panel region.
 * Usage: npm run build && node scripts/capture-browser-tabs-demo.mjs <outName>
 *   THEME=light      capture the light theme (default dark)
 *   ACTIVE=pinned    make a PINNED view tab (Files) active instead of a
 *                    dynamic document tab -- the corner pieces overlap the
 *                    pinned trio's gap, so that state needs its own frame
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { logPageProblems, stubDashboardApi, json } from './lib/stub-dashboard-api.mjs'

const OUT = '../temp-screenshots/side-panel-browser-tabs'
const NAME = process.argv[2] || 'shot'
// THEME=light node scripts/... captures the light theme; default dark.
const THEME = process.env.THEME === 'light' ? 'light' : 'dark'
const ACTIVE = process.env.ACTIVE === 'pinned' ? 'files' : 'f1'
const VIEWPORT = { width: 1280, height: 760 }
const CLIP = { x: 640, y: 40, width: 640, height: 520 }

const crew = (id, name, sshHost, port, state = 'connected') => ({
  id, name, ssh_host: sshHost, remote_port: 7777, local_port: port, ttl: '20h',
  remote_bin: '', connection_method: 'ssh', ssm_target: '', ssm_run_as: '',
  aws_profile: '', aws_region: '', was_connected: false,
  status: { instance_id: id, state, local_port: port, remote_port: 7777 },
})

const CREWS = [
  crew('oncall', 'oncall', 'oncall-alias', 7801),
  crew('research', 'research', 'research-alias', 7802),
  crew('buildfarm', 'build-farm', 'bf-alias', 7803, 'error'),
]
const SSO = { state: 'ok', seconds_remaining: 72000, expires_at: null, reason: 'valid' }
const SLOTS = [
  {
    key: 'browser-tabs-demo', title: 'Browser tab style demo', running: false,
    last_message: 'Judging the tab restyle.', messages: 2, agent: 'kirocrew',
    memory_mode: 'persistent', folder_id: '', modified: Math.floor(Date.now() / 1000),
    source_links: [], source_links_total: 0,
  },
  {
    key: 'fix-sidebar-dnd', title: 'Fix sidebar drag and drop', running: true,
    last_message: 'Running the Playwright repro…', messages: 14, agent: 'kirocrew',
    memory_mode: 'persistent', folder_id: '', modified: Math.floor(Date.now() / 1000) - 900,
    source_links: [], source_links_total: 0,
  },
  {
    key: 'settings-redesign', title: 'Settings redesign spec', running: false,
    last_message: 'Display panel mockups saved.', messages: 31, agent: 'kirocrew',
    memory_mode: 'persistent', folder_id: '', modified: Math.floor(Date.now() / 1000) - 3600,
    source_links: [], source_links_total: 0,
  },
  {
    key: 'weekly-report', title: 'Weekly report draft', running: false,
    last_message: 'Draft ready for review.', messages: 6, agent: 'kirocrew',
    memory_mode: 'persistent', folder_id: '', modified: Math.floor(Date.now() / 1000) - 7200,
    source_links: [], source_links_total: 0,
  },
]

async function main() {
  const { srv, base } = await serveDist()
  mkdirSync(OUT, { recursive: true })
  const browser = await chromium.launch()
  const extra = async (path, route) => {
    if (path === '/api/instances') {
      await json(route, { active: true, instances: CREWS, warm_set_cap: 5, sso: SSO })
      return true
    }
    const tunnel = /^\/api\/instances\/([^/]+)\/(connect|refresh-token)$/.exec(path)
    if (tunnel) {
      const id = decodeURIComponent(tunnel[1])
      const found = CREWS.find(c => c.id === id)
      await json(route, { ...(found ? found.status : { instance_id: id, state: 'connected' }), token: 'stub-token' })
      return true
    }
    if (path.startsWith('/api/instances/')) { await json(route, { ok: true }); return true }
    return false
  }
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await page.route(/127\.0\.0\.1:78\d\d/, route =>
    route.fulfill({ contentType: 'text/html', body: '<!doctype html><title>pane</title>' }),
  )
  // Seed through the stub's own localStorageEntries so the values land AFTER
  // its localStorage.clear() by construction, not by init-script ordering.
  await stubDashboardApi(page, {
    theme: THEME,
    slots: SLOTS,
    extra,
    localStorageEntries: {
      'mc-crew-switcher-pinned': JSON.stringify(['research', 'buildfarm']),
      'mc-panel-tabs:browser-tabs-demo': JSON.stringify({
        activeId: ACTIVE,
        tabs: [
          { id: 'files', kind: 'files', title: 'Files' },
          { id: 'f1', kind: 'file', title: 'InstanceTabBar.tsx', path: '/repo/website/src/components/InstanceTabBar.tsx' },
          { id: 'f2', kind: 'file', title: 'index.css', path: '/repo/website/src/index.css' },
          { id: 'browser', kind: 'browser', title: 'Browser' },
        ],
      }),
      'mc-activity-open:browser-tabs-demo': 'true',
    },
  })
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' })
  await page.waitForSelector('[aria-label^="Switch instance"]', { timeout: 20000 })
  // The screenshot is about the side panel: wait for its strip AND the selected
  // tab to actually render before cropping, so a broken panel cannot pass as a
  // plausible-looking frame.
  await page.waitForSelector('.side-panel-strip', { timeout: 10000 })
  await page.waitForSelector('[role="tab"][aria-selected="true"]', { timeout: 10000 })
  await page.waitForTimeout(400)
  await page.screenshot({ path: `${OUT}/${NAME}.png`, clip: CLIP })
  console.log(`saved ${OUT}/${NAME}.png`)
  await browser.close()
  srv.close()
}
main().catch(e => { console.error(e); process.exit(1) })
