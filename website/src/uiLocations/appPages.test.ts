/**
 * App pages in the find_ui index: the packaged-manifest reader
 * (`scripts/lib/ui-index-apps.mjs`) and how `buildUiIndex` turns its pages
 * into `apppage.*` locations. Synthetic manifests only.
 */
import { describe, expect, it } from 'vitest'

import { appPagesFromManifests, buildUiIndex } from '../../scripts/lib/ui-index.mjs'
import { UI_CONDITIONS } from './conditions'

interface AppPage { id: string; app: string; appLabel: string; route: string; label: string; rel: string }
interface Loc {
  id: string; kind: string; label_key: string; alias_keys?: string[]; app?: string
  placements: { surface_id: string; route: string; entry_kind: string; requires: { kind: string; id?: string }[] }[]
}

const manifest = (body: Record<string, unknown>, rel = 'apps/builtins/x/app.json') => ({ rel, text: JSON.stringify(body) })
const read = (...ms: { rel: string; text: string }[]) => appPagesFromManifests(ms) as { pages: AppPage[]; errors: string[] }

const ROUTES = [
  { path: '/chat/:slug?', line: 1, catchAll: false, redirect: null },
  { path: '/:builtinApp/*', line: 2, catchAll: true, redirect: null },
  { path: '*', line: 3, catchAll: true, redirect: '/chat' },
]

function build(appPages: AppPage[], routes = ROUTES) {
  return buildUiIndex({
    surfaces: [{ navId: 'chat', route: '/chat', label: 'Sessions', labelKey: 'nav.chat', group: 'Main' }],
    extraPages: [],
    extraTitleKeys: {},
    appPages,
    capabilityTabs: [],
    settingsTabs: [],
    settingsSubs: {},
    settingsTabPreview: {},
    settingsEntries: [],
    agentSettings: [],
    descriptors: {},
    previewEnablers: {},
    markerSites: [],
    catalogs: { en: { 'nav.chat': 'Sessions' } },
    locales: ['en'],
    productName: 'Kiro Crew',
    routes,
    conditions: UI_CONDITIONS,
  }) as { index: { locations: Loc[]; labels: Record<string, Record<string, string>> }; errors: string[] }
}

describe('appPagesFromManifests', () => {
  it('reads each page: id from the manifest name and route, label, app name', () => {
    const { pages, errors } = read(manifest({
      name: 'cloud-desk',
      displayName: 'Cloud Desk',
      ui: { pages: [{ route: '/cloud-desk', label: 'Cloud' }] },
    }))
    expect(errors).toEqual([])
    expect(pages).toEqual([{
      id: 'apppage.cloud-desk.cloud-desk',
      app: 'cloud-desk',
      appLabel: 'Cloud Desk',
      route: '/cloud-desk',
      label: 'Cloud',
      rel: 'apps/builtins/x/app.json',
      labelKey: 'apps.cloudDesk.manifest.page_label',
      appLabelKey: 'apps.cloudDesk.manifest.display_name',
    }])
  })

  it('reports a malformed manifest or page and keeps the others', () => {
    const { pages, errors } = read(
      { rel: 'a/app.json', text: '{' },
      manifest({ name: 'Bad Name', ui: { pages: [{ route: '/b', label: 'B' }] } }, 'b/app.json'),
      manifest({ name: 'ok', ui: { pages: [{ route: 'no-slash', label: 'N' }, { route: '/ok', label: ' ' }, { route: '/ok2', label: 'Ok2' }] } }, 'c/app.json'),
    )
    expect(pages.map((p) => p.id)).toEqual(['apppage.ok.ok2'])
    // No displayName: the manifest name stands in for it.
    expect(pages[0].appLabel).toBe('ok')
    expect(errors).toHaveLength(4)
    expect(errors.join('\n')).toMatch(/a\/app\.json: not valid JSON/)
    expect(errors.join('\n')).toMatch(/b\/app\.json: 'name'/)
    expect(errors.join('\n')).toMatch(/ui\.pages\[0\]: 'route'/)
    expect(errors.join('\n')).toMatch(/ui\.pages\[1\]: 'label'/)
  })
})

describe('app pages in the index', () => {
  const pages = (body: Record<string, unknown>) => read(manifest(body)).pages

  it('emits a rail page gated on app_enabled that names its app', () => {
    const { index, errors } = build(pages({
      name: 'cloud-desk',
      displayName: 'Cloud Desk',
      ui: { pages: [{ route: '/cloud-desk', label: 'Cloud' }] },
    }))
    expect(errors).toEqual([])
    const loc = index.locations.find((l) => l.id === 'apppage.cloud-desk.cloud-desk')!
    expect(loc).toMatchObject({
      kind: 'page',
      label_key: 'literal:Cloud',
      alias_keys: ['literal:Cloud Desk'],
      app: 'Cloud Desk',
    })
    expect(loc.placements).toEqual([{
      surface_id: 'cloud-desk', route: '/cloud-desk', parent_ids: [], entry_kind: 'rail',
      requires: [{ kind: 'condition', id: 'app_enabled' }],
    }])
    expect(index.labels.en['literal:Cloud']).toBe('Cloud')
  })

  it('carries no alias when the page label is the app name', () => {
    const { index } = build(pages({ name: 'desk', displayName: 'Desk', ui: { pages: [{ route: '/desk', label: 'Desk' }] } }))
    expect(index.locations.find((l) => l.id === 'apppage.desk.desk')!.alias_keys).toBeUndefined()
  })

  it('leaves a page whose route a core page already has to that page', () => {
    const { index, errors } = build(pages({ name: 'sessions-app', ui: { pages: [{ route: '/chat', label: 'Chat clone' }] } }))
    expect(errors).toEqual([])
    expect(index.locations.some((l) => l.id.startsWith('apppage.'))).toBe(false)
  })

  it('refuses a route a core route draws, or a route table with no built-in-app arm', () => {
    expect(build(pages({ name: 'x', ui: { pages: [{ route: '/chat/inner', label: 'X' }] } })).errors.join('\n'))
      .toMatch(/lands on the dashboard's own '\/chat\/:slug\?' route/)
    expect(build(pages({ name: 'x', ui: { pages: [{ route: '/x', label: 'X' }] } }), ROUTES.filter((r) => r.path !== '/:builtinApp/*')).errors.join('\n'))
      .toMatch(/no '\/:builtinApp\/\*' arm/)
  })
})
