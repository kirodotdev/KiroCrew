/**
 * Screenshot harness for the terminal find bar and dock fullscreen (#10176, #9906).
 *
 * Runs the REAL built SPA (website/dist) behind the shared in-process static
 * server with every /api/** call answered from fixtures via Playwright route
 * interception — no gateway, no PTY. The terminal WebSocket is a scripted PTY
 * that prints a short build log (the word "warning" appears on three lines, so
 * the find has several matches to step through) and then a prompt.
 *
 * Every state is reached through the production input path, not by poking
 * state. This runner is Linux, so it exercises the Linux/Windows bindings:
 * bare Ctrl+F in the focused terminal is asserted to reach the shell as ^F
 * (0x06 on the PTY socket) with no find bar; the overflow menu is asserted to
 * show Find with its Ctrl+Shift+F keycap and Fullscreen with none; the find
 * bar opens on Ctrl+Shift+F (macOS: Cmd+F), the query is typed into the bar,
 * Enter / Shift+Enter step the matches, and fullscreen is the overflow menu's
 * item (macOS also has Cmd+Shift+F). Each frame asserts the DOM state first —
 * the menu keycaps, the "n of m" counter text, the "No results" marker, the
 * fullscreen panel's `absolute inset-0` class and `<main>`'s `isolate` — so a
 * screenshot that passes is a picture of the state it names.
 *
 * Captured, each in dark and light (theme via localStorage `mc-theme`):
 *   00-menu-<theme>.png            overflow menu: Find (Ctrl+Shift+F), Fullscreen (no chord)
 *   01-find-match-<theme>.png      find bar open, "1 of 3", current match highlighted
 *   02-find-step-<theme>.png       after Enter: counter moved to "2 of 3"
 *                                  (Shift+Enter is asserted to step back)
 *   03-find-no-match-<theme>.png   a query with no hit: "No results"
 *   04-fullscreen-<theme>.png      menu Fullscreen: the dock fills the content area,
 *                                  with a visible Exit fullscreen button in its header
 *   05-fullscreen-menu-<theme>.png the overflow menu while fullscreen: "Exit fullscreen"
 *   06-exit-fullscreen-<theme>.png after the header button: back in the dock
 *   07-fullscreen-toggle-dark.webm recording: menu Fullscreen, then the header exit
 *
 * Usage: node scripts/capture-terminal-find.mjs [outDir]
 *   (default outDir: website/temp-screenshots/terminal-find, gitignored)
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { serveDist } from './lib/serve-dist.mjs'
import { json, logPageProblems, stubDashboardApi } from './lib/stub-dashboard-api.mjs'

const OUT = process.argv[2] || fileURLToPath(new URL('../temp-screenshots/terminal-find/', import.meta.url))
mkdirSync(OUT, { recursive: true })

const VIEWPORT = { width: 1280, height: 800 }
const PROMPT = '\x1b[1;32m$\x1b[0m '
/** Scrollback with three "warning" lines, so the find has matches to step. */
const LOG = [
  '$ npm run build',
  '',
  '> website@0.0.0 build',
  '> tsc -b && vite build',
  '',
  'vite v6.3.5 building for production...',
  '\x1b[33mwarning\x1b[0m: src/App.tsx (412): unused import "Loader2"',
  'transforming (1830) src/components/CliPanel.tsx',
  '\x1b[33mwarning\x1b[0m: chunk index-BzK2.js is larger than 500 kB after minification',
  'rendering chunks (214)...',
  'computing gzip size (792)...',
  '\x1b[33mwarning\x1b[0m: 2 dynamic imports could not be resolved',
  'dist/index.html                    1.92 kB │ gzip:   0.81 kB',
  'dist/assets/index-BzK2.js        612.40 kB │ gzip: 188.11 kB',
  '\x1b[32m✓ built in 6.26s\x1b[0m',
  '',
].join('\r\n')

async function stubContext(context, theme, ptyInput) {
  await stubDashboardApi(context, {
    theme,
    // Seed the docked terminal open with one tab (the persisted shape a reload
    // restores); passed here because the stub's own init script clears storage.
    localStorageEntries: {
      'mc-bottom-terminal': JSON.stringify({
        open: true, height: 340, position: 'bottom',
        tabs: [{ id: 'fixture-tab-1' }], activeId: 'fixture-tab-1',
      }),
    },
    extra: async (path, route) => {
      // Create-slot must return a keyed slot object (a keyless one crashes the shell).
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
  // `open` is per window (sessionStorage).
  await context.addInitScript(() => sessionStorage.setItem('mc-bottom-terminal-open', '1'))
  // Registered AFTER the shared stub (which routes /api/ws): the last match wins.
  await context.routeWebSocket(/\/api\/ws\/terminal\//, ws => {
    ws.send(JSON.stringify({ type: 'ready' }))
    ws.send(Buffer.from(LOG + PROMPT))
    ws.onMessage(m => {
      if (typeof m === 'string') return // resize/control frames
      ptyInput.push(m.toString('utf8'))
      if (m.includes(0x06)) return // ^F: recorded, not echoed
      ws.send(Buffer.from(m.toString('utf8').replace(/\r/g, '\r\n')))
    })
  })
}

async function openPage(browser, base, theme, contextOpts = {}) {
  const context = await browser.newContext({ viewport: VIEWPORT, deviceScaleFactor: 2, ...contextOpts })
  /** Every binary frame the terminal sent to the PTY, decoded. */
  const ptyInput = []
  await stubContext(context, theme, ptyInput)
  const page = await context.newPage()
  logPageProblems(page)
  await page.goto(base + '/chat', { waitUntil: 'domcontentloaded' })
  const panel = page.getByTestId('bottom-terminal-panel')
  await panel.waitFor({ state: 'visible', timeout: 20000 })
  // Wait for the scripted log to land in the xterm buffer.
  await page.waitForFunction(() => {
    const rows = document.querySelector('[data-testid="bottom-terminal-panel"] .xterm-rows')
    return !!rows && rows.textContent.includes('built in')
  }, null, { timeout: 20000 })
  await page.waitForTimeout(400) // slide + fit settle
  return { context, page, panel, ptyInput }
}

async function expectCount(page, re, what) {
  const count = page.getByTestId('terminal-find-count')
  await page.waitForFunction(
    ([src, flags]) => new RegExp(src, flags).test(document.querySelector('[data-testid="terminal-find-count"]')?.textContent ?? ''),
    [re.source, re.flags], { timeout: 5000 },
  ).catch(() => {})
  const text = (await count.textContent()) ?? ''
  if (!re.test(text)) throw new Error(`${what}: find counter read ${JSON.stringify(text)}, expected ${re}`)
  return text
}

/** Clip to the docked terminal panel, which holds the find bar and the scrollback. */
async function shotPanel(page, panel, name) {
  await panel.scrollIntoViewIfNeeded()
  const box = await panel.boundingBox()
  if (!box) throw new Error('terminal panel not laid out')
  await page.screenshot({ path: `${OUT}/${name}.png`, clip: box })
}

async function captureTheme(browser, base, theme) {
  // ---- Find: open, step, no-match ----
  const { context, page, panel, ptyInput } = await openPage(browser, base, theme)
  const bar = page.getByTestId('terminal-find-bar')

  // ---- Bare Ctrl+F belongs to the shell: it reaches the PTY as ^F and opens
  //      nothing. ----
  await panel.locator('.xterm-helper-textarea').first().focus()
  await page.keyboard.press('Control+f')
  const gotCtrlF = await (async () => {
    const t0 = Date.now()
    while (Date.now() - t0 < 3000) {
      if (ptyInput.some(d => d.includes('\x06'))) return true
      await page.waitForTimeout(50)
    }
    return false
  })()
  if (!gotCtrlF) throw new Error('bare Ctrl+F did not reach the shell as ^F: ' + JSON.stringify(ptyInput))
  if (await bar.isVisible()) throw new Error('bare Ctrl+F opened the find bar; it must stay with the shell')

  // ---- Overflow menu: Find carries its Ctrl+Shift+F keycap, Fullscreen none. ----
  await page.getByRole('button', { name: 'More actions' }).click()
  const menu = page.getByRole('menu')
  await menu.waitFor({ state: 'visible', timeout: 5000 })
  const findCap = (await menu.getByTestId('terminal-find-shortcut').textContent()) ?? ''
  if (findCap !== 'Ctrl+Shift+F') throw new Error(`menu Find keycap read ${JSON.stringify(findCap)}`)
  await menu.getByTestId('terminal-menu-fullscreen').waitFor({ state: 'visible', timeout: 5000 })
  if (await menu.getByTestId('terminal-fullscreen-shortcut').count()) {
    throw new Error('menu Fullscreen shows a keycap; Linux/Windows have no fullscreen chord')
  }
  await page.waitForTimeout(250) // menu enter animation
  {
    const p = await panel.boundingBox()
    const m = await menu.boundingBox()
    if (!p || !m) throw new Error('panel or menu not laid out')
    const x = Math.min(p.x, m.x), y = Math.min(p.y, m.y) - 8
    const clip = { x, y, width: Math.max(p.x + p.width, m.x + m.width) - x, height: Math.max(p.y + p.height, m.y + m.height) - y }
    await page.screenshot({ path: `${OUT}/00-menu-${theme}.png`, clip })
  }
  await page.keyboard.press('Escape')
  await menu.waitFor({ state: 'detached', timeout: 5000 })

  // ---- Find: Ctrl+Shift+F in the focused terminal, then step and no-match ----
  await panel.locator('.xterm-helper-textarea').first().focus()
  await page.keyboard.press('Control+Shift+F')
  await bar.waitFor({ state: 'visible', timeout: 5000 })
  const input = bar.getByRole('textbox', { name: 'Search terminal scrollback' })
  // The hook focuses the input on the next frame, once it has mounted.
  const focused = await input.evaluate(el => new Promise(resolve => {
    const t0 = performance.now()
    const tick = () => (el === document.activeElement ? resolve(true)
      : performance.now() - t0 > 3000 ? resolve(false) : requestAnimationFrame(tick))
    tick()
  }))
  if (!focused) {
    throw new Error('find chord opened the bar but did not focus its input')
  }
  await input.type('warning')
  const first = await expectCount(page, /^[123] of 3$/, 'open')
  // The highlight decorations are painted into the xterm decoration layer, a
  // frame or two after the count reports.
  const painted = await page.waitForFunction(
    () => document.querySelectorAll('[data-testid="bottom-terminal-panel"] .xterm-decoration').length > 0,
    null, { timeout: 5000 },
  ).then(() => true, () => false)
  if (!painted) throw new Error('no match highlight decorations painted')
  await shotPanel(page, panel, `01-find-match-${theme}`)

  await input.press('Enter')
  const stepped = await expectCount(page, /^[123] of 3$/, 'after Enter')
  if (stepped === first) throw new Error(`Enter did not move the current match (still ${stepped})`)
  await shotPanel(page, panel, `02-find-step-${theme}`)
  await input.press('Shift+Enter')
  const back = await expectCount(page, /^[123] of 3$/, 'after Shift+Enter')
  if (back !== first) throw new Error(`Shift+Enter did not step back (${back}, expected ${first})`)
  await input.press('Enter') // leave it on the stepped match; not shot

  await input.fill('segfault')
  await expectCount(page, /^No results$/, 'no-match')
  await shotPanel(page, panel, `03-find-no-match-${theme}`)

  // ---- Fullscreen: close find (Escape returns focus to the shell), then pick
  //      the overflow menu's Fullscreen item (Linux/Windows have no chord), so
  //      the frame shows a plain maximized shell. ----
  await input.press('Escape')
  await bar.waitFor({ state: 'detached', timeout: 5000 })
  await page.getByRole('button', { name: 'More actions' }).click()
  await page.getByTestId('terminal-menu-fullscreen').click()
  await page.getByRole('menu').waitFor({ state: 'detached', timeout: 5000 })
  await page.waitForFunction(() => {
    const p = document.querySelector('[data-testid="bottom-terminal-panel"]')
    const main = document.getElementById('main-content')
    return !!p && p.classList.contains('inset-0') && p.classList.contains('absolute') && !!main && main.classList.contains('isolate')
  }, null, { timeout: 5000 }).catch(() => {})
  const state = await page.evaluate(() => {
    const p = document.querySelector('[data-testid="bottom-terminal-panel"]')
    const main = document.getElementById('main-content')
    return {
      panelClass: p?.className ?? '',
      mainIsolate: !!main?.classList.contains('isolate'),
      stored: sessionStorage.getItem('mc-bottom-terminal-fullscreen'),
    }
  })
  if (!/\babsolute\b/.test(state.panelClass) || !/\binset-0\b/.test(state.panelClass) || !state.mainIsolate || state.stored !== '1') {
    throw new Error('menu Fullscreen did not maximize the dock: ' + JSON.stringify(state))
  }
  await page.waitForTimeout(400) // refit after the size change
  const pbox = await panel.boundingBox()
  if (!pbox || pbox.height < VIEWPORT.height * 0.8) {
    throw new Error('fullscreen panel does not fill the content area: ' + JSON.stringify(pbox))
  }
  // The whole window, so the frame shows the panel covering the content area
  // under the top bar and beside the nav rail, with the visible Exit
  // fullscreen button in its header.
  const exitBtn = page.getByTestId('terminal-exit-fullscreen')
  await exitBtn.waitFor({ state: 'visible', timeout: 5000 })
  if ((await exitBtn.getAttribute('aria-label')) !== 'Exit fullscreen') {
    throw new Error('fullscreen header exit button is not labelled "Exit fullscreen"')
  }
  await page.screenshot({ path: `${OUT}/04-fullscreen-${theme}.png` })

  // ---- The overflow menu while fullscreen: its item now reads "Exit
  //      fullscreen". ----
  await page.getByRole('button', { name: 'More actions' }).click()
  const fsMenu = page.getByRole('menu')
  await fsMenu.waitFor({ state: 'visible', timeout: 5000 })
  const fsItem = (await fsMenu.getByTestId('terminal-menu-fullscreen').textContent()) ?? ''
  if (!fsItem.includes('Exit fullscreen')) throw new Error(`fullscreen menu item read ${JSON.stringify(fsItem)}`)
  await page.waitForTimeout(250) // menu enter animation
  await page.screenshot({ path: `${OUT}/05-fullscreen-menu-${theme}.png` })
  await page.keyboard.press('Escape')
  await fsMenu.waitFor({ state: 'detached', timeout: 5000 })

  // ---- The header button returns the panel to the dock at its dragged size. ----
  await exitBtn.click()
  await expectDocked(page)
  // Park the pointer off the header so no hover tooltip lands in the frame.
  await page.mouse.move(640, 300)
  await page.waitForTimeout(400) // refit after the size change
  await page.screenshot({ path: `${OUT}/06-exit-fullscreen-${theme}.png` })
  await context.close()
}

/** The panel is docked again: not absolutely positioned, flag cleared,
 *  `<main>` no longer isolated, and the exit button gone. */
async function expectDocked(page) {
  await page.waitForFunction(() => {
    const p = document.querySelector('[data-testid="bottom-terminal-panel"]')
    return !!p && !p.classList.contains('absolute')
  }, null, { timeout: 5000 }).catch(() => {})
  const docked = await page.evaluate(() => ({
    panelClass: document.querySelector('[data-testid="bottom-terminal-panel"]')?.className ?? '',
    mainIsolate: !!document.getElementById('main-content')?.classList.contains('isolate'),
    stored: sessionStorage.getItem('mc-bottom-terminal-fullscreen'),
    exitButton: !!document.querySelector('[data-testid="terminal-exit-fullscreen"]'),
  }))
  if (/\babsolute\b/.test(docked.panelClass) || docked.mainIsolate || docked.stored || docked.exitButton) {
    throw new Error('Exit fullscreen did not return the panel to the dock: ' + JSON.stringify(docked))
  }
}

/** A recording of the dock going fullscreen from the menu and back through the
 *  header's Exit fullscreen button (dark theme). */
async function recordToggle(browser, base) {
  const dir = `${OUT}/video-tmp`
  const { context, page } = await openPage(browser, base, 'dark', {
    recordVideo: { dir, size: VIEWPORT }, deviceScaleFactor: 1,
  })
  await page.waitForTimeout(800)
  await page.getByRole('button', { name: 'More actions' }).click()
  await page.waitForTimeout(600)
  await page.getByTestId('terminal-menu-fullscreen').click()
  const exitBtn = page.getByTestId('terminal-exit-fullscreen')
  await exitBtn.waitFor({ state: 'visible', timeout: 5000 })
  await page.waitForTimeout(1200)
  await exitBtn.hover()
  await page.waitForTimeout(800)
  await exitBtn.click()
  await expectDocked(page)
  await page.waitForTimeout(1200)
  const video = page.video()
  await context.close()
  await video.saveAs(`${OUT}/07-fullscreen-toggle-dark.webm`)
}

async function main() {
  const { srv, base } = await serveDist()
  const browser = await chromium.launch()
  try {
    for (const theme of ['dark', 'light']) await captureTheme(browser, base, theme)
    await recordToggle(browser, base)
  } finally {
    await browser.close()
    srv.close()
  }
  console.log(`wrote 14 screenshots and 1 recording to ${OUT}`)
}

main().catch(err => { console.error(err); process.exit(1) })
