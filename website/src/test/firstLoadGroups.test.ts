import { firstLoadGroups } from '../../scripts/lib/firstLoadGroup.mjs'

// The groups decide which chunk each first-load module lands in. Two promises
// matter: lazy code and app-window code stay out of the dashboard's own chunks,
// and no layer imports a higher one (an upward import is a chunk cycle, and a
// cycle blanks the page before React mounts).

type Mod = { imports?: string[]; dynamic?: string[]; size?: number }

/** A fake chunking context over a module graph keyed by id. */
function ctxOf(graph: Record<string, Mod>) {
  return {
    getModuleInfo(id: string) {
      const m = graph[id]
      if (!m) return null
      return { id, code: 'x'.repeat(m.size ?? 10), importedIds: m.imports ?? [], dynamicallyImportedIds: m.dynamic ?? [] }
    },
  }
}

/** Every module's chunk name under the given groups, highest priority first. */
function assign(graph: Record<string, Mod>, layers: number) {
  const groups = firstLoadGroups({ mainEntryId: 'index.html', otherEntryIds: ['app.html'], layers, priority: 1 })
  const ctx = ctxOf(graph)
  const sorted = [...groups].sort((a, b) => b.priority - a.priority)
  const out: Record<string, string | null> = {}
  for (const id of Object.keys(graph)) {
    out[id] = null
    for (const g of sorted) {
      const name = g.name(id, ctx)
      if (name) {
        out[id] = name
        break
      }
    }
  }
  return out
}

const GRAPH: Record<string, Mod> = {
  'index.html': { imports: ['/src/main.tsx'] },
  '/src/main.tsx': { imports: ['/src/App.tsx', '/src/i18n.ts'], dynamic: ['/src/Lazy.tsx'], size: 100 },
  '/src/App.tsx': { imports: ['/src/a.ts', '/src/b.ts', '/node_modules/lib/index.js'], size: 100 },
  '/src/a.ts': { imports: ['/src/b.ts'], size: 100 },
  // b and c import each other: one component, so one chunk.
  '/src/b.ts': { imports: ['/src/c.ts', '/src/leaf.ts'], size: 100 },
  '/src/c.ts': { imports: ['/src/b.ts'], size: 100 },
  '/src/leaf.ts': { size: 100 },
  '/src/i18n.ts': { size: 100 },
  '/node_modules/lib/index.js': { size: 100 },
  '/src/Lazy.tsx': { imports: ['/src/leaf.ts'], size: 100 },
  'app.html': { imports: ['/src/appMain.ts'] },
  '/src/appMain.ts': { dynamic: ['/src/i18n.ts'] },
}

describe('firstLoadGroups', () => {
  it('keeps lazy code out and sends what an app window reaches to the shared chunk', () => {
    const out = assign(GRAPH, 3)
    expect(out['/src/Lazy.tsx']).toBeNull()
    expect(out['/src/appMain.ts']).toBeNull()
    expect(out['/src/i18n.ts']).toBe('first-load-shared')
    expect(out['/node_modules/lib/index.js']).toBe('first-load-vendor')
  })

  it('keeps an import loop in one chunk and never points a layer upward', () => {
    const out = assign(GRAPH, 3)
    expect(out['/src/b.ts']).toBe(out['/src/c.ts'])
    const layer = (id: string) => Number(/first-load-(\d+)/.exec(out[id] ?? '')?.[1])
    for (const [id, m] of Object.entries(GRAPH)) {
      if (Number.isNaN(layer(id))) continue
      for (const dep of m.imports ?? []) {
        if (!Number.isNaN(layer(dep))) expect(layer(dep)).toBeLessThanOrEqual(layer(id))
      }
    }
    expect(layer('/src/leaf.ts')).toBe(0)
    expect(layer('/src/main.tsx')).toBe(2)
  })
})
