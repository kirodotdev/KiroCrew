/**
 * Screenshot harness for the voice push-to-talk binding rows.
 *
 * Runs the REAL built SPA (website/dist) behind a tiny in-process static server
 * with SPA fallback, and answers every /api/** call from fixtures via Playwright
 * route interception. No gateway, no dashboard token, no kiro-cli spawn — and no
 * microphone: the test strip is key-detection only by design.
 *
 * The client code under test is unmodified, so Settings > Voice > Speech-to-Text
 * renders exactly as it does in production. Scenes flip the persisted push-to-talk
 * config (mode + bound key) so the mode-dependent rows can be captured in each
 * state, and the strip's live states are driven by REAL key events through
 * Playwright's keyboard — which is also what proves AltRight arrives with
 * location=2.
 *
 * Usage: node scripts/capture-voice-ptt.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'

const OUT = process.argv[2] || '../temp-screenshots/voice-ptt'
const PROJECT = '/home/user/workspace/KiroCrew'

mkdirSync(OUT, { recursive: true })

/** What /api/config/stt returns. `enabled` gates the push-to-talk block. */
const stt = {
  enabled: true,
  provider: 'whisper',
  model: 'base',
  available: true,
  streaming: false,
  endpointing: false,
  dictation_panel: true,
  language_code: 'en-US',
  models: { tiny: '75 MB', base: '142 MB', small: '466 MB' },
  providers: ['whisper', 'mlx', 'transcribe'],
  language_codes: ['en-US', 'zh-CN', 'de-DE'],
  install_step: 'done',
  install_detail: '',
  install_error: '',
  prereqs: [],
}

/** Flipped per scene: the localStorage push-to-talk config the panel reads back. */
const scene = { theme: 'dark', ptt: { mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 } }

const json = (route, body, status = 200) => route.fulfill({
  status, contentType: 'application/json', body: JSON.stringify(body),
})

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  const context = await browser.newContext({
    viewport: { width: 1760, height: 1150 },
    // Settings rows are 12–13px type; a 1x shot renders soft on GitHub.
    deviceScaleFactor: 2,
  })
  const page = await context.newPage()

  await page.routeWebSocket(/\/api\/ws/, () => {})

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname
    const method = route.request().method()

    if (path === '/api/config/stt' && method === 'POST') return json(route, stt)
    if (path === '/api/config/stt') return json(route, stt)
    if (path === '/api/config/kirocrew') {
      return json(route, {
        agent: { model: 'claude-opus-4.8', reasoning_effort: 'high' },
        session: { autocompact_pct: 90 },
        dashboard: { user_role: '', user_technical_level: '' },
      })
    }
    // The app shell mounts behind this gate and reads status.operation.status —
    // a generic object stub crashes it, blanking the whole page.
    if (path === '/api/kiro-prerequisite') {
      return json(route, {
        platform: 'darwin', installed: true, authenticated: true, ready: true,
        initial_setup_complete: true, can_auto_install: false, can_login: false,
        repair_required: false, docs_url: '', setup_allowed: false,
        operation: { kind: '', status: 'idle', message: '', detail: '', url: '', error: '' },
      })
    }
    if (path === '/api/chat/slots') return json(route, [])
    if (path === '/api/status') return json(route, { sessions: 0, crons: 0, lessons: 0, uptime: 120, version: 'dev' })
    if (path === '/api/notifications') return json(route, { notifications: [], unread: 0 })
    if (path === '/api/auth/me') return json(route, { user: 'owner', app: '' })
    if (path === '/api/themes') return json(route, { themes: [], installed: [] })
    if (path === '/api/theme/boot') return json(route, { mode: scene.theme, theme: '' })
    if (path === '/api/dashboard/branding') return json(route, { bot_name: 'Kiro', avatar: '' })
    if (path === '/api/recent-projects') return json(route, { dirs: [PROJECT] })
    if (path === '/api/instances') return json(route, { instances: [], active: '' })
    if (path === '/api/dashboard/config') {
      return json(route, { restore_sessions: false, restore_window_minutes: 30, merge_queued_messages: false, widget_density: 'more' })
    }
    const objectish = /(config|tips|voice|autonudge|branding|status|usage-summary)/.test(path)
    if (objectish) return json(route, {})
    return json(route, [])
  })

  page.on('pageerror', err => console.log('PAGEERROR:', String(err).slice(0, 300)))
  page.on('console', msg => { if (msg.type() === 'error') console.log('CONSOLE:', msg.text().slice(0, 300)) })

  async function load(theme = 'dark') {
    scene.theme = theme
    await page.addInitScript(s => {
      localStorage.clear()
      localStorage.setItem('mc-theme', s.theme)
      localStorage.setItem('mc-onboarded', '1')
      localStorage.setItem('mc-ptt-config', JSON.stringify(s.ptt))
    }, scene)
    await page.goto(base + '/settings?tab=voice', { waitUntil: 'domcontentloaded' })
    await page.waitForTimeout(2600)
  }

  const shot = async name => {
    await page.screenshot({ path: `${OUT}/${name}.png` })
    console.log('wrote', `${OUT}/${name}.png`)
  }

  /** Tight crop from the trigger-style row down through the test strip — the
   *  push-to-talk block is the whole story, and the STT card above it is long. */
  async function block(name, pad = 18) {
    const rows = page.locator('[data-setting-label="Shortcut key"], [data-setting-label="How the key works"], [data-setting-label="Tap vs. hold cutoff"]')
    const strip = page.getByRole('status')
    if (await rows.count() && await strip.count()) {
      const boxes = []
      for (let i = 0; i < await rows.count(); i++) boxes.push(await rows.nth(i).boundingBox())
      const sb = await strip.first().boundingBox()
      const valid = boxes.filter(Boolean)
      if (sb && valid.length) {
        const x0 = Math.max(0, Math.min(...valid.map(b => b.x)) - pad)
        const y0 = Math.max(0, Math.min(...valid.map(b => b.y)) - pad - 62)
        const x1 = Math.max(...valid.map(b => b.x + b.width), sb.x + sb.width) + pad
        const y1 = sb.y + sb.height + 46
        await page.screenshot({
          path: `${OUT}/${name}.png`,
          clip: { x: x0, y: y0, width: Math.min(1760 - x0, x1 - x0), height: y1 - y0 },
        })
        console.log('wrote', `${OUT}/${name}.png`)
        return
      }
    }
    await shot(name)
  }

  /** Scroll the push-to-talk block into view — it sits below a long STT card. */
  async function reveal() {
    const row = page.locator('[data-setting-label="Shortcut key"]')
    if (await row.count()) await row.first().scrollIntoViewIfNeeded()
    await page.waitForTimeout(500)
  }

  // 1. Default: hybrid + right Option, strip idle.
  await load('dark')
  await reveal()
  await block('01-ptt-hybrid-idle-dark')

  // 2. Holding the bound key past the threshold — the matched/recording state.
  //    A real keyboard event, so this also proves AltRight reports location=2.
  await page.keyboard.down('AltRight')
  await page.waitForTimeout(760)
  await block('02-ptt-holding-matched-dark')
  await page.keyboard.up('AltRight')
  await page.waitForTimeout(260)
  await block('03-ptt-released-captured-dark')

  // 3. A key that is NOT the bound one: the warning state that tells a user
  //    whose keyboard lacks the chosen key to pick another.
  await page.keyboard.down('AltLeft')
  await page.waitForTimeout(420)
  await block('04-ptt-wrong-key-dark')
  await page.keyboard.up('AltLeft')
  await page.waitForTimeout(200)

  // 4. Toggle mode hides the hold-threshold row entirely (it has no meaning).
  scene.ptt = { mode: 'toggle', binding: { code: 'AltRight' }, holdMs: 500 }
  await load('dark')
  await reveal()
  await block('05-ptt-toggle-no-threshold-dark')

  // 5. Light theme, back on the default binding.
  scene.ptt = { mode: 'hybrid', binding: { code: 'AltRight' }, holdMs: 500 }
  await load('light')
  await reveal()
  await page.keyboard.down('AltRight')
  await page.waitForTimeout(760)
  await block('06-ptt-holding-matched-light')
  await page.keyboard.up('AltRight')

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
