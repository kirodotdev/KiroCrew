import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, act, fireEvent, within, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { AboutPanel } from '../pages/settings/AboutPanel'

const BLANK_STATUS = { uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0 }

function setStatus(extra: Record<string, unknown>) {
  act(() => { store.dispatch(sseStatus({
    ...BLANK_STATUS, version: '0.12.1', update_can_apply: false, update_can_arm: true,
    update_check_status: 'succeeded', ...extra,
  } as never)) })
}

describe('AboutPanel gateway update target', () => {
  it('re-checks a superseded local checkout result and confirms only the fresh release notes', async () => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
    const checkUpdate = vi.fn()
      .mockResolvedValueOnce({
        check_status: 'succeeded', update_available: true, can_apply: true,
        latest_version: '0.12.2', latest_version_display: '0.12.2', last_checked_at: 100,
        changes: 'Cached notes for 0.12.2',
      })
      .mockResolvedValueOnce({
        check_status: 'succeeded', update_available: true, can_apply: true,
        latest_version: '0.12.3', latest_version_display: '0.12.3', last_checked_at: 200,
        changes: 'Fresh notes for 0.12.3',
      })
    vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
      const body = String(input).includes('/api/update/check') ? await checkUpdate() : {}
      return {
        ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body),
        headers: new Headers({ 'content-type': 'application/json' }),
      }
    }))
    setStatus({
      update_can_apply: true, update_can_arm: false, update_available: false,
      update_latest_version: '0.12.1', update_last_checked_at: 100,
    })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter><AboutPanel /></MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    fireEvent.click(await screen.findByTestId('about-check-for-updates'))
    expect(await screen.findByText('Cached notes for 0.12.2')).toBeInTheDocument()
    expect(checkUpdate).toHaveBeenCalledTimes(1)

    setStatus({
      update_can_apply: true, update_can_arm: false, update_available: true,
      update_latest_version: '0.12.3', update_latest_version_display: '0.12.3',
      update_last_checked_at: 200,
    })
    fireEvent.click(await screen.findByRole('button', { name: 'Update to v0.12.3' }))
    const dialog = await screen.findByRole('dialog', { name: 'Update' })
    expect(dialog).not.toHaveTextContent('Cached notes for 0.12.2')
    await waitFor(() => expect(checkUpdate).toHaveBeenCalledTimes(2))
    expect(await within(dialog).findByText('Fresh notes for 0.12.3')).toBeInTheDocument()
    expect(dialog).toHaveTextContent('Update to v0.12.3')
    expect(dialog).not.toHaveTextContent('Cached notes for 0.12.2')
  })

  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('uses the newer background target for the update label and GitHub link after a local check of the current version', async () => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
    vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
      const body = String(input).includes('/api/update/check') ? {
        check_status: 'succeeded', update_available: false, can_apply: false, can_arm: true,
        latest_version: '0.12.1', latest_version_display: '0.12.1', last_checked_at: 100,
      } : {}
      return {
        ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body),
        headers: new Headers({ 'content-type': 'application/json' }),
      }
    }))
    setStatus({ update_available: false, update_latest_version: '0.12.1', update_last_checked_at: 100 })
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <MemoryRouter><AboutPanel /></MemoryRouter>
        </QueryClientProvider>
      </Provider>,
    )
    fireEvent.click(await screen.findByTestId('about-check-for-updates'))
    expect(await screen.findByTestId('up-to-date')).toBeInTheDocument()

    setStatus({
      update_available: true, update_latest_version: '0.12.2',
      update_latest_version_display: '0.12.2', update_last_checked_at: 200,
    })
    expect(await screen.findByTestId('in-app-update-action')).toHaveTextContent('Update to v0.12.2')
    expect(screen.getByText('Kiro Crew 0.12.2 is available.')).toBeInTheDocument()
    const link = within(await screen.findByTestId('update-changes-empty')).getByRole('link')
    expect(link).toHaveAttribute('href', 'https://github.com/kirodotdev/KiroCrew/releases/tag/v0.12.2')
    expect(screen.queryByRole('button', { name: /Update to v0\.12\.1/ })).toBeNull()
  })
})
