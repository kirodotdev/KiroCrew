/**
 * DrivePage.tsx and the owners beside it.
 *
 * The facade stays the drive page's one import path (AwsControlPage, ConsoleView
 * and the specs import it by that id) and re-exports each pane from its owner.
 * These pins keep that shape honest:
 *
 *  - the facade still exports every name it exported as one file, and a
 *    re-exported name is the owner's own binding rather than a copy;
 *  - owners never import the facade, and nothing outside the page (bar specs)
 *    imports an owner;
 *  - the Files pane's hooks all register inside `useDrivePageFiles`, which
 *    `DriveSectionView` calls as its one hook, and its views call none, so the
 *    pane keeps one registration order however its markup is cut.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import ts from 'typescript'

import * as facade from './DrivePage'
import * as library from './DrivePageLibrary'
import * as controls from './DrivePageControls'
import * as files from './DrivePageFiles'
import * as backup from './DrivePageBackup'
import { pageFamilyFiles } from '../../test/pageFamily'

const DIR = __dirname
const SRC = resolve(DIR, '..', '..')
const FILES = pageFamilyFiles('DrivePage', DIR)
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
 *  order. `useMemo` and `useCallback` register no effect and own no state. */
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
    if (/^use[A-Z]/.test(name) && !MEMOS.has(name)) out.push(name)
  }
  visit(body)
  return out
}

describe('DrivePage owners', () => {
  it('are the files the page is made of', () => {
    expect(OWNERS).toEqual([
      'DrivePageBackup.tsx',
      'DrivePageControls.tsx',
      'DrivePageFileDialogs.tsx',
      'DrivePageFiles.tsx',
      'DrivePageFilesViews.tsx',
      'DrivePageLibrary.tsx',
      'useDrivePageFiles.ts',
    ])
  })

  it('include every sibling the facade imports under its name', () => {
    // A part named off the family pattern would drop out of `readPageFamily`,
    // and every pin that reads the page through it would stop seeing that part.
    const named = /^(DrivePage|drivePage|useDrivePage)[A-Z.]/
    const family = new Set(FILES.map((f) => stem(join(DIR, f))))
    const siblings = importsOf(join(DIR, 'DrivePage.tsx'))
      .filter((t) => dirname(t) === DIR && named.test(t.slice(DIR.length + 1)))
    expect(siblings.length).toBeGreaterThan(0)
    expect(siblings.filter((t) => !family.has(t))).toEqual([])
  })

  it('leave the facade exporting every name it exported as one file', () => {
    expect(Object.keys(facade).sort()).toEqual([
      'AccessSection',
      'BackupSection',
      'DriveSectionView',
      'LibrarySection',
      'SECTION_TILES',
      'StorageMeter',
      'TileConfirm',
    ])
  })

  it('re-export each pane as its owner binding, not a copy', () => {
    expect(facade.LibrarySection).toBe(library.LibrarySection)
    expect(facade.TileConfirm).toBe(controls.TileConfirm)
    expect(facade.DriveSectionView).toBe(files.DriveSectionView)
    expect(facade.BackupSection).toBe(backup.BackupSection)
  })

  it('never import the facade', () => {
    const facadePath = stem(join(DIR, 'DrivePage.tsx'))
    expect(OWNERS.filter((f) => importsOf(join(DIR, f)).includes(facadePath))).toEqual([])
  })

  it('are imported only by the facade and by each other', () => {
    const family = new Set(FILES.map((f) => stem(join(DIR, f))))
    const owners = new Set(OWNERS.map((f) => stem(join(DIR, f))))
    const offenders = walk(SRC)
      .filter((file) => !family.has(stem(file)))
      .flatMap((file) => importsOf(file).filter((t) => owners.has(t)).map((t) => `${relative(SRC, file)} -> ${relative(SRC, t)}`))
    expect(offenders).toEqual([])
  })

  it('register the Files pane hooks in one place', () => {
    // `useDrivePageFiles` holds the pane's hooks in the order the pane
    // registers them; the shell calls it once and the views call none, so a
    // hook added to the shell or to a view would interleave a second sequence.
    expect(hookCalls(bodyOf(join(DIR, 'DrivePageFiles.tsx'), 'DriveSectionView'))).toEqual(['useDrivePageFiles'])
    for (const view of ['DriveFilesSearchResults', 'DriveFilesGrid', 'DriveFilesTable']) {
      expect(hookCalls(bodyOf(join(DIR, 'DrivePageFilesViews.tsx'), view)), view).toEqual([])
    }
    expect(hookCalls(bodyOf(join(DIR, 'useDrivePageFiles.ts'), 'useDrivePageFiles')).length).toBeGreaterThan(0)
  })

  it('reads imports the way the pins above rely on', () => {
    const probe = join(DIR, 'probe.tsx')
    const text = "import './Side'\nimport type { T } from '@/apps/Typed'\nexport { X } from './Re'\nconst L = () => import('./Lazy')\n"
    const found = [...text.matchAll(SPEC_RE)].map((m) => m[1])
    expect(found).toEqual(['./Side', '@/apps/Typed', './Re', './Lazy'])
    expect(stem(resolve(dirname(probe), './Side.tsx'))).toBe(join(DIR, 'Side'))
  })

  it('reads hooks the way the pin above relies on', () => {
    const probe = 'function P() { const a = useA(); useEffect(() => { useNested() }); const b = useMemo(() => 1, []); return useB(a, b) }'
    const sf = ts.createSourceFile('probe.tsx', probe, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX)
    const fn = sf.statements[0] as ts.FunctionDeclaration
    expect(hookCalls(fn.body!)).toEqual(['useA', 'useEffect', 'useB'])
  })
})
