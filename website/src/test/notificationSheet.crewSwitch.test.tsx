import { describe, it, expect } from 'vitest'
import { act, renderHook } from '@testing-library/react'
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

  it('opens the sheet in place when the local Crew is already showing', () => {
    const { store, result } = setup(false)
    act(() => { result.current.openPanelOn('1700000000.000001') })
    expect(store.getState().instances.activeId).toBeNull()
    expect(result.current.phase).toBe('open')
  })
})
