// @vitest-environment jsdom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { api } from '../api/client'
import { createTestStore, renderWithProviders } from './helpers'
import { sseSlots } from '../store/dashboardSlice'
import SessionStatusFrame from '../pages/chat/command-center/SessionStatusFrame'
import AutomaticCardSetting from '../pages/chat/command-center/AutomaticCardSetting'
import { dashboardDocument } from '../pages/chat/command-center/dashboardDocument'

vi.mock('../hooks/useSandboxDoc', () => ({
  useSandboxDoc: (html: string | null) => ({ url: html ? '/sandbox/card' : null, pending: false, failed: false, retry: vi.fn() }),
}))

describe('dynamic dashboard text bindings', () => {
  it('reuses arbitrary model layout while binding data as text, never executable HTML', () => {
    const html = '<section class="custom-layout"><h2 data-dashboard-field="result">old</h2></section>'
    const document = new DOMParser().parseFromString(dashboardDocument(html, {}, 'dark', {
      result: '<img src=x onerror=alert(1)>',
    }), 'text/html')
    expect(document.querySelector('.custom-layout h2')?.textContent).toBe('<img src=x onerror=alert(1)>')
    expect(document.querySelector('img')).toBeNull()
    expect(document.querySelector('script')).toBeNull()
  })

  it('clears fields omitted by a data snapshot instead of presenting stale facts', () => {
    const document = new DOMParser().parseFromString(dashboardDocument(
      '<p data-dashboard-field="next">obsolete next step</p>', {}, 'light', {},
    ), 'text/html')
    expect(document.querySelector('p')?.textContent).toBe('')
  })

  it('does not bind prototype fields or treat data as a stylesheet without isolated CSSOM', () => {
    const document = new DOMParser().parseFromString(dashboardDocument(
      '<style data-dashboard-field="css">p{color:inherit}</style><p data-dashboard-field="constructor">old</p>',
      {}, 'light', { css: 'p{display:none}' },
    ), 'text/html')
    expect(document.querySelector('p')?.textContent).toBe('')
    expect(document.querySelector('style[data-dashboard-field]')).toBeNull()
    expect(document.documentElement.textContent).not.toContain('display:none')
  })
})

describe('host-owned card freshness and cost controls', () => {
  beforeEach(() => vi.restoreAllMocks())

  it.each([false, true])('describes a failed update truthfully with previous content available: %s', async available => {
    const get = vi.spyOn(api, 'dashboardCard').mockResolvedValue({
      card: available ? { html: '<p>Valid previous evidence</p>', data: {} } : null,
      status: 'failed', published_at: available ? 1_700_000_000 : null,
      content_event_at: available ? 1_700_000_000 : null, stale: true,
    })
    renderWithProviders(<SessionStatusFrame slot="worker" title="Worker" active />)
    expect(await screen.findByText('Content update failed. The next session event can retry.')).toBeVisible()
    expect(screen.queryByTitle('Worker') !== null).toBe(available)
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('keeps publication time across refreshes and unmounts the inactive iframe', async () => {
    const get = vi.spyOn(api, 'dashboardCard').mockResolvedValue({
      card: { html: '<p data-dashboard-field="next"></p>', data: { next: 'Review' } },
      status: 'published', published_at: 1_700_000_000, content_event_at: 1_699_999_000, stale: false,
    })
    const view = renderWithProviders(<SessionStatusFrame slot="worker" title="Review worker" active />)
    const iframe = await screen.findByTitle('Review worker')
    expect(iframe).toHaveAttribute('sandbox', '')
    const published = screen.getByText(/^Content published/).textContent
    expect(get).toHaveBeenCalledTimes(1)
    await act(async () => { await view.queryClient.invalidateQueries({ queryKey: ['dashboard-card', 'worker'] }) })
    expect(screen.getByText(/^Content published/)).toHaveTextContent(published!)
    expect(get).toHaveBeenCalledTimes(2)
    view.rerender(<SessionStatusFrame slot="worker" title="Review worker" active={false} />)
    expect(screen.queryByTitle('Review worker')).not.toBeInTheDocument()
  })

  it('uses an explicit native config action, never viewing, to opt into generation', async () => {
    vi.spyOn(api, 'kirocrewConfig').mockResolvedValue({ dashboard: { dynamic_dashboard_cards: false } })
    const patch = vi.spyOn(api, 'patchConfig').mockResolvedValue({})
    renderWithProviders(<AutomaticCardSetting active />)
    const toggle = await screen.findByRole('switch', { name: /Automatic cards for all sessions/ })
    await waitFor(() => expect(toggle).toBeEnabled())
    expect(toggle).toHaveAttribute('aria-checked', 'false')
    expect(patch).not.toHaveBeenCalled()
    const benefit = screen.getByText('Summaries of each session’s recent work and next steps.')
    expect(benefit).toBeVisible()
    expect(screen.getByText(/60 attempts per hour shared by all sessions, including failures/)).toBeVisible()
    expect(benefit.compareDocumentPosition(screen.getByText(/Off by default/)) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(toggle)
    await waitFor(() => expect(patch).toHaveBeenCalledWith('dashboard.dynamic_dashboard_cards', true))
  })

  it('drops an already displayed card immediately when its slot becomes private', async () => {
    const get = vi.spyOn(api, 'dashboardCard').mockResolvedValue({
      card: { html: '<p>Earlier content</p>', data: {} }, status: 'published',
      published_at: 1_700_000_000, content_event_at: 1_700_000_000, stale: false,
    })
    const store = createTestStore()
    store.dispatch(sseSlots([{ key: 'worker', messages: 2, running: false, memory_mode: 'persistent' }]))
    renderWithProviders(<SessionStatusFrame slot="worker" title="Worker" active />, { store })
    await screen.findByTitle('Worker')
    act(() => { store.dispatch(sseSlots([{ key: 'worker', messages: 2, running: false, memory_mode: 'incognito' }])) })
    expect(screen.queryByTitle('Worker')).not.toBeInTheDocument()
    expect(screen.queryByText(/^Content published/)).not.toBeInTheDocument()
    expect(get).toHaveBeenCalledTimes(1)
  })
})
