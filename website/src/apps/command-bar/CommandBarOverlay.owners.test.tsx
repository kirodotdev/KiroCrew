/**
 * The Command Bar's facade and the owners beside it.
 *
 * `CommandBarOverlay.tsx` stays the overlay's one import path (the `overlayRegistry`
 * lazy entry and every spec that mounts it import its default export), and calls each
 * owner hook at the point its code sits in the component's hook order. These pins keep
 * that shape honest:
 *
 *  - the facade exports only the component;
 *  - the owners are a fixed family of files, none imports the facade back, and nothing
 *    outside the family imports an owner, so mocking the overlay still replaces all of it;
 *  - the facade calls the owner hooks, and runs the code it keeps, in the component's
 *    registration order. React runs a component's effects in that order, so a reshuffle
 *    is a behaviour change even when every behaviour spec still passes.
 *
 * The order pin guards the split: each owner holds one stretch of the component's hook
 * sequence. A change that regroups state on purpose reorders registrations, reviews that
 * as the behaviour change it is, and edits `ORDER` to match.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'

import * as facade from './CommandBarOverlay'

const DIR = __dirname
const SRC = resolve(DIR, '..', '..')
const FACADE = join(DIR, 'CommandBarOverlay.tsx')
const read = (p: string) => readFileSync(p, 'utf8')
/** The file family the source pins in `CommandBarOverlay.test.tsx` and `rootIndex.test.ts` read. */
const family = readdirSync(DIR).filter(f => /^(?:use)?[Cc]ommandBarOverlay\w*\.tsx?$/.test(f)).sort()
const owners = family.filter(f => f !== 'CommandBarOverlay.tsx')

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.|\.spec\./.test(name)) out.push(p)
  }
  return out
}

/** Every module a file imports (static, side-effect, `export … from`, `import type` and
 *  dynamic), resolved to an absolute path without its extension. `@/` is the `src/` alias. */
const SPEC_RE = /(?:from\s+|import\s*\(?)\s*['"]([^'"]+)['"]/g
const stem = (p: string) => p.replace(/\.(tsx?|jsx?)$/, '').replace(/\/index$/, '')
function importsOf(file: string): string[] {
  return [...read(file).matchAll(SPEC_RE)].map(m => m[1]).flatMap(spec => {
    if (spec.startsWith('@/')) return [stem(join(SRC, spec.slice(2)))]
    if (spec.startsWith('.')) return [stem(resolve(dirname(file), spec))]
    return []
  })
}

describe('CommandBarOverlay facade', () => {
  it('exports only the component', () => {
    expect(Object.keys(facade)).toEqual(['default'])
    expect(typeof facade.default).toBe('function')
  })
})

describe('CommandBarOverlay owners', () => {
  it('are the six files beside the facade', () => {
    expect(family[0]).toBe('CommandBarOverlay.tsx')
    expect(owners).toEqual([
      'CommandBarOverlayRootRows.tsx', 'CommandBarOverlaySlot.tsx', 'commandBarOverlayModel.ts',
      'useCommandBarOverlayActivation.ts', 'useCommandBarOverlayEngines.ts', 'useCommandBarOverlaySlots.ts',
    ])
  })

  it('include every sibling the facade imports under the overlay’s name', () => {
    const siblings = [...read(FACADE).matchAll(/from '\.\/((?:use)?[Cc]ommandBarOverlay\w*)'/g)].map(m => m[1])
    expect(siblings.length).toBe(owners.length)
    for (const s of siblings) expect(owners.map(f => f.replace(/\.tsx?$/, '')), s).toContain(s)
  })

  it('never import the facade back', () => {
    for (const f of owners) expect(importsOf(join(DIR, f)), f).not.toContain(stem(FACADE))
  })

  it('are imported only by the facade and by each other', () => {
    const ownerPaths = new Set(owners.map(f => join(DIR, f)))
    const ownerStems = new Set(owners.map(f => stem(join(DIR, f))))
    const outside = walk(SRC)
      .filter(p => p !== FACADE && !ownerPaths.has(p))
      .filter(p => importsOf(p).some(s => ownerStems.has(s)))
      .map(p => relative(SRC, p))
    expect(outside).toEqual([])
  })

  it('are called by the facade in the component’s hook registration order', () => {
    const src = read(FACADE)
    const ORDER = [
      'const ime = useImeGuard()',
      'const settingsGovernance = useSettingsSearchGovernance({ fetch: false })',
      'const { navigate } = usePaletteActions()',
      'const liveSlots = useAppSelector(s => s.dashboard.slots)',
      'useDialogFocusTrap(dialogRef, onClose, { handleEscape: false })',
      'useCommandBarOverlayEngines(',
      'const t = setTimeout(() => setDebounced(query), DEBOUNCE_MS)',
      'if (!open) dialogRunRef.current += 1',
      'const commandById = useMemo(',
      'useCommandBarOverlayRootRows(',
      'useCommandBarOverlaySlots(',
      'useCommandBarOverlayActivation(',
      'const copyTarget = useMemo(',
      'const measure = () => setPreviewClipped(el.scrollHeight > el.clientHeight + 1)',
      'const submitArgument = useCallback(',
      'const onKeyDown = useCallback(',
      'if (!open) return null',
      'commandBarOverlaySlotRenderer(',
      'return createPortal(',
    ]
    const at = ORDER.map(anchor => {
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
    const hooks = owners.flatMap(f => [...read(join(DIR, f)).matchAll(/^export function (use\w+)\(/gm)].map(m => m[1]))
    expect(hooks.sort()).toEqual([
      'useCommandBarOverlayActivation', 'useCommandBarOverlayEngines',
      'useCommandBarOverlayRootRows', 'useCommandBarOverlaySlots',
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

  it('keep the row renderer and the list model hook-free', () => {
    // The renderer is built after every hook the facade calls; a hook inside it would
    // register below the overlay's own and come and go with the rows it draws. The
    // model is React-free so every owner can read one copy of it.
    const renderer = read(join(DIR, 'CommandBarOverlaySlot.tsx'))
    expect(renderer.match(/\buse[A-Z]\w*\(/g) ?? []).toEqual([])
    const model = read(join(DIR, 'commandBarOverlayModel.ts'))
    expect(model.match(/\buse[A-Z]\w*\(/g) ?? []).toEqual([])
    expect(model).not.toMatch(/from 'react'/)
  })
})
