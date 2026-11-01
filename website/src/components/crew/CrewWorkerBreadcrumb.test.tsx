/**
 * The worker breadcrumb on a phone: the root's name keeps a readable minimum and
 * a tooltip, the worker's name stays, and the status is a dot with its word for
 * readers. The crumbs are plain text with no control. jsdom has no layout, so the rules are
 * pinned on the classes that carry them; the pod check measures the pixels.
 */
import { afterEach, describe, it, expect, vi } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { createTestStore } from '../../test/helpers'

const mobile = vi.hoisted(() => ({ value: true }))
vi.mock('../../hooks/useIsMobile', () => ({ useIsMobile: () => mobile.value }))
const fail = vi.hoisted(() => ({ on: false, workers: [] as unknown[] }))
vi.mock('../../api/client', () => ({ api: { crewWorkers: () => (fail.on ? Promise.reject(new Error('down')) : Promise.resolve({ root: 'root', workers: fail.workers })) } }))

import { CrewWorkerBreadcrumb } from './CrewWorkerBreadcrumb'

function renderCrumb() {
  const store = createTestStore({ dashboard: { slots: [{ key: 'root', title: 'New Teammate Welcome Kit', crew_mode_root: true }] } } as never)
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={qc}><Provider store={store}>
      <CrewWorkerBreadcrumb root="root" worker={{ key: 'w1', title: 'First-week checklist', running: false }} />
    </Provider></QueryClientProvider>,
  )
}

describe('CrewWorkerBreadcrumb on a phone', () => {
  afterEach(() => { vi.useRealTimers() })

  it('is plain text: no ghost, no accent, and no control of its own', () => {
    renderCrumb()
    const root = screen.getByTestId('crew-breadcrumb-root')
    expect(root).toHaveTextContent('New Teammate Welcome Kit')
    expect(root).not.toHaveClass('crew-grad-text')
    expect(screen.queryByTestId('crew-ghost-badge')).toBeNull()
    // The one way back is the Crew tab's "Back to main chat" bar.
    expect(screen.getByTestId('crew-worker-breadcrumb').querySelector('button')).toBeNull()
  })

  it('keeps the root readable and names both', () => {
    renderCrumb()
    const rootCrumb = screen.getByTestId('crew-breadcrumb-root')
    expect(rootCrumb).toHaveClass('min-w-0', '@[18rem]/crumb:min-w-[5.5rem]')
    expect(rootCrumb).toHaveAttribute('title', 'New Teammate Welcome Kit')
    const worker = screen.getByTestId('crew-breadcrumb-worker')
    expect(worker).toHaveTextContent('First-week checklist')
    expect(worker).toHaveAttribute('title', 'First-week checklist')
    // The status word is for readers only on a phone; the dot carries it.
    expect(screen.getByTestId('crew-status').querySelector('.sr-only')).not.toBeNull()
  })

  it('keeps the status word accessible and yields its drawn label to a narrow pane', () => {
    mobile.value = false
    try {
      renderCrumb()
      const label = screen.getByTestId('crew-status').querySelector('span:not([aria-hidden])')
      expect(label).toHaveTextContent('Waiting')
      expect(screen.getByTestId('crew-status')).toHaveClass('[&>span:last-child]:sr-only', '@[40rem]/crumb:[&>span:last-child]:not-sr-only')
      expect(screen.getByTestId('crew-breadcrumb-worker')).not.toHaveClass('@[30rem]/crumb:shrink-0')
    } finally {
      mobile.value = true
    }
  })

  it('says a failed worker-list read beside the status it fell back to', async () => {
    fail.on = true
    try {
      renderCrumb()
      expect(await screen.findByText('Could not load the crew.')).toBeInTheDocument()
      expect(screen.getByTestId('crew-worker-breadcrumb')).toBeInTheDocument()
    } finally {
      fail.on = false
    }
  })

  it('uses the new worker row state when switching workers after a failed read', async () => {
    fail.workers = [{ key: 'w1', group: 'done' }]
    const store = createTestStore({ dashboard: { slots: [{ key: 'root', title: 'Sample goal', crew_mode_root: true }] } } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const view = (worker: { key: string; title: string; running: boolean }) => (
      <QueryClientProvider client={qc}><Provider store={store}>
        <CrewWorkerBreadcrumb root="root" worker={worker} />
      </Provider></QueryClientProvider>
    )
    const { rerender, unmount } = render(view({ key: 'w1', title: 'Finished worker', running: false }))
    try {
      expect(await screen.findByText('Done')).toBeInTheDocument()
      fail.on = true
      await act(async () => { await qc.refetchQueries({ queryKey: ['crew-workers', 'root'] }) })
      expect(await screen.findByText('Could not load the crew.')).toBeInTheDocument()
      expect(screen.getByTestId('crew-status')).toHaveAttribute('data-group', 'done')
      rerender(view({ key: 'w2', title: 'Active worker', running: true }))
      expect(screen.getByTestId('crew-breadcrumb-worker')).toHaveTextContent('Active worker')
      expect(screen.getByTestId('crew-status')).toHaveAttribute('data-group', 'working')
    } finally {
      unmount()
      qc.clear()
      fail.on = false
      fail.workers = []
    }
  })

  it('keeps an OPEN worker Done and a closed dispatcher reopenable when a re-read fails', async () => {
    // The worker is an open slot of this root, so a failed read once handed the
    // breadcrumb its live row (group Waiting) and dropped the closed dispatcher.
    fail.workers = [
      { key: 'w1', title: 'Finished worker', group: 'done', status: 'idle', dispatched_by: 'd1' },
      { key: 'd1', title: 'Sub conductor', group: 'done', status: 'closed', dispatched_by: 'root' },
    ]
    const store = createTestStore({ dashboard: { slots: [
      { key: 'root', title: 'Sample goal', crew_mode_root: true },
      { key: 'w1', title: 'Finished worker', crew_root: 'root', created_by: 'd1', running: false },
    ] } } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const { unmount } = render(
      <QueryClientProvider client={qc}><Provider store={store}>
        <CrewWorkerBreadcrumb root="root" worker={{ key: 'w1', title: 'Finished worker', running: false }} />
      </Provider></QueryClientProvider>,
    )
    try {
      expect(await screen.findByText('Done')).toBeInTheDocument()
      expect(screen.getByTestId('crew-breadcrumb-via')).toHaveTextContent('Sub conductor')
      fail.on = true
      await act(async () => { await qc.refetchQueries({ queryKey: ['crew-workers', 'root'] }) })
      expect(await screen.findByText('Could not load the crew.')).toBeInTheDocument()
      expect(screen.getByTestId('crew-status')).toHaveAttribute('data-group', 'done')
      // The closed dispatcher is still the saved record, named and reachable.
      expect(screen.getByTestId('crew-breadcrumb-via')).toHaveTextContent('Sub conductor')
    } finally {
      unmount()
      qc.clear()
      fail.on = false
      fail.workers = []
    }
  })

  it('keeps the last group read when a later poll fails: a done worker stays Done', async () => {
    // The list answered once (the worker is done); a 5s poll then fails. The
    // row's own flags cannot say done, so the last read stands under the notice.
    vi.useFakeTimers({ shouldAdvanceTime: true })
    fail.workers = [{ key: 'w1', title: 'First-week checklist', agent: 'kirocrew-worker', status: 'idle', group: 'done', last_activity: '', ledger_item: '', summary: '', created_by: '' }]
    try {
      renderCrumb()
      expect(await screen.findByText('Done')).toBeInTheDocument()
      fail.on = true
      await act(async () => { vi.advanceTimersByTime(16_000) })
      expect(await screen.findByText('Could not load the crew.')).toBeInTheDocument()
      expect(screen.getByTestId('crew-status')).toHaveAttribute('data-group', 'done')
    } finally {
      vi.useRealTimers()
      fail.on = false
      fail.workers = []
    }
  })
})

describe('CrewWorkerBreadcrumb for a sub-conductor\'s crew (C)', () => {
  it('reads main chat > sub-conductor > member, as labels with no control', async () => {
    mobile.value = false
    fail.on = false
    fail.workers = [
      { key: 'sub', title: 'Sub conductor', agent: 'kirocrew-conductor', status: 'idle', group: 'done', last_activity: '', ledger_item: '', created_by: 'root', dispatched_by: 'root' },
      { key: 'w1', title: 'First-week checklist', agent: 'kirocrew-worker', status: 'idle', group: 'done', last_activity: '', ledger_item: '', created_by: 'sub', dispatched_by: 'sub' },
    ]
    const store = createTestStore({ dashboard: { slots: [{ key: 'root', title: 'New Teammate Welcome Kit', crew_mode_root: true }, { key: 'sub', title: 'Sub conductor', crew_root: 'root' }] } } as never)
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={qc}><Provider store={store}>
        <CrewWorkerBreadcrumb root="root" worker={{ key: 'w1', title: 'First-week checklist', running: false }} />
      </Provider></QueryClientProvider>,
    )
    const via = await screen.findByTestId('crew-breadcrumb-via')
    const crumbs = [screen.getByTestId('crew-breadcrumb-root'), via, screen.getByTestId('crew-breadcrumb-worker')]
    expect(crumbs.map(c => c.textContent)).toEqual(['New Teammate Welcome Kit', 'Sub conductor', 'First-week checklist'])
    // The middle crumb gives way first on a narrow header.
    expect(via.className).toContain('shrink-[3]')
    // The crumbs are labels; the way back is the Crew tab's bar.
    expect(via.tagName).toBe('SPAN')
    expect(screen.getByTestId('crew-breadcrumb-root').tagName).toBe('SPAN')
    expect(screen.getByTestId('crew-worker-breadcrumb').querySelectorAll('button')).toHaveLength(0)
    expect(store.getState().chat.activeSlot).not.toBe('root')
    fail.workers = []
    mobile.value = true
  })
})
