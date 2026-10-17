import { describe, it, expect, beforeEach, vi } from 'vitest'
import { screen, waitFor, act } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { WsContext } from '../src/App'
import { LogViewer } from '../src/pages/LogsPage'
import { server } from './mocks/server'
import { http, HttpResponse } from 'msw'

// Mock Virtuoso to render items directly in jsdom
vi.mock('react-virtuoso', () => ({
  Virtuoso: React.forwardRef(({ data, itemContent }: any, _ref: any) => (
    <div data-testid="virtuoso">{data?.map((item: any, i: number) => <div key={i}>{itemContent(i, item)}</div>)}</div>
  )),
}))

type LogCb = ((data: { level: string; msg: string }) => void) | null

function renderLogViewer() {
  let logCb: LogCb = null
  const subscribeLogs = (cb: LogCb) => { logCb = cb }
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  const utils = render(
    <QueryClientProvider client={queryClient}>
      <WsContext.Provider value={{ subscribeLogs, subscribeSubagents: () => {} }}>
        <LogViewer />
      </WsContext.Provider>
    </QueryClientProvider>
  )

  const pushLog = (level: string, msg: string) => {
    act(() => { logCb?.({ level, msg }) })
  }

  return { ...utils, pushLog }
}

describe('LogViewer level filtering', () => {
  beforeEach(() => {
    server.use(
      http.get('/api/logs/level', () => HttpResponse.json({ level: 'DEBUG' })),
      http.post('/api/logs/level', async ({ request }) => {
        const body = await request.json() as { level: string }
        return HttpResponse.json({ ok: true, level: body.level })
      })
    )
  })

  it('filters logs by selected level', async () => {
    const { pushLog } = renderLogViewer()

    await waitFor(() => expect(screen.getByText('Debug')).toBeInTheDocument())

    pushLog('DEBUG', '2026-05-25 DEBUG debug-msg')
    pushLog('INFO', '2026-05-25 INFO info-msg')
    pushLog('WARNING', '2026-05-25 WARNING warn-msg')
    pushLog('ERROR', '2026-05-25 ERROR error-msg')

    // Default level is DEBUG (from API mock) — all visible
    await waitFor(() => {
      expect(screen.getByText(/debug-msg/)).toBeInTheDocument()
      expect(screen.getByText(/info-msg/)).toBeInTheDocument()
      expect(screen.getByText(/warn-msg/)).toBeInTheDocument()
      expect(screen.getByText(/error-msg/)).toBeInTheDocument()
    })

    // Switch to ERROR level
    await userEvent.click(screen.getByText('Error'))

    await waitFor(() => {
      expect(screen.queryByText(/debug-msg/)).not.toBeInTheDocument()
      expect(screen.queryByText(/info-msg/)).not.toBeInTheDocument()
      expect(screen.queryByText(/warn-msg/)).not.toBeInTheDocument()
      expect(screen.getByText(/error-msg/)).toBeInTheDocument()
    })
  })

  it('WARNING level shows WARNING and ERROR only', async () => {
    const { pushLog } = renderLogViewer()

    await waitFor(() => expect(screen.getByText('Debug')).toBeInTheDocument())

    pushLog('DEBUG', '2026-05-25 DEBUG d-line')
    pushLog('INFO', '2026-05-25 INFO i-line')
    pushLog('WARNING', '2026-05-25 WARNING w-line')
    pushLog('ERROR', '2026-05-25 ERROR e-line')

    await userEvent.click(screen.getByText('Warning'))

    await waitFor(() => {
      expect(screen.queryByText(/d-line/)).not.toBeInTheDocument()
      expect(screen.queryByText(/i-line/)).not.toBeInTheDocument()
      expect(screen.getByText(/w-line/)).toBeInTheDocument()
      expect(screen.getByText(/e-line/)).toBeInTheDocument()
    })
  })

  it('INFO level hides DEBUG', async () => {
    const { pushLog } = renderLogViewer()

    await waitFor(() => expect(screen.getByText('Debug')).toBeInTheDocument())

    pushLog('DEBUG', '2026-05-25 DEBUG hidden-debug')
    pushLog('INFO', '2026-05-25 INFO visible-info')

    await userEvent.click(screen.getByText('Info'))

    await waitFor(() => {
      expect(screen.queryByText(/hidden-debug/)).not.toBeInTheDocument()
      expect(screen.getByText(/visible-info/)).toBeInTheDocument()
    })
  })
})
