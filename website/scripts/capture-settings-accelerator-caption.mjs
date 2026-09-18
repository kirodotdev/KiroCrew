/**
 * Screenshot harness for the Settings accelerator caption (#9824).
 *
 * On Windows the application menu is DOM, not a native frame: WindowsTitlebarMenu
 * draws the submenu itself rather than calling Menu.popup(), because a native
 * popup captures window input there. So the caption a Windows user reads on
 * File > Settings... is renderable in Chromium, and this script shoots it.
 *
 * The item list is NOT hand-written. It is built by the real
 * electron/app-menu.js `buildMenuTemplate`, then handed to the component through
 * the same `getAppMenuItems` shape the preload bridge uses, so only the IPC
 * transport is faked. The script asserts the template's Settings item before
 * taking any picture, and exits non-zero if it drifts - a screenshot of a
 * hand-picked string would prove nothing.
 *
 * Usage: DIST=<dist dir> OUT_DIR=<out dir> node scripts/capture-settings-accelerator-caption.mjs
 */
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import { serveDist, DEFAULT_DIST } from './lib/serve-dist.mjs'
import { stubDashboardApi } from './lib/stub-dashboard-api.mjs'
import { chromiumExecutable } from './lib/chromium-executable.mjs'

const require = createRequire(import.meta.url)
const { buildMenuTemplate } = require('../electron/app-menu.js')

const OUT = process.env.OUT_DIR || '/tmp/settings-accelerator-shots'
mkdirSync(OUT, { recursive: true })

// Every dep is a no-op: the template's shape is what is under test, not what
// its click handlers do.
const noop = () => {}
const deps = (isMac) => ({
  isMac,
  appName: 'Kiro Crew',
  openSettings: noop,
  openAbout: noop,
  reload: noop,
  forceReload: noop,
  toggleDevTools: noop,
  zoomActualSize: noop,
  zoomIn: noop,
  zoomOut: noop,
  alwaysOnTop: false,
  toggleAlwaysOnTop: noop,
  openNewSessionWindow: noop,
  openNewConnectionWindow: noop,
  renameCurrentWindow: noop,
  promptRemoteHost: noop,
  refreshToken: noop,
  openConfigFile: noop,
})

/** The template is a nested tree; the File submenu is the one holding Settings. */
function findSubmenuWithSettings(template) {
  for (const top of template) {
    const items = top?.submenu
    if (Array.isArray(items) && items.some(i => typeof i?.label === 'string' && i.label.startsWith('Settings'))) {
      return items
    }
  }
  throw new Error('no submenu containing a Settings item')
}

/** Electron template -> the AppMenuItem rows the preload bridge sends. */
const toRows = (items) => items.map((item, index) => ({
  type: item.type === 'separator' ? 'separator' : (item.type || 'normal'),
  index,
  label: item.label ?? '',
  accelerator: item.accelerator ?? '',
  enabled: item.enabled !== false,
  checked: item.checked === true,
}))

const winItems = findSubmenuWithSettings(buildMenuTemplate(deps(false)))
const settings = winItems.find(i => typeof i.label === 'string' && i.label.startsWith('Settings'))

// Guard the evidence: shoot only what the template actually produces.
if (settings.accelerator !== 'Alt+,') {
  throw new Error(`expected the off-macOS Settings caption to be "Alt+," , got ${JSON.stringify(settings.accelerator)}`)
}
if (settings.registerAccelerator !== false) {
  throw new Error('off-macOS Settings must be display-only (registerAccelerator false)')
}
console.log(`template OK: Settings accelerator=${settings.accelerator} registerAccelerator=${settings.registerAccelerator}`)

const rows = toRows(winItems)

// The chat shell needs at least one slot. With an empty list the app leaves the
// chat route a couple of seconds in and the whole <header> unmounts, taking the
// titlebar rail with it - which reads as the menu refusing to open.
const SLOTS = [
  { key: 'chat-1-a', title: 'Settings accelerator', running: false, messages: 2, agent: 'kirocrew', last_ts: new Date().toISOString() },
]

const { srv, base } = await serveDist(process.env.DIST || DEFAULT_DIST)
const browser = await chromium.launch({ executablePath: chromiumExecutable() })

for (const theme of ['light', 'dark']) {
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 })
  const page = await ctx.newPage()
  // Windows Electron host: App.tsx renders WindowsTitlebarMenu only when
  // isElectron and platform === 'win32'. The menu rows arrive over the same
  // getAppMenuItems call the preload bridge exposes.
  //
  // The rest of the bridge has to be present too, or the app shell throws before
  // the titlebar ever renders: App.tsx calls `electronAPI.onNavigate` without
  // optional chaining, and useGlobalHotkey reads an accelerator string off
  // getGlobalHotkey() and calls .startsWith on it. A partial bridge fails as an
  // ErrorBoundary crash with an empty header, which looks like the menu refusing
  // to open rather than like a missing stub.
  await page.addInitScript(([menuRows]) => {
    window.kirocrew = { isElectron: true, platform: 'win32' }
    window.electronAPI = {
      getAppMenuItems: async (id) => (id === 'file-menu' ? menuRows : []),
      executeAppMenuItem: () => {},
      onNavigate: () => () => {},
      getGlobalHotkey: async () => ({ accelerator: 'Alt+Shift+K', default: 'Alt+Shift+K' }),
      setDevMode: () => {},
      reportMicDenied: () => {},
      clearPaneHttpCache: async () => {},
    }
  }, [rows])
  await stubDashboardApi(page, { slots: SLOTS, folders: [], theme, localStorageEntries: { 'mc-lang': 'en' } })

  await page.goto(`${base}/chat`)
  // The titlebar rail re-renders while the chat page settles, fast enough that
  // Playwright's actionability check never sees a stable node. Drive it with
  // direct DOM clicks instead: the component's handlers are ordinary onClick,
  // so a dispatched click is the same input path a user's click takes.
  //
  // Scope the selector to the rail: `aria-label="Open menu"` alone also matches
  // a control outside the titlebar, and clicking that one does nothing here.
  const HAMBURGER = '.win-titlebar-menu-item[aria-label="Open menu"]'
  await page.waitForSelector(HAMBURGER, { timeout: 20000 })
  await page.waitForTimeout(2500)
  await page.evaluate((sel) => {
    document.querySelector(sel)?.dispatchEvent(
      new MouseEvent('click', { bubbles: true }),
    )
  }, HAMBURGER)
  // The rail expands its labels with a transition; wait it out, then hover and
  // click File (the component switches submenus on hover).
  await page.getByRole('menuitem', { name: /Settings/ }).waitFor({ timeout: 15000 })
  await page.waitForTimeout(600)

  // The popup is positioned at the titlebar's BOTTOM edge, so it sits outside
  // the <header> box - screenshotting the header alone crops the very row this
  // picture exists to show. Clip a region covering the rail and the open menu.
  await page.screenshot({
    path: `${OUT}/windows-file-menu-settings-${theme}.png`,
    clip: { x: 0, y: 0, width: 620, height: 340 },
  })
  console.log(`windows-file-menu-settings-${theme}.png`)
  await ctx.close()
}

await browser.close()
srv.close()
console.log('DONE', OUT)
