/**
 * The render benchmark's in-page probe (scripts/lib/render-probe.mjs) names a
 * cause for every render. This drives a small known tree on react-dom/profiling
 * and checks each cause the classifier can report, so a React upgrade that
 * moves a fiber field fails here instead of producing quietly wrong benchmark
 * attributions.
 *
 * The probe installs a stub DevTools hook that react-dom reads once, at module
 * evaluation, and a loaded copy keeps the hook it saw. So the file installs the
 * probe once, before its only import of react-dom/profiling, and each test
 * zeroes the probe's counters instead of reinstalling it.
 */
import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
// @ts-expect-error -- untyped .mjs helper shared with the Node benchmark
import { renderProbe } from '../../scripts/lib/render-probe.mjs'

type Why = { n: number; causes: Record<string, number>; parents: Record<string, number> }
type Perf = {
  counting: boolean; commits: number; renders: number
  comp: Record<string, { n: number; mounts: number }>
  why: Record<string, Why>; roots: Record<string, number>; err: string | null
}
const W = window as unknown as Record<string, unknown>
const GLOBALS = ['__REACT_DEVTOOLS_GLOBAL_HOOK__', '__perf', '__kcPerfMw', '__kcSelWrap']
const NAMES = ['Plain', 'MemoStable', 'MemoObj', 'MemoFn', 'MemoChanging', 'CtxReader', 'StoreReader', 'Local']

let React: typeof import('react')
let createRoot: typeof import('react-dom/client').createRoot
let perf: Perf

beforeAll(async () => {
  renderProbe({ why: NAMES })
  vi.resetModules()
  React = (await import('react')).default
  createRoot = (await import('react-dom/profiling')).createRoot
  perf = W.__perf as Perf
  ;(globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT = true
})

afterAll(() => {
  for (const k of GLOBALS) delete W[k]
  delete (globalThis as Record<string, unknown>).IS_REACT_ACT_ENVIRONMENT
  vi.resetModules()
})

beforeEach(() => {
  Object.assign(perf, { counting: false, commits: 0, renders: 0, comp: {}, why: {}, roots: {}, err: null })
  // react-dom/profiling is the root entry, which warns that createRoot belongs
  // to react-dom/client. The benchmark build aliases both to it on purpose.
  // eslint-disable-next-line no-console -- other errors still reach the test output
  const error = console.error
  vi.spyOn(console, 'error').mockImplementation((...a: unknown[]) => {
    if (!String(a[0]).includes('You are importing createRoot from "react-dom"')) error(...a)
  })
  return () => { vi.restoreAllMocks() }
})

describe('renderProbe', () => {
  it('names each render cause on a known tree', async () => {
    const names = NAMES
    const h = React.createElement
    const TestCtx = React.createContext(0)
    TestCtx.displayName = 'TestCtx'
    let storeValue = 0
    const listeners = new Set<() => void>()
    const store = {
      subscribe: (l: () => void) => { listeners.add(l); return () => listeners.delete(l) },
      getSnapshot: function getSnap() { return storeValue },
    }
    let bumpLocal: () => void = () => {}
    let bumpParent: () => void = () => {}

    const Plain = () => h('i')
    const MemoStable = React.memo(function MemoStable(_: { v: number }) { return h('i') })
    const MemoObj = React.memo(function MemoObj(_: { o: { a: number } }) { return h('i') })
    const MemoFn = React.memo(function MemoFn(_: { f: () => number }) { return h('i') })
    const MemoChanging = React.memo(function MemoChanging(_: { v: number }) { return h('i') })
    const CtxReader = React.memo(function CtxReader() { React.useContext(TestCtx); return h('i') })
    const StoreReader = React.memo(function StoreReader() {
      React.useSyncExternalStore(store.subscribe, store.getSnapshot)
      return h('i')
    })
    const Local = React.memo(function Local() {
      const [n, setN] = React.useState(0)
      bumpLocal = () => setN((x) => x + 1)
      return h('i', null, n)
    })
    function App() {
      const [tick, setTick] = React.useState(0)
      bumpParent = () => setTick((t) => t + 1)
      return h(TestCtx.Provider, { value: tick },
        h(Plain), h(MemoStable, { v: 1 }), h(MemoObj, { o: { a: 1 } }), h(MemoFn, { f: () => 1 }),
        h(MemoChanging, { v: tick }), h(CtxReader), h(StoreReader), h(Local))
    }

    const root = createRoot(document.createElement('div'))
    perf.counting = true
    await React.act(async () => { root.render(h(App)) })
    await React.act(async () => { bumpParent() })
    await React.act(async () => { storeValue = 1; for (const l of listeners) l() })
    await React.act(async () => { bumpLocal() })
    perf.counting = false
    await React.act(async () => { root.unmount() })

    expect(perf.err).toBeNull()
    expect(perf.commits).toBe(4)
    const causes = (n: string) => perf.why[n]?.causes ?? {}
    for (const n of names) expect(causes(n).mount).toBe(1)
    // A memo child whose props are all Object.is equal is skipped wholesale.
    expect(perf.why.MemoStable.n).toBe(1)
    expect(causes('Plain')['parent(same props)<-App']).toBe(1)
    expect(causes('MemoObj')['props:o~']).toBe(1)
    expect(causes('MemoFn')['props:fƒ']).toBe(1)
    expect(causes('MemoChanging')['props:v!']).toBe(1)
    expect(causes('CtxReader')['ctx:TestCtx!']).toBe(1)
    expect(causes('StoreReader')['uSES#0 [snap:getSnap]!']).toBe(1)
    expect(causes('Local')['useState#0!']).toBe(1)
    // The commit roots name the fiber each update landed on.
    expect(Object.keys(perf.roots)).toEqual(expect.arrayContaining(['App:useState#0!', 'StoreReader:uSES#0 [snap:getSnap]!', 'Local:useState#0!']))
  })

  it('returns one stable selector wrapper per selector', () => {
    type Wrap = ((s: unknown) => unknown) & { __kcSel?: unknown }
    const wrap = W.__kcSelWrap as (sel: (s: unknown) => unknown) => Wrap
    const f = (s: unknown) => s
    const g = (s: unknown) => s
    const wf = wrap(f)
    expect(wrap(f)).toBe(wf)
    expect(wrap(g)).not.toBe(wf)
    expect(wf.__kcSel).toBe(f)
    expect(wrap(g).__kcSel).toBe(g)
    expect(wf(7)).toBe(7)
  })

  it('counts nothing while counting is off', async () => {
    const root = createRoot(document.createElement('div'))
    await React.act(async () => { root.render(React.createElement('div')) })
    await React.act(async () => { root.unmount() })
    expect(perf.commits).toBe(0)
    expect(perf.renders).toBe(0)
  })
})
