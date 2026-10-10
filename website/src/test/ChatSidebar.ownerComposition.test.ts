/**
 * Who owns what in the Sessions sidebar.
 *
 * `pages/ChatSidebar.tsx` is the component every consumer imports, and it composes the
 * owners under `pages/chat-sidebar/`. These pins keep that shape:
 *
 * - the owners never import the facade, and their runtime imports form no cycle, so
 *   each loads on its own and the facade stays the only root;
 * - nothing outside the sidebar imports an owner, so a `vi.mock('../pages/ChatSidebar')`
 *   factory still replaces the whole sidebar for the page tests;
 * - the facade calls the owner hooks in one fixed order. React runs a component's
 *   effects in hook order, so moving a call changes which effect sees another's
 *   writes first; the pinned order is the order the sidebar ran them in before the
 *   owners were extracted;
 * - every browser-storage key the sidebar owns is declared in ./persistence, except
 *   the four status-filter keys that ride on `SESSION_FILTERS`, and only
 *   ./persistence, ./resize, ./history and ./filters read storage;
 * - every name the facade re-exports is the owner's own binding, not a copy.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import * as facade from '../pages/ChatSidebar'
import * as collision from '../pages/chat-sidebar/dnd/collision'
import * as board from '../pages/chat-sidebar/board'

const PAGES = join(__dirname, '..', 'pages')
const OWNER_DIR = join(PAGES, 'chat-sidebar')
const FACADE = join(PAGES, 'ChatSidebar.tsx')
const WEBSITE = join(__dirname, '..', '..')

function walk(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) out.push(...walk(p))
    else if (/\.(ts|tsx)$/.test(name)) out.push(p)
  }
  return out
}

/** Every `import`/`export ... from` and dynamic `import('...')` in a module: its
 *  specifier and whether it is type-only. */
function importsOf(text: string): Array<{ spec: string; typeOnly: boolean }> {
  const out: Array<{ spec: string; typeOnly: boolean }> = []
  const re = /^(?:import|export)\s+(type\s+)?[^'";]*?\bfrom\s+'([^']+)'/gm
  for (const m of text.matchAll(re)) out.push({ spec: m[2], typeOnly: Boolean(m[1]) })
  for (const m of text.matchAll(/\bimport\(\s*'([^']+)'\s*\)/g)) out.push({ spec: m[1], typeOnly: false })
  return out
}

const ownerFiles = walk(OWNER_DIR).filter(p => !/\.test\./.test(p))
const ownerName = (p: string) => relative(OWNER_DIR, p).replace(/\.tsx?$/, '').split('\\').join('/')
const ownerSource = new Map(ownerFiles.map(p => [ownerName(p), readFileSync(p, 'utf8')]))
const facadeSource = readFileSync(FACADE, 'utf8')

/** Resolve an owner-relative specifier to an owner name, or null when it leaves the dir. */
function resolveOwner(from: string, spec: string): string | null {
  if (!spec.startsWith('.')) return null
  const target = relative(OWNER_DIR, join(OWNER_DIR, from, '..', spec)).split('\\').join('/')
  return target.startsWith('..') ? null : target
}

describe('chat-sidebar owners', () => {
  it('never import the ChatSidebar facade', () => {
    const hits = [...ownerSource].flatMap(([name, text]) =>
      importsOf(text).filter(i => /(^|\/)ChatSidebar$/.test(i.spec)).map(i => `${name} -> ${i.spec}`))
    expect(hits).toEqual([])
  })

  it('form an acyclic runtime import graph', () => {
    const edges = new Map<string, string[]>()
    for (const [name, text] of ownerSource) {
      edges.set(name, importsOf(text)
        .filter(i => !i.typeOnly)
        .map(i => resolveOwner(name, i.spec))
        .filter((n): n is string => n !== null))
    }
    const done = new Set<string>()
    const cycles: string[] = []
    const visit = (node: string, path: string[]) => {
      if (path.includes(node)) { cycles.push([...path.slice(path.indexOf(node)), node].join(' -> ')); return }
      if (done.has(node)) return
      for (const next of edges.get(node) ?? []) visit(next, [...path, node])
      done.add(node)
    }
    for (const name of edges.keys()) visit(name, [])
    expect(cycles).toEqual([])
  })

  it('are imported only by the facade and by each other', () => {
    const roots = ['src', 'integration', 'capture'].map(r => join(WEBSITE, r))
    const hits: string[] = []
    for (const root of roots) {
      let files: string[] = []
      try { files = walk(root) } catch { continue }
      for (const p of files) {
        if (p === FACADE || p.startsWith(OWNER_DIR) || /\.test\.tsx?$/.test(p)) continue
        for (const i of importsOf(readFileSync(p, 'utf8'))) {
          if (/(^|\/)chat-sidebar(\/|$)/.test(i.spec)) hits.push(`${relative(WEBSITE, p)} -> ${i.spec}`)
        }
      }
    }
    expect(hits).toEqual([])
  })
})

describe('the facade composition', () => {
  // The owner hooks in the order the facade calls them. Each sits where its block sat
  // before the owners were extracted, which is what keeps React's effect order.
  const CALL_ORDER = [
    'useSessionSources', 'useDebouncedSessionSearch', 'useSessionRename', 'useSessionAutoTitle', 'useSidebarLane',
    'useSessionFilterState', 'useSessionStatusFilters', 'useHistoryPane', 'usePinnedSessionOrder',
    'useStaleCollapse', 'useFolderSort', 'useFolderRename', 'useSidebarTags',
    'useBoardColumns', 'useSidebarResize', 'useFullTitlesOnScrollSetting', 'usePinnedOrderAuthority', 'useColumnPopover', 'useBoardColumnMutations',
    'useColumnMatches', 'useFolderVisibility', 'useStaleMoveWatcher', 'useSearchMatches',
    'useCollapsedCrews', 'useCrewEnable', 'useHoverHold', 'useLineageAvailable', 'useHoverPinLiveness', 'useStaleNarrowBridge',
    'useFolderFilterReveal', 'useConductorLane', 'useLaneCycle', 'useSidebarScrollPeek', 'useShortcutOrder',
    'useFolderFilterRows', 'useFolderMutations', 'useHoldPinnedHeaderOnCollapse', 'useBoardFolderCollapse',
    'useNativeSessionDrag', 'useFolderDropOps',
    'useFolderTree', 'useSidebarReveal', 'useSidebarMoveUndo', 'useSidebarDragHandlers',
    'useFolderChatCreate', 'useSessionCreate', 'usePinnedKeyboardReorder', 'useRootFolderLanes',
  ]

  it('calls every owner hook once, in the pinned order', () => {
    const imported = new Set<string>()
    for (const m of facadeSource.matchAll(/^import \{([^}]*)\} from '\.\/chat-sidebar\/[^']+'/gm)) {
      for (const n of m[1].split(',').map(s => s.trim())) if (n.startsWith('use')) imported.add(n)
    }
    const calls = [...facadeSource.matchAll(/\b(use[A-Z]\w*)\(/g)].map(m => m[1]).filter(n => imported.has(n))
    expect(calls).toEqual(CALL_ORDER)
  })

  it('re-exports the owners\' own bindings', () => {
    expect(facade.sidebarCollision).toBe(collision.sidebarCollision)
    expect(facade.isFolderNestBand).toBe(collision.isFolderNestBand)
    expect(facade.boardSidebarWidth).toBe(board.boardSidebarWidth)
  })
})

describe('browser-storage keys', () => {
  const keyLiterals = (text: string) => [...text.matchAll(/'(mc-[a-z0-9-]+)'/g)].map(m => m[1])
  // Event names share the prefix but are not storage keys.
  const EVENTS = new Set(['mc-config-changed'])

  it('are declared in ./persistence, bar the four status-filter keys', () => {
    const elsewhere: string[] = []
    for (const [name, text] of [...ownerSource, ['ChatSidebar', facadeSource] as const]) {
      if (name === 'persistence') continue
      for (const key of keyLiterals(text)) {
        if (EVENTS.has(key)) continue
        if (name === 'filters' && /^mc-session-(unread|running|pinned|recent)-only$/.test(key)) continue
        elsewhere.push(`${name}: ${key}`)
      }
    }
    expect(elsewhere).toEqual([])
    // The control: the scan is looking at real declarations.
    expect(keyLiterals(ownerSource.get('persistence') ?? '')).toContain('mc-sidebar-lane')
  })

  it('are read only in ./persistence and the three owners whose readers stay beside their state', () => {
    // resize (width, pre-board width), history (pane height) and filters (status
    // chips, folders-shelved) read their own keys; everything else goes through
    // a ./persistence reader. The facade reads none.
    const readers = [...ownerSource, ['ChatSidebar', facadeSource] as const]
      .filter(([, text]) => text.includes('localStorage.getItem('))
      .map(([name]) => name)
      .sort()
    expect(readers).toEqual(['filters', 'history', 'persistence', 'resize'])
  })
})
