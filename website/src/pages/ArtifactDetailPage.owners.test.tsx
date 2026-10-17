/**
 * The artifact detail page's facade and its owners beside it.
 *
 * ArtifactDetailPage.tsx stays the page's one public surface (App's route,
 * ArtifactPopoutFrame and the capture entries import its default export, and
 * the App specs `vi.mock('../pages/ArtifactDetailPage')`), and calls each owner
 * hook at the point its code sits in the page's hook order. These pins keep that
 * shape honest:
 *
 *  - the facade still exports what it exported, as the same bindings;
 *  - owners never import the facade, and nothing outside this page imports an
 *    owner, so a mock of the page still replaces all of it;
 *  - the facade calls the owner hooks, and runs the page code it keeps, in the
 *    page's registration order. React runs a component's effects in that order,
 *    so a reshuffle is a behaviour change even when every test of each owner
 *    still passes. Kept page code an owner is ordered against is pinned in the
 *    same sequence by an anchor text unique to it.
 *
 * The order pin guards the split itself: the owners are stretches of the hook
 * sequence the page had as one file, not a design to keep. A later change that
 * regroups state by responsibility reorders registrations on purpose, reviews
 * that as the behaviour change it is, and edits `ORDER` below to match.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'

import * as facade from './ArtifactDetailPage'
import * as body from '../components/ArtifactBody'
import { pageFamilyFiles } from '../test/pageFamily'

const DIR = __dirname
const SRC = resolve(DIR, '..')
const FACADE = join(DIR, 'ArtifactDetailPage.tsx')
const owners = pageFamilyFiles('ArtifactDetailPage', DIR).slice(1)
const read = (p: string) => readFileSync(p, 'utf8')

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.|\.spec\./.test(name)) out.push(p)
  }
  return out
}

/** Every module a file imports (static, side-effect, `export … from` and dynamic), resolved to
 *  an absolute path without its extension. `@/` is the `src/` alias. */
const SPEC_RE = /(?:from\s+|import\s*\(?)\s*['"]([^'"]+)['"]/g
const stem = (p: string) => p.replace(/\.(tsx?|jsx?)$/, '').replace(/\/index$/, '')
function importsOf(file: string): string[] {
  return [...read(file).matchAll(SPEC_RE)].map((m) => m[1]).flatMap((spec) => {
    if (spec.startsWith('@/')) return [stem(join(SRC, spec.slice(2)))]
    if (spec.startsWith('.')) return [stem(resolve(dirname(file), spec))]
    return []
  })
}

describe('ArtifactDetailPage facade', () => {
  it('keeps the exports it had, as the same bindings', () => {
    expect(Object.keys(facade).sort()).toEqual(['default', 'isEditableKind'])
    expect(typeof facade.default).toBe('function')
    expect(facade.isEditableKind).toBe(body.isEditableKind)
  })
})

describe('ArtifactDetailPage owners', () => {
  it('are the six files beside the facade', () => {
    expect(owners).toEqual([
      'ArtifactDetailPageBody.tsx', 'ArtifactDetailPageHeader.tsx', 'useArtifactDetailPageComments.ts',
      'useArtifactDetailPageCompanion.ts', 'useArtifactDetailPageDocument.ts', 'useArtifactDetailPageEditor.ts',
    ])
  })

  it('include every sibling the facade imports under the page’s name', () => {
    const siblings = [...read(FACADE).matchAll(/from '\.\/((?:use)?ArtifactDetailPage[A-Z.][\w.]*)'/g)].map((m) => m[1])
    expect(siblings.length).toBeGreaterThan(0)
    for (const s of siblings) expect(owners.map((f) => f.replace(/\.tsx?$/, '')), s).toContain(s)
  })

  it('exist, and none imports the facade back', () => {
    for (const f of owners) {
      expect(importsOf(join(DIR, f)), f).not.toContain(stem(FACADE))
    }
  })

  it('are imported only by the facade and by each other', () => {
    const ownerPaths = new Set(owners.map((f) => join(DIR, f)))
    const ownerStems = new Set(owners.map((f) => stem(join(DIR, f))))
    const outside = walk(SRC)
      .filter((p) => p !== FACADE && !ownerPaths.has(p))
      .filter((p) => importsOf(p).some((s) => ownerStems.has(s)))
      .map((p) => relative(SRC, p))
    expect(outside).toEqual([])
  })

  it('are called by the facade in the page’s hook registration order', () => {
    const src = read(FACADE)
    const ORDER = [
      'useArtifactDetailPageState()',
      'const commentsQuery = useQuery<',
      'const isMobile = useIsMobile()',
      "const [panel, setPanel] = useState<'none' | 'comments' | 'chat'>('none')",
      'const sidebarNavRef = useRef(slug)',
      'const navigated = sidebarNavRef.current !== slug',
      'useArtifactDetailPageDocument(',
      'useArtifactDetailPageEditor(',
      'const handleRevert = useCallback(',
      'useArtifactDetailPageComments(',
      'const submitAnchored = useCallback(',
      'useArtifactDetailPageComposer(',
      'const buildCompanionContext = useCallback(',
      'useArtifactDetailPageCompanion(',
      'const openCompanionChat = useCallback(',
      "window.addEventListener('kirocrew:artifact-deleted', onDeleted)",
      'if (editing || !isCurrent) clearSelectionState()',
      'useArtifactDetailPageThreads(',
      'useArtifactDetailPageMenu(',
    ]
    const at = ORDER.map((anchor) => {
      const first = src.indexOf(anchor)
      expect(first, anchor).toBeGreaterThan(-1)
      expect(src.indexOf(anchor, first + 1), `${anchor} appears once`).toBe(-1)
      return first
    })
    for (let i = 1; i < at.length; i++) {
      expect(at[i], `${ORDER[i]} after ${ORDER[i - 1]}`).toBeGreaterThan(at[i - 1])
    }
  })

  it('each owner hook is called once, by the facade', () => {
    const hooks = owners.flatMap((f) => [...read(join(DIR, f)).matchAll(/^export function (use\w+)\(/gm)].map((m) => m[1]))
    expect(hooks.sort()).toEqual([
      'useArtifactDetailPageComments', 'useArtifactDetailPageCompanion', 'useArtifactDetailPageComposer',
      'useArtifactDetailPageDocument', 'useArtifactDetailPageEditor', 'useArtifactDetailPageMenu',
      'useArtifactDetailPageState', 'useArtifactDetailPageThreads',
    ])
    const src = read(FACADE)
    for (const h of hooks) {
      expect(src.match(new RegExp(`\\b${h}\\(`, 'g'))?.length ?? 0, h).toBe(1)
      for (const f of owners) {
        const calls = read(join(DIR, f)).match(new RegExp(`(?<!function )\\b${h}\\(`, 'g'))?.length ?? 0
        expect(calls, `${f} calls ${h}`).toBe(0)
      }
    }
  })

  it('render no hook of their own in the views', () => {
    // The views run after every hook the facade calls; a hook inside one would
    // register below the page's and move with whatever the view renders.
    for (const f of ['ArtifactDetailPageHeader.tsx', 'ArtifactDetailPageBody.tsx']) {
      const src = read(join(DIR, f))
      const at = src.indexOf(`export function ${f.replace('.tsx', '')}(`)
      expect(at, `${f} declares its view`).toBeGreaterThan(-1)
      expect(src.slice(at).match(/\buse[A-Z]\w*\(/g) ?? [], f).toEqual([])
    }
  })
})
