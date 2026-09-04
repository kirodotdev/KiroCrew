/**
 * Screenshot harness for the terminal Paste soft key (issue #5571).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with every /api/** call answered from fixtures via Playwright route
 * interception — no gateway, no PTY. The terminal WebSocket is a tiny scripted
 * PTY that prints a prompt and ECHOES input back (converting the CR that
 * term.paste emits into CRLF), so a real paste renders visibly in the shot.
 *
 * Touch is emulated the same way the existing touch harness does it:
 * `hasTouch` context + CDP `(hover: none)` / `(pointer: coarse)` media, which
 * is exactly the predicate `useIsTouchDevice` renders the key bar on.
 *
 * The clipboard is NOT mocked. The grant scenario uses Playwright's real
 * `clipboard-read` permission grant and seeds the clipboard through
 * `navigator.clipboard.writeText`; the deny scenario runs in a second context
 * with no grant, so `readText()` rejects exactly as it does in production.
 *
 * Captured:
 *   01-paste-key-touch-dark.png     key bar on a touch viewport with the new
 *                                   Paste key at the end of the row
 *   02-paste-delivered-dark.png     multi-line clipboard text pasted and
 *                                   echoed by the scripted PTY
 *   03-paste-denied-dark.png        the denied-permission state: red
 *                                   "Paste failed" label on the key
 *   04-paste-key-touch-light.png    light-theme parity of the key bar
 *   05-paste-key-phone-390-dark.png  iPhone-width (390px) portrait: the key
 *                                    caps scroll in their own region and the
 *                                    Paste key stays pinned on-screen
 *   06-paste-denied-phone-390.png    the denied state at 390px: the remedy
 *                                    text truncates inside the pinned region
 *                                    instead of widening past the viewport
 *
 * Usage: node scripts/capture-terminal-paste-key.mjs [outDir]
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || '../temp-screenshots/terminal-paste-key'
mkdirSync(OUT, { recursive: true })

async function stubContext(context, theme) {
  // The shared stub takes a page in its own harnesses; a BrowserContext exposes
  // the same three methods it uses (route, routeWebSocket, addInitScript) and
  // this harness runs exactly one page per context, so the effect is identical.
  await stubDashboardApi(context, {
    theme,
    // Seed the docked terminal open with one tab, the same persisted shape a
    // reload restores — clicking the nav row in a harness is flaky. Passed here
    // rather than via addInitScript because the stub's own script clears storage.
    localStorageEntries: {
      'mc-bottom-terminal': JSON.stringify({
        open: true, height: 300,
        tabs: [{ id: 'fixture-tab-1' }], activeId: 'fixture-tab-1',
      }),
    },
    extra: async (path, route) => {
      // Create-slot must return a keyed slot object — the shared stub's array
      // answer puts a keyless slot in redux and crashes the shell.
      if (path === '/api/chat/slots' && route.request().method() === 'POST') {
        await json(route, { key: 'fixture-chat', title: 'New Session…', agent: 'kirocrew' })
        return true
      }
      if (path.startsWith('/api/chat/slots/')) {
        await json(route, {})
        return true
      }
      return false
    },
  })

  // AFTER the shared stub, which routes `/api/ws` itself: the terminal socket
  // matches that pattern too, and the LAST matching route registered wins.
  // Scripted PTY: ready + prompt on connect, then echo keystrokes/pastes back
  // (CR → CRLF so multi-line pastes render as lines, as a shell would).
  await context.routeWebSocket(/\/api\/ws\/terminal\//, ws => {
    ws.send(JSON.stringify({ type: 'ready' }))
    ws.send(Buffer.from('\x1b[1;32m$\x1b[0m '))
    ws.onMessage(m => {
      if (typeof m === 'string') return // resize/control frames
      ws.send(Buffer.from(m.toString('utf8').replace(/\r/g, '\r\n')))
    })
  })
}

/** New page with touch emulation active (the key bar's render condition). */
async function touchPage(context, base) {
  const page = await context.newPage()
  logPageProblems(page)
  const cdp = await context.newCDPSession(page)
  await cdp.send('Emulation.setEmulatedMedia', {
    features: [
      { name: 'hover', value: 'none' },
      { name: 'any-hover', value: 'none' },
      { name: 'pointer', value: 'coarse' },
      { name: 'any-pointer', value: 'coarse' },
    ],
  })
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  await page.getByTestId('terminal-key-bar').waitFor({ state: 'visible', timeout: 20000 })
  await page.waitForTimeout(1200) // scripted PTY prompt renders
  return page
}

/** Crop: the terminal panel including the key bar (the surface under test). */
async function shotPanel(page, name) {
  const bar = await page.getByTestId('terminal-key-bar').boundingBox()
  if (!bar) throw new Error('key bar not laid out')
  const clip = {
    x: Math.max(0, bar.x - 8),
    y: Math.max(0, bar.y - 240),
    width: Math.min(page.viewportSize().width, bar.width + 16),
    height: bar.height + 240 + 12,
  }
  await page.screenshot({ path: `${OUT}/${name}.png`, clip })
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()

  // ---- Grant context: real clipboard-read permission ----
  const grantCtx = await browser.newContext({
    viewport: { width: 820, height: 780 }, deviceScaleFactor: 2,
    hasTouch: true,
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  await stubContext(grantCtx, 'dark')
  const page = await touchPage(grantCtx, base)

  // 1) The bar with the Paste key present, prompt rendered.
  const pasteBtn = page.getByRole('button', { name: 'Paste' })
  if (!(await pasteBtn.count())) throw new Error('Paste key not rendered')
  await shotPanel(page, '01-paste-key-touch-dark')

  // 2) Seed the real clipboard with multi-line text, tap Paste, and let the
  //    echo PTY render what arrived. Proves the read → term.paste → PTY path.
  await page.evaluate(() =>
    navigator.clipboard.writeText('echo pasted from the touch key\nls -la'))
  await pasteBtn.click()
  await page.waitForTimeout(900)
  const body = await page.locator('.xterm').first().textContent()
  if (!body?.includes('pasted from the touch key')) {
    throw new Error('pasted text did not render in the terminal: ' + (body || '').slice(0, 200))
  }
  if (!body.includes('ls -la')) throw new Error('multi-line paste lost its second line')
  await shotPanel(page, '02-paste-delivered-dark')

  // ---- Deny context: NO clipboard permission, readText really rejects ----
  const denyCtx = await browser.newContext({
    viewport: { width: 820, height: 780 }, deviceScaleFactor: 2,
    hasTouch: true,
  })
  await stubContext(denyCtx, 'dark')
  const denyPage = await touchPage(denyCtx, base)
  await denyPage.getByRole('button', { name: 'Paste' }).click()
  const failed = denyPage.getByRole('button', { name: 'Allow clipboard access' })
  await failed.waitFor({ state: 'visible', timeout: 5000 })
  await shotPanel(denyPage, '03-paste-denied-dark')
  // The failed state self-reverts after ~2.5s; if the shot slipped past the
  // revert it captured an ordinary Paste key — misleading evidence, fail loud.
  if (!(await failed.isVisible())) {
    throw new Error('failed state reverted before the screenshot was taken — rerun')
  }

  // ---- Phone width (390px portrait): Paste must be ON-SCREEN, pinned ----
  const phoneCtx = await browser.newContext({
    viewport: { width: 390, height: 720 }, deviceScaleFactor: 2,
    hasTouch: true, isMobile: true,
  })
  await stubContext(phoneCtx, 'dark')
  const phonePage = await touchPage(phoneCtx, base)
  const pinned = phonePage.getByRole('button', { name: 'Paste' })
  const box = await pinned.boundingBox()
  if (!box || box.x + box.width > 390 + 1 || box.x < 0) {
    throw new Error(`Paste key not fully on-screen at 390px: ${JSON.stringify(box)}`)
  }
  await phonePage.screenshot({ path: `${OUT}/05-paste-key-phone-390-dark.png`, clip: { x: 0, y: Math.max(0, box.y - 60), width: 390, height: Math.min(720, box.y + box.height + 12) - Math.max(0, box.y - 60) } })

  // Denied state at 390px: the remedy label must stay inside the viewport.
  const denyPhoneCtx = await browser.newContext({
    viewport: { width: 390, height: 720 }, deviceScaleFactor: 2,
    hasTouch: true, isMobile: true,
  })
  await stubContext(denyPhoneCtx, 'dark')
  const denyPhone = await touchPage(denyPhoneCtx, base)
  await denyPhone.getByRole('button', { name: 'Paste' }).click()
  const deniedPinned = denyPhone.getByRole('button', { name: 'Allow clipboard access' })
  await deniedPinned.waitFor({ state: 'visible', timeout: 5000 })
  const dbox = await deniedPinned.boundingBox()
  if (!dbox || dbox.x + dbox.width > 390 + 1 || dbox.x < 0) {
    throw new Error(`denied-state key overflows the 390px viewport: ${JSON.stringify(dbox)}`)
  }
  await denyPhone.screenshot({ path: `${OUT}/06-paste-denied-phone-390.png`, clip: { x: 0, y: Math.max(0, dbox.y - 60), width: 390, height: Math.min(720, dbox.y + dbox.height + 12) - Math.max(0, dbox.y - 60) } })

  // ---- Light theme parity ----
  const lightCtx = await browser.newContext({
    viewport: { width: 820, height: 780 }, deviceScaleFactor: 2,
    hasTouch: true,
    permissions: ['clipboard-read', 'clipboard-write'],
  })
  await stubContext(lightCtx, 'light')
  const lightPage = await touchPage(lightCtx, base)
  await shotPanel(lightPage, '04-paste-key-touch-light')

  await browser.close()
  srv.close()
  console.log(`wrote 6 screenshots to ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
