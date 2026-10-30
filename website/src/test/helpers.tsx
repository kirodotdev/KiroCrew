import React from 'react'
import { act, fireEvent, render, renderHook, screen, waitFor, type RenderOptions } from '@testing-library/react'
import { expect, vi } from 'vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import type { RootState } from '../store'
import { ThemeProvider } from '../hooks/useTheme'
import { TAB_ID_ATTR } from '../hooks/useRevealActiveTab'

/** Create a fresh Redux store, optionally with preloaded state. */
export function createTestStore(preloadedState?: Partial<RootState>) {
  return configureStore({
    reducer: {
      dashboard: dashboardReducer,
      chat: chatReducer,
      notifications: notificationsReducer,
      instances: instancesReducer,
    },
    preloadedState,
  })
}

/**
 * Create a store whose dashboard is connected to the gateway.
 *
 * `createTestStore()` starts where the real dashboard boots: `connected: false`,
 * until the SSE stream opens. A component gated on `connected` therefore renders
 * its offline branch by default, and a test of its connected path must say so.
 * This is that opt-in. Any `dashboard` fields you pass are laid over the reducer's
 * real initial state, so you only name what the test is about; `connected` is
 * always true. Other slices pass through unchanged.
 */
export function createConnectedTestStore(preloadedState?: Partial<RootState>) {
  const initialDashboard = dashboardReducer(undefined, { type: '@@test/init' })
  return createTestStore({
    ...preloadedState,
    dashboard: { ...initialDashboard, ...preloadedState?.dashboard, connected: true },
  })
}

type TestStore = ReturnType<typeof createTestStore>

interface WrapperOptions extends Omit<RenderOptions, 'wrapper'> {
  store?: TestStore
  route?: string
  /**
   * Extra query defaults, merged over this helper's own.
   *
   * The default client here uses React Query's `staleTime: 0`, while the shipped
   * client (`api/queryClient.ts`) sets `staleTime: 30_000`. A cache-freshness bug
   * is therefore INVISIBLE by default: a `fetchQuery` that wrongly serves a cached
   * entry in production re-fetches happily under the test client. Pass
   * `queryDefaults: { staleTime: 30_000 }` when the behaviour under test depends
   * on an entry being considered fresh.
   */
  queryDefaults?: Record<string, unknown>
}

/** Render with Redux Provider + MemoryRouter + ThemeProvider. */
export function renderWithProviders(
  ui: React.ReactElement,
  {
    store = createTestStore(),
    route = '/',
    queryDefaults,
    ...renderOptions
  }: WrapperOptions = {},
) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false, ...queryDefaults } },
  })
  function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>
    )
  }
  return { store, queryClient, ...render(ui, { wrapper: Wrapper, ...renderOptions }) }
}

/** renderHook with Redux Provider + MemoryRouter + ThemeProvider. */
export function renderHookWithProviders<T>(
  hook: () => T,
  { store = createTestStore(), route = '/' }: { store?: TestStore; route?: string } = {},
) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  function Wrapper({ children }: { children: React.ReactNode }) {
    return (
      <QueryClientProvider client={queryClient}>
        <Provider store={store}>
          <ThemeProvider>
            <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
          </ThemeProvider>
        </Provider>
      </QueryClientProvider>
    )
  }
  return { store, ...renderHook(hook, { wrapper: Wrapper }) }
}

/** Let a freshly mounted ChatSidebar finish its first population, so its rows
 *  spring and slide in again: `useRowAnimSettled` makes them snap until the
 *  slot, folder and tag-column reads settle and two frames have painted, and the
 *  sidebar root reports that as `data-row-anim="settled"`. The store must mark
 *  `dashboard.slotsLoaded` (or a non-zero `slotsGeneration`), as the live slot
 *  list does: with neither, the sidebar waits for a list and this times out.
 *
 *  The budget is above `waitFor`'s 1 s default because settling spans whole
 *  re-renders of the list: a fixture with a hundred-plus rows re-renders in over
 *  a second on a loaded runner, and the wait ends as soon as the root settles. */
export async function settleSidebarRowAnim(): Promise<void> {
  await waitFor(() => expect(document.querySelector('[data-row-anim="settled"]')).not.toBeNull(), { timeout: 5000 })
}

export function expectWheelToScrollRow(row: HTMLElement): void {
  let left = 0
  const stub = (scrollWidth: number) => {
    Object.defineProperty(row, 'clientWidth', { configurable: true, get: () => 100 })
    Object.defineProperty(row, 'scrollWidth', { configurable: true, get: () => scrollWidth })
    Object.defineProperty(row, 'scrollLeft', { configurable: true, get: () => left, set: (v: number) => { left = v } })
  }
  const wheel = () => {
    const e = new WheelEvent('wheel', { deltaY: 40, bubbles: true, cancelable: true })
    row.dispatchEvent(e)
    return e
  }
  stub(400)
  const clipped = wheel()
  expect(left).toBe(40)
  expect(clipped.defaultPrevented).toBe(true)
  left = 300
  const atEnd = wheel()
  expect(left).toBe(300)
  expect(atEnd.defaultPrevented).toBe(false)
  stub(100)
  left = 0
  const fits = wheel()
  expect(left).toBe(0)
  expect(fits.defaultPrevented).toBe(false)
}

export function openAllTabsMenu(): HTMLElement[] {
  // Radix opens a dropdown on a primary-button pointerdown.
  act(() => { fireEvent.pointerDown(screen.getByTestId('all-tabs-trigger'), { button: 0, ctrlKey: false, pointerType: 'mouse' }) })
  return screen.getAllByRole('menuitemradio')
}

/** The test DOM has no layout: give `scroller` a 100px scrollport and every tab in it 40px, including later ones. */
export function stubTabRow(scroller: HTMLElement): { left: () => number; tabRect: (id: string) => DOMRect } {
  let left = 0
  const tabs = () => [...scroller.querySelectorAll<HTMLElement>(`[${TAB_ID_ATTR}]`)]
  const rect = (x: number, width: number) => ({ x, y: 0, left: x, top: 0, width, height: 32, right: x + width, bottom: 32, toJSON: () => ({}) }) as DOMRect
  const tabRect = (id: string) => rect(tabs().findIndex(t => t.dataset.tabId === id) * 40 - left, 40)
  Object.defineProperty(scroller, 'clientWidth', { configurable: true, get: () => 100 })
  Object.defineProperty(scroller, 'scrollWidth', { configurable: true, get: () => tabs().length * 40 })
  Object.defineProperty(scroller, 'scrollLeft', { configurable: true, get: () => left, set: (v: number) => { left = v } })
  const real = Element.prototype.getBoundingClientRect
  vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
    if (this === scroller) return rect(0, 100)
    const id = this instanceof HTMLElement ? this.dataset.tabId : undefined
    return id !== undefined && scroller.contains(this) ? tabRect(id) : real.call(this)
  })
  return { left: () => left, tabRect }
}
