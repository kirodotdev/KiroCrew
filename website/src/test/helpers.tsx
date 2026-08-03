import React from 'react'
import { render, renderHook, type RenderOptions } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { configureStore } from '@reduxjs/toolkit'
import dashboardReducer from '../store/dashboardSlice'
import chatReducer from '../store/chatSlice'
import notificationsReducer from '../store/notificationsSlice'
import instancesReducer from '../store/instancesSlice'
import appStatusReducer from '../store/appStatusSlice'
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
      appStatus: appStatusReducer,
    },
    preloadedState,
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
