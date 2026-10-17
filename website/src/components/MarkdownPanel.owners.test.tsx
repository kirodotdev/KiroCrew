import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import * as facadeModule from './MarkdownPanel'

/* ── MarkdownPanel's module boundary. `components/MarkdownPanel.tsx` is the only import
 *    path: hosts import it and the specs that mount a host mock it by that id,
 *    so a module that imported an owner directly would slip past those mocks.
 *    Imports run one way: the facade imports its owners, and no owner imports
 *    the facade (a spec's stub, or a half-evaluated module, would come back).
 *    The facade exports every value name its consumers import. ── */

const SRC = join(__dirname, '..')
const COMPONENTS = __dirname
const FACADE = 'MarkdownPanel.tsx'
const OWNER_RE = /^(use)?[Mm]arkdownPanel/
const OWNERS = readdirSync(COMPONENTS)
  .filter(f => OWNER_RE.test(f) && f !== FACADE && !/\.test\.tsx?$/.test(f))
  .sort()

/** Static and dynamic import specifiers of a module. */
function importSpecifiers(src: string): string[] {
  const out: string[] = []
  for (const m of src.matchAll(/^(?:import|export)\b[^'"]*?\bfrom\s+['"]([^'"]+)['"]/gm)) out.push(m[1])
  for (const m of src.matchAll(/\bimport\(\s*['"]([^'"]+)['"]\s*\)/g)) out.push(m[1])
  return out
}

/** The component-directory module a specifier names, or null for anything else. */
function componentOf(spec: string, fromDir: string): string | null {
  if (!spec.startsWith('.')) return null
  const rel = relative(COMPONENTS, join(fromDir, spec)).split(sep).join('/')
  return rel.includes('/') ? null : rel.replace(/\.tsx?$/, '')
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir, { recursive: true, encoding: 'utf8' })
    .map(p => p.split('\\').join('/'))
    .filter(p => /\.tsx?$/.test(p) && !/\.test\.tsx?$/.test(p) && !p.startsWith('test/'))
}

describe('MarkdownPanel module boundary', () => {
  it('names its owners', () => {
    expect(OWNERS).toEqual([
      'MarkdownPanelBody.tsx',
      'MarkdownPanelHeader.tsx',
      'markdownPanelSourcePos.ts',
      'useMarkdownPanelDisk.ts',
      'useMarkdownPanelFileState.ts',
      'useMarkdownPanelFind.ts',
    ])
  })

  it('no owner imports the facade', () => {
    const offenders = OWNERS.filter(f => importSpecifiers(readFileSync(join(COMPONENTS, f), 'utf8'))
      .some(s => componentOf(s, COMPONENTS) === 'MarkdownPanel'))
    expect(offenders).toEqual([])
  })

  it('outside the facade and its owners, nothing imports an owner', () => {
    const owners = new Set(OWNERS.map(f => f.replace(/\.tsx?$/, '')))
    const offenders: string[] = []
    for (const rel of sourceFiles(SRC)) {
      const file = rel.split('/').pop()!
      if (rel.startsWith('components/') && !rel.slice('components/'.length).includes('/')
        && (file === FACADE || OWNERS.includes(file))) continue
      const dir = join(SRC, rel, '..')
      for (const spec of importSpecifiers(readFileSync(join(SRC, rel), 'utf8'))) {
        const target = componentOf(spec, dir)
        if (target && owners.has(target)) offenders.push(`${rel} -> ${spec}`)
      }
    }
    expect(offenders).toEqual([])
  })

  it('the import scan sees both import shapes', () => {
    const probe = "import a from './MarkdownPanel'\nexport { b } from '../x'\nconst c = () => import('./Other')\n"
    expect(importSpecifiers(probe)).toEqual(['./MarkdownPanel', '../x', './Other'])
    expect(componentOf('./MarkdownPanel', COMPONENTS)).toBe('MarkdownPanel')
    expect(componentOf('../x', COMPONENTS)).toBeNull()
  })

  it('exports every value name its consumers import', () => {
    expect(Object.keys(facadeModule).sort()).toEqual(expect.arrayContaining([
      'FileHeaderBreadcrumb',
      'MISSING_FILE_RETRY_MAX_MS',
      'MISSING_FILE_RETRY_MS',
      'OverflowMenu',
      'breadcrumbSegments',
      'default',
      'findCoords',
      'resolveSourcePos',
    ]))
  })
})
