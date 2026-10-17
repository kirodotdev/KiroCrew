/**
 * The evidence guard that decides whether the Electron-shell screenshot harness
 * is allowed to take a picture (#11737).
 *
 * The harness itself needs real Electron, an X display and a 220MB binary, so it
 * cannot run here. What CAN run here is the part that decides: given the menu
 * read back out of the running app, does it carry the caption the caller
 * declared? Everything in this file is about that decision, because a harness
 * that photographs whatever it finds produces pictures that prove nothing.
 *
 * The first test is also a drift pin between the harness and the product: it
 * builds the menu with the REAL `electron/app-menu.js` and asserts the harness's
 * built-in expectation still matches it. Without it, a deliberate caption change
 * in the product would leave the harness refusing every run with a message
 * blaming the app.
 */
import { describe, expect, it } from 'vitest'
import { createRequire } from 'node:module'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { tmpdir } from 'node:os'
import {
  assertMenuCaption,
  cropRectForWindow,
  defaultCaptionExpectation,
  electronExecutable,
  parseDisplayNumber,
  xvfbExecutable,
} from '../../scripts/lib/electron-shell-evidence.mjs'

const require = createRequire(import.meta.url)

/** An item inside a top-level submenu, as the template spells it. */
type TemplateItem = {
  label?: string
  accelerator?: string
  registerAccelerator?: boolean
  type?: string
  role?: string
}
/** A top-level entry of the menu template. */
type TemplateTop = { id?: string; label?: string; submenu?: TemplateItem[]; role?: string }
/** One row as the harness reports it after reading the live menu. */
type MenuRow = { label: string; accelerator: string | null; registerAccelerator: boolean }

/**
 * The inert dependency bag, required from the SAME module the harness main
 * process uses. Sharing it is the point: this test runs in CI and the harness
 * does not, so a second copy here would be the copy that stays correct while the
 * harness's silently rotted.
 */
const { menuDeps } = require('../../scripts/lib/electron-shell-menu-deps.cjs') as {
  menuDeps: (isMac: boolean) => Record<string, unknown>
}

const { buildMenuTemplate } = require('../../electron/app-menu.js') as {
  buildMenuTemplate: (deps: ReturnType<typeof menuDeps>) => TemplateTop[]
}

/**
 * The template as the LIVE menu reports it.
 *
 * `registerAccelerator` defaults to true for an item that omits it, which is
 * Electron's own default and what the harness observes on a real run: the macOS
 * template's `CmdOrCtrl+,` item reads back as `registerAccelerator=true` there.
 * So this mirrors the running app rather than the literal template text.
 *
 * One thing it cannot mirror: a `{ role: ... }` item carries no label in the
 * template, and Electron supplies one ("Minimize", "Quit") only when it builds
 * the menu. So rows for role-only items are blank here while the live menu labels
 * them. Every item this guard is asked about names itself in the template.
 */
function liveRows(isMac: boolean): Record<string, MenuRow[]> {
  const out: Record<string, MenuRow[]> = {}
  for (const top of buildMenuTemplate(menuDeps(isMac))) {
    const id = top.id || top.label || '(unnamed)'
    out[id] = (top.submenu ?? []).map((item) => ({
      label: item.label ?? '',
      accelerator: item.accelerator ?? null,
      registerAccelerator: item.registerAccelerator ?? true,
    }))
  }
  return out
}

describe('the harness default expectation tracks the real menu', () => {
  it('accepts the off-macOS Settings caption the product actually builds', () => {
    const row = assertMenuCaption(liveRows(false), defaultCaptionExpectation('linux'))
    expect(row.label).toMatch(/^Settings/)
    expect(row.accelerator).toBe('Alt+,')
    // The whole reason a caption needs a guard: off macOS this chord is DISPLAYED
    // and not registered, and a screenshot cannot show the difference.
    expect(row.registerAccelerator).toBe(false)
  })

  it('accepts the macOS Settings caption the product actually builds', () => {
    const row = assertMenuCaption(liveRows(true), defaultCaptionExpectation('darwin'))
    expect(row.accelerator).toBe('CmdOrCtrl+,')
    expect(row.registerAccelerator).toBe(true)
  })
})

describe('the guard refuses a drifted menu', () => {
  const rows = () => liveRows(false)

  it('refuses a caption that is not the declared one', () => {
    expect(() =>
      assertMenuCaption(rows(), { menuId: 'file-menu', itemLabelPrefix: 'Settings', accelerator: 'Ctrl+,' }),
    ).toThrow(/accelerator "Ctrl\+,".*shows "Alt\+,"/s)
  })

  it('refuses a displayed-only chord declared as a real binding', () => {
    expect(() =>
      assertMenuCaption(rows(), { menuId: 'file-menu', itemLabelPrefix: 'Settings', registerAccelerator: true }),
    ).toThrow(/registerAccelerator true.*has false/s)
  })

  it('refuses an unknown top-level menu, naming the ones the app has', () => {
    let message = ''
    try {
      assertMenuCaption(rows(), { menuId: 'app-menu', itemLabelPrefix: 'Settings' })
    } catch (e) {
      message = (e as Error).message
    }
    expect(message).toContain('no top-level menu with id "app-menu"')
    // Off macOS the app menu does not exist; the message must point at File.
    expect(message).toContain('file-menu')
  })

  it('refuses a missing item, listing the labels that are present', () => {
    let message = ''
    try {
      assertMenuCaption(rows(), { menuId: 'file-menu', itemLabelPrefix: 'Preferences' })
    } catch (e) {
      message = (e as Error).message
    }
    expect(message).toContain('no item whose label starts with "Preferences"')
    expect(message).toMatch(/Settings/)
  })

  it('leaves a field unconstrained when the caller omits it', () => {
    // Neither accelerator nor registerAccelerator declared: a caller shooting the
    // View menu should not have to state a chord it does not care about.
    const row = assertMenuCaption(rows(), { menuId: 'view-menu', itemLabelPrefix: 'Reload' })
    expect(row.label).toBe('Reload')
    expect(row.accelerator).toBe('CmdOrCtrl+R')
  })
})

describe('locating the Electron binary', () => {
  it('uses an explicit override when it exists', () => {
    expect(electronExecutable(process.execPath)).toBe(process.execPath)
  })

  it('refuses an override that does not exist, naming the path', () => {
    expect(() => electronExecutable('/nonexistent/electron-binary')).toThrow(
      /ELECTRON_BINARY is set to \/nonexistent\/electron-binary/,
    )
  })
})

describe('the display the capture happens on', () => {
  it('uses an explicit Xvfb override when it exists', () => {
    expect(xvfbExecutable(process.execPath)).toBe(process.execPath)
  })

  it('refuses an Xvfb override that does not exist', () => {
    expect(() => xvfbExecutable('/nonexistent/Xvfb')).toThrow(/XVFB_BIN is set to \/nonexistent\/Xvfb/)
  })

  it('finds Xvfb on PATH and prefers the earliest entry that has one', () => {
    const early = mkdtempSync(join(tmpdir(), 'xvfb-early-'))
    const late = mkdtempSync(join(tmpdir(), 'xvfb-late-'))
    try {
      writeFileSync(join(early, 'Xvfb'), '')
      writeFileSync(join(late, 'Xvfb'), '')
      expect(xvfbExecutable(undefined, `/nonexistent:${early}:${late}`)).toBe(join(early, 'Xvfb'))
    } finally {
      rmSync(early, { recursive: true, force: true })
      rmSync(late, { recursive: true, force: true })
    }
  })

  it('refuses when Xvfb is nowhere on PATH, and says how to get one', () => {
    let message = ''
    try {
      xvfbExecutable(undefined, '/nonexistent:/also-nonexistent')
    } catch (e) {
      message = (e as Error).message
    }
    expect(message).toContain('no Xvfb on PATH')
    // The whole point of owning the display: the shot cannot contain anything else.
    expect(message).toContain('cannot contain')
    // Fail closed. A message that offered an existing display as a way forward
    // would be describing the exact path this function exists to remove.
    expect(message).toContain('will not shoot on an existing display')
  })

  it('reads the display number Xvfb reported on its descriptor', () => {
    expect(parseDisplayNumber('7\n')).toBe(7)
    expect(parseDisplayNumber('103\n')).toBe(103)
    expect(parseDisplayNumber('  12  \nnoise\n')).toBe(12)
  })

  it('refuses a report that carries no number, rather than defaulting', () => {
    // A default here would be a guess about which screen the picture is of, which
    // is the one thing this whole path exists to make certain.
    for (const junk of ['', '\n', 'abc\n', ':9\n', '-1\n']) {
      expect(() => parseDisplayNumber(junk)).toThrow(/Xvfb reported no display number/)
    }
  })
  it('names the same setup commands the recipe does', () => {
    // The drift this pins: the recipe tells an author how to install Electron and
    // the refusal message tells them how to repair a half-install. A reader who
    // follows only the recipe has to end up with a working binary, so if the
    // message knows a step the recipe omits, the author hits the refusal. Read
    // rather than driven, because this worktree HAS the binary, so the throw the
    // message belongs to cannot be reached from here.
    const here = dirname(fileURLToPath(import.meta.url))
    const lib = readFileSync(join(here, '..', '..', 'scripts', 'lib', 'electron-shell-evidence.mjs'), 'utf8')
    const recipe = readFileSync(
      join(here, '..', '..', '..', 'docs', 'guides', 'worktree-verification-recipes.md'),
      'utf8',
    )
    for (const command of [
      'npm ci --prefix website/electron',
      'node website/electron/node_modules/electron/install.js',
    ]) {
      expect(lib).toContain(command)
      expect(recipe).toContain(command)
    }
  })
})

describe('the harness can only shoot on a screen it created', () => {
  // These are structural: they are the reason the whole-screen grab is safe to take
  // at all, and none is reachable from a unit test because they live in the
  // harness's launch path. So they are pinned against the harness source.
  const harness = readFileSync(
    join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'scripts', 'capture-electron-shell.mjs'),
    'utf8',
  )

  it('reads no display flag, so there is no way to aim it at a real desktop', () => {
    expect(harness).not.toMatch(/flags\.(get|has)\(\s*'display'\s*\)/)
  })

  it('overrides DISPLAY after the environment spread, so an exported one cannot win', () => {
    // Order is load-bearing: a later key wins in an object literal, so moving the
    // spread below this line would hand the run whatever DISPLAY was exported.
    expect(harness).toMatch(/\.\.\.process\.env,\s*\n\s*DISPLAY: display,/)
  })

  it('starts its own server from the resolved Xvfb binary', () => {
    expect(harness).toMatch(/spawn\(\s*\n?\s*xvfbExecutable\(\),/)
  })

  it('lets Xvfb bind the display number and report it, instead of picking one', () => {
    // The fix for the concurrent-run race: a number this harness chose could be
    // taken by a peer between the choice and the bind, and the loser would then
    // draw on the winner's screen.
    expect(harness).toMatch(/'-displayfd', '3',/)
    expect(harness).not.toMatch(/\.X11-unix/)
  })

  it('spawns the server inside the try, so a failed start still stops it', () => {
    // The Xvfb child is not detached and is not reaped on parent exit, so a throw
    // that skips the finally leaks a running X server. Line-anchored on purpose:
    // `displayFrom` has its own indented try/finally, and a plain substring search
    // finds that one instead.
    const spawnAt = harness.indexOf('const { proc: xvfb, stop: stopDisplay } = spawnDisplay()')
    const tryAt = harness.search(/^try \{$/m)
    const finallyAt = harness.search(/^\} finally \{$/m)
    expect(spawnAt).toBeGreaterThan(-1)
    expect(tryAt).toBeGreaterThan(spawnAt)
    expect(finallyAt).toBeGreaterThan(tryAt)
    expect(harness.slice(tryAt, finallyAt)).toContain('await displayFrom(xvfb)')
    expect(harness.slice(finallyAt)).toContain('stopDisplay()')
  })
})

describe('narrowing the capture to the harness window', () => {
  const display = { x: 0, y: 0, width: 1600, height: 1000 }

  it('keeps exactly the window rectangle when image and display agree', () => {
    expect(cropRectForWindow(display, { width: 1600, height: 1000 }, { x: 210, y: 140, width: 1180, height: 720 }))
      .toEqual({ x: 210, y: 140, width: 1180, height: 720 })
  })

  it('scales into image space when the capture is a different pixel size', () => {
    expect(cropRectForWindow(display, { width: 800, height: 500 }, { x: 200, y: 100, width: 400, height: 200 }))
      .toEqual({ x: 100, y: 50, width: 200, height: 100 })
  })

  it('translates by the display origin on a second monitor', () => {
    const second = { x: 1600, y: 0, width: 1600, height: 1000 }
    expect(cropRectForWindow(second, { width: 1600, height: 1000 }, { x: 1700, y: 40, width: 300, height: 200 }))
      .toEqual({ x: 100, y: 40, width: 300, height: 200 })
  })

  it('clamps a window that hangs off the left and top edges', () => {
    // A negative origin must not become a negative crop: nativeImage would
    // silently produce something other than the window, which is the whole thing
    // this rectangle exists to prevent.
    const rect = cropRectForWindow(display, { width: 1600, height: 1000 }, { x: -50, y: -30, width: 400, height: 300 })
    expect(rect.x).toBe(0)
    expect(rect.y).toBe(0)
    expect(rect).toEqual({ x: 0, y: 0, width: 350, height: 270 })
  })

  it('clamps a window that hangs off the right and bottom edges', () => {
    expect(cropRectForWindow(display, { width: 1600, height: 1000 }, { x: 1500, y: 900, width: 400, height: 300 }))
      .toEqual({ x: 1500, y: 900, width: 100, height: 100 })
  })

  it('never returns a zero-sized rectangle', () => {
    const rect = cropRectForWindow(display, { width: 1600, height: 1000 }, { x: 1600, y: 1000, width: 0, height: 0 })
    expect(rect.width).toBeGreaterThan(0)
    expect(rect.height).toBeGreaterThan(0)
  })
})
