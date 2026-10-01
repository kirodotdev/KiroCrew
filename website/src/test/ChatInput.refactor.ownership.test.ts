import { describe, expect, it } from 'vitest'
import { readdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

/* ── The composer's module boundary. `components/ChatInput.tsx` is the only
 *    import path and the composition root; its owners live in
 *    `components/chat-input/`. Imports run one way: the facade imports the
 *    owners and no owner imports the facade, because about thirty specs mock
 *    the facade by id and three of them build the mock from `importOriginal`,
 *    so an owner reaching back would receive the stub (or a half-evaluated
 *    module). The owners import each other without cycles.
 *
 *    `useComposerTreeDrop` reads the optimizer's `optimizing`, so the facade
 *    calls it after `usePromptOptimizer`, and its three effects run after the
 *    optimizer's completion effect. The two share no state: the drop caret,
 *    its animation frame and the window drag listeners on one side, the
 *    optimize slot and the undo boundary on the other. ── */

const COMPONENTS = join(__dirname, '..', 'components')
const OWNERS_DIR = join(COMPONENTS, 'chat-input')
const OWNERS = readdirSync(OWNERS_DIR).filter(f => /\.tsx?$/.test(f)).sort()
const sourceOf = (file: string) => readFileSync(join(OWNERS_DIR, file), 'utf8')

/** Static import specifiers of a module (`import ... from 'x'`, `export ... from 'x'`). */
function staticImports(src: string): string[] {
  const out: string[] = []
  for (const m of src.matchAll(/^(?:import|export)\b[^'"]*?\bfrom\s+['"]([^'"]+)['"]/gm)) out.push(m[1])
  return out
}

/** The owner a relative specifier names inside `chat-input/`, or null. */
function ownerOf(spec: string): string | null {
  if (!spec.startsWith('./')) return null
  const stem = spec.slice(2)
  return OWNERS.find(f => f.replace(/\.tsx?$/, '') === stem) ?? null
}

/** A cycle in `graph` as a path of nodes, or null. */
function findCycle(graph: Record<string, string[]>): string[] | null {
  const state: Record<string, 'open' | 'done'> = {}
  const stack: string[] = []
  const visit = (n: string): string[] | null => {
    if (state[n] === 'done') return null
    if (state[n] === 'open') return [...stack.slice(stack.indexOf(n)), n]
    state[n] = 'open'
    stack.push(n)
    for (const next of graph[n] ?? []) {
      const found = visit(next)
      if (found) return found
    }
    stack.pop()
    state[n] = 'done'
    return null
  }
  for (const n of Object.keys(graph)) {
    const found = visit(n)
    if (found) return found
  }
  return null
}

describe('ChatInput module boundary', () => {
  it('has owners to check', () => {
    expect(OWNERS.length).toBeGreaterThan(10)
  })

  it('no owner imports the facade', () => {
    const offenders = OWNERS.filter(f => staticImports(sourceOf(f)).some(s => /(^|\/)ChatInput$/.test(s)))
    expect(offenders).toEqual([])
  })

  it('the owners import each other without a cycle', () => {
    const graph = Object.fromEntries(OWNERS.map(f => [
      f,
      staticImports(sourceOf(f)).map(ownerOf).filter((o): o is string => o !== null),
    ]))
    expect(findCycle(graph)).toBeNull()
  })

  it('the cycle check can fail', () => {
    expect(findCycle({ a: ['b'], b: ['c'], c: ['a'] })).toEqual(['a', 'b', 'c', 'a'])
    expect(findCycle({ a: ['b'], b: [], c: ['a'] })).toBeNull()
  })

  it('loads the Lexical editor only as a lazy chunk', () => {
    // A static import would evaluate the editor with the composer, and a chunk
    // that fails to load must fall back to the textarea instead.
    const staticLexical = OWNERS.filter(f => staticImports(sourceOf(f)).some(s => /LexicalComposerInput$/.test(s)))
    expect(staticLexical).toEqual([])
    expect(sourceOf('engine.tsx')).toContain("lazy(() => import('../LexicalComposerInput'))")
    expect(staticImports(readFileSync(join(COMPONENTS, 'ChatInput.tsx'), 'utf8')).filter(s => /LexicalComposerInput$/.test(s))).toEqual([])
  })
})
