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
import {
  assertMenuCaption,
  defaultCaptionExpectation,
  electronExecutable,
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

const { buildMenuTemplate } = require('../../electron/app-menu.js') as {
  buildMenuTemplate: (deps: ReturnType<typeof menuDeps>) => TemplateTop[]
}

/** Every callback the template destructures, inert: no click is ever dispatched. */
const noop = () => {}
const menuDeps = (isMac: boolean) => ({
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
