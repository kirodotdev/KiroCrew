/**
 * The find_ui auto tier (`scripts/lib/ui-index.mjs`): which ONE page a source
 * file is drawn on, proven from the module graph of synthetic sources, and how
 * a mapped candidate becomes an `auto` location. Ranking (curated beats auto,
 * weak overlap never answers) is pinned on the query side, in
 * `test/test_find_ui_auto.py`.
 */
import { describe, expect, it } from 'vitest'
import * as fs from 'node:fs'
import * as path from 'node:path'

import {
  AUTO_ARTIFACT_NAME,
  buildAutoArtifact,
  buildPageMap,
  buildUiIndex,
  candidateKind,
  classifyUses,
  collectRouteElementSpans,
  collectTabPanels,
  makeRouteLabel,
  parseSource,
  scanCandidates,
  scanImports,
} from '../../scripts/lib/ui-index.mjs'
import { UI_CONDITIONS, UI_REVEAL_STATES } from './conditions'

type Edge = { to: string; binds: string[] }
type Mapped = { page: string; tab: string | null }
type Loc = {
  id: string; kind: string; tier?: string; label_key: string; alias_keys?: string[]; conditions_unknown?: boolean
  terms?: unknown; placements: { surface_id: string; route: string; parent_ids: string[]; entry_kind: string; requires: unknown[] }[]
}

const SOURCES: Record<string, string> = {
  'src/main.tsx': "import App from './App'\nimport './Boot'\n",
  'src/Boot.tsx': "import './BootBit'\n",
  'src/BootBit.tsx': 'export const x = 1\n',
  'src/App.tsx': `import ChatPage from './ChatPage'
import Shell from './Shell'
const SettingsPage = lazy(() => import('./SettingsPage'))
const Popout = lazy(() => import('./Popout'))
export default function App() {
  return <><Shell /><Routes>
    <Route path="/chat/:slug?" element={<ChatPage />} />
    <Route path="/settings/*" element={<Suspense><SettingsPage /></Suspense>} />
    <Route path="/popout/chat" element={<Popout />} />
  </Routes></>
}`,
  'src/ChatPage.tsx': `import { Shared } from './Shared'
import { ChatOnly } from './ChatOnly'
import { AppUsed } from './AppUsed'
import type { T } from './TypesOnly'
export default function ChatPage() { return <><Shared /><ChatOnly /><AppUsed /></> }`,
  'src/SettingsPage.tsx': `import { Shared } from './Shared'
import { ChatPanel } from './ChatPanel'
import { AboutPanel } from './AboutPanel'
import { Search } from './Search'
import { TypesOnly } from './TypesOnly'
export default function SettingsPage() {
  return <SidePanelLayout tabs={tabs} navTop={<Search />}>
    {tab => <>
      {tab === 'chat' && <ChatPanel />}
      {tab === 'about' && <><AboutPanel /><TypesOnly /></>}
      {tab === 'gone' && <Shared />}
    </>}
  </SidePanelLayout>
}`,
  'src/ChatPanel.tsx': "import { PanelBit } from './PanelBit'\nimport { ChatPanelBit } from './ChatPanelBit'\nimport { SearchBit } from './SearchBit'\n",
  'src/AboutPanel.tsx': "import { PanelBit } from './PanelBit'\n",
  'src/PanelBit.tsx': 'export const PanelBit = () => null\n',
  'src/ChatPanelBit.tsx': 'export const ChatPanelBit = () => null\n',
  'src/Search.tsx': "import { SearchBit } from './SearchBit'\nexport const Search = () => null\n",
  'src/SearchBit.tsx': 'export const SearchBit = () => null\n',
  'src/Shared.tsx': 'export const Shared = () => null\n',
  'src/ChatOnly.tsx': 'export const ChatOnly = () => null\n',
  'src/AppUsed.tsx': 'export const AppUsed = () => null\n',
  'src/TypesOnly.tsx': 'export type T = 1\nexport const TypesOnly = () => null\n',
  'src/Shell.tsx': "import { ShellBit } from './ShellBit'\n",
  'src/ShellBit.tsx': 'export const ShellBit = () => null\n',
  'src/Popout.tsx': "import ChatPage from './ChatPage'\n",
  'src/apps/x/Page.tsx': "import { AppUsed } from '../../AppUsed'\n",
  'src/Orphan.tsx': 'export const Orphan = () => null\n',
}

const ROUTES = [
  { path: '/chat/:slug?', line: 1, catchAll: false, redirect: null },
  { path: '/settings/*', line: 2, catchAll: false, redirect: null },
  { path: '/popout/chat', line: 3, catchAll: false, redirect: null },
  { path: '/apps', line: 4, catchAll: false, redirect: null },
  { path: '/apps/-/updates', line: 5, catchAll: false, redirect: null },
  { path: '/apps/:name', line: 6, catchAll: false, redirect: null },
  { path: '/old', line: 7, catchAll: false, redirect: '/chat' },
  { path: '*', line: 8, catchAll: true, redirect: null },
]

function graphOf(sources: Record<string, string>) {
  const parsed = new Map(Object.entries(sources).map(([rel, text]) => [rel, parseSource(text, `/virtual/${rel}`)]))
  const graph = new Map<string, Edge[]>()
  for (const [rel, sf] of parsed) {
    const edges = (scanImports(sf) as { spec: string; binds: string[] }[]).flatMap((e) => {
      const to = path.posix.normalize(path.posix.join(path.posix.dirname(rel), e.spec)) + '.tsx'
      return to in sources ? [{ to, binds: e.binds }] : []
    })
    graph.set(rel, edges)
  }
  return { parsed, graph }
}

function pageMap(sources = SOURCES) {
  const { parsed, graph } = graphOf(sources)
  const names = (rel: string) => new Set(graph.get(rel)!.flatMap((e) => e.binds))
  const appSf = parsed.get('src/App.tsx')!
  const appUses = classifyUses(appSf, names('src/App.tsx'), collectRouteElementSpans(appSf))
  const settingsSf = parsed.get('src/SettingsPage.tsx')!
  const spans = collectTabPanels(settingsSf, new Set(['chat', 'about']))
  const tabHosts = new Map([['src/SettingsPage.tsx', {
    page: 'page.settings',
    tabs: new Map([['chat', 'settings.tab.chat'], ['about', 'settings.tab.about']]),
    spans,
    uses: classifyUses(settingsSf, names('src/SettingsPage.tsx'), spans),
  }]])
  const routeLabel = makeRouteLabel({
    routes: ROUTES,
    pages: [{ id: 'page.chat', route: '/chat' }, { id: 'page.settings', route: '/settings' }],
    routeFiles: new Map(),
  })
  return buildPageMap({
    graph, appRel: 'src/App.tsx', mainRel: 'src/main.tsx', appUses, routeLabel, tabHosts,
    isAppFile: (f: string) => f.startsWith('src/apps/'),
  }) as { byFile: Map<string, Mapped>; owners: Map<string, Set<string>> }
}

describe('which one page a file is drawn on', () => {
  it('maps a file only one page reaches to that page', () => {
    const { byFile } = pageMap()
    expect(byFile.get('src/ChatOnly.tsx')).toEqual({ page: 'page.chat', tab: null })
    expect(byFile.get('src/ChatPage.tsx')).toEqual({ page: 'page.chat', tab: null })
    // Reached through `lazy(() => import())` and a <Suspense> wrapper.
    expect(byFile.get('src/SettingsPage.tsx')?.page).toBe('page.settings')
  })

  it('skips a file two pages import (a shared panel), naming both owners', () => {
    const { byFile, owners } = pageMap()
    expect(byFile.has('src/Shared.tsx')).toBe(false)
    expect([...owners.get('src/Shared.tsx')!].sort()).toEqual(['page.chat', 'page.settings'])
  })

  it('skips what the shell or an embedded app also draws, and what nothing reaches', () => {
    const { byFile, owners } = pageMap()
    expect(byFile.has('src/ShellBit.tsx')).toBe(false)
    expect([...owners.get('src/ShellBit.tsx')!]).toEqual(['shell'])
    expect(byFile.has('src/BootBit.tsx')).toBe(false)
    expect(byFile.has('src/AppUsed.tsx')).toBe(false)
    expect([...owners.get('src/AppUsed.tsx')!].sort()).toEqual(['apps', 'page.chat'])
    expect(owners.has('src/Orphan.tsx')).toBe(false)
  })

  it('does not count a pop-out frame that re-hosts a page as another page', () => {
    expect(pageMap().owners.get('src/ChatOnly.tsx')).toEqual(new Set(['page.chat']))
  })

  it('does not count a type-only import as drawing the module', () => {
    expect(pageMap().byFile.get('src/TypesOnly.tsx')).toEqual({ page: 'page.settings', tab: 'settings.tab.about' })
  })

  it('gives the tab only when one tab panel alone reaches the file', () => {
    const { byFile } = pageMap()
    expect(byFile.get('src/ChatPanel.tsx')).toEqual({ page: 'page.settings', tab: 'settings.tab.chat' })
    expect(byFile.get('src/ChatPanelBit.tsx')).toEqual({ page: 'page.settings', tab: 'settings.tab.chat' })
    // Two tabs reach it: the page, not a guessed tab.
    expect(byFile.get('src/PanelBit.tsx')).toEqual({ page: 'page.settings', tab: null })
    // Rendered outside every tab panel (the rail's search).
    expect(byFile.get('src/Search.tsx')).toEqual({ page: 'page.settings', tab: null })
    // One tab's panel reaches it, but so does the rail's search: not that tab's.
    expect(byFile.get('src/SearchBit.tsx')).toEqual({ page: 'page.settings', tab: null })
  })

  it('reads no edge from a type-only import, whole or per name', () => {
    const edges = (src: string) => scanImports(parseSource(src, '/virtual/E.tsx')) as { spec: string }[]
    expect(edges("import type { A } from './a'\nimport { type B } from './b'\n")).toEqual([])
    expect(edges("import { type B, C } from './b'\nexport * from './c'\nexport type { D } from './d'\nvoid import('./e')\n").map((e) => e.spec))
      .toEqual(['./b', './c', './e'])
  })

  it('takes a tab panel only for a key the page really has', () => {
    const sf = parseSource(SOURCES['src/SettingsPage.tsx'], '/virtual/S.tsx')
    expect((collectTabPanels(sf, new Set(['chat', 'about'])) as { key: string }[]).map((s) => s.key)).toEqual(['chat', 'about'])
    const notAHost = parseSource("export const A = () => <List items={xs}>{tab => tab === 'chat' && <B />}</List>", '/virtual/A.tsx')
    expect(collectTabPanels(notAHost, new Set(['chat']))).toEqual([])
    const host = parseSource("export const A = () => <List tabs={xs}>{tab => tab === 'chat' && <B />}</List>", '/virtual/A.tsx')
    expect(collectTabPanels(host, new Set(['chat']))).toHaveLength(1)
  })

  it('labels routes: a twin route is its page, a frame or redirect is no root', () => {
    const files = new Map([['/apps', new Set(['src/Discover.tsx'])], ['/apps/-/updates', new Set(['src/Discover.tsx'])], ['/apps/:name', new Set(['src/AppPage.tsx'])]])
    const label = makeRouteLabel({ routes: ROUTES, pages: [{ id: 'page.apps', route: '/apps' }], routeFiles: files })
    expect(label('/apps')).toBe('page.apps')
    expect(label('/apps/-/updates')).toBe('page.apps')
    expect(label('/apps/:name')).toBe('route:/apps/:name')
    expect(label('/popout/chat')).toBeNull()
    expect(label('/old')).toBeNull()
    expect(label('*')).toBeNull()
  })

  it('reads the kind from tag, role and input type', () => {
    const k = (c: Record<string, unknown>) => candidateKind({ role: null, type: null, ...c })
    expect(k({ tag: 'button' })).toBe('button')
    expect(k({ tag: 'button', role: 'menuitem' })).toBe('menu-item')
    expect(k({ tag: 'Link' })).toBe('link')
    expect(k({ tag: 'input', type: 'checkbox' })).toBe('toggle')
    expect(k({ tag: 'input', type: 'text' })).toBe('field')
    expect(k({ tag: 'summary' })).toBe('disclosure')
  })
})

const EN = {
  'nav.chat': 'Sessions', 'nav.settings': 'Settings', 'tab.chat': 'Chat', 'k.toggle': 'Show sessions',
  'k.auto': 'Reindex sources', 'k.auto.same': 'Reindex sources', 'k.count': '{{count}} items',
  'k.enonly': 'Only in English', 'k.setting': 'A setting',
}
const ZH = {
  'nav.chat': '会话', 'k.auto': '重新索引来源', 'k.auto.same': '重新索引来源', 'k.count': '{{count}} 项',
  'k.toggle': '显示会话',
}

function build(autoCandidates: Record<string, unknown>[]) {
  return buildUiIndex({
    surfaces: [
      { navId: 'chat', route: '/chat', label: 'Sessions', labelKey: 'nav.chat', group: 'Main' },
      { navId: 'settings', route: '/settings', label: 'Settings', labelKey: 'nav.settings', group: 'Bottom' },
    ],
    extraPages: [], extraTitleKeys: {}, capabilityTabs: [], settingsSubs: {}, settingsTabPreview: {},
    settingsTabs: [{ id: 'chat', key: 'tab.chat' }],
    settingsEntries: [{ id: 'chat.a-setting', label: 'A setting', labelKey: 'k.setting', tab: 'chat', type: 'toggle', occurrence: 1 }],
    agentSettings: [{ id: 'chat.a-setting', label: 'A setting', tab: 'chat', route: '/settings/chat?highlight=chat.a-setting' }],
    descriptors: { 'chat.toggle': { kind: 'toggle', placements: [{ surface: 'chat', parent: 'page.chat', entry: 'toolbar' }] } },
    markerSites: [{ id: 'chat.toggle', rel: 'F.tsx', line: 1, resolved: { source: { key: 'k.toggle' }, excluded: [] } }],
    previewEnablers: {},
    catalogs: { en: EN, 'zh-CN': ZH },
    locales: ['en', 'zh-CN'],
    productName: 'Kiro Crew',
    inputDigest: 'sha256:test',
    routes: ROUTES,
    conditions: UI_CONDITIONS,
    revealStates: UI_REVEAL_STATES,
    resolveGuide: () => ({ ok: false, reason: 'none' }),
    autoCandidates,
  }) as {
    index: { locations: Loc[]; labels: Record<string, Record<string, string>>; coverage: { tiers: Record<string, number>; counts: Record<string, number> } }
    errors: string[]
    auto: { covered: number; entries: number; skipped: Record<string, number> }
  }
}

describe('auto locations', () => {
  const cand = (key: string, over: Record<string, unknown> = {}) => ({ page: 'page.chat', tab: null, key, kind: 'button', rel: 'A.tsx', ...over })

  it('includes a single-page candidate as tier auto, with its page path and labels in every locale', () => {
    const { index, errors, auto } = build([cand('k.auto')])
    expect(errors).toEqual([])
    const loc = index.locations.find((l) => l.id === 'auto:page.chat:k.auto')!
    expect(loc).toMatchObject({ tier: 'auto', conditions_unknown: true, kind: 'button', label_key: 'k.auto' })
    expect(loc.terms).toBeUndefined()
    expect(loc.placements).toEqual([{ surface_id: 'chat', route: '/chat', parent_ids: ['page.chat'], entry_kind: 'content', requires: [] }])
    expect(index.labels.en['k.auto']).toBe('Reindex sources')
    expect(index.labels['zh-CN']['k.auto']).toBe('重新索引来源')
    expect(index.coverage.tiers).toEqual({ generated: 4, curated: 1, auto: 1 })
    expect(index.coverage.counts.button).toBeUndefined()
    expect(auto).toMatchObject({ covered: 1, entries: 1 })
    expect(index.locations.find((l) => l.id === 'chat.toggle')!.tier).toBe('curated')
    expect(index.locations.find((l) => l.id === 'page.chat')!.tier).toBe('generated')
  })

  it('hangs a tab candidate under the tab, inheriting its path and route', () => {
    const { index } = build([cand('k.auto', { page: 'page.settings', tab: 'settings.tab.chat' })])
    const loc = index.locations.find((l) => l.tier === 'auto')!
    expect(loc.id).toBe('auto:settings.tab.chat:k.auto')
    expect(loc.placements[0]).toMatchObject({ route: '/settings/chat', parent_ids: ['page.settings', 'settings.tab.chat'] })
  })

  it('leaves out a label a curated or generated location shows, an interpolated one, and an untranslated one', () => {
    const { index, auto } = build([cand('k.toggle'), cand('nav.settings'), cand('k.count'), cand('k.enonly')])
    expect(index.locations.filter((l) => l.tier === 'auto')).toEqual([])
    expect(auto.skipped).toMatchObject({ shown_by_curated: 2, interpolated: 1, untranslated: 1 })
  })

  it('folds two controls with one English label on one page into one answer', () => {
    const { index, auto } = build([cand('k.auto'), cand('k.auto.same'), cand('k.auto', { page: 'page.settings' })])
    expect(index.locations.filter((l) => l.tier === 'auto').map((l) => l.id).sort())
      .toEqual(['auto:page.chat:k.auto', 'auto:page.settings:k.auto'])
    expect(auto.covered).toBe(3)
  })

  it('puts a control only the app shell draws on every page: shell surface, no route, no path', () => {
    const { index, errors } = build([cand('k.auto', { page: 'shell' })])
    expect(errors).toEqual([])
    const loc = index.locations.find((l) => l.tier === 'auto')!
    expect(loc.id).toBe('auto:shell:k.auto')
    expect(loc.placements).toEqual([{ surface_id: 'shell', route: '', parent_ids: [], entry_kind: 'content', requires: [] }])
  })

  it('refuses a candidate mapped to a location the index does not have', () => {
    expect(build([cand('k.auto', { page: 'page.nowhere' })]).errors.join('\n')).toMatch(/unknown location 'page.nowhere'/)
  })

  it('leaves the auto tier out of the coverage block when no candidates were asked for', () => {
    const { index } = build(undefined as unknown as Record<string, unknown>[])
    expect(index.coverage.tiers).toEqual({ generated: 4, curated: 1 })
    expect(index.coverage).not.toHaveProperty('auto_controls')
    expect((index.coverage as unknown as { scope: string }).scope).not.toMatch(/auto-indexed/)
  })
})

describe('the build-time auto artifact', () => {
  const cand = (key: string) => ({ page: 'page.chat', tab: null, key, kind: 'button', rel: 'A.tsx' })

  it('carries only the auto entries and their labels, naming the committed index it hangs off', () => {
    const { index } = build([cand('k.auto')])
    const art = buildAutoArtifact(index, { baseInputDigest: 'sha256:base', inputDigest: 'sha256:auto', coverage: { core_coverage_pct: 12.5 } }) as {
      artifact: string; base_input_digest: string; input_digest: string; locales: string[]
      coverage: Record<string, unknown>; locations: Loc[]; labels: Record<string, Record<string, string>>
    }
    expect(art).toMatchObject({ artifact: 'auto', base_input_digest: 'sha256:base', input_digest: 'sha256:auto', locales: ['en', 'zh-CN'] })
    expect(art.locations.map((l) => l.id)).toEqual(['auto:page.chat:k.auto'])
    expect(art.labels).toEqual({ en: { 'k.auto': 'Reindex sources' }, 'zh-CN': { 'k.auto': '重新索引来源' } })
    expect(art.coverage).toMatchObject({ auto_controls: 1, core_coverage_pct: 12.5 })
    expect(AUTO_ARTIFACT_NAME).toBe('ui-index.auto.json')
  })
})

describe('the committed index', () => {
  const real = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../src/kiro_crew/docs/ui-index.generated.json'), 'utf-8'))

  it('carries no auto tier: it is generated at build time, so a new button never stales the committed file', () => {
    expect(real.locales).toHaveLength(12)
    expect((real.locations as Loc[]).filter((l) => l.tier === 'auto')).toEqual([])
    expect(real.coverage.tiers).not.toHaveProperty('auto')
    expect(real.coverage).not.toHaveProperty('auto_controls')
  })
})

describe('icon-only candidates', () => {
  const scan = (body: string) => scanCandidates(
    `import { i18nT } from './i18n/t'\nexport function F({ done }: { done: boolean }) {\n  return (<div>${body}</div>)\n}\n`,
    '/virtual/src/F.tsx', 'src/F.tsx',
  ) as { keys: string[]; resolved: boolean }[]

  it('takes a later attribute that is ONE static key when the first is dynamic', () => {
    const [c] = scan(`<button title={i18nT('k.copy')} aria-label={outcome(done, i18nT('k.copy'))} onClick={go}>{icon(done, <Copy />)}</button>`)
    expect(c).toMatchObject({ resolved: true, keys: ['k.copy'] })
  })

  it('never relabels a control that has (or may have) text of its own from its title', () => {
    expect(scan(`<button title={i18nT('k.copy')} aria-label={outcome(done)}>{label}</button>`)[0].resolved).toBe(false)
    // Its own text is the label, as before; the title is not borrowed.
    expect(scan(`<button title={i18nT('k.copy')} aria-label={outcome(done)}>Copy</button>`)[0].keys).toEqual([])
  })

  it('does not pick one of two keys a ternary attribute names', () => {
    const [c] = scan(`<button aria-label={done ? i18nT('k.a') : i18nT('k.b')} title={x}><Pin /></button>`)
    expect(c.keys).toEqual(['k.a', 'k.b'])
  })
})
