/**
 * Screenshot harness: an app install the gateway refuses BEFORE its event stream
 * opens (owner-only, a bad body) shows the refusal's message on the detail page,
 * not the raw JSON body.
 *
 * Serves the REAL built SPA (website/dist) and answers /api/** from fixtures. The
 * scene-specific stub is `POST /api/apps/registry/install-stream`, which answers
 * the 403 `_owner_denial_response` really returns to a non-owner dashboard session:
 * `{"error": "owner authorization required", "code": "owner_only"}`.
 *
 * One frame per locale (en, de): the registry app's detail page after Install.
 *
 * Usage: node scripts/capture-apps-install-stream-refusal.mjs [outDir] [prefix]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { execFileSync } from 'node:child_process'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/apps-install-stream-refusal'
const PREFIX = process.argv[3] || 'after'
const NAME = 'oncall-radar'

mkdirSync(OUT, { recursive: true })

const REGISTRY_APP = {
  name: NAME, displayName: 'Oncall Radar', version: '1.0.0',
  description: 'Watches your on-call rotations.', author: 'zezhexu',
  repo: 'https://github.com/example/oncall-radar', category: 'productivity', tags: ['oncall'],
}

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  if (!process.env.SKIP_BUILD) {
    console.log('building dist (SKIP_BUILD=1 to reuse)…')
    execFileSync('npm', ['run', 'build'], { stdio: 'inherit', shell: process.platform === 'win32' })
  }
  const { srv, base } = await serveDist()
  const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env
  const browser = await chromium.launch({ env: browserEnv })

  for (const lang of ['en', 'de']) {
    const context = await browser.newContext({ viewport: { width: 1300, height: 820 }, deviceScaleFactor: 2 })
    const page = await context.newPage()
    await page.routeWebSocket(/\/api\/ws/, () => {})
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname
      if (path === '/api/apps/registry/install-stream') {
        return json(route, { error: 'owner authorization required', code: 'owner_only' }, 403)
      }
      if (path === `/api/apps/${NAME}`) return json(route, { error: 'not installed' }, 404)
      if (path === '/api/apps') return json(route, [])
      if (path === '/api/apps/registry') {
        return json(route, { apps: [REGISTRY_APP], categoryOrder: [], editorialSections: [], serverPlatform: { os: 'linux', arch: 'x64' } })
      }
      if (path === '/api/apps/registries') return json(route, { registries: [] })
      if (path === '/api/kiro-prerequisite') {
        return json(route, {
          platform: 'linux', installed: true, authenticated: true, ready: true,
          initial_setup_complete: true, can_auto_install: false, can_login: false,
          repair_required: false, docs_url: '', setup_allowed: false,
          operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
        })
      }
      if (path === '/api/system') return json(route, { os: 'linux', arch: 'x64' })
      if (path === '/api/chat/slots') return json(route, [])
      if (path.startsWith('/api/instances')) return json(route, { instances: [], active: '' })
      if (path === '/api/status') return json(route, { sessions: 0, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
      if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
      if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
      if (path === '/api/themes') return json(route, { themes: [], installed: [] })
      if (path === '/api/theme/boot') return json(route, { mode: 'dark', theme: '' })
      if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
      if (path === '/api/dashboard/config') return json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' })
      const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary|ui-prefs)/.test(path)
      if (objectish) return json(route, {})
      return json(route, [])
    })
    page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
    await page.addInitScript(l => {
      localStorage.clear()
      localStorage.setItem('mc-theme', 'dark')
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-lang', l)
    }, lang)

    await page.goto(`${base}/apps/detail/${NAME}`, { waitUntil: 'domcontentloaded' })
    const install = page.getByRole('button', { name: /^(Install|Installieren)$/ }).first()
    await install.waitFor({ timeout: 20000 })
    await install.click()
    // Wait on the failure banner, not a sleep, so the shot cannot race the render.
    await page.getByRole('alert').first().waitFor({ state: 'visible', timeout: 20000 })
    const out = `${OUT}/${PREFIX}-${lang}.png`
    // Clipped to the header, the refusal banner and the install card: the scene
    // under test, without the unrelated empty-fixture toasts lower down.
    await page.screenshot({ path: out, clip: { x: 0, y: 0, width: 1300, height: 440 } })
    console.log('wrote', out)
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
