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
