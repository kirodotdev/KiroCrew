/**
 * The Dev Fleet page's facade and its owners beside it.
 *
 * DevFleetPage.tsx stays the page's one public surface: the builtin-app
 * registry lazy-loads its default export, the capture entries mount it, and the
 * page's specs import its named exports. These pins keep that shape honest:
 *
 *  - the facade still exports what it exported, each moved name as the owner's
 *    own binding (a copy would split the module state the restart watcher and
 *    the prune labels read);
 *  - owners never import the facade, and nothing outside this page imports an
 *    owner;
 *  - no owner exports a hook for the page to call, and the render pieces the
 *    page component handed out call none, so every hook the page registers is
 *    still the facade's own.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'

import * as facade from './DevFleetPage'
import * as model from './devFleetPageModel'
import { pageFamilyFiles } from '../test/pageFamily'

const DIR = __dirname
const SRC = resolve(DIR, '..')
const FACADE = join(DIR, 'DevFleetPage.tsx')
const owners = pageFamilyFiles('DevFleetPage', DIR).slice(1)
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

describe('DevFleetPage facade', () => {
  it('keeps the exports it had, the moved ones as the model’s own bindings', () => {
    expect(Object.keys(facade).sort()).toEqual([
      'LOG_GAP_MARKER', 'PRUNE_STATUS_META', '__resetDevFleetNoticesForTests', 'default',
      'gatewayRecovered', 'mergeLogWindow', 'pruneVerdictLabel',
    ])
    expect(typeof facade.default).toBe('function')
    expect(facade.gatewayRecovered).toBe(model.gatewayRecovered)
    expect(facade.pruneVerdictLabel).toBe(model.pruneVerdictLabel)
    expect(facade.PRUNE_STATUS_META).toBe(model.PRUNE_STATUS_META)
  })
})

describe('DevFleetPage owners', () => {
  it('are the five files beside the facade', () => {
    expect(owners).toEqual([
      'DevFleetPageMenus.tsx', 'DevFleetPageNotices.tsx', 'DevFleetPagePrune.tsx', 'DevFleetPageRow.tsx',
      'devFleetPageModel.ts',
    ])
  })

  it('include every sibling the facade imports under the page’s name', () => {
    const siblings = [...read(FACADE).matchAll(/from '\.\/((?:use)?[Dd]evFleetPage[A-Z.][\w.]*)'/g)].map((m) => m[1])
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

  it('leave the page component’s hooks in the facade', () => {
    // Every hook an owner calls sits in a component it renders (the menus, the
    // log panel, the detail panel); no owner exports a hook for the page to call.
    for (const f of owners) {
      expect(read(join(DIR, f)).match(/^export function use\w+\(/gm) ?? [], f).toEqual([])
    }
  })

  it('hand out render pieces that call no hook', () => {
    // These were render code inside the page component; a hook in one would
    // register under that component instead of in the page's sequence.
    const PIECES: Array<[string, string]> = [
      ['DevFleetPageRow.tsx', 'DevFleetRowActions'], ['DevFleetPageRow.tsx', 'DevFleetSyncStepper'],
      ['DevFleetPageRow.tsx', 'DevFleetProvStepper'], ['DevFleetPagePrune.tsx', 'DevFleetPruneReview'],
      ['DevFleetPagePrune.tsx', 'DevFleetPruneProgress'], ['DevFleetPageNotices.tsx', 'DevFleetPageNotices'],
    ]
    for (const [f, name] of PIECES) {
      const src = read(join(DIR, f))
      const at = src.indexOf(`export function ${name}(`)
      expect(at, `${f} declares ${name}`).toBeGreaterThan(-1)
      const next = src.indexOf('\nexport ', at + 1)
      const body = src.slice(at, next < 0 ? undefined : next)
      expect(body.match(/\buse[A-Z]\w*\(/g) ?? [], name).toEqual([])
    }
  })
})
