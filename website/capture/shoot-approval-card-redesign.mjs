/**
 * Screenshot harness for the redesigned chat tool-approval card.
 *
 * Serves the isolated capture entry (capture/approval-card-redesign.tsx) with a
 * programmatic Vite dev server on loopback, then photographs the REAL composer
 * in each of the card's five states, with the Trust and Reject menus open, with
 * the all-tools row armed, and after a decision, in a light and a dark theme,
 * plus short recordings of a decision and of arming the lone all-tools control.
 * Every row is fabricated inside the entry and every /api call is answered
 * here, so no gateway / kiro-cli / token is involved.
 *
 * Usage (from website/): node capture/shoot-approval-card-redesign.mjs [outDir] [light,dark themes]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/approval-card-redesign')
const THEMES = (process.argv[3] || 'dracula-light,dracula-dark').split(',')
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5199, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})
const page = await browser.newPage({ viewport: { width: 860, height: 520 }, deviceScaleFactor: 2 })
page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
// A decision (a POST) succeeds with an empty body; every background read is
// left to the dev server, which has no such route, as in a session with no
// gateway. Matched on the path's first segment: a `**/api/**` glob also catches
// Vite's own `/src/api/*` module requests.
const stubApi = p => p.route(url => url.pathname.startsWith('/api/'), route => (
  route.request().method() === 'POST'
    ? route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    : route.continue()
))
await stubApi(page)

const open = async (scene, theme, extra = '') => {
  await page.goto(`${base}/capture/approval-card-redesign.html?scene=${scene}&theme=${theme}${extra}`)
  await page.locator('[data-approval-actions]').waitFor({ timeout: 30000 })
  await page.waitForTimeout(700)
}
const shoot = async (name, theme) => {
  const path = `${OUT}/${name}-${theme.endsWith('light') ? 'light' : 'dark'}.png`
  await page.screenshot({ path })
  console.log('wrote', path)
}
const trust = () => page.locator('[data-approval-actions] button', { hasText: 'Trust' })
const reject = () => page.locator('[data-approval-actions] button', { hasText: 'Reject' })

for (const theme of THEMES) {
  // The five states.
  for (const scene of ['shell', 'reads', 'env', 'mcp', 'none', 'override', 'ghost']) {
    await open(scene, theme)
    await shoot(`state-${scene}`, theme)
  }

  // The MCP card's lone all-tools control, armed by its first click. Its
  // measured width, idle and armed, is logged: the reserve keeps them equal.
  await open('mcp', theme)
  const lone = await trust().elementHandle()
  const width = async () => Math.round((await lone.boundingBox()).width)
  const idleWidth = await width()
  await lone.click()
  await page.waitForTimeout(200)
  console.log(`  [${theme}] lone all-tools control width: idle ${idleWidth}px, armed ${await width()}px`)
  await shoot('state-mcp-armed', theme)

  // An MCP card that can also take "Always allow this tool": a menu again.
  await open('mcp-always', theme)
  await trust().click()
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(300)
  await shoot('menu-trust-always-allow', theme)
  await page.keyboard.press('Escape')

  // Trust menu, as it opens: the session lifetime, every row.
  await open('reads', theme)
  await trust().click()
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(300)
  await shoot('menu-trust', theme)

  // A timed lifetime chosen, and the all-tools row armed by its first click.
  await page.getByRole('menuitemradio').first().click()
  await page.getByRole('menuitem').last().click()
  await page.waitForTimeout(200)
  await shoot('menu-trust-hour-armed', theme)
  await page.keyboard.press('Escape')

  // The long command: the menu must clear a three-line command box too.
  await open('shell', theme)
  await trust().click()
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(300)
  await shoot('menu-trust-long-command', theme)
  await page.keyboard.press('Escape')

  // A short pane: the card's request region is capped and cues what it hides,
  // and the Trust menu's exact-command row prints the whole command.
  await page.keyboard.press('Escape')
  // The ghost scene: no stand-in transcript pill, which at this height would
  // flip the card in and out of its ghost state in this in-flow harness.
  await page.setViewportSize({ width: 860, height: 360 })
  await open('ghost', theme)
  await shoot('short-pane', theme)
  await trust().click()
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(300)
  await shoot('short-pane-menu', theme)
  // The menu's own scroll fades the edge that hides rows (data-more-*).
  // Checked with each attribute set by hand, since a menu that fits sets none.
  const menuCue = await page.getByTestId('trust-menu').evaluate(el => {
    const mask = () => getComputedStyle(el).maskImage || getComputedStyle(el).webkitMaskImage
    const at = attrs => { attrs.forEach(a => el.setAttribute(a, '')); const m = mask(); attrs.forEach(a => el.removeAttribute(a)); return m }
    return { none: mask(), below: at(['data-more-below']), above: at(['data-more-above']), both: at(['data-more-above', 'data-more-below']) }
  })
  console.log(`  [${theme}] short-pane menu cue: ${JSON.stringify(menuCue)}`)
  await page.keyboard.press('Escape')
  await page.setViewportSize({ width: 860, height: 520 })

  // Cramped: a title overlay low enough that the request has under 64px of
  // room (requestRegionCap.ts). Sized from the card as it lays out without
  // one: room = region bottom - (overlay bottom + 8px gap) - the card's
  // heading, so an overlay ending 38px above that line leaves 30px.
  await open('shell', theme)
  const geo = await page.getByTestId('approval-request-region').evaluate(r => {
    const card = r.parentElement.getBoundingClientRect()
    const box = r.getBoundingClientRect()
    return { bottom: box.bottom, heading: box.top - card.top }
  })
  await open('shell', theme, `&overlay=${Math.round(geo.bottom - geo.heading - 38)}`)
  await page.getByTestId('approval-cramped').waitFor()
  const cramped = await page.evaluate(() => ({
    allow: document.querySelector('[data-approval-primary]').disabled,
    trust: [...document.querySelectorAll('[data-approval-actions] button')].find(b => b.textContent.includes('Trust')).disabled,
    reject: [...document.querySelectorAll('[data-approval-actions] button')].find(b => b.textContent.includes('Reject')).disabled,
  }))
  console.log(`  [${theme}] cramped: Allow once disabled ${cramped.allow}, Trust disabled ${cramped.trust}, Reject disabled ${cramped.reject}`)
  await shoot('cramped', theme)
  await open('shell', theme)

  // Reject menu (unchanged), with its reason box.
  await reject().click()
  await page.getByRole('menu').waitFor()
  await page.waitForTimeout(300)
  await shoot('menu-reject', theme)
  await page.keyboard.press('Escape')

  // After a decision: a timed family grant, then a rejection.
  await trust().click()
  await page.getByRole('menuitemradio').first().click()
  await page.getByRole('menuitem').nth(1).click()
  await page.locator('[data-testid="approval-receipt"]').waitFor()
  await page.waitForTimeout(500)
  await shoot('receipt-trusted', theme)

  await open('shell', theme)
  await reject().click()
  await page.getByRole('menuitem').first().click()
  await page.locator('[data-testid="approval-receipt"]').waitFor()
  await page.waitForTimeout(500)
  await shoot('receipt-rejected', theme)
}

// Recordings, one clip each: a decision settling into its receipt (the status
// pill moves and turns into the verdict), and the lone all-tools control's
// first click. Video at 1x, so each clip stays small.
const record = async (name, theme, scene, act) => {
  const ctx = await browser.newContext({ viewport: { width: 860, height: 520 }, recordVideo: { dir: `${OUT}/video-tmp`, size: { width: 860, height: 520 } } })
  const p = await ctx.newPage()
  await stubApi(p)
  await p.goto(`${base}/capture/approval-card-redesign.html?scene=${scene}&theme=${theme}`)
  await p.locator('[data-approval-actions]').waitFor({ timeout: 30000 })
  await p.waitForTimeout(900)
  await act(p)
  await p.waitForTimeout(1600)
  await ctx.close()
  await p.video().saveAs(`${OUT}/${name}-${theme.endsWith('light') ? 'light' : 'dark'}.webm`)
  console.log('wrote', `${OUT}/${name}`)
}
for (const theme of THEMES) {
  await record('rec-allow-once', theme, 'shell', p => p.getByRole('button', { name: 'Allow once' }).click())
  await record('rec-reject-once', theme, 'shell', async p => {
    await p.locator('[data-approval-actions] button', { hasText: 'Reject' }).click()
    await p.waitForTimeout(400)
    await p.getByRole('menuitem').first().click()
  })
  await record('rec-all-tools-arm', theme, 'mcp', async p => {
    await p.locator('[data-approval-actions] button', { hasText: 'Trust' }).hover()
    await p.waitForTimeout(400)
    await p.locator('[data-approval-actions] button', { hasText: 'Trust' }).click()
  })
}

await browser.close()
await server.close()
