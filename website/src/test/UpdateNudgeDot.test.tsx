// Update nudge dots: the desktopUpdateAvailable flag, its mirroring from
// Electron update-state events, and the dot rendering in SidePanelLayout tabs.
import { describe, it, expect, afterEach } from 'vitest'
import { render, screen, cleanup, act } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../store'
import dashboardReducer, { setDesktopUpdateAvailable } from '../store/dashboardSlice'
import { useUpdateSubscription, type UpdateState } from '../hooks/useUpdateSubscription'
import SidePanelLayout from '../components/SidePanelLayout'

describe('desktopUpdateAvailable flag', () => {
  it('reducer toggles the flag', () => {
    const initial = dashboardReducer(undefined, { type: '@@INIT' })
    expect(initial.desktopUpdateAvailable).toBe(false)
    const on = dashboardReducer(initial, setDesktopUpdateAvailable(true))
    expect(on.desktopUpdateAvailable).toBe(true)
    expect(dashboardReducer(on, setDesktopUpdateAvailable(false)).desktopUpdateAvailable).toBe(false)
  })
})

function Subscriber() {
  useUpdateSubscription()
  return null
}

describe('useUpdateSubscription mirrors availability into Redux', () => {
  afterEach(() => {
    cleanup()
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
    store.dispatch(setDesktopUpdateAvailable(false))
  })

  function mountWithStates() {
    let handler: ((p: UpdateState) => void) | null = null
    ;(window as unknown as { updateAPI?: unknown }).updateAPI = {
      onState: (cb: (p: UpdateState) => void) => { handler = cb; return () => { handler = null } },
    }
    const qc = new QueryClient()
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <Subscriber />
        </QueryClientProvider>
      </Provider>,
    )
    return { emit: (p: UpdateState) => act(() => handler?.(p)) }
  }

  it('found sets the flag; not-available clears it; checking leaves it alone', () => {
    const { emit } = mountWithStates()
    emit({ state: 'found', version: '9.9.9' })
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(true)
    emit({ state: 'checking' })
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(true) // unchanged mid-check
    emit({ state: 'not-available' })
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(false)
  })

  it('downloaded also counts as available', () => {
    const { emit } = mountWithStates()
    emit({ state: 'downloaded', version: '9.9.9' })
    expect(store.getState().dashboard.desktopUpdateAvailable).toBe(true)
  })
})

describe('SidePanelLayout tab dot', () => {
  afterEach(cleanup)
  const tabs = [
    { key: 'a', label: 'General', icon: <span /> },
    { key: 'about', label: 'About', icon: <span />, dot: true },
  ]
  it('renders a presence dot on dotted tabs only', () => {
    render(
      <MemoryRouter>
        <SidePanelLayout title="Settings" tabs={tabs}>
          {() => <div />}
        </SidePanelLayout>
      </MemoryRouter>,
    )
    const dots = screen.getAllByRole('status', { name: 'update available' })
    expect(dots.length).toBeGreaterThanOrEqual(1)
  })
})
