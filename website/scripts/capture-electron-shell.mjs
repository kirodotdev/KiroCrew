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
 * through Playwright's `_electron` driver and captures the screen, so the menu
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
 * WHAT KEEPS THE PICTURE TO THE HARNESS
 *
 * A whole screen has to be grabbed: a menu popup is its own window, so capturing
 * the app window alone would omit the very thing under test. So the screen that
 * gets grabbed must be one that holds nothing else, and two bounds enforce that.
 *
 *   - The harness starts its OWN Xvfb and lets XVFB bind the display number, which
 *     Xvfb reports back on `-displayfd`. So the number in use is one this harness's
 *     own child holds, not one this harness guessed was free. There is no flag to
 *     shoot anywhere else and `DISPLAY` from the environment is ignored, so no code
 *     path here can photograph a screen the harness did not create. With no Xvfb
 *     binary the run REFUSES rather than falling back to whatever display happens
 *     to be exported.
 *   - The capture is then narrowed to the window's own rectangle before anything
 *     is written (`cropRectForWindow`), and the window is raised topmost first.
 *     So even the harness's own screen contributes no pixel outside the window.
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
 * carry a chord at all. Window decorations come from the window manager, and the
 * harness's own Xvfb runs none, so the window is undecorated there -- that is a
 * property of the display, not of the app.
 *
 * LOCAL ONLY, BY DECISION
 *
 * This is not wired into any CI lane and adds no CI dependency. It needs an
 * Xvfb binary and a ~220MB Electron binary that `website` `npm ci` does not
 * install, and none of its ~546 siblings run in CI either. Making desktop-shell
 * evidence a required lane would change the contract for every pull request in
 * the repository, which is a maintainer's decision and not this harness's to
 * take.
 *
 * USAGE
 *
 *   npm ci --prefix website/electron        # once: installs Electron
 *   # an npm that gates lifecycle scripts (npm 12 by default) skips Electron's
 *   # postinstall, which is what actually downloads the binary. Then also run:
 *   node website/electron/node_modules/electron/install.js
 *   node website/scripts/capture-electron-shell.mjs
 *
 *   OUT_DIR=<dir>          where the PNGs land (default /tmp/electron-shell-shots)
 *   ELECTRON_BINARY=<path> override the Electron binary
 *   XVFB_BIN=<path>        override the Xvfb binary
 *   --menu=<id>            top-level menu to open (default file-menu)
 *   --platform=darwin      build the macOS menu template instead
 *   --url=<path|url>       fill the window body (default: a flat backdrop, so a
 *                          diff of two runs reports only the chrome)
 *   --expect-item=<prefix> item whose caption must match (default "Settings")
 *   --expect-accelerator=<chord>            required caption, "" for none
 *   --expect-register-accelerator=<bool>    required registerAccelerator value
 */
import { _electron as electron } from 'playwright'
import { spawn } from 'node:child_process'
import { mkdirSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import {
  assertMenuCaption,
  cropRectForWindow,
  defaultCaptionExpectation,
  electronExecutable,
  parseDisplayNumber,
  WEBSITE_ROOT,
  xvfbExecutable,
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
  throw new Error(`this harness captures an X screen, so it runs on linux; this is ${process.platform}`)
}

/** What the live menu must hold before any picture is taken. */
const defaults = defaultCaptionExpectation(platform)
const expectation = { ...defaults, menuId, itemLabelPrefix: flags.get('expect-item') || defaults.itemLabelPrefix }
if (flags.has('expect-accelerator')) expectation.accelerator = flags.get('expect-accelerator') || null
if (flags.has('expect-register-accelerator')) {
  expectation.registerAccelerator = flags.get('expect-register-accelerator') === 'true'
}

mkdirSync(OUT, { recursive: true })

/**
 * Start a private X server and return the child plus a stop function.
 *
 * `-displayfd` is the load-bearing part. The harness does NOT pick a display
 * number: it hands Xvfb a file descriptor, Xvfb binds a free number itself and
 * writes that number back, so the number the harness uses is by construction one
 * its OWN child holds. Choosing a number here by probing for a free X socket was a
 * check-then-act: two concurrent runs could choose the same number, the loser's
 * Xvfb would exit because the number was taken, and the loser's Electron would
 * then draw on the WINNER's screen -- where a crop to its own window rectangle can
 * include the peer's overlapping window.
 *
 * Arriving on that descriptor is also the readiness signal, so there is no socket
 * poll to race either.
 *
 * There is deliberately no parameter and no flag. A display this harness created
 * is the only one whose contents it can vouch for, so it is the only one it will
 * shoot on, and a run with no Xvfb binary refuses (see `xvfbExecutable`) rather
 * than falling back to a screen that may hold anything.
 */
function spawnDisplay() {
  const proc = spawn(
    xvfbExecutable(),
    ['-displayfd', '3', '-screen', `0`, `${SCREEN.width}x${SCREEN.height}x24`, '-nolisten', 'tcp'],
    // fd 3 is where Xvfb reports the number it bound. Its stderr is inherited so
    // a real X error is visible rather than swallowed.
    { stdio: ['ignore', 'ignore', 'inherit', 'pipe'], detached: false },
  )
  return { proc, stop: () => proc.kill('SIGTERM') }
}

/**
 * The display Xvfb bound, read off its `-displayfd`.
 *
 * Resolves only on a number from this child. An exit before the number arrives is
 * an error rather than a fallback, because a fallback here would be a guess about
 * which screen the picture is of.
 */
function displayFrom(proc) {
  return new Promise((resolve, reject) => {
    let buf = ''
    const timer = setTimeout(
      () => reject(new Error('Xvfb did not report a display number on -displayfd within 15s')),
      15_000,
    )
    const settle = (fn) => (arg) => {
      clearTimeout(timer)
      fn(arg)
    }
    proc.stdio[3].on('data', (chunk) => {
      buf += chunk.toString()
      if (buf.includes('\n')) {
        try {
          resolve(`:${parseDisplayNumber(buf)}`)
        } catch (e) {
          reject(e)
        } finally {
          clearTimeout(timer)
        }
      }
    })
    proc.on('exit', settle((code) => reject(new Error(`Xvfb exited with code ${code} before reporting a display`))))
    proc.on('error', settle((e) => reject(new Error(`Xvfb failed to start: ${e.message}`))))
  })
}

// Everything past this point is inside the try, so the server is stopped on any
// failure. It used to sit outside, and a display that never came up threw past the
// `finally` and left the Xvfb child running.
const { proc: xvfb, stop: stopDisplay } = spawnDisplay()

let app
try {
  const display = await displayFrom(xvfb)
  app = await electron.launch({
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
      DISPLAY: display,
      SHELL_HARNESS_PLATFORM: platform,
      ...(flags.has('url') ? { SHELL_HARNESS_URL: flags.get('url') } : {}),
      ELECTRON_DISABLE_SECURITY_WARNINGS: '1',
    },
  })

  const page = await app.firstWindow()
  await page.waitForLoadState('domcontentloaded')
  // The shipped splash animates. Freeze any animation in the body so two runs of
  // this harness produce byte-identical pictures and a real change is the only
  // thing a diff shows. This is a harness-side stabiliser injected into the
  // harness window; it edits no product code and touches nothing the menu draws.
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

  /** Screen geometry plus the window's rectangle, so the crop can be computed here. */
  const geometry = () =>
    app.evaluate(({ BrowserWindow, screen }) => {
      const win = BrowserWindow.getAllWindows()[0]
      const bounds = win.getBounds()
      return { displayBounds: screen.getDisplayMatching(bounds).bounds, windowBounds: bounds }
    })

  const shoot = async (name) => {
    const { displayBounds, windowBounds } = await geometry()
    const raw = await app.evaluate(async ({ desktopCapturer }, size) => {
      const sources = await desktopCapturer.getSources({ types: ['screen'], thumbnailSize: size })
      if (!sources.length) throw new Error('desktopCapturer found no screen source')
      const img = sources[0].thumbnail
      return { b64: img.toPNG().toString('base64'), size: img.getSize() }
    }, displayBounds)
    const rect = cropRectForWindow(displayBounds, raw.size, windowBounds)
    const png = await app.evaluate(
      async ({ nativeImage }, { b64, crop }) =>
        nativeImage.createFromBuffer(Buffer.from(b64, 'base64')).crop(crop).toPNG().toString('base64'),
      { b64: raw.b64, crop: rect },
    )
    const buf = Buffer.from(png, 'base64')
    if (buf.length === 0) throw new Error(`empty capture for ${name}`)
    const file = join(OUT, `${name}.png`)
    writeFileSync(file, buf)
    console.log(`${name}.png (${buf.length} bytes, ${rect.width}x${rect.height} cropped to the window)`)
    return file
  }

  const suffix = macTemplate ? '-mac-template' : ''
  await shoot(`electron-shell-window${suffix}`)

  await app.evaluate(async ({ Menu, BrowserWindow }, id) => {
    const win = BrowserWindow.getAllWindows()[0]
    const top = Menu.getApplicationMenu().items.find((i) => (i.id || i.label) === id)
    if (!top?.submenu) throw new Error(`no top-level menu ${id} to open`)
    // Wait on the menu's own show event rather than on a clock. A fixed sleep is a
    // guess about the machine: too short on a slow one ships a half-painted menu as
    // evidence, which is the failure this harness exists to prevent. The event says
    // the popup is up; the short settle after it covers the paint, and it stays
    // fixed so two runs still produce byte-identical files.
    const shown = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`menu ${id} never reported itself shown`)), 10000)
      top.submenu.once('menu-will-show', () => {
        clearTimeout(timer)
        resolve()
      })
    })
    top.submenu.popup({ window: win, x: 8, y: 4 })
    await shown
    await new Promise((r) => setTimeout(r, 400))
  }, menuId)
  await shoot(`electron-shell-menu-${menuId}${suffix}`)

  console.log('DONE', OUT)
} finally {
  if (app) await app.close()
  stopDisplay()
}
