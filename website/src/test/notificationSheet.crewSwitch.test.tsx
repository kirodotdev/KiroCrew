import { describe, it, expect, vi, afterEach } from 'vitest'
import { act, renderHook, waitFor } from '@testing-library/react'
import { api } from '../api/client'
import { setActiveSlot, switchSlot } from '../store/chatSlice'
import { dispatchLiveNotification } from '../hooks/notificationEvent'
import { useMarkTranscriptShown } from '../hooks/useAckCronNotesInView'
import type { Notification } from '../types'
import { addNotification } from '../store/notificationsSlice'
import { Provider, useSelector } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import type { ReactNode } from 'react'
import { createTestStore } from './helpers'
import type { RootState } from '../store'
import { setActiveId } from '../store/instancesSlice'
import { LocalOverlayRoot } from '../components/overlayRoot'
import { useNotificationSheet } from '../shell/notifications/notificationSheet'
import { closeCrewWindow, openCrewWindow } from '../pages/chat/crew-window/crewWindowStore'

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

  // #23564: the sheet's hook reads a cron note arriving live while a pane shows
  // its chat, but not while a remote Crew or a crew window covers the pane.
  describe('cron note read on view', () => {
    const RUN_NOTE = { kind: 'cron', title: 'Digest', body: 'done', ts: 'run-1', slot: 'cron-a', job_id: 'job-a' } as const
    function setupCron(hidden: boolean) {
      vi.spyOn(document, 'hasFocus').mockReturnValue(true)
      const ack = vi.spyOn(api, 'ackNotification').mockResolvedValue(new Response('{}'))
      const store = createTestStore()
      store.dispatch(setActiveId(hidden ? 'remote-crew' : null))
      store.dispatch(setActiveSlot('cron-a'))
      function Root({ children }: { children: ReactNode }) {
        const remoteActive = useSelector((s: RootState) => s.instances.activeId !== null)
        return <LocalOverlayRoot hidden={remoteActive}>{children}</LocalOverlayRoot>
      }
      const wrapper = ({ children }: { children: ReactNode }) => (
        <Provider store={store}>
          <MemoryRouter initialEntries={['/chat']}><Root>{children}</Root></MemoryRouter>
        </Provider>
      )
      // A pane showing the cron chat's transcript, as ChatTranscriptView marks it.
      const { result } = renderHook(() => { useMarkTranscriptShown('cron-a', true); return useNotificationSheet() }, { wrapper })
      const arrive = (n: Notification) => act(() => { store.dispatch(addNotification(n)); dispatchLiveNotification(n) })
      return { store, ack, arrive, result }
    }
    const ackedTs = (store: ReturnType<typeof createTestStore>) => store.getState().notifications.items.filter(n => n.acked).map(n => n.ts)
    afterEach(() => vi.restoreAllMocks())

    it('reads it with the local Crew in front', async () => {
      const { store, ack, arrive } = setupCron(false)
      arrive(RUN_NOTE)
      await waitFor(() => expect(store.getState().notifications.items[0].acked).toBe(true))
      expect(ack).toHaveBeenCalledWith('run-1')
    })

    it('leaves it unread while a crew window covers the chat', async () => {
      // ChatPaneChrome draws the crew window over the pane without moving
      // activeSlot or unmounting the transcript under it.
      openCrewWindow({ instanceId: 'peer-1', key: 'peer-chat' })
      try {
        const { store, ack, arrive } = setupCron(false)
        arrive(RUN_NOTE)
        await act(async () => { await Promise.resolve() })
        expect(ack).not.toHaveBeenCalled()
        expect(store.getState().notifications.items[0].acked).toBeFalsy()
      } finally {
        closeCrewWindow()
      }
    })

    it('leaves it unread while the sheet shows another note in its detail card, and reads the next once the card closes', async () => {
      // The detail card is opaque and covers the transcript (the whole width on
      // a phone), so a run arriving while the user reads another note is not seen.
      const { store, ack, arrive, result } = setupCron(false)
      const OTHER = { kind: 'approval', title: 'Approve', body: 'x', ts: 'other-1', slot: 'chat-b' } as const
      act(() => { store.dispatch(addNotification(OTHER)) })
      act(() => { result.current.openPanelOn('other-1') })
      await waitFor(() => expect(result.current.selectedTs).toBe('other-1'))
      arrive(RUN_NOTE)
      await act(async () => { await Promise.resolve() })
      expect(ack).not.toHaveBeenCalledWith('run-1')
      expect(ackedTs(store)).not.toContain('run-1')
      // Back to the list: the transcript is uncovered again, so the next run is read.
      act(() => { result.current.setSelectedTs(null) })
      arrive({ ...RUN_NOTE, ts: 'run-2' })
      await waitFor(() => expect(ackedTs(store)).toContain('run-2'))
      expect(ackedTs(store)).not.toContain('run-1')
    })

    it('reads the chat a card\'s "Go to chat" opens: the sheet closes in the same gesture, and a closing sheet covers nothing', async () => {
      // On /chat already: the jump keeps the pathname, so only the card's own
      // close (onLeave) takes the sheet away before the open lands.
      const { store, ack, result } = setupCron(false)
      act(() => { store.dispatch(addNotification(RUN_NOTE)) })
      act(() => { store.dispatch(addNotification({ ...RUN_NOTE, ts: 'run-2' })) })
      act(() => { result.current.openPanelOn('run-1') })
      await waitFor(() => expect(result.current.selectedTs).toBe('run-1'))
      act(() => { result.current.closePanel() })
      expect(result.current.phase).not.toBe('open')
      act(() => {
        store.dispatch(switchSlot.fulfilled(
          { key: 'cron-a', messages: [], running: false, stopping: false, hasMore: false, total: 0, queue: [] } as never,
          'req-go-to-chat',
          { key: 'cron-a', announceOnMissing: true, gesture: true },
        ))
      })
      await waitFor(() => expect(ackedTs(store)).toContain('run-2'))
      expect(ack).toHaveBeenCalledWith('run-2')
    })

    it('leaves it unread behind a remote Crew', async () => {
      const { store, ack, arrive } = setupCron(true)
      arrive(RUN_NOTE)
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
