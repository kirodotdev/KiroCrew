// `useWebSocket.ts` is a facade over the owners in `hooks/websocket/`. Three
// properties of that composition are invisible to the frame-level specs, so
// they are pinned here at the source level (the way virtualizerOwnership.test.ts
// pins the chat virtualizer's):
//
//   1. ONE PUBLIC SURFACE. Every consumer (App, SessionGridView, the capture
//      pages) and every test that mocks `../hooks/useWebSocket` reaches the
//      socket through the facade, so the facade keeps its exact export list,
//      re-exports each moved helper as the owner's own binding, and no module
//      outside `hooks/websocket/` except the facade (and this test) imports an
//      owner. An owner importing the facade back would form an import cycle
//      with the module that composes it, where a binding read during module
//      evaluation can still be uninitialized.
//   2. EFFECT ORDER. React runs effects in hook-call order. The workflow heal
//      tick, the pending-chunk drain registration, the silence watchdog and the
//      mount effect run in that order, so only the two owners that carry one
//      of them may declare an effect, and the facade calls them in order.
//   3. DEPENDENCY DIRECTION. Owners exchange values through the facade; the
//      value imports between owners are the short list below, and they form no
//      cycle.

import { describe, it, expect } from 'vitest'
import { readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'
import { readSource } from './readSource'
import * as facade from '../hooks/useWebSocket'
import * as serverState from '../hooks/websocket/serverState'
import * as composerCards from '../hooks/websocket/composerCards'
import * as retiredIds from '../hooks/websocket/retiredIds'
import * as bundleReload from '../hooks/websocket/bundleReload'
import * as attention from '../hooks/websocket/attention'

const SRC = join(__dirname, '..')
const WEBSITE = join(SRC, '..')
const DIR = join(SRC, 'hooks', 'websocket')
const FACADE = join(SRC, 'hooks', 'useWebSocket.ts')
/** Strip comments so prose naming a module or a hook is not a match. */
const code = (path: string) => readSource(path)
  .replace(/^\s*\/\*[\s\S]*?\*\//gm, '')
  .replace(/^\s*\/\/.*$/gm, '')

const OWNER_MODULES = [
  'approvals.ts',
  'attention.ts',
  'automationSeed.ts',
  'browserEvents.ts',
  'bundleReload.ts',
  'chatStream.ts',
  'composerCards.ts',
  'connection.ts',
  'frames.ts',
  'reconnectCatchUp.ts',
  'retiredIds.ts',
  'serverState.ts',
  'slotList.ts',
  'streamBuffers.ts',
  'turnCompletion.ts',
  'voicePlayback.ts',
  'workflowRuns.ts',
]

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

describe('the websocket owner directory is fully classified', () => {
  it('every module is a listed owner', () => {
    // A new file has to be added here, which puts it under the rules below.
    const files = readdirSync(DIR).filter((f) => /\.tsx?$/.test(f)).sort()
    expect(files).toEqual([...OWNER_MODULES].sort())
  })
})

describe('one public surface', () => {
  it('the facade exports exactly its original names', () => {
    expect(Object.keys(facade).sort()).toEqual([
      'UPDATE_RESTART_LATCH_KEY',
      'UPDATE_RESTART_LATCH_TTL_MS',
      'WS_SILENCE_CHECK_MS',
      'WS_SILENCE_MAX_MS',
      'WS_SILENCE_MS',
      '__resetRedactionHealForTests',
      'askIdsOf',
      'consumeUpdateRestartLatch',
      'emitSlotFocused',
      'healRedactionSwitchAfterReconnect',
      'identityOf',
      'reconcileQuestions',
      'resolvedSince',
      'staleAskIds',
      'useWebSocket',
    ])
  })

  it('re-exports each moved helper as the owner binding, so module state has one home', () => {
    // The redaction latch and the slot-focus sender are module state: a copy
    // would let a reset or a bind reach a different variable from the reader.
    expect(facade.__resetRedactionHealForTests).toBe(serverState.__resetRedactionHealForTests)
    expect(facade.healRedactionSwitchAfterReconnect).toBe(serverState.healRedactionSwitchAfterReconnect)
    expect(facade.identityOf).toBe(composerCards.identityOf)
    expect(facade.askIdsOf).toBe(composerCards.askIdsOf)
    expect(facade.reconcileQuestions).toBe(composerCards.reconcileQuestions)
    expect(facade.staleAskIds).toBe(composerCards.staleAskIds)
    expect(facade.resolvedSince).toBe(retiredIds.resolvedSince)
    expect(facade.UPDATE_RESTART_LATCH_KEY).toBe(bundleReload.UPDATE_RESTART_LATCH_KEY)
    expect(facade.UPDATE_RESTART_LATCH_TTL_MS).toBe(bundleReload.UPDATE_RESTART_LATCH_TTL_MS)
    expect(facade.consumeUpdateRestartLatch).toBe(bundleReload.consumeUpdateRestartLatch)
    expect(facade.emitSlotFocused).toBe(attention.emitSlotFocused)
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
  it('only the workflow heal and the chunk-drain owners declare an effect', () => {
    const withEffects = OWNER_MODULES.filter((file) => /\buse(Layout)?Effect\(/.test(code(join(DIR, file))))
    expect(withEffects).toEqual(['streamBuffers.ts', 'workflowRuns.ts'])
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
    expect(src.slice(effects[0], effects[1])).toContain('silenceWindowMs')
    expect(src.slice(effects[1])).toContain('attachFocusRelay(')
  })
})

describe('dependency direction', () => {
  // The only value edges between owners. Everything else an owner needs from
  // another arrives through the facade as a parameter; `import type` is free.
  const EDGES: Record<string, string[]> = {
    'approvals.ts': ['attention.ts', 'retiredIds.ts'],
    'chatStream.ts': ['attention.ts', 'browserEvents.ts'],
    'composerCards.ts': ['attention.ts', 'retiredIds.ts'],
    'reconnectCatchUp.ts': ['bundleReload.ts', 'serverState.ts'],
    'serverState.ts': ['browserEvents.ts'],
    'turnCompletion.ts': ['attention.ts', 'serverState.ts'],
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
