// `useWebSocket.ts` is a facade over the owners in `hooks/websocket/`. Three
// properties of that composition are invisible to the frame-level specs, so
// they are pinned here at the source level (the way virtualizerOwnership.test.ts
// pins the chat virtualizer's):
//
//   1. ONE PUBLIC SURFACE. Every consumer (App, SessionGridView, the capture
//      pages) and every test that mocks `../hooks/useWebSocket` reaches the
//      socket through the facade, so every name they import from it resolves,
//      it re-exports each moved helper as the owner's own binding, and no module
//      outside `hooks/websocket/` except the facade (and this test) imports an
//      owner. An owner importing the facade back would form an import cycle
//      with the module that composes it, where a binding read during module
//      evaluation can still be uninitialized.
//   2. EFFECT ORDER. React runs effects in hook-call order. The workflow heal
//      tick, the pending-chunk drain registration, the silence watchdog and the
//      mount effect run in that order, so only the two owners that carry one
//      of them may declare an effect, and the facade calls them in order.
//   3. DEPENDENCY DIRECTION. Owners exchange values through the facade; the
//      value imports between owners, read from their sources, form no cycle.
//
// Every non-test module in `hooks/websocket/` is an owner, read from the
// directory, so a new owner falls under these rules without editing this file.

import { describe, it, expect } from 'vitest'
import { readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { readSource } from './readSource'
import * as facade from '../hooks/useWebSocket'

type Module = Record<string, unknown>
const OWNERS = import.meta.glob<Module>(['../hooks/websocket/*.ts', '!../hooks/websocket/*.test.ts'], { eager: true })

const SRC = join(__dirname, '..')
const WEBSITE = join(SRC, '..')
const DIR = join(SRC, 'hooks', 'websocket')
const FACADE = join(SRC, 'hooks', 'useWebSocket.ts')
/** Strip comments so prose naming a module or a hook is not a match. */
const code = (path: string) => readSource(path)
  .replace(/^\s*\/\*[\s\S]*?\*\//gm, '')
  .replace(/^\s*\/\/.*$/gm, '')

const OWNER_MODULES = readdirSync(DIR)
  .filter((f) => /\.tsx?$/.test(f) && !/\.test\.tsx?$/.test(f))
  .sort()

/** Value imports (not `import type`) of `./<owner>` specifiers, per owner. */
function ownerValueImports(file: string): string[] {
  const out: string[] = []
  // Each statement ends at its first `from '...'`, so a lazy match cannot run
  // from one import into the next.
  for (const m of code(join(DIR, file)).matchAll(/^import\s+(type\s+)?[\s\S]*?from\s+'([^']+)'/gm)) {
    const local = /^\.\/([A-Za-z]+)$/.exec(m[2])
    if (local && !m[1]) out.push(`${local[1]}.ts`)
  }
  return out.sort()
}

describe('one public surface', () => {
  it('reads the owners and the facade it checks', () => {
    expect(OWNER_MODULES.length).toBeGreaterThan(0)
    expect(Object.keys(OWNERS).map((k) => k.split('/').pop()).sort()).toEqual(OWNER_MODULES)
    expect(Object.keys(facade).length).toBeGreaterThan(0)
  })

  it('resolves every runtime name a module imports from the facade', () => {
    const wanted = new Map<string, string>()
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const full = join(dir, name)
        if (statSync(full).isDirectory()) { walk(full); continue }
        if (!/\.tsx?$/.test(name)) continue
        for (const [, clause, spec] of readSource(full).matchAll(/import\s+(?!type\b)((?:[\w$]+\s*,\s*)?\{[^}]*\}|[\w$]+)\s+from\s+'(\.{1,2}\/[^']+)'/g)) {
          if (resolve(dirname(full), spec) !== FACADE.replace(/\.ts$/, '')) continue
          for (const raw of (clause.match(/\{([^}]*)\}/)?.[1] ?? '').split(',')) {
            const part = raw.trim()
            if (part && !part.startsWith('type ')) wanted.set(part.split(/\s+as\s+/)[0], relative(WEBSITE, full))
          }
        }
      }
    }
    walk(SRC)
    walk(join(WEBSITE, 'capture'))
    walk(join(WEBSITE, 'integration'))
    expect(wanted.size, 'no importer of the facade found').toBeGreaterThan(0)
    for (const [name, file] of wanted) {
      expect(name in facade, `${file} imports ${name}, which useWebSocket does not export`).toBe(true)
    }
  })

  it('re-exports each moved helper as the owner binding, so module state has one home', () => {
    // The redaction latch, the slot-focus sender, the accepted-revision ledger and
    // the usage-frame refresh are module state: a copy would let a reset or a
    // bind reach a different variable from the reader. Every facade export that
    // an owner also exports must be that owner's own binding.
    let shared = 0
    for (const [name, value] of Object.entries(facade)) {
      for (const [path, owner] of Object.entries(OWNERS)) {
        if (!(name in owner)) continue
        shared += 1
        expect(value, `useWebSocket.${name} is a copy of ${path}'s, not its binding`).toBe(owner[name])
      }
    }
    expect(shared, 'the facade re-exports nothing from an owner').toBeGreaterThan(0)
  })

  it('no owner imports the facade', () => {
    for (const file of OWNER_MODULES) {
      expect(code(join(DIR, file)), file).not.toMatch(/from\s+'\.\.\/useWebSocket'/)
    }
  })

  it('outside the owner directory, only the facade imports an owner', () => {
    const offenders: string[] = []
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const full = join(dir, name)
        if (statSync(full).isDirectory()) {
          if (full !== DIR) walk(full)
          continue
        }
        if (!/\.tsx?$/.test(name) || full === FACADE) continue
        if (full === join(SRC, 'test', 'useWebSocket.ownership.test.ts')) continue
        for (const m of code(full).matchAll(/from\s+'(\.{1,2}\/[^']+)'/g)) {
          if (resolve(dirname(full), m[1]).startsWith(DIR)) offenders.push(relative(WEBSITE, full))
        }
      }
    }
    walk(SRC)
    walk(join(WEBSITE, 'capture'))
    walk(join(WEBSITE, 'integration'))
    expect(offenders).toEqual([])
  })
})

describe('effect order', () => {
  it('only the workflow heal, the chunk-drain and the row-watchdog owners declare an effect', () => {
    const withEffects = OWNER_MODULES.filter((file) => /\buse(Layout)?Effect\(/.test(code(join(DIR, file))))
    expect(withEffects).toEqual(['rowDeliveryWatchdog.ts', 'streamBuffers.ts', 'workflowRuns.ts'])
  })

  it('the facade composes them before its own watchdog and mount effects', () => {
    const src = code(FACADE)
    const heal = src.indexOf('useWorkflowRunReconcile(')
    const drain = src.indexOf('useStreamBuffers(')
    const effects = [...src.matchAll(/\buseEffect\(/g)].map((m) => m.index ?? -1)
    expect(heal).toBeGreaterThan(-1)
    expect(drain).toBeGreaterThan(heal)
    // Exactly two of its own: the silence watchdog, then the mount effect.
    expect(effects).toHaveLength(2)
    expect(effects[0]).toBeGreaterThan(drain)
    expect(src.slice(effects[0], effects[1])).toContain('silenceHostRef.current = {')
    expect(src.slice(effects[1])).toContain('attachFocusRelay(')
  })
})

describe('dependency direction', () => {
  // Review list: owners exchange values through the facade as parameters, so a
  // direct value import between two owners is a reviewed exception, one line per
  // owner. `import type` is free.
  const EDGES: Record<string, string[]> = {
    'approvals.ts': ['attention.ts', 'retiredIds.ts'],
    'chatStream.ts': ['attention.ts', 'browserEvents.ts'],
    'composerCards.ts': ['attention.ts', 'retiredIds.ts'],
    'connection.ts': ['gatewayConnectivity.ts', 'relayClose.ts'],
    'foldWatch.ts': ['serverState.ts'],
    'reconnectCatchUp.ts': ['bundleReload.ts', 'gatewayConnectivity.ts', 'serverState.ts'],
    'serverState.ts': ['browserEvents.ts'],
    'serverReads.ts': ['attention.ts'],
    'slotList.ts': ['idleRowRefresh.ts', 'paneResync.ts', 'serverReads.ts'],
    'turnCompletion.ts': ['attention.ts', 'idleRowRefresh.ts', 'serverState.ts'],
  }

  it('owners import each other only along the listed edges', () => {
    for (const file of OWNER_MODULES) {
      expect(ownerValueImports(file), file).toEqual(EDGES[file] ?? [])
    }
  })

  it('the edges form no cycle', () => {
    const visiting = new Set<string>()
    const done = new Set<string>()
    const visit = (file: string, path: string[]) => {
      expect(visiting.has(file), [...path, file].join(' -> ')).toBe(false)
      if (done.has(file)) return
      visiting.add(file)
      for (const next of EDGES[file] ?? []) visit(next, [...path, file])
      visiting.delete(file)
      done.add(file)
    }
    for (const file of OWNER_MODULES) visit(file, [])
  })
})
