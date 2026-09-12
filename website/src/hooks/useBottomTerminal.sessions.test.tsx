import { act, renderHook, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  __resetBottomTerminal, addTab, closeBottomTerminal, getBottomTerminalSnapshot,
  MAX_TERMINALS, refreshTerminalState, openBottomTerminal, removeTab, setActiveTab, setBottomTerminalWidth,
  setTerminalPosition, terminalSessionStorageKey, useBottomTerminal, useTerminalStateFailed, useTerminalCapacityExceeded, toggleBottomTerminal,
} from './useBottomTerminal'

import BottomTerminalPanel from '../components/BottomTerminalPanel'
import { renderWithProviders } from '../test/helpers'
import { withTerminalStateLock, readTerminalStateSnapshot } from '../utils/terminalStateLock'
const lock = vi.hoisted(() => ({ tail: Promise.resolve() as Promise<unknown>, values: new Map<string, string>(), revision: 0, fail: false }))
vi.mock('../utils/terminalStateLock', () => ({
  withTerminalStateLock: <T,>(operation: (values: Map<string, string>, revision: number) => T) => {
    const next = lock.tail.then(() => {
      if (lock.fail) throw new Error('unavailable')
      const values = new Map(lock.values)
      const result = operation(values, lock.revision)
      lock.values = values
      lock.revision++
      return { result, snapshot: { revision: lock.revision, values: new Map(values) } }
    })
    lock.tail = next.catch(() => {})
    return next
  },
  readTerminalStateSnapshot: () => lock.tail.then(() => {
    if (lock.fail) throw new Error('unavailable')
    return { revision: lock.revision, values: new Map(lock.values) }
  }),
  __resetTerminalStateForTests: () => lock.tail.then(() => { lock.values = new Map(); lock.revision = 0; lock.fail = false }),
}))

beforeEach(async () => { await __resetBottomTerminal(); localStorage.clear() })
describe('session-owned terminal panel', () => {
  it('switches snapshots immediately and restores exact tabs, active tab, and visibility', async () => {
    const a1 = (await addTab('/a', 'A'))!
    const a2 = (await addTab('/a2', 'A'))!
    await setActiveTab(a1, 'A')
    const { result, rerender } = renderHook(({ scope }) => useBottomTerminal(scope), { initialProps: { scope: 'A' } })
    expect(result.current.tabs.map(tab => tab.id)).toEqual([a1, a2])
    rerender({ scope: 'B' })
    expect(result.current.tabs).toEqual([])
    expect(result.current.open).toBe(false)
    let b = ''
    await act(async () => { b = (await addTab('/b', 'B'))!; await closeBottomTerminal('B') })
    expect(result.current.tabs[0].id).toBe(b)
    expect(result.current.open).toBe(false)
    rerender({ scope: 'A' })
    expect(result.current.tabs.map(tab => tab.id)).toEqual([a1, a2])
    expect(result.current.activeId).toBe(a1)
    expect(result.current.open).toBe(true)
  })

  it('keeps layout shared and the allocation cap global across all scopes', async () => {
    setTerminalPosition('right')
    setBottomTerminalWidth(240)
    for (let i = 0; i < MAX_TERMINALS; i++) expect(await addTab(undefined, i % 2 ? 'A' : 'B')).not.toBeNull()
    expect(await addTab(undefined, 'C')).toBeNull()
    await openBottomTerminal(undefined, 'C')
    expect(getBottomTerminalSnapshot('C').tabs).toEqual([])
    expect(getBottomTerminalSnapshot('A').position).toBe('right')
    expect(getBottomTerminalSnapshot('B').width).toBe(240)
    const id = getBottomTerminalSnapshot('A').tabs[0].id
    await removeTab(id, 'A')
    expect(await addTab(undefined, 'C')).not.toBeNull()
  })

  it('a delayed close bound to A cannot remove or hide B after selection changes', async () => {
    const a = (await addTab(undefined, 'A'))!
    const b = (await addTab(undefined, 'B'))!
    let finish!: () => void
    const deleting = new Promise<void>(resolve => { finish = resolve }).then(async () => await removeTab(a, 'A'))
    getBottomTerminalSnapshot('B')
    finish(); await deleting
    expect(getBottomTerminalSnapshot('A').tabs).toEqual([])
    expect(getBottomTerminalSnapshot('B').tabs.map(tab => tab.id)).toEqual([b])
    expect(getBottomTerminalSnapshot('B').open).toBe(true)
  })

  it('adopts only the changed session storage record', async () => {
    const a = (await addTab(undefined, 'A'))!
    const b = (await addTab(undefined, 'B'))!
    const updated = { open: false, tabs: [{ id: a, cwd: '/updated' }], activeId: a }
    const key = terminalSessionStorageKey('A')
    await withTerminalStateLock(values => { values.set(key, JSON.stringify(updated)) })
    window.dispatchEvent(new StorageEvent('storage', { key: 'mc-bottom-terminal-updated', newValue: 'changed' }))
    await waitFor(() => expect(getBottomTerminalSnapshot('A').open).toBe(false))
    expect(getBottomTerminalSnapshot('B').tabs.map(tab => tab.id)).toEqual([b])
    await removeTab(a, 'A')
    expect(JSON.parse((await readTerminalStateSnapshot()).values.get(terminalSessionStorageKey('B'))!).tabs[0].id).toBe(b)
  })

  it('keeps ownerless legacy tabs until selection, migrates once, and survives reload', async () => {
    localStorage.setItem('mc-bottom-terminal', JSON.stringify({ open: true, height: 260, width: 410, position: 'right',
      tabs: [{ id: 'legacy-1', cwd: '/keep' }, { id: 'legacy-2' }], activeId: 'legacy-2' }))
    vi.resetModules()
    const first = await import('./useBottomTerminal')
    await first.initializeTerminalSession(null)
    expect(first.getBottomTerminalSnapshot(null).tabs.map(tab => tab.id)).toEqual(['legacy-1', 'legacy-2'])
    expect(JSON.parse(localStorage.getItem('mc-bottom-terminal')!).tabs).toHaveLength(2)
    await first.initializeTerminalSession('A')
    expect(first.getBottomTerminalSnapshot('A').activeId).toBe('legacy-2')
    expect(first.getBottomTerminalSnapshot('B').tabs).toEqual([])
    expect(first.getBottomTerminalSnapshot(null).tabs).toEqual([])
    vi.resetModules()
    const reloaded = await import('./useBottomTerminal')
    await reloaded.initializeTerminalSession('B')
    expect(reloaded.getBottomTerminalSnapshot('B').tabs).toEqual([])
    expect(reloaded.getBottomTerminalSnapshot('A').tabs).toEqual([{ id: 'legacy-1', cwd: '/keep' }, { id: 'legacy-2' }])
    expect(reloaded.getBottomTerminalSnapshot('A').position).toBe('right')
    expect(JSON.parse((await readTerminalStateSnapshot()).values.get('mc-bottom-terminal-migration')!))
      .toEqual({ owner: 'A', completed: true })
    // A stale preference sync cannot repeat the one-time legacy import.
    localStorage.setItem('mc-bottom-terminal', JSON.stringify({ tabs: [{ id: 'stale' }], open: true }))
    await reloaded.initializeTerminalSession('C')
    expect(reloaded.getBottomTerminalSnapshot('C').tabs).toEqual([])
    expect(reloaded.getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)).toEqual(['legacy-1', 'legacy-2'])
  })

  it('serializes simultaneous allocations across independent module windows at the shared cap', async () => {
    for (let i = 0; i < MAX_TERMINALS - 1; i++) await addTab(undefined, 'seed')
    vi.resetModules()
    const other = await import('./useBottomTerminal')
    const ids = await Promise.all([addTab(undefined, 'A'), other.addTab(undefined, 'B')])
    expect(ids.filter(Boolean)).toHaveLength(1)
    const snapshot = await readTerminalStateSnapshot()
    const count = Array.from(snapshot.values).filter(([key]) => key.startsWith('mc-bottom-terminal-session:'))
      .reduce((total, [, raw]) => total + JSON.parse(raw).tabs.length, 0)
    expect(count).toBe(MAX_TERMINALS)
  })


  it('fails closed with a scoped state error when canonical storage is unavailable', async () => {
    lock.fail = true
    const { result } = renderHook(() => ({ a: useTerminalStateFailed('A'), b: useTerminalStateFailed('B') }))
    await act(async () => { expect(await addTab(undefined, 'A')).toBeNull() })
    expect(result.current).toEqual({ a: true, b: false })
    expect(getBottomTerminalSnapshot('A').tabs).toEqual([])
  })

  it('explains a full shared cap only in B when A owns every terminal', async () => {
    for (let i = 0; i < MAX_TERMINALS; i++) await addTab('/a', 'A')
    const originalIds = getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)
    const { result } = renderHook(() => ({
      a: useTerminalCapacityExceeded('A'), b: useTerminalCapacityExceeded('B'),
      storageError: useTerminalStateFailed('B'),
    }))
    await act(async () => { await toggleBottomTerminal('/b', 'B') })
    expect(result.current).toEqual({ a: false, b: true, storageError: false })
    expect(getBottomTerminalSnapshot('B').tabs).toEqual([])
    expect(getBottomTerminalSnapshot('B').open).toBe(false)
    expect(getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)).toEqual(originalIds)
    expect(getBottomTerminalSnapshot('A').open).toBe(true)
    renderWithProviders(<BottomTerminalPanel sessionScope="B" />)
    expect(screen.getByTestId('terminal-capacity-error')).toHaveTextContent('Maximum 8 terminals')
    expect(screen.queryByTestId('terminal-state-error')).not.toBeInTheDocument()
  })

  it('publishes the final canonical removal before allowing a closed popout to reconnect', async () => {
    const id = (await addTab('/a', 'A'))!
    const committed = await withTerminalStateLock(values => {
      values.set(terminalSessionStorageKey('A'), JSON.stringify({ tabs: [], open: false, activeId: null }))
    })
    expect(getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)).toEqual([id])
    expect(await refreshTerminalState('A')).toBe(true)
    expect(getBottomTerminalSnapshot('A').tabs).toEqual([])
    expect(getBottomTerminalSnapshot('A').open).toBe(false)
    expect((await readTerminalStateSnapshot()).revision).toBe(committed.snapshot.revision)
  })

  it('keeps the popout detached with only its scoped error when canonical refresh fails', async () => {
    await addTab('/a', 'A')
    const { result } = renderHook(() => ({ a: useTerminalStateFailed('A'), b: useTerminalStateFailed('B') }))
    lock.fail = true
    await act(async () => { expect(await refreshTerminalState('A')).toBe(false) })
    expect(result.current).toEqual({ a: true, b: false })
  })

  it('does not bootstrap or reconnect from revision-zero authority during close acknowledgement', async () => {
    localStorage.setItem('mc-bottom-terminal', JSON.stringify({ tabs: [{ id: 'stale-pty' }], open: true }))
    const { result } = renderHook(() => useTerminalStateFailed('A'))
    await act(async () => { expect(await refreshTerminalState('A')).toBe(false) })
    expect(result.current).toBe(true)
    expect((await readTerminalStateSnapshot()).revision).toBe(0)
    expect(getBottomTerminalSnapshot('A').tabs).toEqual([])
  })

})
