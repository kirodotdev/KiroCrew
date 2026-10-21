import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join, relative, sep } from 'node:path'
import * as facadeModule from './AppDetailPage'
import * as artModule from './AppDetailPageArt'
import { pageFamilyFiles } from '../test/pageFamily'

/* ── AppDetailPage's module boundary. `pages/AppDetailPage.tsx` is the only
 *    import path: the router lazy-loads it, and the specs that mount the shell
 *    mock it by that id, so a module that imported an owner directly would slip
 *    past those mocks. Imports run one way: the page imports its owners, and no
 *    owner imports the page. The page exports the two art components its
 *    callers import, each as the owning module's own binding. The owners are
 *    the page family `test/pageFamily.ts` reads, so a pin that joins the
 *    family's text (`readPageFamily`) covers every one of them. ── */

const WEBSITE = join(__dirname, '..', '..')
const ROOTS = ['src', 'capture']
const PAGES = __dirname
const FACADE = 'AppDetailPage.tsx'
const OWNERS = pageFamilyFiles('AppDetailPage', PAGES).slice(1)

/** Static, side-effect and dynamic import specifiers of a module. */
function importSpecifiers(src: string): string[] {
  const out: string[] = []
  for (const m of src.matchAll(/^(?:import|export)\b[^'"]*?\bfrom\s+['"]([^'"]+)['"]/gm)) out.push(m[1])
  for (const m of src.matchAll(/^import\s+['"]([^'"]+)['"]/gm)) out.push(m[1])
  for (const m of src.matchAll(/\bimport\(\s*['"]([^'"]+)['"]\s*\)/g)) out.push(m[1])
  return out
}

/** The pages-directory module a relative or `@/` specifier names, or null for anything else. */
function pagesModuleOf(spec: string, fromDir: string): string | null {
  const abs = spec.startsWith('@/') ? join(WEBSITE, 'src', spec.slice(2))
    : spec.startsWith('.') ? join(fromDir, spec) : null
  if (abs === null) return null
  const rel = relative(PAGES, abs).split(sep).join('/')
  return rel.includes('/') ? null : rel.replace(/\.tsx?$/, '')
}

/** Non-test source files under a website root, relative to the website dir. */
function sourceFiles(root: string): string[] {
  return readdirSync(join(WEBSITE, root), { recursive: true, encoding: 'utf8' })
    .map(p => `${root}/${p.split('\\').join('/')}`)
    .filter(p => /\.tsx?$/.test(p) && !/\.test\.tsx?$/.test(p) && !p.startsWith('src/test/'))
}

describe('AppDetailPage module boundary', () => {
  it('names its owners', () => {
    expect(OWNERS).toEqual([
      'AppDetailPageArt.tsx',
      'appDetailPageTypes.ts',
    ])
  })

  it('include every sibling module the page imports under its name', () => {
    const siblings = importSpecifiers(readFileSync(join(PAGES, FACADE), 'utf8'))
      .map(s => pagesModuleOf(s, PAGES))
      .filter((m): m is string => m !== null && /^(use)?[Aa]ppDetailPage/.test(m))
    expect(siblings.length).toBeGreaterThan(0)
    for (const m of siblings) expect(OWNERS.map(f => f.replace(/\.tsx?$/, '')), m).toContain(m)
  })

  it('no owner imports the page', () => {
    const offenders = OWNERS.filter(f => importSpecifiers(readFileSync(join(PAGES, f), 'utf8'))
      .some(s => pagesModuleOf(s, PAGES) === 'AppDetailPage'))
    expect(offenders).toEqual([])
  })

  it('outside the page and its owners, no source module imports an owner', () => {
    const owners = new Set(OWNERS.map(f => f.replace(/\.tsx?$/, '')))
    const family = new Set([FACADE, ...OWNERS].map(f => `src/pages/${f}`))
    const offenders: string[] = []
    for (const root of ROOTS) {
      for (const rel of sourceFiles(root)) {
        if (family.has(rel)) continue
        const dir = join(WEBSITE, rel, '..')
        for (const spec of importSpecifiers(readFileSync(join(WEBSITE, rel), 'utf8'))) {
          const target = pagesModuleOf(spec, dir)
          if (target && owners.has(target)) offenders.push(`${rel} -> ${spec}`)
        }
      }
    }
    expect(offenders).toEqual([])
  })

  it('the import scan sees every import shape and resolves from any directory', () => {
    const probe = "import a from './AppDetailPageArt'\nexport { b } from '../x'\nimport './AppDetailPage'\nconst c = () => import('./Other')\n"
    expect(importSpecifiers(probe)).toEqual(['./AppDetailPageArt', '../x', './AppDetailPage', './Other'])
    expect(pagesModuleOf('./AppDetailPage', PAGES)).toBe('AppDetailPage')
    expect(pagesModuleOf('../AppDetailPageArt', join(PAGES, 'apps'))).toBe('AppDetailPageArt')
    expect(pagesModuleOf('../pages/appDetailPageTypes', join(WEBSITE, 'src', 'test'))).toBe('appDetailPageTypes')
    expect(pagesModuleOf('@/pages/AppDetailPageArt', join(WEBSITE, 'capture'))).toBe('AppDetailPageArt')
    expect(pagesModuleOf('@/lib/electron', PAGES)).toBeNull()
    expect(pagesModuleOf('./apps/DiscoverPage', PAGES)).toBeNull()
    expect(pagesModuleOf('react', PAGES)).toBeNull()
    expect(sourceFiles('capture').length).toBeGreaterThan(0)
  })

  it('exports the art components its callers import, as the owner bindings', () => {
    expect(Object.keys(facadeModule)).toEqual(expect.arrayContaining(['HeroBanner', 'ScreenshotGallery', 'default']))
    expect(facadeModule.HeroBanner).toBe(artModule.HeroBanner)
    expect(facadeModule.ScreenshotGallery).toBe(artModule.ScreenshotGallery)
  })
})
