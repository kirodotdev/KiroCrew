/**
 * Goldens for the `apps` area (Discover, Library and an app's detail page): the
 * exact parent chain, entry, route, label key and requirements each location
 * carries in the committed index.
 */
import { describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'

interface Req { kind: string; value?: string; location?: string; when?: string; id?: string }
interface Placement { surface_id: string; route: string; parent_ids: string[]; entry_kind: string; requires: Req[] }
interface Loc { id: string; kind: string; label_key: string; alias_keys?: string[]; placements: Placement[] }

const INDEX_FILE = path.resolve(__dirname, '../../../src/kiro_crew/docs/ui-index.generated.json')
const index = JSON.parse(fs.readFileSync(INDEX_FILE, 'utf-8')) as { locations: Loc[] }
const byId = new Map(index.locations.map(l => [l.id, l]))
const src = (rel: string) => fs.readFileSync(path.resolve(__dirname, rel), 'utf-8')
const DISCOVER_SRC = src('../pages/apps/DiscoverPage.tsx')
const LIBRARY_SRC = src('../pages/apps/LibraryPage.tsx')
const DETAIL_SRC = src('../pages/AppDetailPage.tsx')
const TILE_SRC = src('../pages/apps/LaunchpadTile.tsx')
const SOURCES_SRC = src('../components/appstore/SourcesPopover.tsx')
const REGISTRY_SRC = src('../components/RegistryManager.tsx')

// Discover opens an app's details from its card; Library only from the card's
// ⋯ menu -> Details (a Library card click LAUNCHES an app that has a page).
const DETAIL_OPEN: Req = { kind: 'condition', id: 'app_details_open' }
const TILE_MENU: Req = { kind: 'condition', id: 'app_tile_menu_open' }
const openApp = (id: string): Req[] => [DETAIL_OPEN, { kind: 'condition', id }]
const viaDetails = (id: string): Req[] => [TILE_MENU, { kind: 'condition', id }]

type Expect = {
  kind: string; labelKey: string; aliasKeys?: string[]; source: string
  placement: { surface: string; route: string; parents: string[]; entry: string; requires: Req[] }
}
const library = (requires: Req[]) => ({
  surface: 'apps-library', route: '/apps/library', parents: ['page.apps-library', 'apps.library.tile-details'], entry: 'content', requires,
})

const EXPECTED: Record<string, Expect> = {
  'apps.refresh-store': {
    kind: 'button', labelKey: 'pages.appsPage.refresh_store', source: DISCOVER_SRC,
    placement: { surface: 'apps', route: '/apps', parents: ['page.apps'], entry: 'toolbar', requires: [] },
  },
  // The Discover header's Sources gear, and the Add Registry button in its popover.
  'apps.sources': {
    kind: 'button', labelKey: 'components.appstore.sourcesPopover.manage_app_sources', source: SOURCES_SRC,
    placement: { surface: 'apps', route: '/apps', parents: ['page.apps'], entry: 'toolbar', requires: [] },
  },
  'apps.sources.add-registry': {
    kind: 'button', labelKey: 'components.registryManager.add_registry', source: REGISTRY_SRC,
    placement: { surface: 'apps', route: '/apps', parents: ['page.apps', 'apps.sources'], entry: 'menu', requires: [] },
  },
  'apps.detail.install': {
    kind: 'button', labelKey: 'pages.appDetailPage.install', source: DETAIL_SRC,
    placement: { surface: 'apps', route: '/apps', parents: ['page.apps'], entry: 'content', requires: openApp('open_app_not_installed') },
  },
  // The card grid a guide's "choose the app" step points at, and the card
  // menu's Uninstall item reached from it.
  'apps.library.app-list': {
    kind: 'list', labelKey: 'pages.libraryPage.installed_apps', source: LIBRARY_SRC,
    placement: { surface: 'apps-library', route: '/apps/library', parents: ['page.apps-library'], entry: 'content', requires: [] },
  },
  'apps.library.tile-uninstall': {
    kind: 'menu-item', labelKey: 'components.appstore.installedAppCard.uninstall', source: TILE_SRC,
    placement: { surface: 'apps-library', route: '/apps/library', parents: ['page.apps-library'], entry: 'menu', requires: [TILE_MENU] },
  },
  'apps.library.tile-details': {
    kind: 'menu-item', labelKey: 'pages.libraryPage.tile_details', source: TILE_SRC,
    placement: { surface: 'apps-library', route: '/apps/library', parents: ['page.apps-library'], entry: 'menu', requires: [TILE_MENU] },
  },
  'apps.library.tile-disable': {
    kind: 'menu-item', labelKey: 'components.appstore.installedAppCard.disable', source: TILE_SRC,
    placement: { surface: 'apps-library', route: '/apps/library', parents: ['page.apps-library'], entry: 'menu', requires: [TILE_MENU] },
  },
  'apps.detail.disable': {
    kind: 'button', labelKey: 'pages.appDetailPage.disable', source: DETAIL_SRC, placement: library(viaDetails('open_app_enabled')),
  },
  'apps.detail.sync': {
    kind: 'button', labelKey: 'pages.appDetailPage.sync', source: DETAIL_SRC,
    aliasKeys: ['pages.appDetailPage.sync_app_from_its_source_directory'], placement: library(viaDetails('open_app_syncable')),
  },
  'apps.detail.update': {
    kind: 'button', labelKey: 'pages.appDetailPage.update', source: DETAIL_SRC, placement: library(viaDetails('open_app_update_available')),
  },
  'apps.detail.uninstall': {
    kind: 'button', labelKey: 'pages.appDetailPage.uninstall', source: DETAIL_SRC, placement: library(viaDetails('open_app_removable')),
  },
}

describe('apps area locations', () => {
  it('indexes exactly the expected apps.* ids', () => {
    const ids = index.locations.map(l => l.id).filter(id => id.startsWith('apps.')).sort()
    expect(ids).toEqual(Object.keys(EXPECTED).sort())
  })

  for (const [id, exp] of Object.entries(EXPECTED)) {
    it(`${id}: label, kind and its one placement`, () => {
      const loc = byId.get(id)
      expect(loc, id).toBeDefined()
      expect(loc!.kind).toBe(exp.kind)
      expect(loc!.label_key).toBe(exp.labelKey)
      expect(loc!.alias_keys ?? []).toEqual(exp.aliasKeys ?? [])
      expect(loc!.placements).toEqual([{
        surface_id: exp.placement.surface, route: exp.placement.route,
        parent_ids: exp.placement.parents, entry_kind: exp.placement.entry, requires: exp.placement.requires,
      }])
    })

    it(`${id}: exactly one marker in the apps render files`, () => {
      const count = (s: string) => s.split(`uiLocation('${id}')`).length - 1
      expect(count(exp.source)).toBe(1)
      expect(count(DISCOVER_SRC) + count(LIBRARY_SRC) + count(DETAIL_SRC) + count(TILE_SRC) + count(SOURCES_SRC) + count(REGISTRY_SRC)).toBe(1)
    })
  }

  it('marks the gateway-managed branch of the detail page, not the builtin or self-managed twins', () => {
    // Disable is drawn once for builtin apps and once for the rest; Uninstall
    // once for self-managed apps and once for the rest. One id per site.
    const disable = DETAIL_SRC.split('\n').filter(l => l.includes("handleAction('disable')"))
    expect(disable.length).toBe(2)
    expect(disable.filter(l => l.includes("uiLocation('apps.detail.disable')")).length).toBe(1)
    const uninstall = DETAIL_SRC.split('\n').filter(l => l.includes("handleAction('uninstall')"))
    expect(uninstall.length).toBe(2)
    expect(uninstall.filter(l => l.includes("uiLocation('apps.detail.uninstall')")).length).toBe(1)
    // Update is drawn once for self-managed apps and once for the rest.
    const update = DETAIL_SRC.split('\n').filter(l => /[{ ]app\.updateAvailable && <Btn/.test(l))
    expect(update.length).toBe(2)
    expect(update.filter(l => l.includes("uiLocation('apps.detail.update')")).length).toBe(1)
    const builtinBranch = DETAIL_SRC.slice(DETAIL_SRC.indexOf('app.installed && isBuiltin && ('), DETAIL_SRC.indexOf('app.installed && !isSelfManaged && !isBuiltin && ('))
    expect(builtinBranch).not.toContain('uiLocation(')
  })

  it("leaves the Library tile's crash-fallback buttons unmarked", () => {
    // LibraryPage's own Disable/Uninstall render only when a tile crashed; a
    // healthy tile's actions live in LaunchpadTile's menu.
    // The one marker there is the card grid (the picker), never a button.
    expect(LIBRARY_SRC.split('uiLocation(').length - 1).toBe(1)
    expect(LIBRARY_SRC).toContain("uiLocation('apps.library.app-list')")
  })
})
