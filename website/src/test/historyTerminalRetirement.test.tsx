import { act, fireEvent, screen, waitFor, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { createTestStore, renderWithProviders } from './helpers'
import { deleteHistorySession, deleteSlot, resumeFromHistory, switchSlot } from '../store/chatSlice'
import HistoryTerminalCleanupNotice from '../components/HistoryTerminalCleanupNotice'
import { __resetBottomTerminal, activateTerminalSession, addTab, adoptTab, captureTerminalSessionLease,
  getBottomTerminalSnapshot, openBottomTerminal, removeTab, prepareTerminalRetirement, cancelTerminalRetirement, readTerminalSessionLease, retireTerminalSession, subscribeTerminalRetirement, useTerminalCapacityExceeded } from '../hooks/useBottomTerminal'
import { __resetHistoryTerminalCleanupForTests, deleteHistoryWithTerminals, retryHistoryTerminalCleanup } from '../lib/historyTerminalCleanup'
import * as persistence from '../utils/terminalStateLock'
const pty = vi.hoisted(() => ({ dispose: vi.fn(), remove: vi.fn() }))
vi.mock('../components/CliPanel', () => ({ disposeTerminalSession: pty.dispose, deleteTerminalSessionRequest: pty.remove }))
beforeEach(async () => {
  vi.restoreAllMocks(); await __resetBottomTerminal(); __resetHistoryTerminalCleanupForTests()
  pty.dispose.mockReset(); pty.remove.mockReset(); pty.remove.mockResolvedValue(undefined)
  vi.spyOn(api, 'deleteSession').mockResolvedValue({ ok: true })
})
function historyStore(key = 'dashboard_A') {
  const base = createTestStore().getState()
  return createTestStore({ ...base, chat: { ...base.chat, activeSlot: 'B', history: [{ key, title: 'A' }] } })
}
describe('confirmed history deletion retires only that terminal generation', () => {
  it('frees eight slots and remains retired after reload', async () => {
    const ids: string[] = []
    for (let i = 0; i < 8; i++) ids.push((await addTab(undefined, 'A'))!)
    const store = historyStore()
    await store.dispatch(deleteHistorySession('dashboard_A')).unwrap()
    expect(store.getState().chat.history).toEqual([])
    expect(getBottomTerminalSnapshot('A')).toMatchObject({ tabs: [], open: false, retired: true, totalTabs: 0 })
    expect(pty.dispose.mock.calls.map(args => args[0])).toEqual(ids)
    expect(pty.remove.mock.calls.map(args => args[0])).toEqual(ids)
    expect(await openBottomTerminal(undefined, 'B')).toBe(true)
    vi.resetModules()
    const fresh = await import('../hooks/useBottomTerminal')
    await fresh.initializeTerminalSession('A')
    expect(fresh.getBottomTerminalSnapshot('A')).toMatchObject({ tabs: [], retired: true, totalTabs: 1 })
    expect(await fresh.addTab(undefined, 'A')).toBeNull()
  })
  it.each(['http', 'ok:false'])('preserves history and terminals on %s', async failure => {
    const id = await addTab(undefined, 'A')
    if (failure === 'http') vi.mocked(api.deleteSession).mockRejectedValueOnce(new Error('offline'))
    else vi.mocked(api.deleteSession).mockResolvedValueOnce({ ok: false })
    const store = historyStore()
    await expect(store.dispatch(deleteHistorySession('dashboard_A')).unwrap()).rejects.toBeTruthy()
    expect(store.getState().chat.history).toHaveLength(1)
    expect(getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)).toEqual([id])
    expect(getBottomTerminalSnapshot('A').retired).toBe(false)
    expect(pty.remove).not.toHaveBeenCalled()
  })
  it('matches transport aliases without matching a similar name', async () => {
    await addTab(undefined, 'A'); await addTab(undefined, 'dashboard_A')
    const other = await addTab(undefined, 'A-extra')
    await historyStore().dispatch(deleteHistorySession('dashboard:dashboard_A')).unwrap()
    expect(getBottomTerminalSnapshot('A').tabs).toEqual([])
    expect(getBottomTerminalSnapshot('dashboard_A').tabs).toEqual([])
    expect(getBottomTerminalSnapshot('A-extra').tabs.map(tab => tab.id)).toEqual([other])
  })
  it('preserves archive/resume and allows confirmed reuse of a retired deterministic key', async () => {
    const id = await addTab(undefined, 'A'); const store = historyStore()
    vi.spyOn(api, 'deleteChatSlot').mockResolvedValue({ ok: true })
    vi.spyOn(api, 'resumeChatSlot').mockResolvedValue({ ok: true, key: 'A', messages: [] })
    await store.dispatch(deleteSlot('A')).unwrap()
    expect(getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)).toEqual([id])
    await store.dispatch(resumeFromHistory({ key: 'dashboard_A', title: 'A' })).unwrap()
    expect(getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)).toEqual([id])
    await store.dispatch(deleteHistorySession('dashboard_A')).unwrap()
    await store.dispatch(resumeFromHistory({ key: 'dashboard_A', title: 'New A' })).unwrap()
    await waitFor(() => expect(getBottomTerminalSnapshot('A').retired).toBe(false))
    const next = await addTab(undefined, 'A'); expect(next).not.toBeNull(); expect(next).not.toBe(id)
  })
  it('rejects a queued old open and late adoption after valid reuse without a false capacity error', async () => {
    await addTab(undefined, 'A'); const lease = captureTerminalSessionLease('A')
    const actual = persistence.withTerminalStateLock; let release!: () => void
    vi.spyOn(persistence, 'withTerminalStateLock').mockImplementationOnce(operation => new Promise(resolve => {
      release = () => { void actual(operation).then(resolve) }
    }))
    const pending = openBottomTerminal(undefined, 'A')
    await retireTerminalSession(lease)
    await activateTerminalSession('A', await readTerminalSessionLease('A'))
    const next = await addTab(undefined, 'A'); release(); await pending
    expect(await adoptTab('late-old-pty', undefined, 'A', lease)).toBe(false)
    expect(await addTab(undefined, 'A', lease)).toBeNull()
    expect(getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)).toEqual([next])
    const notice = renderHook(() => useTerminalCapacityExceeded('A')); expect(notice.result.current).toBe(false)
  })
  it('shows cleanup-only retry when IDB fails after the server deleted history', async () => {
    for (let i = 0; i < 8; i++) await addTab(undefined, 'A')
    vi.mocked(api.deleteSession).mockImplementationOnce(async () => {
      vi.spyOn(persistence, 'withTerminalStateLock').mockRejectedValueOnce(new Error('quota'))
      return { ok: true }
    })
    const store = historyStore(); renderWithProviders(<HistoryTerminalCleanupNotice />, { store })
    await act(async () => { await store.dispatch(deleteHistorySession('dashboard_A')).unwrap() })
    expect(store.getState().chat.history).toEqual([])
    expect(screen.getByTestId('history-terminal-cleanup-error')).toHaveTextContent('terminal cleanup failed')
    expect(pty.remove).not.toHaveBeenCalled(); expect(getBottomTerminalSnapshot('A').totalTabs).toBe(8)
    fireEvent.click(screen.getByRole('button', { name: 'Retry', exact: true }))
    await waitFor(() => expect(screen.queryByTestId('history-terminal-cleanup-error')).not.toBeInTheDocument())
    expect(api.deleteSession).toHaveBeenCalledTimes(1)
    expect(getBottomTerminalSnapshot('A').totalTabs).toBe(0)
    expect(await openBottomTerminal(undefined, 'B')).toBe(true)
  })
  it('retries only the failed PTY request after canonical retirement', async () => {
    const first = await addTab(undefined, 'A'); await addTab(undefined, 'A')
    pty.remove.mockRejectedValueOnce(new Error('offline'))
    await historyStore().dispatch(deleteHistorySession('dashboard_A')).unwrap()
    expect(getBottomTerminalSnapshot('A').totalTabs).toBe(0)
    await retryHistoryTerminalCleanup('dashboard_A')
    expect(pty.remove.mock.calls.map(args => args[0])).toEqual([first, expect.any(String), first])
    expect(api.deleteSession).toHaveBeenCalledTimes(1)
  })
  it('never waits on unavailable terminal storage for ordinary switch or resume', async () => {
    vi.spyOn(persistence, 'readTerminalStateSnapshot').mockReturnValue(new Promise(() => {}))
    const write = vi.spyOn(persistence, 'withTerminalStateLock').mockReturnValue(new Promise(() => {}))
    vi.spyOn(api, 'chatSlotDetail').mockResolvedValue({ messages: [], key: 'B', state: 'idle' })
    vi.spyOn(api, 'resumeChatSlot').mockResolvedValue({ ok: true, key: 'B', messages: [] })
    const store = historyStore()
    await store.dispatch(switchSlot('B')).unwrap()
    await store.dispatch(resumeFromHistory({ key: 'B', title: 'B' })).unwrap()
    expect(write).not.toHaveBeenCalled()
  })
  it('keeps committed IDs available when a local retirement observer throws', async () => {
    const id = await addTab(undefined, 'A')
    const warn = vi.spyOn(console, 'error').mockImplementation(() => {})
    const unsubscribe = subscribeTerminalRetirement(() => { throw new Error('view failed') })
    try {
      const ids = await retireTerminalSession(await prepareTerminalRetirement('A'))
      expect(ids).toEqual([id])
      expect(getBottomTerminalSnapshot('A').tabs).toEqual([])
      expect(warn).toHaveBeenCalled()
    } finally { unsubscribe() }
  })
  it('does not reject successful chat resume when retired-scope revival cannot write', async () => {
    await addTab(undefined, 'A')
    await retireTerminalSession(await prepareTerminalRetirement('A'))
    vi.spyOn(persistence, 'withTerminalStateLock').mockRejectedValue(new Error('unavailable'))
    vi.spyOn(api, 'resumeChatSlot').mockResolvedValue({ ok: true, key: 'A', messages: [] })
    const store = historyStore()
    await expect(store.dispatch(resumeFromHistory({ key: 'A', title: 'A' })).unwrap()).resolves.toMatchObject({ ok: true, key: 'A' })
  })

  it('coalesces overlapping deletes and never overwrites confirmed cleanup with a later ok:false', async () => {
    await addTab(undefined, 'A')
    let answer!: (value: { ok: boolean }) => void
    vi.mocked(api.deleteSession).mockImplementationOnce(() => new Promise(resolve => { answer = resolve }))
      .mockResolvedValue({ ok: false })
    const first = deleteHistoryWithTerminals('dashboard_A')
    const second = deleteHistoryWithTerminals('dashboard:A')
    expect(second).toBe(first)
    await waitFor(() => expect(api.deleteSession).toHaveBeenCalledTimes(1))
    vi.spyOn(persistence, 'withTerminalStateLock').mockRejectedValueOnce(new Error('quota'))
    answer({ ok: true }); await Promise.all([first, second])
    expect(getBottomTerminalSnapshot('A').totalTabs).toBe(1)
    // A caller repeating the original operation must use its confirmed receipt.
    await deleteHistoryWithTerminals('dashboard_A')
    expect(api.deleteSession).toHaveBeenCalledTimes(1)
    expect(getBottomTerminalSnapshot('A').tabs).toEqual([])
  })

  it('cannot let an old completion clear a newer per-key operation after test reset', async () => {
    await addTab(undefined, 'A')
    const answers: ((value: { ok: boolean }) => void)[] = []
    vi.mocked(api.deleteSession).mockImplementation(() => new Promise(resolve => { answers.push(resolve) }))
    const old = deleteHistoryWithTerminals('A')
    await waitFor(() => expect(answers).toHaveLength(1))
    await __resetBottomTerminal()
    __resetHistoryTerminalCleanupForTests()
    await addTab(undefined, 'A')
    const current = deleteHistoryWithTerminals('A')
    await waitFor(() => expect(answers).toHaveLength(2))
    answers[0]({ ok: true }); await old
    expect(deleteHistoryWithTerminals('A')).toBe(current)
    expect(pty.remove).not.toHaveBeenCalled()
    answers[1]({ ok: true }); await current
    expect(pty.remove).toHaveBeenCalledTimes(1)
  })

  it('revives a valid retired key after a cold cache through a fresh server validation', async () => {
    await addTab(undefined, 'A')
    await retireTerminalSession(await prepareTerminalRetirement('A'))
    vi.resetModules()
    const freshHook = await import('../hooks/useBottomTerminal')
    const freshCleanup = await import('../lib/historyTerminalCleanup')
    const freshApi = (await import('../api/client')).api
    vi.spyOn(freshApi, 'chatSlotDetail').mockResolvedValue({ messages: [], key: 'A', state: 'idle' })
    const activation = freshCleanup.captureTerminalActivation('A')
    expect(activation.loaded).toBe(false)
    // Represents the first navigation's successful response. The cold lease
    // cannot authorize revival; recovery must make its own fresh lookup.
    await freshCleanup.activateTerminalsAfterNavigation('A', activation)
    expect(freshApi.chatSlotDetail).toHaveBeenCalledWith('A', 1, 0)
    expect(freshHook.getBottomTerminalSnapshot('A').retired).toBe(false)
    expect(await freshHook.addTab(undefined, 'A')).not.toBeNull()
  })
  it('retires the confirmed old generation before valid reuse after cleanup failure', async () => {
    const old = await addTab(undefined, 'A')
    vi.mocked(api.deleteSession).mockImplementationOnce(async () => {
      vi.spyOn(persistence, 'withTerminalStateLock').mockRejectedValueOnce(new Error('quota'))
      return { ok: true }
    })
    const store = historyStore()
    await store.dispatch(deleteHistorySession('dashboard_A')).unwrap()
    expect(await addTab(undefined, 'A')).toBeNull()
    vi.spyOn(api, 'resumeChatSlot').mockResolvedValue({ ok: true, key: 'A', messages: [] })
    await store.dispatch(resumeFromHistory({ key: 'A', title: 'New A' })).unwrap()
    await waitFor(() => expect(getBottomTerminalSnapshot('A').retired).toBe(false))
    const next = await addTab(undefined, 'A')
    expect(next).not.toBeNull(); expect(next).not.toBe(old)
    await retryHistoryTerminalCleanup('dashboard_A')
    expect(getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)).toEqual([next])
    await waitFor(() => expect(pty.remove).toHaveBeenCalledWith(old, true))
    expect(pty.remove.mock.calls.map(args => args[0])).not.toContain(next)
    expect(api.deleteSession).toHaveBeenCalledTimes(1)
  })

  it('fences allocation in a second window while confirmed retirement needs retry', async () => {
    const old = await addTab(undefined, 'A')
    vi.mocked(api.deleteSession).mockImplementationOnce(async () => {
      vi.spyOn(persistence, 'withTerminalStateLock').mockRejectedValueOnce(new Error('quota'))
      return { ok: true }
    })
    await historyStore().dispatch(deleteHistorySession('dashboard_A')).unwrap()
    vi.resetModules()
    const other = await import('../hooks/useBottomTerminal')
    await other.initializeTerminalSession('A')
    expect(other.getBottomTerminalSnapshot('A').preparing).toBe(true)
    expect(await other.addTab(undefined, 'A')).toBeNull()
    await other.activateTerminalSession('A', await other.readTerminalSessionLease('A'))
    expect(await other.addTab(undefined, 'A')).toBeNull()
    await retryHistoryTerminalCleanup('dashboard_A')
    await other.refreshTerminalState('A')
    await other.activateTerminalSession('A', await other.readTerminalSessionLease('A'))
    const next = await other.addTab(undefined, 'A')
    expect(next).not.toBeNull(); expect(next).not.toBe(old)
    await retryHistoryTerminalCleanup('dashboard_A')
    expect(pty.remove.mock.calls.map(args => args[0])).not.toContain(next)
  })

  it('keeps existing views and explicit close usable while preparation is unconfirmed', async () => {
    const first = await addTab(undefined, 'A'); const second = await addTab(undefined, 'A')
    const lease = await prepareTerminalRetirement('A')
    expect(getBottomTerminalSnapshot('A')).toMatchObject({ open: true, preparing: true })
    expect(getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)).toEqual([first, second])
    expect(await addTab(undefined, 'A')).toBeNull()
    await removeTab(first!, 'A')
    expect(getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)).toEqual([second])
    await cancelTerminalRetirement(lease)
    expect(getBottomTerminalSnapshot('A')).toMatchObject({ open: true, preparing: false, epoch: 0 })
  })
  it('can finish terminal-only cleanup after reload without guessing the HTTP outcome', async () => {
    for (let i = 0; i < 8; i++) await addTab(undefined, 'A')
    const lease = await prepareTerminalRetirement('A')
    vi.resetModules()
    const freshHook = await import('../hooks/useBottomTerminal')
    const freshCleanup = await import('../lib/historyTerminalCleanup')
    await freshHook.initializeTerminalSession('B')
    expect(await freshHook.openBottomTerminal(undefined, 'B')).toBe(false)
    const snapshot = await persistence.readTerminalStateSnapshot()
    const record = JSON.parse(snapshot.values.get('mc-bottom-terminal-lifecycle:"A"')!)
    await freshCleanup.closePreparedTerminals({ scope: 'A', epoch: record.epoch, token: record.deletingToken })
    expect(api.deleteSession).not.toHaveBeenCalled()
    expect(await freshHook.openBottomTerminal(undefined, 'B')).toBe(true)
    expect(freshHook.getBottomTerminalSnapshot('A').preparing).toBe(false)
    expect(lease.token).toBe(record.deletingToken)
  })
  it('revives an already-retired scope while retrying only an old failed PTY deletion', async () => {
    const old = await addTab(undefined, 'A')
    pty.remove.mockRejectedValueOnce(new Error('offline'))
    const store = historyStore()
    await store.dispatch(deleteHistorySession('dashboard_A')).unwrap()
    expect(getBottomTerminalSnapshot('A')).toMatchObject({ retired: true, epoch: 1 })
    vi.spyOn(api, 'resumeChatSlot').mockResolvedValue({ ok: true, key: 'A', messages: [] })
    await store.dispatch(resumeFromHistory({ key: 'A', title: 'A again' })).unwrap()
    await waitFor(() => expect(getBottomTerminalSnapshot('A').retired).toBe(false))
    const next = await addTab(undefined, 'A')
    expect(next).not.toBeNull()
    await waitFor(() => expect(pty.remove).toHaveBeenCalledTimes(2))
    expect(pty.remove.mock.calls.map(args => args[0])).toEqual([old, old])
    expect(getBottomTerminalSnapshot('A').tabs.map(tab => tab.id)).toEqual([next])
  })

  it('keeps preparation recovery reachable even if the user retries the original history delete', async () => {
    await addTab(undefined, 'A'); await prepareTerminalRetirement('A')
    const store = historyStore()
    renderWithProviders(<HistoryTerminalCleanupNotice />, { store })
    await act(async () => { await store.dispatch(deleteHistorySession('dashboard_A')) })
    expect(api.deleteSession).not.toHaveBeenCalled()
    expect(screen.getByTestId('interrupted-terminal-retirement')).toHaveTextContent('unconfirmed')
    fireEvent.click(screen.getByRole('button', { name: 'Close terminals', exact: true }))
    await waitFor(() => expect(screen.queryByTestId('interrupted-terminal-retirement')).not.toBeInTheDocument())
    expect(store.getState().chat.history).toHaveLength(1)
    expect(getBottomTerminalSnapshot('A').preparing).toBe(false)
    expect(await openBottomTerminal(undefined, 'B')).toBe(true)
  })

  it('does not use an old cleanup confirmation to delete a later conversation with the same key', async () => {
    const old = await addTab(undefined, 'A'); const store = historyStore()
    pty.remove.mockRejectedValueOnce(new Error('offline'))
    await store.dispatch(deleteHistorySession('dashboard_A')).unwrap()
    vi.resetModules()
    const other = await import('../hooks/useBottomTerminal')
    await other.activateTerminalSession('A', await other.readTerminalSessionLease('A'))
    const next = await other.addTab(undefined, 'A')
    // No storage notification has updated this window's cached epoch.
    expect(getBottomTerminalSnapshot('A').epoch).toBe(1)
    await store.dispatch(deleteHistorySession('dashboard_A')).unwrap()
    expect(api.deleteSession).toHaveBeenCalledTimes(2)
    expect(pty.remove.mock.calls.map(args => args[0])).toEqual([old, old, next])
  })

})
