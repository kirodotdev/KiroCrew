import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { describe, it, expect } from 'vitest'

import { keepInLibrary, libraryView } from '../pages/apps/useAppsData'
import type { LibrarySlot } from '../pages/apps/useAppsData'
import type { InstalledApp } from '../components/appstore/types'

/**
 * Library tab filter -- the page's own `keepInLibrary` predicate, imported.
 *
 * Every installed app is listed, including a disabled builtin -- except a hidden
 * app while it is disabled, which nothing else offers either. Hiding one used to
 * make a default-off builtin with no published catalog row unreachable in the UI:
 * Discover is built from the published catalog and defers to Library for what is
 * installed locally, so an app hidden here was in neither tab.
 */
type LibraryEntry = Pick<InstalledApp, 'origin' | 'enabled' | 'manifest'> & { name: string }

const overlayApp = (enabled: boolean): LibraryEntry => ({
  name: 'command-bar',
  enabled,
  origin: 'builtin',
  manifest: { ui: { overlays: [{ id: 'command-bar', replaces: 'quick-search' }] } },
} as LibraryEntry)

const plainBuiltin = (enabled: boolean): LibraryEntry => ({
  name: 'papyrus',
  enabled,
  origin: 'builtin',
} as LibraryEntry)

const installedApp = (enabled: boolean): LibraryEntry => ({
  name: 'oncall-watchtower',
  enabled,
  origin: 'registry',
} as LibraryEntry)

const hiddenBuiltin = (enabled: boolean): LibraryEntry => ({
  name: 'channels',
  enabled,
  origin: 'builtin',
  manifest: { hidden: true },
} as LibraryEntry)

const hiddenThirdParty = (enabled: boolean): LibraryEntry => ({
  name: 'sneaky',
  enabled,
  origin: 'registry',
  manifest: { hidden: true },
} as LibraryEntry)

describe('Library lists every installed app', () => {
  it('lists a disabled builtin that only adds a page', () => {
    // The regression this closes: a default-off builtin (AWS Control) whose
    // published catalog row does not exist yet was hidden here AND dropped from
    // Discover, leaving no UI anywhere to turn it on.
    expect(keepInLibrary(plainBuiltin(false))).toBe(true)
  })

  it('lists a disabled app that replaces a host surface', () => {
    // Previously the sole exception; now covered by the general rule, so
    // disabling a launcher to get the old surface back stays reversible without
    // a per-capability carve-out.
    expect(keepInLibrary(overlayApp(false))).toBe(true)
  })

  it('still withholds a hidden BUILTIN while it is disabled', () => {
    // `hidden` means the app is not offered to a reader at all, and Discover
    // drops it by name too -- `channels` and `workflows` both ship hidden AND
    // default-off, so listing them would announce apps nothing else mentions.
    expect(keepInLibrary(hiddenBuiltin(false))).toBe(false)
  })

  it('lists a hidden app once something has enabled it', () => {
    // Then the reader needs a surface to manage and turn it off, which is the
    // visibility the previous predicate already gave a hidden enabled app.
    expect(keepInLibrary(hiddenBuiltin(true))).toBe(true)
  })

  it('lists a disabled THIRD-PARTY app that declares itself hidden', () => {
    // `hidden` is only ours to honour on a manifest we shipped. Library is the
    // only surface carrying Enable and Uninstall, so honouring the flag on an
    // untrusted manifest would let an installed app conceal itself from the one
    // place it can be removed -- this PR's own failure, handed to a third party.
    expect(keepInLibrary(hiddenThirdParty(false))).toBe(true)
  })

  it('lists enabled and third-party apps unchanged', () => {
    expect(
      [overlayApp(true), plainBuiltin(true), installedApp(true), installedApp(false)]
        .filter(keepInLibrary).length,
    ).toBe(4)
  })
})

/**
 * `libraryView` holds each app's placement for the visit, so a toggle never moves
 * or deletes the row the reader just clicked. Exercised directly: the property is
 * about what a SECOND call does with the same map, which is what a re-render after
 * a toggle is.
 */
describe('libraryView holds a row in place across a toggle', () => {
  const named = (name: string, enabled: boolean, over: Partial<LibraryEntry> = {}): LibraryEntry => ({
    name, enabled, origin: 'builtin', ...over,
  } as LibraryEntry)

  it('orders enabled first on the first pass, ignoring arrival order', () => {
    const view = new Map<string, LibrarySlot>()
    const out = libraryView([named('zeta', false), named('alpha', true)], view)
    expect(out.map(a => a.name)).toEqual(['alpha', 'zeta'])
  })

  it('keeps a just-disabled row in its old position', () => {
    const view = new Map<string, LibrarySlot>()
    libraryView([named('alpha', true), named('zeta', true)], view)
    // The reader clicks Disable on alpha; the refetched list says enabled: false.
    // Read live, alpha would fall below the still-enabled zeta.
    const after = libraryView([named('alpha', false), named('zeta', true)], view)
    expect(after.map(a => a.name)).toEqual(['alpha', 'zeta'])
  })

  it('keeps a hidden builtin listed after the reader disables it', () => {
    // Otherwise the row deletes itself under the click and the switch is one-way
    // in the UI -- the failure this change exists to remove.
    const view = new Map<string, LibrarySlot>()
    const hidden = (enabled: boolean) => named('channels', enabled, { manifest: { hidden: true } })
    expect(libraryView([hidden(true)], view).map(a => a.name)).toEqual(['channels'])
    expect(libraryView([hidden(false)], view).map(a => a.name)).toEqual(['channels'])
  })

  it('promotes a concealed builtin if another surface enables it', () => {
    const view = new Map<string, LibrarySlot>()
    const hidden = (enabled: boolean) => named('channels', enabled, { manifest: { hidden: true } })
    const stillDisabled = named('zeta-off', false)
    expect(libraryView([stillDisabled, hidden(false)], view).map(a => a.name)).toEqual(['zeta-off'])
    // An enabled row first becoming visible belongs above the disabled group;
    // it must not inherit the classification from its concealed placeholder.
    expect(libraryView([stillDisabled, hidden(true)], view).map(a => a.name))
      .toEqual(['channels', 'zeta-off'])
  })

  it('re-decides on a fresh visit, re-concealing the hidden builtin', () => {
    // A new map is what a remount hands it, and concealment is the wheel's call.
    const hidden = named('channels', false, { manifest: { hidden: true } })
    expect(libraryView([hidden], new Map<string, LibrarySlot>())).toEqual([])
  })

  it('forgets an app once it is uninstalled', () => {
    const view = new Map<string, LibrarySlot>()
    libraryView([named('alpha', true)], view)
    libraryView([], view)
    expect(view.size).toBe(0)
  })
})

/**
 * The same predicate, fed the manifests we actually SHIP rather than fixtures.
 *
 * The cases above pin `keepInLibrary` itself, so they fail if someone rewrites the
 * predicate. They cannot fail for the other half of the same bug: a builtin that
 * becomes unreachable because its OWN manifest changed. `hidden` is the only field
 * that withholds a disabled builtin from Library, and Discover suppresses the same
 * name by design, so adding it to a manifest is what puts an app in neither tab --
 * the state AWS Control was reported to be in. Read from disk, in the same shape
 * the gateway serves (a builtin registers `origin: 'builtin'`, and default-off means
 * `enabled: false`), so either half of the invariant breaking fails a test.
 */
describe('every builtin we ship stays reachable while disabled', () => {
  /** Concealment is a product decision; this list is the record of it. */
  const SANCTIONED_HIDDEN = ['channels', 'workflows']

  const BUILTINS = resolve(__dirname, '../../../src/kiro_crew/apps/builtins')

  type Manifest = { name: string; hidden?: boolean }

  const shipped: Manifest[] = readdirSync(BUILTINS, { withFileTypes: true })
    .filter(e => e.isDirectory() && !e.name.startsWith('.') && !e.name.startsWith('_'))
    .map(e => join(BUILTINS, e.name, 'app.json'))
    .filter(existsSync)
    .map(f => JSON.parse(readFileSync(f, 'utf8')) as Manifest)

  /** A disabled builtin exactly as `GET /api/apps` reports one. */
  const asDisabled = (manifest: Manifest) =>
    ({ origin: 'builtin', enabled: false, manifest } as unknown as Parameters<typeof keepInLibrary>[0])

  it('reads the real manifests', () => {
    // Guards the walk itself: a bad path yields an empty list, and every
    // assertion below would then pass over nothing.
    expect(shipped.length).toBeGreaterThan(10)
  })

  it('lists every builtin that does not conceal itself, while disabled', () => {
    for (const manifest of shipped.filter(m => !m.hidden)) {
      expect(keepInLibrary(asDisabled(manifest)), `'${manifest.name}' is unreachable while disabled`)
        .toBe(true)
    }
  })

  it('lists aws-control, the app this invariant was written for', () => {
    // Named rather than left to the sweep: it ships default-off with no published
    // catalog row, which is the exact combination that had no surface at all.
    const awsControl = shipped.find(m => m.name === 'aws-control')
    expect(awsControl, 'aws-control no longer ships as a builtin').toBeDefined()
    expect(awsControl!.hidden, 'aws-control must not conceal itself').toBeFalsy()
    expect(keepInLibrary(asDisabled(awsControl!))).toBe(true)
  })

  it('holds the set of self-concealing builtins to the sanctioned two', () => {
    // Not a style rule. A new name here is an app reachable from no tab, so it
    // has to be chosen deliberately and land in this list with it.
    expect(shipped.filter(m => m.hidden).map(m => m.name).sort()).toEqual(SANCTIONED_HIDDEN)
  })

  it('withholds those two while they are disabled, from their real manifests', () => {
    // The contrast case, asserted over shipped bytes rather than a fixture: if
    // `hidden` stopped being honoured, the assertions above would still pass
    // (the flag is present either way) while concealment silently stopped
    // working. This is the half that reads the OUTPUT for those manifests.
    const concealing = shipped.filter(m => m.hidden)
    expect(concealing.map(m => m.name).sort()).toEqual(SANCTIONED_HIDDEN)
    for (const manifest of concealing) {
      expect(keepInLibrary(asDisabled(manifest)), `'${manifest.name}' should stay concealed while disabled`)
        .toBe(false)
    }
  })
})
