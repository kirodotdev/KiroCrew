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
 *   04-remote-editor-host-hint-dark.png    VS Code picked, SSH host empty: the
 *                                          "Set an SSH host…" hint under the card
 *   05-display-rail-dark.png               the Display side rail ("Remote editor"
 *                                          selected) beside the card
 *   06-remote-editor-save-failed-dark.png  the host PATCH answered 400 (as the
 *                                          real schema-pattern refusal is): the
 *                                          inline "Couldn’t save the SSH host. Use a hostname…"
 *   07-remote-editor-editor-save-failed-dark.png  the editor PATCH failed: the
 *                                          inline "Couldn’t save the editor choice."
 *
 * Asserts the card heading and both fields are present before shooting (plus
 * each frame's own subject: the hint text, the selected rail entry, the error),
 * and asserts the applied data-theme matches the frame name (an unset preference
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

/**
 * Tight crop around the Remote editor card, derived from real boxes. `extra`
 * locators (a hint or an inline error under the fields) are asserted present
 * and folded into the crop; `rail` widens it leftward to include the Display
 * side rail.
 */
async function card(page, name, { pad = 20, extra = [], rail = null } = {}) {
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
  for (const loc of extra) {
    if (!(await loc.count())) throw new Error(`${name}: expected element missing (${loc})`)
    boxes.push(await loc.first().boundingBox())
  }
  if (rail) boxes.push(await rail.boundingBox())
  const valid = boxes.filter(Boolean)
  const x0 = Math.max(0, Math.min(hb.x, ...valid.map(b => b.x)) - pad)
  const y0 = Math.max(0, Math.min(hb.y, ...valid.map(b => b.y)) - pad)
  const x1 = Math.max(hb.x + hb.width, ...valid.map(b => b.x + b.width)) + pad
  const y1 = Math.max(...valid.map(b => b.y + b.height)) + pad
  await page.screenshot({
    path: `${OUT}/${name}.png`,
    clip: { x: x0, y: y0, width: Math.min(VIEW.width - x0, x1 - x0), height: Math.min(VIEW.height - y0, y1 - y0) },
  })
  console.log('wrote', `${OUT}/${name}.png`)
}

/** Assert `loc` is visible and reads exactly `text`; returns it for the crop. */
async function expectText(name, loc, text) {
  await loc.first().waitFor({ state: 'visible', timeout: 5000 })
  const got = (await loc.first().innerText()).trim()
  if (got !== text) throw new Error(`${name}: wanted ${JSON.stringify(text)}, got ${JSON.stringify(got)}`)
  return loc
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
        // The save-failed scene: answer the host write the way the real
        // config.patch handler answers a value outside the schema pattern
        // (`_deny(f"invalid value for {path_key}")` → 400 {error}), persisting
        // nothing, so the field's onFailure path is the production one.
        if (scene.rejectHost && body.path === 'dashboard.remote_editor.host') {
          json(route, { error: 'invalid value for dashboard.remote_editor.host' }, 400)
          return true
        }
        // The editor save-failed scene: a failed request (the editor is a
        // closed select, so the only cause is the write itself failing).
        if (scene.failEditor && body.path === 'dashboard.remote_editor.editor') {
          json(route, { error: 'config write failed' }, 500)
          return true
        }
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
  await page.goto(base + '/settings/display/remote-editor', { waitUntil: 'domcontentloaded' })
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

  // 4. VS Code picked, SSH host empty: the card says why no menu row appears.
  {
    const scene = { editor: 'vscode', host: '' }
    const { context, page } = await openSettings(browser, base, 'dark', scene)
    const hint = await expectText('04-remote-editor-host-hint-dark',
      page.getByTestId('remote-editor-host-required'),
      'Set an SSH host to show “Open in VS Code” in file menus.')
    await card(page, '04-remote-editor-host-hint-dark', { extra: [hint] })
    await context.close()
  }

  // 5. The Display rail beside the pane, "Remote editor" the selected entry.
  {
    const scene = { editor: 'vscode', host: HOST }
    const { context, page } = await openSettings(browser, base, 'dark', scene)
    // SettingsSubNav's two-pane rail: a listbox of options, aria-selected on
    // the active one.
    const rail = page.locator('nav').filter({ has: page.getByRole('option', { name: 'Remote editor' }) }).first()
    const entry = rail.getByRole('option', { name: 'Remote editor' })
    if (!(await entry.count())) throw new Error('05-display-rail-dark: no "Remote editor" rail entry')
    if ((await entry.getAttribute('aria-selected')) !== 'true') {
      throw new Error('05-display-rail-dark: "Remote editor" rail entry is not selected')
    }
    await card(page, '05-display-rail-dark', { rail })
    await context.close()
  }

  // 6. A host the backend refuses: the inline save error under the field.
  {
    const scene = { editor: 'vscode', host: '', rejectHost: true }
    const { context, page } = await openSettings(browser, base, 'dark', scene)
    const hostField = page.getByLabel('SSH host')
    await hostField.fill('bad:host')
    await hostField.blur()
    const err = await expectText('06-remote-editor-save-failed-dark',
      page.getByRole('alert').filter({ hasText: 'Couldn’t save the SSH host.' }),
      'Couldn’t save the SSH host. Use a hostname or ~/.ssh/config alias only, without a port, path or scheme; put a port in ~/.ssh/config.')
    // The rejected value stays in the field for the user to fix.
    if ((await hostField.inputValue()) !== 'bad:host') {
      throw new Error(`06-remote-editor-save-failed-dark: field reads ${await hostField.inputValue()}`)
    }
    await card(page, '06-remote-editor-save-failed-dark', { extra: [err] })
    await context.close()
  }

  // 7. The editor write fails: the inline notice under the Editor select.
  {
    const scene = { editor: '', host: HOST, failEditor: true }
    const { context, page } = await openSettings(browser, base, 'dark', scene)
    await page.getByLabel('Editor', { exact: true }).first().click()
    await page.getByRole('option', { name: 'VS Code' }).click()
    const err = await expectText('07-remote-editor-editor-save-failed-dark',
      page.getByRole('alert').filter({ hasText: 'Couldn’t save the editor choice.' }),
      'Couldn’t save the editor choice.')
    await card(page, '07-remote-editor-editor-save-failed-dark', { extra: [err] })
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
