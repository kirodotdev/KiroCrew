/**
 * Evidence capture for the Group A settings-redirect migration (prose that
 * names a Settings tab → <SettingsLink>). Runs the SAME scenes against any
 * built SPA so a BEFORE dist and an AFTER dist produce pairwise-comparable
 * files with identical names.
 *
 * Every /api/* request is answered by a fixture (no gateway). Each scene
 * asserts a distinctive fragment of the sentence is visible BEFORE shooting,
 * then crops to the containing card / modal / panel with padding. A scene that
 * cannot be reached prints `SKIPPED <id>: <reason>` — it never fakes a frame.
 *
 * Usage: node scripts/capture-settings-redirects-a.mjs <distDir> <outDir>
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { serveDist } from './lib/serve-dist.mjs'

const [distDir, outArg] = process.argv.slice(2)
if (!distDir || !outArg) {
  console.error('usage: node scripts/capture-settings-redirects-a.mjs <distDir> <outDir>')
  process.exit(2)
}
const outDir = resolve(outArg)
mkdirSync(outDir, { recursive: true })

const PAD = 16
const VERSION = '0.4.0'

const STATUS = {
  uptime: '2h', start_time: 0, sessions: 1, messages: 3, cron_jobs: 0,
  lessons: 0, subagents: 0, no_crons: false, branch: '', commit: '',
  release_channel: 'stable', version: VERSION, version_display: VERSION,
  update_available: false, update_can_apply: false,
  update_check_status: 'succeeded', update_command: 'kirocrew update',
  update_latest_version: VERSION, update_channel: 'stable',
  update_managed_by: 'kirocrew', update_commits_ahead: 0,
  update_commits_behind: 0, update_can_arm: false,
}
const UPDATE_CHECK_NONE = {
  check_status: 'succeeded', update_available: false, error_code: null,
  latest_version: VERSION, channel: 'stable', managed_by: 'kirocrew',
  can_apply: false, update_command: 'kirocrew update', current_version: VERSION,
  commits_ahead: 0, commits_behind: 0,
}
const INSTANCES_NONE = { active: false, instances: [], warm_set_cap: 0, sso: {} }
const STT_CONFIG_OK = { enabled: true, provider: 'local', available: true, streaming: false, dictation_panel: true, providers: ['local'], language_code: 'en-US' }
const STT_STATUS_OK = { available: true, code: 'ok', detail: '', models: [] }

const { srv, base } = await serveDist(distDir)
const browser = await chromium.launch()

/**
 * Open a page whose /api/* traffic is fully fixtured. `override(u, route, json)`
 * may answer a request itself (return true) before the shared defaults apply.
 */
async function openPage({ override, init } = {}) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 1 })
  await ctx.addInitScript(() => {
    localStorage.setItem('mc-onboarded', '1')
    localStorage.setItem('mc-import-onboarded', '1')
    localStorage.setItem('mc-privacy-acked', '1')
  })
  if (init) await ctx.addInitScript(init)
  const page = await ctx.newPage()
  current = { ctx, page }
  await page.route('**/*', route => {
    const u = new URL(route.request().url())
    if (!u.pathname.startsWith('/api/')) return route.continue()
    const json = (body, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
    if (override && override(u, route, json) === true) return
    if (u.pathname === '/api/status') return json(STATUS)
    if (u.pathname.startsWith('/api/update/check')) return json(UPDATE_CHECK_NONE)
    if (u.pathname.startsWith('/api/changelog')) return json({ content: '' })
    if (u.pathname.startsWith('/api/models')) return json({ models: [] })
    if (u.pathname === '/api/instances') return json(INSTANCES_NONE)
    if (u.pathname === '/api/config/stt') return json(STT_CONFIG_OK)
    if (u.pathname === '/api/stt/status') return json(STT_STATUS_OK)
    if (u.pathname.startsWith('/api/kiro-prerequisite')) return json({ ready: true, initial_setup_complete: true, setup_allowed: true })
    // List-shaped endpoints crash the app when handed `{}` (e.g.
    // pendingApprovals.filter): answer every array consumer with [].
    if (u.pathname === '/api/chat/slots') return json({ slots: [] })
    if (/approvals|sessions|crons|lessons|skills|notifications|artifacts|apps\b|chat\/(folders|tags|tag-columns)|agents$|themes$/.test(u.pathname)) return json([])
    return json({})
  })
  return { ctx, page }
}

/** Assert `fragment` is on screen, then shoot `container` (+padding) to `file`. */
async function shoot(page, { file, fragment, container }) {
  const text = page.getByText(fragment, { exact: false }).first()
  await text.waitFor({ state: 'visible', timeout: 15_000 })
  await text.scrollIntoViewIfNeeded()
  await page.waitForTimeout(400)
  const box = await container.boundingBox()
  if (!box) throw new Error('container has no bounding box')
  const vp = page.viewportSize()
  const x = Math.max(0, box.x - PAD)
  const y = Math.max(0, box.y - PAD)
  const clip = {
    x, y,
    width: Math.min(vp.width - x, box.width + 2 * PAD),
    height: Math.min(vp.height - y, box.height + 2 * PAD),
  }
  const path = `${outDir}/${file}`
  await page.screenshot({ path, clip })
  console.log(`captured ${path} (asserted: ${fragment})`)
}

/** The context/page most recently opened, so a failed scene can be dumped. */
let current = null

async function scene(id, fn) {
  current = null
  try {
    await fn()
  } catch (e) {
    console.log(`SKIPPED ${id}: ${String(e && e.message ? e.message : e).split('\n')[0]}`)
    // DEBUG_CAPTURE=1 leaves a full-page frame + the visible control names behind
    // so a skipped scene can be diagnosed without re-running under a debugger.
    if (process.env.DEBUG_CAPTURE && current?.page) {
      await current.page.screenshot({ path: `${outDir}/debug-${id}.png`, fullPage: true }).catch(() => {})
      const names = await current.page.locator('button, [role=tab], [role=menuitemradio]')
        .evaluateAll(els => els.map(e => (e.getAttribute('aria-label') || e.textContent || '').trim().slice(0, 40)).filter(Boolean))
        .catch(() => [])
      console.log(`  debug ${id}: controls=${JSON.stringify(names)}`)
    }
  } finally {
    if (current?.ctx) await current.ctx.close().catch(() => {})
    current = null
  }
}

// ── A2 InstancesViewport error footer ────────────────────────────────────────
await scene('A2', async () => {
  const INST = {
    id: 'remote-1', name: 'Remote crew', ssh_host: 'crew.example.internal', remote_port: 7788,
    local_port: 0, ttl: '2h', remote_bin: 'kirocrew', connection_method: 'ssh', ssm_target: '',
    aws_profile: '', aws_region: '', ssm_run_as: '', was_connected: true,
    status: { instance_id: 'remote-1', state: 'error', error: 'ssh: connect to host crew.example.internal port 22: Connection refused' },
  }
  const o = await openPage({
    override: (u, route, json) => {
      if (u.pathname === '/api/instances') return json({ active: true, instances: [INST], warm_set_cap: 2, sso: {} }), true
      if (u.pathname.startsWith('/api/instances/remote-1/')) return json(INST.status), true
      return false
    },
  })
  const { page } = o
  await page.goto(`${base}/chat`, { waitUntil: 'networkidle' })
  const trigger = page.getByRole('button', { name: /switch instance/i }).first()
  await trigger.waitFor({ state: 'visible', timeout: 15_000 })
  await trigger.click()
  await page.getByRole('menuitemradio', { name: /remote crew/i }).first().click()
  const fragment = 'This tab stays until you disconnect the instance'
  await shoot(page, {
    file: 'a2-instances-viewport-error-footer.png', fragment,
    container: page.locator('div.max-w-md').filter({ hasText: fragment }).first(),
  })
  return o
})

// ── A7 SttSettings prerequisites paragraph ───────────────────────────────────
await scene('A7', async () => {
  const o = await openPage({
    override: (u, route, json) => {
      if (u.pathname === '/api/config/stt') {
        return json({
          ...STT_CONFIG_OK, enabled: true, provider: 'local', available: false,
          prereqs: ['python3 -m pip install faster-whisper ctranslate2'],
        }), true
      }
      if (u.pathname === '/api/stt/status') return json({ available: false, code: 'missing_packages', detail: 'faster-whisper is not importable', models: [] }), true
      return false
    },
  })
  const { page } = o
  await page.goto(`${base}/settings/voice`, { waitUntil: 'networkidle' })
  const fragment = 'Then restart the gateway'
  const text = page.getByText(fragment, { exact: false }).first()
  await text.waitFor({ state: 'visible', timeout: 15_000 })
  await shoot(page, {
    file: 'a7-stt-prerequisites.png', fragment,
    container: text.locator('xpath=ancestor::div[contains(@class,"rounded-lg")][1]'),
  })
  return o
})

await browser.close()
srv.close()
