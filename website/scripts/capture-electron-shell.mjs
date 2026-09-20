/**
 * Screenshot harness for the Electron shell: the application menu, its
 * accelerator captions, and the window chrome around them (#11737).
 *
 * WHY THIS IS NOT ANOTHER CHROMIUM CAPTURE SCRIPT
 *
 * Its ~546 siblings in this directory drive the web app in Chromium, so they can
 * only photograph pixels the renderer draws. Electron draws the menu bar, the
 * menu popups and the native frame outside any web page, which is why a change
 * to `electron/app-menu.js` had no route to rendered evidence at all: the
 * behaviour is pinned by `electron/test/app-menu.test.js`, and the picture the
 * UX Review lane asks for was unobtainable. This script launches real Electron
 * through Playwright's `_electron` driver and captures the X screen, so the menu
 * frame lands in the picture along with the window.
 *
 * WHAT MAKES THE PICTURE EVIDENCE
 *
 * The menu is not built here. It is read back out of the RUNNING app with
 * `Menu.getApplicationMenu()`, and `assertMenuCaption` refuses the run unless
 * the caption the caller declared is the caption the app actually holds. A shot
 * of a string this script chose would prove nothing, so this script is not
 * allowed to choose one.
 *
 * SCOPE
 *
 * Linux. The menu bar there is Chromium-drawn but Electron-owned, so it is
 * capturable on an ordinary Linux machine. macOS's menu bar belongs to the
 * system and no Linux harness can photograph it; `--platform=darwin` renders the
 * macOS menu TEMPLATE in a popup so its items and captions are reviewable, which
 * is a weaker claim and is labelled as such in the filename. Two things about
 * that shot are the Linux toolkit's rendering rather than macOS's: `CmdOrCtrl`
 * prints as "Ctrl" and `role:` labels interpolate the running binary's name
 * ("Hide Electron"). What it does show is which items exist and which of them
 * carry a chord at all. Window decorations come from the window manager, so a
 * bare X server with no WM shows the window undecorated - that is a property of
 * the display, not of the app.
 *
 * LOCAL ONLY, BY DECISION
 *
 * This is not wired into any CI lane and adds no CI dependency. It needs an X
 * display and a ~220MB Electron binary that `website` `npm ci` does not install,
 * and none of its ~546 siblings run in CI either. Making desktop-shell evidence
 * a required lane would change the contract for every pull request in the
 * repository, which is a maintainer's decision and not this harness's to take.
 *
 * USAGE
 *
 *   npm ci --prefix website/electron        # once: installs Electron
 *   xvfb-run -a --server-args='-screen 0 1600x1000x24' \
 *     node website/scripts/capture-electron-shell.mjs
 *
 *   OUT_DIR=<dir>          where the PNGs land (default /tmp/electron-shell-shots)
 *   ELECTRON_BINARY=<path> override the Electron binary
 *   --menu=<id>            top-level menu to open (default file-menu)
 *   --platform=darwin      build the macOS menu template instead
 *   --url=<path|url>       fill the window body (default: a flat backdrop, so a
 *                          diff of two runs reports only the chrome)
 *   --expect-item=<prefix> item whose caption must match (default "Settings")
 *   --expect-accelerator=<chord>            required caption, "" for none
 *   --expect-register-accelerator=<bool>    required registerAccelerator value
 */
import { _electron as electron } from 'playwright'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  assertMenuCaption,
  defaultCaptionExpectation,
  electronExecutable,
  WEBSITE_ROOT,
} from './lib/electron-shell-evidence.mjs'

const flags = new Map(
  process.argv
    .slice(2)
    .filter((a) => a.startsWith('--'))
    .map((a) => {
      const eq = a.indexOf('=')
      return eq === -1 ? [a.slice(2), 'true'] : [a.slice(2, eq), a.slice(eq + 1)]
    }),
)

const OUT = process.env.OUT_DIR || '/tmp/electron-shell-shots'
const SCREEN = { width: 1600, height: 1000 }
const platform = flags.get('platform') || process.platform
const macTemplate = platform === 'darwin'
const menuId = flags.get('menu') || defaultCaptionExpectation(platform).menuId

if (process.platform !== 'linux') {
  throw new Error(`this harness captures the X screen, so it runs on linux; this is ${process.platform}`)
}
if (!process.env.DISPLAY) {
  throw new Error('no DISPLAY. Run under xvfb-run, or export DISPLAY for a real X server.')
}

/** What the live menu must hold before any picture is taken. */
const defaults = defaultCaptionExpectation(platform)
const expectation = { ...defaults, menuId, itemLabelPrefix: flags.get('expect-item') || defaults.itemLabelPrefix }
if (flags.has('expect-accelerator')) expectation.accelerator = flags.get('expect-accelerator') || null
if (flags.has('expect-register-accelerator')) {
  expectation.registerAccelerator = flags.get('expect-register-accelerator') === 'true'
}

mkdirSync(OUT, { recursive: true })

const app = await electron.launch({
  executablePath: electronExecutable(),
  args: [
    join(WEBSITE_ROOT, 'scripts', 'lib', 'electron-shell-main.cjs'),
    // The agent sandbox denies the user namespace Chromium's zygote needs, and
    // /dev/shm is small in containers. Neither flag touches what is rendered.
    '--no-sandbox',
    '--disable-dev-shm-usage',
  ],
  env: {
    ...process.env,
    SHELL_HARNESS_PLATFORM: platform,
    ...(flags.has('url') ? { SHELL_HARNESS_URL: flags.get('url') } : {}),
    ELECTRON_DISABLE_SECURITY_WARNINGS: '1',
  },
})

const page = await app.firstWindow()
await page.waitForLoadState('domcontentloaded')
// The shipped splash animates. Freeze it so two runs of this harness produce
// byte-identical pictures and a real change is the only thing a diff shows. This
// is a harness-side stabiliser injected into the harness window; it edits no
// product code and touches nothing the menu draws.
await page.addStyleTag({
  content: '*, *::before, *::after { animation: none !important; transition: none !important; }',
})
if (macTemplate) {
  console.log(
    'note: --platform=darwin renders the macOS TEMPLATE on Linux. The items and which of them\n' +
      '      carry a chord are the real thing; the modifier NAMES are drawn by the Linux toolkit\n' +
      '      (CmdOrCtrl prints as "Ctrl", not "Cmd") and role labels interpolate the binary name.',
  )
}

/** The live application menu, flattened per top-level id. */
const menusById = await app.evaluate(({ Menu }) => {
  const out = {}
  for (const top of Menu.getApplicationMenu().items) {
    const id = top.id || top.label || '(unnamed)'
    out[id] = (top.submenu ? top.submenu.items : []).map((item) => ({
      label: item.label ?? '',
      accelerator: item.accelerator ?? null,
      // Electron defaults this to true when a template omits it; report the
      // effective value so the guard checks what the OS was told, not what the
      // template literally spelled.
      registerAccelerator: item.registerAccelerator ?? null,
    }))
  }
  return out
})

const row = assertMenuCaption(menusById, expectation)
console.log(
  `live menu OK: ${menuId} > ${row.label} accelerator=${JSON.stringify(row.accelerator)} ` +
    `registerAccelerator=${JSON.stringify(row.registerAccelerator)}`,
)

/** Capture the whole X screen: a menu popup is its own window, not page pixels. */
const captureScreen = () =>
  app.evaluate(async ({ desktopCapturer }, size) => {
    const sources = await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: size })
    if (!sources.length) throw new Error('desktopCapturer found no screen source')
    return sources[0].thumbnail.toPNG().toString('base64')
  }, SCREEN)

const shoot = async (name) => {
  const png = Buffer.from(await captureScreen(), 'base64')
  if (png.length === 0) throw new Error(`empty capture for ${name}`)
  const file = join(OUT, `${name}.png`)
  writeFileSync(file, png)
  console.log(`${name}.png (${png.length} bytes)`)
  return file
}

const suffix = macTemplate ? '-mac-template' : ''
await shoot(`electron-shell-window${suffix}`)

await app.evaluate(async ({ Menu, BrowserWindow }, id) => {
  const top = Menu.getApplicationMenu().items.find((i) => (i.id || i.label) === id)
  if (!top?.submenu) throw new Error(`no top-level menu ${id} to open`)
  top.submenu.popup({ window: BrowserWindow.getAllWindows()[0], x: 8, y: 4 })
  // The popup maps and paints asynchronously; without this the capture races it.
  await new Promise((r) => setTimeout(r, 1200))
}, menuId)
await shoot(`electron-shell-menu-${menuId}${suffix}`)

await app.close()
console.log('DONE', OUT)
