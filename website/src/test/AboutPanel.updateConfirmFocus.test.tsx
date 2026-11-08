/**
 * Settings -> About "Update" confirm — a modal dialog that keeps keyboard
 * focus inside while open and gives it back to the Update button on close.
 *
 * The dialog is `aria-modal`, so focus left behind on the page, or a Tab that
 * walks out of it, reaches controls hidden behind the overlay. Escape stays
 * owned by AboutPanel.tsx (it refuses while an apply is in flight).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { store } from '../store'
import { sseStatus } from '../store/dashboardSlice'
import { MemoryRouter } from 'react-router-dom'
import { AboutPanel } from '../pages/settings/AboutPanel'

const BLANK_STATUS = {
  uptime: '1m', sessions: 0, messages: 0, cron_jobs: 0, subagents: 0, lessons: 0,
} as const

function stubFetch() {
  const json = (body: unknown) => ({
    ok: true, status: 200, json: async () => body,
    text: async () => JSON.stringify(body),
    headers: new Headers({ 'content-type': 'application/json' }),
  })
  vi.stubGlobal('fetch', vi.fn(async (input: unknown) => {
    const url = String(input)
    if (url.includes('/api/update/check')) {
      return json({
        check_status: 'succeeded', update_available: true, error_code: null,
        managed_by: 'git', can_apply: true, latest_version: '0.1.3', changes: '### 0.1.3\n- thing',
      })
    }
    // The apply never answers, so the dialog stays in its in-flight state.
    if (url.endsWith('/api/update')) return new Promise<never>(() => {})
    if (url.includes('/api/config/kirocrew')) return json({ auto_update: true })
    if (url.includes('/api/changelog')) return json({ content: '' })
    return json({})
  }))
}

async function openConfirm() {
  store.dispatch(sseStatus({ ...BLANK_STATUS, update_available: true, update_can_apply: true } as never))
  stubFetch()
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <MemoryRouter>
          <AboutPanel />
        </MemoryRouter>
      </QueryClientProvider>
    </Provider>,
  )
  const trigger = await screen.findByRole('button', { name: /^Update(?! the gateway)/ })
  trigger.focus()
  fireEvent.click(trigger)
  const dialog = await screen.findByRole('dialog')
  // Wait for the pre-apply check to land so the dialog's controls settle.
  await within(dialog).findByRole('button', { name: /^Update now$/i })
  return { trigger, dialog }
}

describe('AboutPanel update confirm focus', () => {
  beforeEach(() => {
    delete (window as unknown as { updateAPI?: unknown }).updateAPI
  })
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
    store.dispatch(sseStatus({ ...BLANK_STATUS } as never))
  })

  it('moves focus into the dialog when it opens', async () => {
    const { dialog } = await openConfirm()
    expect(dialog.contains(document.activeElement)).toBe(true)
  })

  it('wraps Tab from the last control back to the first, and Shift+Tab back again', async () => {
    const { dialog } = await openConfirm()
    const close = within(dialog).getByRole('button', { name: /close/i })
    const apply = within(dialog).getByRole('button', { name: /^Update now$/i })
    apply.focus()
    fireEvent.keyDown(apply, { key: 'Tab' })
    expect(document.activeElement).toBe(close)
    fireEvent.keyDown(close, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(apply)
  })

  it('keeps Tab inside while the update runs, and Close still refuses', async () => {
    const { dialog } = await openConfirm()
    fireEvent.click(within(dialog).getByRole('button', { name: /^Update now$/i }))
    const close = within(dialog).getByRole('button', { name: /close/i })
    await waitFor(() => expect(close).toHaveAttribute('aria-disabled', 'true'))
    // The apply button is disabled now; focus has fallen out to the page.
    ;(document.activeElement as HTMLElement | null)?.blur()
    fireEvent.keyDown(document.body, { key: 'Tab' })
    expect(document.activeElement).toBe(close)
    fireEvent.click(close)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
  })

  it('returns focus to the Update button when the dialog closes', async () => {
    const { dialog, trigger } = await openConfirm()
    // Focus sits inside the dialog, as it does after a Tab into it; when the
    // dialog unmounts, the focused node goes with it.
    const close = within(dialog).getByRole('button', { name: /close/i })
    close.focus()
    fireEvent.click(close)
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    expect(document.activeElement).toBe(trigger)
  })
})
