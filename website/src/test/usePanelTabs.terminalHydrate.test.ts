/**
 * Hydrate-time ruling on the side-panel strip's restored terminal tabs.
 *
 * `loadPersisted` restores terminal tabs by session id without asking the
 * backend which PTYs still exist, and the WS route mints a fresh shell for an
 * id it does not know. So the strip stays pending until two looks at
 * `GET /api/terminal/sessions` rule, drops only tabs absent both times, and
 * persists the trimmed bucket. Each case boots the module fresh from storage.
 */
import { describe, it, expect, afterEach, vi } from 'vitest'
import { renderHook, act } from '@testing-library/react'

type Store = typeof import('../hooks/usePanelTabs')
let current: Store | null = null

const term = (sid: string) => ({ id: `terminal:${sid}`, kind: 'terminal', title: 'sh', sessionId: sid })
const doc = { id: 'file:/a.txt', kind: 'file', title: 'a.txt', path: '/a.txt' }

async function bootWith(buckets: Record<string, { tabs: object[]; activeId: string | null }>): Promise<Store> {
  for (const [slot, b] of Object.entries(buckets)) localStorage.setItem(`mc-panel-tabs:${slot}`, JSON.stringify(b))
  vi.resetModules()
  current = await import('../hooks/usePanelTabs')
  return current
}

const persisted = (slot: string) => {
  const raw = localStorage.getItem(`mc-panel-tabs:${slot}`)
  return raw ? (JSON.parse(raw) as { tabs: { id: string }[]; activeId: string | null }) : null
}

const listing = (...entries: [string, boolean][]) => ({
  enabled: true,
  sessions: entries.map(([session_id, alive]) => ({ session_id, alive })),
})

afterEach(() => {
  vi.useRealTimers()
  current?.__resetPanelTabs()
  current = null
  localStorage.clear()
})

describe('usePanelTabs: restored terminal tabs at hydrate', () => {
  it('is not pending when no terminal tab was restored', async () => {
    const store = await bootWith({ s1: { tabs: [doc], activeId: doc.id } })
    const { result } = renderHook(() => store.usePanelTerminalsPending())
    expect(result.current).toBe(false)
  })

  it('boots past a malformed persisted tab entry', async () => {
    const store = await bootWith({ s1: { tabs: [null, term('a')], activeId: 'terminal:a' } })
    expect(store.reconcileRestoredPanelTerminals(listing())).toEqual(['a'])
  })

  it('keeps live terminals and settles on the first look', async () => {
    const store = await bootWith({ s1: { tabs: [doc, term('a')], activeId: 'terminal:a' } })
    const { result } = renderHook(() => store.usePanelTerminalsPending())
    expect(result.current).toBe(true)
    act(() => { expect(store.reconcileRestoredPanelTerminals(listing(['a', true]))).toEqual([]) })
    expect(result.current).toBe(false)
    const { result: tabs } = renderHook(() => store.usePanelTabs('s1'))
    expect(tabs.current.tabs.map(t => t.id)).toEqual([doc.id, 'terminal:a'])
  })

  it('drops a dead terminal after the second look and persists the trimmed bucket', async () => {
    const store = await bootWith({
      s1: { tabs: [doc, term('dead')], activeId: 'terminal:dead' },
      s2: { tabs: [term('live')], activeId: 'terminal:live' },
    })
    vi.useFakeTimers()
    const { result } = renderHook(() => store.usePanelTerminalsPending())
    act(() => { expect(store.reconcileRestoredPanelTerminals(listing(['live', true], ['dead', false]))).toEqual(['dead']) })
    expect(result.current).toBe(true)
    act(() => { expect(store.confirmRestoredPanelTerminals(listing(['live', true]))).toEqual(['dead']) })
    expect(result.current).toBe(false)
    act(() => { vi.advanceTimersByTime(1_000) })
    expect(persisted('s1')).toEqual(expect.objectContaining({ activeId: doc.id }))
    expect(persisted('s1')?.tabs.map(t => t.id)).toEqual([doc.id])
    expect(persisted('s2')?.tabs.map(t => t.id)).toEqual(['terminal:live'])
  })

  it('keeps an unknown terminal that the second look confirms live', async () => {
    const store = await bootWith({ s1: { tabs: [term('opening')], activeId: 'terminal:opening' } })
    expect(store.reconcileRestoredPanelTerminals(listing())).toEqual(['opening'])
    expect(store.confirmRestoredPanelTerminals(listing(['opening', true]))).toEqual([])
    const { result } = renderHook(() => store.usePanelTabs('s1'))
    expect(result.current.tabs.map(t => t.id)).toEqual(['terminal:opening'])
    const { result: pending } = renderHook(() => store.usePanelTerminalsPending())
    expect(pending.current).toBe(false)
  })
})
