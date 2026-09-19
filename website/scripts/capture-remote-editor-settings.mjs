/**
 * Screenshot harness for the Settings → Display "Remote editor" card
 * (PR #11345 / issue #11338).
 *
 * Runs the REAL built SPA (website/dist) behind the shared loopback static
 * server with /api/** answered from the shared stub, so the card lays out
 * exactly as in production. The PATCH route persists into the scene the way
 * the real backend would, so the "configured" frame reads back real state.
 *
 * Captured:
 *   01-remote-editor-card-off-dark.png     the card, feature off (editor unset)
 *   02-remote-editor-card-set-dark.png     VS Code + an SSH host configured
 *   03-remote-editor-card-set-light.png    light-theme parity of the set state
 *
 * Asserts the card heading and both fields are present before shooting, and
 * asserts the applied data-theme matches the frame name (an unset preference
 * resolves to the HOST's mode, so a frame can otherwise claim a theme it does
 * not carry) — a blank or wrongly-themed page fails the run rather than
 * emitting misleading evidence.
 *
 * Usage: node scripts/capture-remote-editor-settings.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { stubDashboardApi, json, logPageProblems } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/remote-editor-settings'
const VIEW = { width: 1500, height: 950 }
const HOST = 'dev-host.example.com'

mkdirSync(OUT, { recursive: true })

// mise's node injects LD_LIBRARY_PATH at its own bundled libstdc++, which is
// older than the system Mesa needs; children inherit it, so scrub it here.
const { LD_LIBRARY_PATH: _mise, ...browserEnv } = process.env

/** Tight crop around the Remote editor card, derived from real boxes. */
async function card(page, name, pad = 20) {
  const heading = page.getByRole('heading', { name: 'Remote editor', exact: true })
  const rows = page.locator(
    '[data-setting-label="Editor"], [data-setting-label="SSH host"]',
  )
  if (!(await heading.count()) || (await rows.count()) < 2) {
    throw new Error(`${name}: Remote editor card did not render (heading=` +
      `${await heading.count()}, rows=${await rows.count()})`)
  }
  const hb = await heading.first().boundingBox()
  const boxes = []
  for (let i = 0; i < await rows.count(); i++) boxes.push(await rows.nth(i).boundingBox())
  const valid = boxes.filter(Boolean)
  const x0 = Math.max(0, Math.min(hb.x, ...valid.map(b => b.x)) - pad)
  const y0 = Math.max(0, hb.y - pad)
  const x1 = Math.max(hb.x + hb.width, ...valid.map(b => b.x + b.width)) + pad
  const y1 = Math.max(...valid.map(b => b.y + b.height)) + pad
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: { x: x0, y: y0, width: Math.min(VIEW.width - x0, x1 - x0), height: y1 - y0 },
  })
  console.log('wrote', `${OUT}/${name}.png`)
}

/**
 * Fresh context + stub install per theme: the shared stub pins its theme (both
 * the init-script localStorage seed and /api/theme/boot) at install time, so a
 * same-page reload cannot change palette — the light frame silently rendered
 * dark until this was per-theme.
 */
async function openSettings(browser, base, theme, scene) {
  const context = await browser.newContext({ viewport: VIEW, deviceScaleFactor: 2 })
  const page = await context.newPage()
  logPageProblems(page)
  await stubDashboardApi(page, {
    theme,
    extra: async (path, route) => {
      const method = route.request().method()
      if (path === '/api/config/kirocrew' && method === 'PATCH') {
        const body = JSON.parse(route.request().postData() || '{}')
        if (body.path === 'dashboard.remote_editor.editor') scene.editor = body.value
        if (body.path === 'dashboard.remote_editor.host') scene.host = body.value
        json(route, { ok: true })
        return true
      }
      if (path === '/api/config/kirocrew' && method === 'GET') {
        json(route, {
          agent: { model: 'auto', reasoning_effort: '' },
          dashboard: {
            terminal: { enabled: true, shell: '' },
            remote_editor: { editor: scene.editor, host: scene.host },
          },
        })
        return true
      }
      // The card's saves refetch ['branding']; serve the same values back so
      // the stubbed world stays consistent with the config it just wrote.
      if (path === '/api/dashboard/branding') {
        json(route, {
          bot_name: 'Kiro Crew', avatar: '/logo.png', direct_local: false,
          remote_editor: { editor: scene.editor, host: scene.host },
        })
        return true
      }
      return false
    },
  })
  await page.goto(base + '/settings?tab=display', { waitUntil: 'domcontentloaded' })
  await page.waitForTimeout(2400)
  // Assert the palette matches the frame name rather than assuming it.
  const applied = await page.evaluate(() => document.documentElement.getAttribute('data-theme'))
  const want = theme === 'light' ? 'kiro-light' : 'kiro-dark'
  if (applied !== want) throw new Error(`theme=${theme}: data-theme=${applied}, wanted ${want}`)
  await page.getByRole('heading', { name: 'Remote editor', exact: true }).scrollIntoViewIfNeeded()
  return { context, page }
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch({ env: browserEnv })

  // 1 + 2. Dark: the off default, then configure VS Code + host live.
  {
    const scene = { editor: '', host: '' }
    const { context, page } = await openSettings(browser, base, 'dark', scene)
    await card(page, '01-remote-editor-card-off-dark')

    // SimpleSelect is a custom trigger carrying aria-label={label}: open it
    // and pick the option by its visible text.
    await page.getByLabel('Editor', { exact: true }).first().click()
    await page.getByRole('option', { name: 'VS Code' }).click()
    await page.waitForTimeout(600)
    const hostField = page.getByLabel('SSH host')
    await hostField.fill(HOST)
    await hostField.blur()
    await page.waitForTimeout(900)
    await card(page, '02-remote-editor-card-set-dark')
    await context.close()
  }

  // 3. Light-theme parity of the configured state.
  {
    const scene = { editor: 'vscode', host: HOST }
    const { context, page } = await openSettings(browser, base, 'light', scene)
    await card(page, '03-remote-editor-card-set-light')
    await context.close()
  }

  await browser.close()
  srv.close()
}

main().catch(err => { console.error(err); process.exit(1) })
