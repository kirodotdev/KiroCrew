/**
 * A rejected hook-test request must surface an inline failure panel in the test
 * region, not just clear the previous result. Before the fix, handleTest cleared
 * testResult and the failed mutation had no onError, so the panel rendered empty
 * and only the global banner (which the user may have scrolled past) reported it.
 *
 * These pin both halves: the inline panel appears with the error message, AND the
 * global error banner still fires — the inline state adds to the global path, it
 * does not replace it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor, act, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

const testHook = vi.fn()

let hooksPayload: { hooks: unknown[] } = { hooks: [] }

vi.mock('../api/client', () => ({
  api: new Proxy({} as Record<string, unknown>, {
    get: (_t, prop: string) => {
      if (prop === 'hooks') return vi.fn(async () => hooksPayload)
      if (prop === 'testHook') return testHook
      return vi.fn().mockResolvedValue({})
    },
  }),
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    capabilities: { hooks: true },
    labels: { hooksSection: 'Provider hooks', configFile: '~/.kiro/config.json' },
    fetchProviderHooks: () => Promise.resolve({}),
  }),
}))

import HooksPage from '../pages/HooksPage'

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <HooksPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  hooksPayload = {
    hooks: [
      {
        id: 'h1',
        name: 'fmt',
        event: 'PreToolUse',
        command: 'black .',
        matcher: 'Bash',
        enabled: true,
        run_count: 0,
        last_status: '',
        last_error: '',
      },
    ],
  }
})

describe('Hooks page — inline test-failure state', () => {
  it('renders an inline failure panel and keeps the global banner when a test rejects', async () => {
    testHook.mockRejectedValueOnce(new Error('hook test endpoint unreachable'))
    renderPage()

    // The Test button lives in the row; wait for the hook to load.
    const testBtn = await screen.findByRole('button', { name: /^test$/i })
    await act(async () => { fireEvent.click(testBtn) })

    await waitFor(() => {
      // Inline failure panel, scoped to the hook that was tested.
      const alert = screen.getByRole('alert')
      const label = within(alert).getByText(/test failed: fmt/i)
      const message = within(alert).getByText('hook test endpoint unreachable')
      const dismiss = within(alert).getByRole('button', { name: /dismiss/i })
      expect(label).toHaveClass('min-w-0', 'break-words')
      expect(message).toHaveClass('break-words')
      expect(dismiss).toHaveClass('shrink-0')
      expect(alert).toHaveTextContent(/test failed: fmt/i)
      expect(alert).toHaveTextContent('hook test endpoint unreachable')
    })

    // The global error path is preserved: mutError still reports the same message.
    const banners = screen.getAllByText('hook test endpoint unreachable')
    expect(banners.length).toBeGreaterThanOrEqual(2)
  })

  it('clears a prior failure panel when a new test starts', async () => {
    testHook
      .mockRejectedValueOnce(new Error('first failure'))
      .mockResolvedValueOnce({ result: { exit_code: 0, duration_ms: 5, stdout: 'ok' } })
    renderPage()

    const testBtn = await screen.findByRole('button', { name: /^test$/i })
    await act(async () => { fireEvent.click(testBtn) })
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('first failure'))

    await act(async () => { fireEvent.click(testBtn) })
    await waitFor(() => {
      expect(screen.queryByRole('alert')).toBeNull()
      expect(screen.getByText(/test result/i)).toBeInTheDocument()
    })
  })

  it('allows only one hook test request at a time', async () => {
    let resolveTest!: (value: { result: { exit_code: number; duration_ms: number } }) => void
    testHook.mockImplementationOnce(() => new Promise(resolve => { resolveTest = resolve }))
    renderPage()

    const testBtn = await screen.findByRole('button', { name: /^test$/i })
    act(() => {
      fireEvent.click(testBtn)
      fireEvent.click(testBtn)
    })

    await waitFor(() => expect(testHook).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(testBtn).toBeDisabled())

    await act(async () => {
      resolveTest({ result: { exit_code: 0, duration_ms: 5 } })
    })
    await waitFor(() => expect(testBtn).not.toBeDisabled())
  })
})
