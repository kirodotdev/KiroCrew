import React from 'react'
import { render, renderHook, waitFor, type RenderOptions } from '@testing-library/react'
import { expect } from 'vitest'
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
