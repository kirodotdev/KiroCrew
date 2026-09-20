/**
 * The parts of the Electron-shell screenshot harness that need no display
 * server: the evidence guard, and locating the Electron binary.
 *
 * Why the guard exists. A screenshot is only evidence if the thing in the
 * picture is the thing the code produces. `capture-electron-shell.mjs` reads the
 * menu back out of the RUNNING app (`Menu.getApplicationMenu()`), hands the rows
 * to `assertMenuCaption` here, and takes no picture unless they match what the
 * caller declared. So a caption the harness invented, or one left over from an
 * older build, fails the run instead of being photographed.
 *
 * The guard is a separate module from the harness main process because that main
 * process imports `electron` and therefore cannot be unit-tested. This file can:
 * see `src/test/electronShellEvidence.test.ts`.
 */
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = dirname(fileURLToPath(import.meta.url))
/** website/ - two levels up from website/scripts/lib/. */
export const WEBSITE_ROOT = resolve(HERE, '..', '..')

/**
 * One row of a live Electron application menu, flattened by the harness main
 * process. `registerAccelerator` is included because on Windows and Linux it is
 * the difference between a caption and a system-wide key binding, and that
 * difference is invisible in a screenshot.
 *
 * @typedef {{label: string, accelerator: string|null, registerAccelerator: boolean|null}} MenuRow
 */

/**
 * What the caller says the menu must contain before a picture is worth taking.
 *
 * `accelerator` and `registerAccelerator` are each optional: omit one and the
 * guard does not constrain it. `itemLabelPrefix` matches on a prefix because
 * menu labels carry trailing ellipses that differ by platform convention
 * ("Settings...").
 *
 * @typedef {{menuId: string, itemLabelPrefix: string,
 *            accelerator?: string, registerAccelerator?: boolean}} CaptionExpectation
 */

/**
 * Refuse the run unless the live menu carries the declared caption.
 *
 * Throws rather than returning a verdict: every caller's only correct response
 * to a drifted caption is to stop, so a boolean would just be re-thrown at each
 * call site.
 *
 * @param {Record<string, MenuRow[]>} menusById rows per top-level menu id
 * @param {CaptionExpectation} expectation
 * @returns {MenuRow} the row that satisfied the expectation
 */
export function assertMenuCaption(menusById, expectation) {
  const { menuId, itemLabelPrefix } = expectation
  const rows = menusById?.[menuId]
  if (!Array.isArray(rows)) {
    const known = Object.keys(menusById || {}).join(', ') || '(none)'
    throw new Error(`no top-level menu with id ${JSON.stringify(menuId)}; the app menu has: ${known}`)
  }
  const row = rows.find((r) => typeof r?.label === 'string' && r.label.startsWith(itemLabelPrefix))
  if (!row) {
    const labels = rows.map((r) => r?.label).filter(Boolean).join(' | ') || '(no labelled rows)'
    throw new Error(
      `menu ${menuId} has no item whose label starts with ${JSON.stringify(itemLabelPrefix)}; it has: ${labels}`,
    )
  }
  if ('accelerator' in expectation && row.accelerator !== expectation.accelerator) {
    throw new Error(
      `expected ${menuId} > ${row.label} to show accelerator ${JSON.stringify(expectation.accelerator)}, ` +
        `the running app shows ${JSON.stringify(row.accelerator)}`,
    )
  }
  if ('registerAccelerator' in expectation && row.registerAccelerator !== expectation.registerAccelerator) {
    throw new Error(
      `expected ${menuId} > ${row.label} to have registerAccelerator ` +
        `${JSON.stringify(expectation.registerAccelerator)}, the running app has ` +
        `${JSON.stringify(row.registerAccelerator)}`,
    )
  }
  return row
}

/**
 * The caption the harness checks when the caller declares nothing.
 *
 * It lives here rather than in the driver so the Vitest suite can assert that the
 * REAL `electron/app-menu.js` template still satisfies it. Without that, the
 * default could drift away from the product and the harness would refuse every
 * run with a message blaming the app.
 *
 * `Settings...` is the default subject because it is the item whose caption is
 * load-bearing: off macOS it is a caption with no binding behind it
 * (`registerAccelerator: false`, #9824), and a picture cannot show the
 * difference between that and a real accelerator.
 *
 * @param {string} platform a process.platform value
 * @returns {CaptionExpectation}
 */
export function defaultCaptionExpectation(platform) {
  return platform === 'darwin'
    ? { menuId: 'app-menu', itemLabelPrefix: 'Settings', accelerator: 'CmdOrCtrl+,', registerAccelerator: true }
    : { menuId: 'file-menu', itemLabelPrefix: 'Settings', accelerator: 'Alt+,', registerAccelerator: false }
}

/**
 * Absolute path to an `Xvfb` binary, or an error naming what to install.
 *
 * The harness starts its OWN X server rather than drawing on whatever `DISPLAY`
 * happens to be exported, and there is no flag to opt out of that. The reason is
 * the evidence itself: the capture has to take a whole screen, because a menu
 * popup is its own window, so the screen it takes must be one that holds nothing
 * else. A developer's real desktop can hold anything. Hence the refusal here is
 * the fail-closed half of that decision: no Xvfb means no run, never a fallback
 * to an existing display.
 *
 * @param {string} [override] value of XVFB_BIN, if set
 * @param {string} [pathEnv] value of PATH, split on ':'
 * @returns {string}
 */
export function xvfbExecutable(override = process.env.XVFB_BIN, pathEnv = process.env.PATH || '') {
  if (override) {
    if (!existsSync(override)) throw new Error(`XVFB_BIN is set to ${override}, which does not exist`)
    return override
  }
  const found = pathEnv
    .split(':')
    .filter(Boolean)
    .map((dir) => join(dir, 'Xvfb'))
    .find(existsSync)
  if (!found) {
    throw new Error(
      'no Xvfb on PATH. The harness needs its own X server so the capture cannot contain\n' +
        'anything but the harness window, and it will not shoot on an existing display instead.\n' +
        'Install it (Debian/Ubuntu: xvfb, Fedora/Amazon Linux: xorg-x11-server-Xvfb), or point\n' +
        'XVFB_BIN at a binary.',
    )
  }
  return found
}

/**
 * The display number Xvfb reported on its `-displayfd`.
 *
 * Xvfb binds a free number itself and writes it back, so this parses a fact rather
 * than choosing one. That is the point: choosing a number from a filesystem probe
 * is a check-then-act, and two concurrent runs can choose the same one.
 *
 * Garbage or an empty report is an error, never a default. A default here would be
 * a guess about which screen the picture is of.
 *
 * @param {string} reported bytes read off the descriptor
 * @returns {number}
 */
export function parseDisplayNumber(reported) {
  const first = String(reported).split("\n")[0].trim()
  if (!/^[0-9]+$/.test(first)) {
    throw new Error(
      'Xvfb reported no display number on -displayfd, so which screen the capture would be of is ' +
        `unknown; refusing. It wrote ${JSON.stringify(String(reported).slice(0, 60))}`,
    )
  }
  return Number(first)
}

/**
 * The rectangle of a screen capture that holds one window, and nothing else.
 *
 * This is what keeps the evidence to the harness's own pixels. `desktopCapturer`
 * can only capture a whole screen - a menu popup is its own window, so capturing
 * the app window alone would omit the very thing under test - so the capture is
 * narrowed afterwards to the window's own rectangle. Even on a shared display
 * that leaves the PNG carrying no pixels from anything beside the harness.
 *
 * Coordinates are translated from screen space to image space and clamped to the
 * image, because a window can be partly offscreen and the captured image can be a
 * different pixel size from the display's DIP bounds.
 *
 * @param {{x:number,y:number,width:number,height:number}} displayBounds
 * @param {{width:number,height:number}} imageSize
 * @param {{x:number,y:number,width:number,height:number}} windowBounds
 * @returns {{x:number,y:number,width:number,height:number}}
 */
export function cropRectForWindow(displayBounds, imageSize, windowBounds) {
  const sx = displayBounds.width > 0 ? imageSize.width / displayBounds.width : 1
  const sy = displayBounds.height > 0 ? imageSize.height / displayBounds.height : 1
  const left = Math.round((windowBounds.x - displayBounds.x) * sx)
  const top = Math.round((windowBounds.y - displayBounds.y) * sy)
  const x = Math.max(0, Math.min(left, imageSize.width))
  const y = Math.max(0, Math.min(top, imageSize.height))
  const width = Math.max(1, Math.min(Math.round(windowBounds.width * sx) + (left - x), imageSize.width - x))
  const height = Math.max(1, Math.min(Math.round(windowBounds.height * sy) + (top - y), imageSize.height - y))
  return { x, y, width, height }
}

/**
 * Absolute path to the Electron binary, or an error naming the command that
 * installs it.
 *
 * `website/npm ci` does not install it: Electron is a devDependency of the
 * nested `website/electron` package, and its ~220MB binary arrives through that
 * package's own install script. So a contributor who has only ever run the
 * website install has no Electron at all, and the useful failure names the fix
 * rather than reporting a missing file.
 *
 * @param {string} [override] value of ELECTRON_BINARY, if set
 * @returns {string}
 */
export function electronExecutable(override = process.env.ELECTRON_BINARY) {
  if (override) {
    if (!existsSync(override)) throw new Error(`ELECTRON_BINARY is set to ${override}, which does not exist`)
    return override
  }
  const pkgDir = join(WEBSITE_ROOT, 'electron', 'node_modules', 'electron')
  // path.txt holds the binary's name relative to the package's dist/, which is
  // how the electron package's own index.js resolves it ("electron" on Linux,
  // "Electron.app/Contents/MacOS/Electron" on macOS).
  const pathTxt = join(pkgDir, 'path.txt')
  const relative = existsSync(pathTxt) ? readFileSync(pathTxt, 'utf8').trim() : 'electron'
  const binary = join(pkgDir, 'dist', relative)
  if (!existsSync(binary)) {
    throw new Error(
      `no Electron binary at ${binary}. Install it with:\n` +
        `  npm ci --prefix website/electron\n` +
        `and, if that skipped the download, node website/electron/node_modules/electron/install.js\n` +
        `Or point ELECTRON_BINARY at an existing binary.`,
    )
  }
  return binary
}
