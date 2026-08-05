import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import CronTab from '../src/pages/overview/CronTab'
import dashboardSlice, { sseStatus } from '../src/store/dashboardSlice'
import chatSlice from '../src/store/chatSlice'
import notificationsSlice from '../src/store/notificationsSlice'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'

const createTestStore = (noCrons = false) => {
  const store = configureStore({
    reducer: {
      dashboard: dashboardSlice,
      chat: chatSlice,
      notifications: notificationsSlice,
    },
  })
  store.dispatch(sseStatus({
    uptime: '1h',
    start_time: Date.now() / 1000 - 3600,
    sessions: 1,
    messages: 0,
    cron_jobs: 0,
    lessons: 0,
    subagents: 0,
    update_available: false,
    no_crons: noCrons,
  } as any))
  return store
}

describe('CronTab --no-crons banner', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/agents/installed', () =>
        HttpResponse.json([{ name: 'default', source: 'builtin' }])
      ),
      http.get('/api/cron', () => HttpResponse.json([])),
    )
  })

  it('shows warning banner when no_crons is true', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Provider store={createTestStore(true)}>
          <BrowserRouter><CronTab refreshTrigger={0} /></BrowserRouter>
        </Provider>
      </QueryClientProvider>
    )
    expect(screen.getByText(/Cron execution disabled/)).toBeDefined()
    expect(screen.getByText('--no-crons')).toBeDefined()
  })

  it('hides banner when no_crons is false', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}>
        <Provider store={createTestStore(false)}>
          <BrowserRouter><CronTab refreshTrigger={0} /></BrowserRouter>
        </Provider>
      </QueryClientProvider>
    )
    expect(screen.queryByText(/Cron execution disabled/)).toBeNull()
  })
})
