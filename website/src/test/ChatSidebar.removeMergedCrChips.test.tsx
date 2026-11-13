import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createConnectedTestStore } from './helpers'
import { ThemeProvider } from '../hooks/useTheme'
import type { RootState } from '../store'
import { initI18n } from '../i18n/all'
import type { ChatSlot } from '../types'
import type { PullRequestStatusBatch } from '../types'

// Live-status endpoint + the per-link unlink are the two the sweep drives; mock
// both so the test asserts WHICH links the action removes, from the LIVE status
// the provider reports — not from the chip's own cached `state`.
const { statusMock, unlinkMock } = vi.hoisted(() => ({
  statusMock: vi.fn<(urls: string[]) => Promise<PullRequestStatusBatch>>(),
  unlinkMock: vi.fn<(slot: string, identity: string, expect: string) => Promise<unknown>>(),
}))
vi.mock('../api/client', async importOriginal => {
  const actual = await importOriginal<typeof import('../api/client')>()
  return {
    ...actual,
    api: {
      ...actual.api,
      pullRequestStatuses: (...a: [string[]]) => statusMock(...a),
      unlinkSourceLink: (...a: [string, string, string]) => unlinkMock(...a),
    },
  }
})

import { DropdownMenu, DropdownMenuContent } from '../components/ui/dropdown-menu'
import RemoveMergedCrChips from '../pages/chat-sidebar/RemoveMergedCrChips'

const MERGED_URL = 'https://github.com/acme/widgets/pull/100'
const OPEN_URL = 'https://github.com/acme/widgets/pull/200'
const ISSUE_URL = 'https://github.com/acme/widgets/issues/300'
const MERGED_ID = '["github","github.com","acme","widgets",100,"","change",""]'
const OPEN_ID = '["github","github.com","acme","widgets",200,"","change",""]'
const ISSUE_ID = '["github","github.com","acme","widgets",300,"","issue",""]'

// Three sessions: one chip for a MERGED change, one for an OPEN change, one
// ISSUE chip. Each chip carries `state: 'merged'` IN THE CACHED PAYLOAD for both
// changes on purpose — the point is the action must NOT trust that cache, it
// must read the live status below, where only #100 is actually merged.
function rows(): ChatSlot[] {
  return [
    { key: 's-merged', title: 'merged', messages: 1, running: false, row_identity: 's-merged', created: '2026-01-01T00:00:00Z',
      source_links: [{ provider: 'github', number: 100, label: '#100', url: MERGED_URL, state: 'merged', kind: 'change', identity: MERGED_ID }], source_links_total: 1 },
    { key: 's-open', title: 'open', messages: 1, running: false, row_identity: 's-open', created: '2026-01-02T00:00:00Z',
      // Cached state LIES (says merged), live status says open — the chip must survive.
      source_links: [{ provider: 'github', number: 200, label: '#200', url: OPEN_URL, state: 'merged', kind: 'change', identity: OPEN_ID }], source_links_total: 1 },
    { key: 's-issue', title: 'issue', messages: 1, running: false, row_identity: 's-issue', created: '2026-01-03T00:00:00Z',
      source_links: [{ provider: 'github', number: 300, label: '#300', url: ISSUE_URL, kind: 'issue', identity: ISSUE_ID }], source_links_total: 1 },
  ] as unknown as ChatSlot[]
}

function renderAction(list = rows()) {
  const store = createConnectedTestStore({
    dashboard: { slots: list } as unknown as RootState['dashboard'],
  })
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(
    <QueryClientProvider client={qc}><Provider store={store}><ThemeProvider><MemoryRouter>
      <DropdownMenu open>
        <DropdownMenuContent forceMount>
          <RemoveMergedCrChips />
        </DropdownMenuContent>
      </DropdownMenu>
    </MemoryRouter></ThemeProvider></Provider></QueryClientProvider>,
  )
  return { store }
}

function item() { return screen.getByTestId('remove-merged-cr-chips') }

describe('RemoveMergedCrChips — bulk-remove merged-CR chips across the sidebar', () => {
  beforeEach(async () => {
    await initI18n()
    statusMock.mockReset()
    unlinkMock.mockReset().mockResolvedValue({ ok: true })
    // LIVE status: only #100 is merged. #200 is OPEN despite its cached chip
    // state claiming 'merged'. #300 is an issue and is never asked about.
    statusMock.mockResolvedValue({
      statuses: {
        [MERGED_URL]: { state: 'merged' },
        [OPEN_URL]: { state: 'open' },
      },
    })
  })

  it('removes ONLY the chip whose change is live-merged, leaving the unmerged one', async () => {
    renderAction()
    // Two-step confirm: first select stages, second commits the sweep.
    fireEvent.click(item())
    fireEvent.click(item())

    await waitFor(() => expect(unlinkMock).toHaveBeenCalled())

    // Exactly one unlink — the merged change on its session, with the right
    // generation `expect`. The open change and the issue are left alone.
    expect(unlinkMock).toHaveBeenCalledTimes(1)
    expect(unlinkMock).toHaveBeenCalledWith('s-merged', MERGED_ID, 's-merged|2026-01-01T00:00:00Z|')

    const removedIdentities = unlinkMock.mock.calls.map(c => c[1])
    expect(removedIdentities).not.toContain(OPEN_ID) // live-open change kept
    expect(removedIdentities).not.toContain(ISSUE_ID) // issue chip never removed

    // The live status endpoint was asked about the two CHANGE urls only (the
    // issue is excluded before the status call).
    expect(statusMock).toHaveBeenCalledTimes(1)
    const askedUrls = statusMock.mock.calls[0][0]
    expect(new Set(askedUrls)).toEqual(new Set([MERGED_URL, OPEN_URL]))
  })

  it('removes nothing when the live status says nothing is merged', async () => {
    statusMock.mockResolvedValue({ statuses: { [MERGED_URL]: { state: 'open' }, [OPEN_URL]: { state: 'open' } } })
    renderAction()
    fireEvent.click(item())
    fireEvent.click(item())
    await waitFor(() => expect(statusMock).toHaveBeenCalled())
    await waitFor(() => expect(item()).not.toHaveAttribute('data-confirming'))
    expect(unlinkMock).not.toHaveBeenCalled()
  })

  it('batches status requests so a merged change past the per-request cap is still removed', async () => {
    // 65 sessions, each with a distinct change chip: the first 64 open, the
    // 65th merged. The server reads only the first 64 urls per request, so a
    // single request would never answer (or remove) the 65th. The action must
    // chunk the urls; assert the 65th merged chip IS unlinked.
    const many: ChatSlot[] = Array.from({ length: 65 }, (_, i) => ({
      key: `s${i}`, title: `s${i}`, messages: 1, running: false, row_identity: `s${i}`, created: '2026-01-01T00:00:00Z',
      source_links: [{ provider: 'github', number: i, label: `#${i}`, url: `https://example.com/pull/${i}`, kind: 'change', identity: `id-${i}` }], source_links_total: 1,
    })) as unknown as ChatSlot[]
    const MERGED = 'https://example.com/pull/64'
    // Echo back only the urls the caller actually sent in each chunk, and mark
    // #64 merged. A caller that sends everything in one request gets a reply
    // that (like the real server) drops everything past the 64th — so #64,
    // sent as the 65th url, would be absent and never removed.
    statusMock.mockReset().mockImplementation(async (urls: string[]) => {
      const capped = urls.slice(0, 64)
      const statuses: Record<string, { state: 'open' | 'merged' }> = {}
      for (const u of capped) statuses[u] = u === MERGED ? { state: 'merged' } : { state: 'open' }
      return { statuses }
    })
    renderAction(many)
    fireEvent.click(item())
    fireEvent.click(item())
    await waitFor(() => expect(unlinkMock).toHaveBeenCalled())
    expect(unlinkMock).toHaveBeenCalledTimes(1)
    expect(unlinkMock).toHaveBeenCalledWith('s64', 'id-64', 's64|2026-01-01T00:00:00Z|')
    // More than one status request was made (the urls were chunked), and every
    // request carried at most 64 urls.
    expect(statusMock.mock.calls.length).toBeGreaterThan(1)
    for (const call of statusMock.mock.calls) expect(call[0].length).toBeLessThanOrEqual(64)
  })

  it('skips the unlink when the session generation changed after the status fetch', async () => {
    // Merged per live status, but the session behind the key is recreated
    // (new created stamp) between the status call and the removal: the
    // generation no longer matches the one captured, so the unlink must be
    // skipped rather than landing on the replacement session.
    const { store } = renderAction([
      { key: 's-merged', title: 'merged', messages: 1, running: false, row_identity: 's-merged', created: '2026-01-01T00:00:00Z',
        source_links: [{ provider: 'github', number: 100, label: '#100', url: MERGED_URL, state: 'merged', kind: 'change', identity: MERGED_ID }], source_links_total: 1 },
    ] as unknown as ChatSlot[])
    statusMock.mockReset().mockImplementation(async () => {
      // Recreate the session (fresh `created`) while the status request is in
      // flight — its generation triple now differs from the captured `expect`.
      store.dispatch({ type: 'dashboard/updateSlot', payload: { key: 's-merged', created: '2026-09-09T00:00:00Z' } })
      return { statuses: { [MERGED_URL]: { state: 'merged' } } }
    })
    fireEvent.click(item())
    fireEvent.click(item())
    await waitFor(() => expect(statusMock).toHaveBeenCalled())
    await waitFor(() => expect(item()).not.toHaveAttribute('data-confirming'))
    expect(unlinkMock).not.toHaveBeenCalled()
  })

  it('surfaces a stopped sweep through ErrorNotice, not a bare span', async () => {
    statusMock.mockReset().mockRejectedValue(new Error('gateway offline'))
    renderAction()
    fireEvent.click(item())
    fireEvent.click(item())
    await waitFor(() => expect(screen.getByTestId('remove-merged-cr-chips-error')).toBeInTheDocument())
    expect(unlinkMock).not.toHaveBeenCalled()
  })

  it('names an owner-only refusal (403) rather than a generic stop', async () => {
    // The provider-status endpoint is owner-only; a non-owner dashboard user
    // still sees the item (slots carry their chips) but the status request 403s.
    statusMock.mockReset().mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403 }))
    renderAction()
    fireEvent.click(item())
    fireEvent.click(item())
    await waitFor(() => expect(screen.getByTestId('remove-merged-cr-chips-error')).toBeInTheDocument())
    // The notice names it as owner-only, and nothing was unlinked.
    expect(screen.getByTestId('remove-merged-cr-chips-error')).toHaveTextContent(/only the owner/i)
    expect(unlinkMock).not.toHaveBeenCalled()
  })

  it('a session-expiry 403 is a generic stop, not the owner-only refusal', async () => {
    // The owner's own cookie can lapse mid-session: the gateway then answers
    // ANY request with a 403 carrying `X-Auth-Required: true`, surfaced on the
    // ApiError as `authRequired`. That reaches the owner, and a re-auth clears
    // it — so it must NOT be mislabelled "only the owner can remove", which
    // tells the actual owner they lack a permission they hold. It falls through
    // to the generic "stopped" notice whose hand-off carries the real reason.
    statusMock
      .mockReset()
      .mockRejectedValue(Object.assign(new Error('forbidden'), { status: 403, authRequired: true }))
    renderAction()
    fireEvent.click(item())
    fireEvent.click(item())
    await waitFor(() => expect(screen.getByTestId('remove-merged-cr-chips-error')).toBeInTheDocument())
    expect(screen.getByTestId('remove-merged-cr-chips-error')).not.toHaveTextContent(/only the owner/i)
    expect(unlinkMock).not.toHaveBeenCalled()
  })

  it('self-hides when no session carries an unlinkable change chip', () => {
    renderAction([
      { key: 's-issue', title: 'issue only', messages: 1, running: false, row_identity: 's-issue', created: '2026-01-03T00:00:00Z',
        source_links: [{ provider: 'github', number: 300, label: '#300', url: ISSUE_URL, kind: 'issue', identity: ISSUE_ID }], source_links_total: 1 },
    ] as unknown as ChatSlot[])
    expect(screen.queryByTestId('remove-merged-cr-chips')).toBeNull()
  })
})
