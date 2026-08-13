/**
 * Tests for IntelliJ plugin embed mode components:
 * - EmbedTabStrip
 * - KiroCrewNavBridge
 * - EmbedSettingsPage
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, act } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createTestStore } from './helpers'
import type { RootState } from '../store'

// Tests preload intentionally-incomplete slices; reducers fill in the rest.
type StoreOverrides = { dashboard?: Partial<RootState['dashboard']>; chat?: Partial<RootState['chat']> }

// Plugin bridge global installed on window (mirrors KiroCrewNavBridge.tsx).
type BridgeWindow = { __kirocrewReportSlotTitle?: (slug: string, title: string) => void }
const bridgeWindow = window as unknown as BridgeWindow

// --- Mocks ---
const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return { ...actual, useNavigate: () => mockNavigate }
})

vi.mock('../store/chatSlice', async () => {
  const actual = await vi.importActual('../store/chatSlice')
  return { ...actual, createSlot: vi.fn(() => ({ unwrap: () => Promise.resolve({ key: 'new-slot' }) })) }
})

// Stub settings panels (heavy, not under test)
vi.mock('../pages/settings/DisplayPanel', () => ({ DisplayPanel: () => <div data-testid="display-panel" /> }))
vi.mock('../pages/settings/ChatPanel', () => ({ ChatPanel: () => <div data-testid="chat-panel" /> }))
vi.mock('../pages/settings/NotificationsPanel', () => ({ NotificationsPanel: () => <div data-testid="notifications-panel" /> }))

import EmbedTabStrip from '../components/EmbedTabStrip'
import KiroCrewNavBridge from '../components/KiroCrewNavBridge'
import EmbedSettingsPage from '../pages/EmbedSettingsPage'

let queryClient: QueryClient

function wrap(ui: React.ReactElement, storeOverrides?: StoreOverrides) {
  const store = createTestStore({
    dashboard: { slots: [{ key: 'chat-1', title: 'First Chat' }, { key: 'chat-2', title: 'Second Chat' }], unreadSlots: [], ...storeOverrides?.dashboard },
    chat: { activeSlot: 'chat-1', ...storeOverrides?.chat },
  } as Partial<RootState>)
  return render(
    <Provider store={store}>
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/embed/chat/chat-1']}>
          {ui}
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  sessionStorage.clear()
})

afterEach(() => {
  sessionStorage.clear()
})

// ─── EmbedTabStrip ───

describe('EmbedTabStrip', () => {
  it('renders with activeSlot tab when no stored state', () => {
    wrap(<EmbedTabStrip />)
    // With activeSlot='chat-1' in store and no sessionStorage, loadTabs uses activeSlot
    expect(screen.getByText('First Chat')).toBeTruthy()
  })

  it('renders tab from sessionStorage', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-1']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />)
    expect(screen.getByText('First Chat')).toBeTruthy()
  })

  it('renders + button with aria-label', () => {
    wrap(<EmbedTabStrip />)
    expect(screen.getByLabelText('New tab')).toBeTruthy()
  })

  it('clicking + button opens sessions tab', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-1']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />)
    fireEvent.click(screen.getByLabelText('New tab'))
    expect(screen.getByText('Sessions')).toBeTruthy()
    expect(mockNavigate).toHaveBeenCalledWith('/embed/sessions')
  })

  it('clicking a tab navigates to its chat', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-1', 'chat-2']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />)
    fireEvent.click(screen.getByText('Second Chat'))
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-2?sid=chat-2')
  })

  it('close button removes tab', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-1', 'chat-2']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />)
    const closeButtons = screen.getAllByLabelText('Close tab')
    fireEvent.click(closeButtons[1]) // close chat-2
    expect(screen.queryByText('Second Chat')).toBeNull()
  })

  it('does not show status dot for sessions tab', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['', 'chat-1']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />)
    // Sessions tab (first) should not have a status dot
    const sessionsTab = screen.getByText('Sessions').closest('[class*="group/tab"]')
    const dots = sessionsTab?.querySelectorAll('[class*="rounded-full"][class*="w-1.5"]')
    expect(dots?.length ?? 0).toBe(0)
  })

  it('dispatches kirocrew-tab-update on tab change', () => {
    const handler = vi.fn()
    window.addEventListener('kirocrew-tab-update', handler)
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-1']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />)
    fireEvent.click(screen.getByLabelText('New tab'))
    expect(handler).toHaveBeenCalled()
    window.removeEventListener('kirocrew-tab-update', handler)
  })

  it('navigates on mount to restore active tab URL', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-2']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />)
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-2?sid=chat-2', { replace: true })
  })

  it('handles onPointerCancel by resetting drag state', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-1', 'chat-2']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    const { container } = wrap(<EmbedTabStrip />)
    const strip = container.querySelector('[style*="scrollbar"]')
    // Firing pointercancel should not throw
    if (strip) {
      fireEvent.pointerCancel(strip)
    }
    // Component should still render normally
    expect(screen.getByText('First Chat')).toBeTruthy()
  })

  it('closing active tab navigates to next tab', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-1', 'chat-2']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />)
    const closeButtons = screen.getAllByLabelText('Close tab')
    fireEvent.click(closeButtons[0]) // close active tab (chat-1)
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-2?sid=chat-2')
  })

  it('closing last tab creates sessions tab and navigates', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-1']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />)
    const closeButtons = screen.getAllByLabelText('Close tab')
    fireEvent.click(closeButtons[0])
    expect(mockNavigate).toHaveBeenCalledWith('/embed/sessions')
    expect(screen.getByText('Sessions')).toBeTruthy()
  })

  it('responds to kirocrew-tab-state event from plugin', () => {
    wrap(<EmbedTabStrip />)
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-tab-state', {
        detail: { tabs: ['chat-2', 'chat-1'], activeIndex: 1 }
      }))
    })
    // After event, tabs should be reordered
    const tabTexts = screen.getAllByText(/First Chat|Second Chat/)
    expect(tabTexts[0].textContent).toBe('Second Chat')
    expect(tabTexts[1].textContent).toBe('First Chat')
  })

  it('slot deletion removes tab and navigates when active tab deleted', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-1', 'chat-3']))
    sessionStorage.setItem('kirocrew-embed-active-index', '1') // chat-3 is active
    // Store only has chat-1 and chat-2 — chat-3 is "deleted"
    wrap(<EmbedTabStrip />)
    // chat-3 should be removed, navigate to chat-1
    expect(screen.queryByText('chat-3')).toBeNull()
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-1?sid=chat-1')
  })

  it('mount navigates to sessions when active tab is empty slug', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />)
    expect(mockNavigate).toHaveBeenCalledWith('/embed/sessions', { replace: true })
  })

  it('shows status dot for chat tabs with running state', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-1']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />, { dashboard: { slots: [{ key: 'chat-1', title: 'First Chat', running: true }], unreadSlots: [] } })
    const tab = screen.getByText('First Chat').closest('[class*="group/tab"]')
    const dot = tab?.querySelector('[class*="animate-pulse"]')
    expect(dot).toBeTruthy()
  })

  it('closing non-active tab does not navigate', () => {
    sessionStorage.setItem('kirocrew-embed-tabs', JSON.stringify(['chat-1', 'chat-2']))
    sessionStorage.setItem('kirocrew-embed-active-index', '0')
    wrap(<EmbedTabStrip />)
    mockNavigate.mockClear() // clear mount navigation
    const closeButtons = screen.getAllByLabelText('Close tab')
    fireEvent.click(closeButtons[1]) // close non-active chat-2
    // Should NOT navigate since active tab wasn't closed
    const navCalls = mockNavigate.mock.calls.filter(c => !c[1]?.replace)
    expect(navCalls.length).toBe(0)
  })
})

// ─── KiroCrewNavBridge ───

describe('KiroCrewNavBridge', () => {
  it('renders nothing (returns null)', () => {
    const { container } = wrap(<KiroCrewNavBridge />)
    expect(container.innerHTML).toBe('')
  })

  it('navigates on kirocrew-soft-navigate event', () => {
    wrap(<KiroCrewNavBridge />)
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-soft-navigate', {
        detail: { url: '/embed/chat/chat-2?sid=chat-2' }
      }))
    })
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-2?sid=chat-2')
  })

  it('ignores events without url', () => {
    wrap(<KiroCrewNavBridge />)
    act(() => {
      window.dispatchEvent(new CustomEvent('kirocrew-soft-navigate', { detail: {} }))
    })
    expect(mockNavigate).not.toHaveBeenCalled()
  })

  it('reports slot titles to plugin bridge function', () => {
    const reportFn = vi.fn()
    bridgeWindow.__kirocrewReportSlotTitle = reportFn
    wrap(<KiroCrewNavBridge />)
    expect(reportFn).toHaveBeenCalledWith('chat-1', 'First Chat')
    expect(reportFn).toHaveBeenCalledWith('chat-2', 'Second Chat')
    delete bridgeWindow.__kirocrewReportSlotTitle
  })

  it('does not throw if bridge function is missing', () => {
    delete bridgeWindow.__kirocrewReportSlotTitle
    expect(() => wrap(<KiroCrewNavBridge />)).not.toThrow()
  })
})

// ─── EmbedSettingsPage ───

describe('EmbedSettingsPage', () => {
  it('renders settings header with back button', () => {
    wrap(<EmbedSettingsPage />)
    expect(screen.getByText('Settings')).toBeTruthy()
    expect(screen.getByLabelText('Back')).toBeTruthy()
  })

  it('renders the tab buttons (no Provider tab — KiroACP is the only provider)', () => {
    wrap(<EmbedSettingsPage />)
    expect(screen.getByText('Display')).toBeTruthy()
    expect(screen.getByText('Chat')).toBeTruthy()
    expect(screen.getByText('Notifications')).toBeTruthy()
    expect(screen.queryByText('Provider')).toBeNull()
  })

  it('shows DisplayPanel by default', () => {
    wrap(<EmbedSettingsPage />)
    expect(screen.getByTestId('display-panel')).toBeTruthy()
  })

  it('switches to Chat panel on tab click', () => {
    wrap(<EmbedSettingsPage />)
    fireEvent.click(screen.getByText('Chat'))
    expect(screen.getByTestId('chat-panel')).toBeTruthy()
  })

  it('back button navigates to active chat when slot exists', () => {
    wrap(<EmbedSettingsPage />, { chat: { activeSlot: 'chat-1' } })
    fireEvent.click(screen.getByLabelText('Back'))
    expect(mockNavigate).toHaveBeenCalledWith('/embed/chat/chat-1?sid=chat-1')
  })

  it('back button navigates to sessions when no active slot', () => {
    wrap(<EmbedSettingsPage />, { chat: { activeSlot: null } })
    fireEvent.click(screen.getByLabelText('Back'))
    expect(mockNavigate).toHaveBeenCalledWith('/embed/sessions')
  })
})
