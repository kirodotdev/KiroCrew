import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { api } from '../api/client'
import { setActiveSlot } from '../store/chatSlice'
import { addNotification } from '../store/notificationsSlice'
import { Provider, useSelector } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import { setActiveId } from '../store/instancesSlice'
import { LocalOverlayRoot } from '../components/overlayRoot'
import { useNotificationSheet } from '../shell/notifications/notificationSheet'

// The arrival banner shows over every Crew, but the sheet it opens hides with
// the local one: a banner tap over a remote Crew must bring the local Crew
// forward rather than open the sheet out of sight.
describe('notification sheet across Crew switches', () => {
  function setup(hidden: boolean) {
    const store = createTestStore()
    store.dispatch(setActiveId(hidden ? 'remote-crew' : null))
    // As in App: the local Crew is hidden exactly while a remote one is active.
    function Root({ children }: { children: ReactNode }) {
      const remoteActive = useSelector((s: RootState) => s.instances.activeId !== null)
      return <LocalOverlayRoot hidden={remoteActive}>{children}</LocalOverlayRoot>
    }
    const wrapper = ({ children }: { children: ReactNode }) => (
      <Provider store={store}>
        <MemoryRouter><Root>{children}</Root></MemoryRouter>
      </Provider>
    )
    const { result } = renderHook(() => useNotificationSheet(), { wrapper })
    return { store, result }
  }

  it('brings the local Crew forward when the banner opens a note over a remote one', () => {
    const { store, result } = setup(true)
    act(() => { result.current.openPanelOn('1700000000.000001') })
    expect(store.getState().instances.activeId).toBeNull()
    expect(result.current.selectedTs).toBe('1700000000.000001')
  })

  // #23564: the sheet's hook marks an open cron chat's note read, but not while
  // a remote Crew covers the local one.
  describe('cron note read on view', () => {
    function setupCron(hidden: boolean) {
      vi.spyOn(document, 'hasFocus').mockReturnValue(true)
      const ack = vi.spyOn(api, 'ackNotification').mockResolvedValue(new Response('{}'))
      const store = createTestStore()
      store.dispatch(setActiveId(hidden ? 'remote-crew' : null))
      store.dispatch(setActiveSlot('cron-a'))
      store.dispatch(addNotification({ kind: 'cron', title: 'Digest', body: 'done', ts: 'run-1', slot: 'cron-a', job_id: 'job-a' }))
      function Root({ children }: { children: ReactNode }) {
        const remoteActive = useSelector((s: RootState) => s.instances.activeId !== null)
        return <LocalOverlayRoot hidden={remoteActive}>{children}</LocalOverlayRoot>
      }
      const wrapper = ({ children }: { children: ReactNode }) => (
        <Provider store={store}>
          <MemoryRouter initialEntries={['/chat']}><Root>{children}</Root></MemoryRouter>
        </Provider>
      )
      renderHook(() => useNotificationSheet(), { wrapper })
      return { store, ack }
    }
    afterEach(() => vi.restoreAllMocks())

    it('marks it read with the local Crew in front', async () => {
      const { store, ack } = setupCron(false)
      await waitFor(() => expect(store.getState().notifications.items[0].acked).toBe(true))
      expect(ack).toHaveBeenCalledWith('run-1')
    })

    it('leaves it unread behind a remote Crew', async () => {
      const { store, ack } = setupCron(true)
      await act(async () => { await Promise.resolve() })
      expect(ack).not.toHaveBeenCalled()
      expect(store.getState().notifications.items[0].acked).toBeFalsy()
    })
  })

  it('opens the sheet in place when the local Crew is already showing', () => {
    const { store, result } = setup(false)
    act(() => { result.current.openPanelOn('1700000000.000001') })
    expect(store.getState().instances.activeId).toBeNull()
    expect(result.current.phase).toBe('open')
  })
})
