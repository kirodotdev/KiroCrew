/**
 * ArtifactsPage.tsx and the owners beside it.
 *
 * The facade stays the page's one import path (its importers and the specs that
 * `vi.mock` it by that id) and calls each owner where its code sat when the page
 * was one file. These pins keep that shape honest:
 *
 *  - the facade still exports every name it exported as one file;
 *  - owners never import the facade, and nothing outside the page (bar its own
 *    specs) imports an owner, so a mock of the facade still replaces the whole
 *    page;
 *  - the page registers its hooks in the order it did as one file
 *    (`PAGE_HOOKS`): React runs a component's effects in registration order, so
 *    a reshuffle is a behaviour change even when every other test still passes.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import ts from 'typescript'

import * as facade from './ArtifactsPage'
import { PAGES_DIR, pageFamilyFiles } from '../test/pageFamily'

const SRC = resolve(PAGES_DIR, '..')
const FILES = pageFamilyFiles('ArtifactsPage')
const OWNERS = FILES.slice(1)
const read = (p: string) => readFileSync(p, 'utf8')

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.|\.spec\./.test(name)) out.push(p)
  }
  return out
}

/** Every module a file imports (static, side-effect, `export … from` and
 *  dynamic), resolved to an absolute path without its extension. `@/` is the
 *  `src/` alias. */
const SPEC_RE = /(?:from\s+|import\s*\(?)\s*['"]([^'"]+)['"]/g
const stem = (p: string) => p.replace(/\.(tsx?|jsx?)$/, '').replace(/\/index$/, '')
function importsOf(file: string): string[] {
  return [...read(file).matchAll(SPEC_RE)].map((m) => m[1]).flatMap((spec) => {
    if (spec.startsWith('@/')) return [stem(join(SRC, spec.slice(2)))]
    if (spec.startsWith('.')) return [stem(resolve(dirname(file), spec))]
    return []
  })
}

/** The `use*` calls a function body makes outside its nested functions, in
 *  order, with each owner hook expanded into the calls its own body makes.
 *  `useMemo` and `useCallback` register no effect and own no state. */
const MEMOS = new Set(['useMemo', 'useCallback'])
function bodyOf(file: string, name: string): ts.Block {
  const sf = ts.createSourceFile(file, read(file), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
  const fn = sf.statements.find((s): s is ts.FunctionDeclaration => ts.isFunctionDeclaration(s) && s.name?.text === name)
  if (!fn?.body) throw new Error(`${name} is not a function in ${relative(SRC, file)}`)
  return fn.body
}
function hookCalls(body: ts.Block): string[] {
  const out: string[] = []
  const visit = (n: ts.Node) => {
    if (n !== body && ts.isFunctionLike(n)) return
    ts.forEachChild(n, visit)
    if (!ts.isCallExpression(n) || !ts.isIdentifier(n.expression)) return
    const name = n.expression.text
    if (!/^use[A-Z]/.test(name) || MEMOS.has(name)) return
    const owner = OWNER_HOOKS[name]
    out.push(...(owner ? hookCalls(bodyOf(join(PAGES_DIR, owner), name)) : [name]))
  }
  visit(body)
  return out
}

/** The hooks the split lifted out of the page body, and the owner file
 *  defining each; a hook that was its own function as one file stays opaque. */
const OWNER_HOOKS: Record<string, string> = {
  useArtifactsPageCreate: 'useArtifactsPageCreate.ts',
}

/** The hook registrations of `ArtifactsPage` as one file, before the split,
 *  computed by `hookCalls` above. A change that adds, removes or moves a page
 *  hook edits this list in the same commit, at the position the hook
 *  registers. */
const PAGE_HOOKS = [
  'useNavigate', 'useQueryClient', 'useCloudDeploymentEnabled', 'usePreviewFlag', 'useState', 'useIsMobile',
  'useState', 'useState', 'useState', 'useState', 'useState', 'useSearchParams', 'useArtifactFolders',
  'useState', 'useInvalidateArtifactFolders', 'useMutation', 'useMutation', 'useState',
  'useMoveArtifactToFolder', 'useState', 'useState', 'useState', 'useRef', 'useState', 'useState',
  'useMutation', 'useMutation', 'useQuery', 'useQuery', 'useMoveUndo', 'useMoveUndo', 'useDndSensors',
  'useState', 'useState', 'useRef', 'useEffect', 'useQuery', 'useMutation', 'useMutation', 'useQuery',
  'useState', 'useRef', 'useEffect', 'useState', 'useState', 'useEffect', 'useMutation', 'useGuideSelection',
  'useColumnCount', 'useRef', 'useCollapseOnScroll',
]

describe('ArtifactsPage owners', () => {
  it('are the files the page is made of', () => {
    expect(OWNERS).toEqual([
      'ArtifactsPageFolders.tsx',
      'ArtifactsPageGallery.tsx',
      'ArtifactsPageSections.tsx',
      'artifactsPageModel.ts',
      'useArtifactsPageCreate.ts',
    ])
  })

  it('include every sibling the facade imports under its name', () => {
    // A part named off the family pattern would drop out of `readPageFamily`,
    // and every pin that reads the page through it would stop seeing that part.
    const lower = 'ArtifactsPage'.charAt(0).toLowerCase() + 'ArtifactsPage'.slice(1)
    const named = new RegExp(`^(ArtifactsPage|${lower}|useArtifactsPage)[A-Z.]`)
    const family = new Set(FILES.map((f) => stem(join(PAGES_DIR, f))))
    const siblings = importsOf(join(PAGES_DIR, 'ArtifactsPage.tsx'))
      .filter((t) => dirname(t) === PAGES_DIR && named.test(t.slice(PAGES_DIR.length + 1)))
    expect(siblings.length).toBeGreaterThan(0)
    expect(siblings.filter((t) => !family.has(t))).toEqual([])
  })

  it('leave the facade exporting every name it exported as one file', () => {
    expect(Object.keys(facade).sort()).toEqual([
      'VIRTUALIZE_AT',
      'default',
    ])
  })

  it('never import the facade', () => {
    const facadePath = stem(join(PAGES_DIR, 'ArtifactsPage.tsx'))
    expect(OWNERS.filter((f) => importsOf(join(PAGES_DIR, f)).includes(facadePath))).toEqual([])
  })

  it('are imported only by the facade and by each other', () => {
    const family = new Set(FILES.map((f) => stem(join(PAGES_DIR, f))))
    const owners = new Set(OWNERS.map((f) => stem(join(PAGES_DIR, f))))
    const offenders = walk(SRC)
      .filter((file) => !family.has(stem(file)))
      .flatMap((file) => importsOf(file).filter((t) => owners.has(t)).map((t) => `${relative(SRC, file)} -> ${relative(SRC, t)}`))
    expect(offenders).toEqual([])
  })

  it('register the page hooks in the order the page did as one file', () => {
    expect(hookCalls(bodyOf(join(PAGES_DIR, 'ArtifactsPage.tsx'), 'ArtifactsPage'))).toEqual(PAGE_HOOKS)
  })

  it('reads imports the way the pins above rely on', () => {
    const probe = join(PAGES_DIR, 'probe.tsx')
    const text = "import './Side'\nimport type { T } from '@/pages/Typed'\nexport { X } from './Re'\nconst L = () => import('./Lazy')\n"
    const found = [...text.matchAll(SPEC_RE)].map((m) => m[1])
    expect(found).toEqual(['./Side', '@/pages/Typed', './Re', './Lazy'])
    expect(stem(resolve(dirname(probe), './Side.tsx'))).toBe(join(PAGES_DIR, 'Side'))
  })

  it('reads hooks the way the pin above relies on', () => {
    const probe = 'function P() { const a = useA(); useEffect(() => { useNested() }); const b = useMemo(() => 1, []); return useB(a, b) }'
    const sf = ts.createSourceFile('probe.tsx', probe, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
    const fn = sf.statements[0] as ts.FunctionDeclaration
    expect(hookCalls(fn.body!)).toEqual(['useA', 'useEffect', 'useB'])
  })
})
