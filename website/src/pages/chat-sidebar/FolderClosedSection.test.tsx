/**
 * FolderClosedSection — the per-folder closed-sessions row.
 *
 * Pins: the collapsed label carries the folder's closed count and fetches
 * NOTHING (the active tree pays no request per folder); opening requests this
 * folder's closed, user-facing sessions; a click resumes; an empty or failed
 * fetch says so instead of rendering a blank region; a longer list offers the
 * Older Sessions pane; a count change while open refetches.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useState } from 'react'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

const { apiSessions } = vi.hoisted(() => ({ apiSessions: vi.fn() }))

vi.mock('../../api/client', () => ({
  api: { sessions: (...a: unknown[]) => apiSessions(...a) },
}))

vi.mock('../../i18n/t', async importOriginal => {
  const orig = await importOriginal<typeof import('../../i18n/t')>()
  return {
    ...orig,
    i18nT: (key: string, params?: Record<string, unknown>) =>
      params ? `${key} ${JSON.stringify(params)}` : key,
  }
})

import { FolderClosedSection, FOLDER_CLOSED_LIMIT, FOLDER_CLOSED_FOOT_TOGGLE_AT } from './FolderClosedSection'

interface HarnessProps {
  count?: number
  connected?: boolean
  onResume: (s: { key: string; title: string }) => void
  onOpenOlderSessions: () => void
}

/** Owns `open` the way the sidebar does, outside the row. */
function Harness({ count = 2, connected = true, onResume, onOpenOlderSessions }: HarnessProps) {
  const [open, setOpen] = useState(false)
  return (
    <FolderClosedSection folderId="f-1" folderName="Reviews" count={count} open={open} onToggle={() => setOpen(o => !o)}
      connected={connected} onResume={onResume} onOpenOlderSessions={onOpenOlderSessions}
      renderChevron={o => <span>{o ? 'v' : '>'}</span>} />
  )
}

function renderHarness(props: Partial<HarnessProps> = {}) {
  const onResume = vi.fn()
  const onOpenOlderSessions = vi.fn()
  // The app's client defaults to `staleTime: Infinity`; mirror it so caching bites.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  const ui = (p: Partial<HarnessProps>) => (
    <QueryClientProvider client={qc}><Harness onResume={onResume} onOpenOlderSessions={onOpenOlderSessions} {...props} {...p} /></QueryClientProvider>
  )
  const utils = render(ui({}))
  return { ...utils, onResume, onOpenOlderSessions, rerenderWith: (p: Partial<HarnessProps>) => utils.rerender(ui(p)) }
}

const toggle = () => screen.getByTestId('folder-closed-toggle-f-1')

beforeEach(() => { apiSessions.mockReset() })

describe('FolderClosedSection', () => {
  it('names the closed count while collapsed and fetches nothing', () => {
    renderHarness({ count: 3 })
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    expect(toggle().textContent).toContain('pages.chatSidebar.folder_closed_show {"count":3}')
    expect(apiSessions).not.toHaveBeenCalled()
  })

  it('opens to this folder\'s closed sessions and resumes one on click', async () => {
    apiSessions.mockResolvedValue({ sessions: [
      { key: 'dashboard_a', title: 'Alpha', messages: 2 },
      { key: 'dashboard_b', title: 'Beta', messages: 4 },
    ], has_more: false })
    const { onResume } = renderHarness()
    fireEvent.click(toggle())
    // exclude_open + user_only, the Older Sessions pane's own narrowing, scoped to f-1.
    await waitFor(() => expect(apiSessions).toHaveBeenCalledWith(FOLDER_CLOSED_LIMIT, 0, false, true, true, 'f-1'))
    await screen.findByTestId('folder-closed-row-dashboard_a')
    expect(toggle().textContent).toContain('pages.chatSidebar.folder_closed_hide {"count":2}')

    fireEvent.click(screen.getByTestId('folder-closed-row-dashboard_a'))
    expect(onResume).toHaveBeenCalledWith({ key: 'dashboard_a', title: 'Alpha' })
  })

  it('says so when the list comes back empty', async () => {
    apiSessions.mockResolvedValue({ sessions: [], has_more: false })
    renderHarness()
    fireEvent.click(toggle())
    await screen.findByText('pages.chatSidebar.folder_closed_none')
  })

  it('says so when the fetch fails', async () => {
    apiSessions.mockRejectedValue(new Error('boom'))
    renderHarness()
    fireEvent.click(toggle())
    expect((await screen.findByTestId('folder-closed-error-f-1')).textContent).toContain('pages.chatSidebar.folder_closed_load_failed')
  })

  it('offers the Older Sessions pane when there is more than one page', async () => {
    apiSessions.mockResolvedValue({ sessions: [{ key: 'dashboard_a', title: 'Alpha', messages: 1 }], has_more: true })
    const { onOpenOlderSessions } = renderHarness()
    fireEvent.click(toggle())
    fireEvent.click(await screen.findByTestId('folder-closed-more-f-1'))
    expect(onOpenOlderSessions).toHaveBeenCalledTimes(1)
  })

  it('refetches when the count moves while open, and not while closed', async () => {
    apiSessions.mockResolvedValue({ sessions: [], has_more: false })
    const { rerenderWith } = renderHarness()
    rerenderWith({ count: 4 })
    expect(apiSessions).not.toHaveBeenCalled()
    fireEvent.click(toggle())
    await waitFor(() => expect(apiSessions).toHaveBeenCalledTimes(1))
    rerenderWith({ count: 5 })
    await waitFor(() => expect(apiSessions).toHaveBeenCalledTimes(2))
  })

  it('refetches when the count returns to an earlier value', async () => {
    apiSessions.mockResolvedValue({ sessions: [], has_more: false })
    const { rerenderWith } = renderHarness({ count: 3 })
    fireEvent.click(toggle())
    await waitFor(() => expect(apiSessions).toHaveBeenCalledTimes(1))
    rerenderWith({ count: 4 })
    await waitFor(() => expect(apiSessions).toHaveBeenCalledTimes(2))
    rerenderWith({ count: 3 })
    await waitFor(() => expect(apiSessions).toHaveBeenCalledTimes(3))
  })

  it('names the reopen action on each row', async () => {
    apiSessions.mockResolvedValue({ sessions: [{ key: 'dashboard_a', title: 'Alpha', messages: 1 }], has_more: false })
    renderHarness()
    fireEvent.click(toggle())
    const row = await screen.findByTestId('folder-closed-row-dashboard_a')
    expect(row.getAttribute('title')).toBe('pages.chatSidebar.folder_closed_reopen {"title":"Alpha"}')
  })

  it('repeats the toggle at the foot of a long list, and it collapses the list', async () => {
    const many = Array.from({ length: FOLDER_CLOSED_FOOT_TOGGLE_AT + 1 }, (_, i) => ({ key: `dashboard_${i}`, title: `S${i}`, messages: 1 }))
    apiSessions.mockResolvedValue({ sessions: many, has_more: false })
    renderHarness({ count: many.length })
    fireEvent.click(toggle())
    const foot = await screen.findByTestId('folder-closed-foot-toggle-f-1')
    fireEvent.click(foot)
    expect(toggle().getAttribute('aria-expanded')).toBe('false')
    expect(screen.queryByTestId('folder-closed-foot-toggle-f-1')).toBeNull()
  })

  it('has no foot toggle on a short list', async () => {
    apiSessions.mockResolvedValue({ sessions: [{ key: 'dashboard_a', title: 'Alpha', messages: 1 }], has_more: false })
    renderHarness({ count: 1 })
    fireEvent.click(toggle())
    await screen.findByTestId('folder-closed-row-dashboard_a')
    expect(screen.queryByTestId('folder-closed-foot-toggle-f-1')).toBeNull()
  })

  it('cannot resume while disconnected, and says why', async () => {
    apiSessions.mockResolvedValue({ sessions: [{ key: 'dashboard_a', title: 'Alpha', messages: 1 }], has_more: false })
    const { onResume } = renderHarness({ connected: false })
    fireEvent.click(toggle())
    const row = await screen.findByTestId('folder-closed-row-dashboard_a')
    expect((row as HTMLButtonElement).disabled).toBe(true)
    expect(row.getAttribute('title')).not.toContain('folder_closed_reopen')
    fireEvent.click(row)
    expect(onResume).not.toHaveBeenCalled()
  })
})
