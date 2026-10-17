/**
 * The dialog that opens from a clicked id on the recall strip.
 *
 * What is pinned here, each a way the popover could mislead:
 *
 *  - it resolves the id in the STORE the recall came from, by `{kind,id}` identity,
 *    never a free-text search — so a member (V2) recall's ids look up in their own
 *    store rather than always the default one.
 *  - the four states it can be in: loading, found (the memory's full text), not-found
 *    (an exact-id miss reads as "not found", never a fuzzy match), and error.
 *  - the error surface hands off to the agent and closes the dialog first, so the
 *    hand-off it navigates to is not left sitting behind this dialog.
 *  - Escape closes it, because it runs a query and a host mounts it only while open.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* ── Mock api/client BEFORE the component imports it ── */
const mockApi = vi.hoisted(() => ({
  memoryRecordsRefresh: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import MemoryPopover from '../pages/chat/MemoryPopover'
import { i18nT } from '../i18n/t'

const ID = 'episode-7d1e04a9b2c3'
const t = (k: string) => i18nT(`pages.chat.decisionStrip.${k}`)

/** One resolved episode row, as `memoryRecordsRefresh` returns it. */
const episode = (id: string, text: string) => ({
  entries: [{ kind: 'episode', id, key: null, value_json: null, text }],
  missing: [],
})

function renderPopover(props: Partial<React.ComponentProps<typeof MemoryPopover>> = {}) {
  const onClose = props.onClose ?? vi.fn()
  // Fresh client per render: no cross-test cache bleed. The component sets its own
  // per-query `retry`, so a settled-refusal status is what stops a retry below.
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  const utils = render(
    <QueryClientProvider client={qc}>
      <MemoryPopover
        recordId={'recordId' in props ? (props.recordId as string | null) : ID}
        store={props.store ?? 'default'}
        onClose={onClose}
      />
    </QueryClientProvider>,
  )
  return { ...utils, onClose }
}

beforeEach(() => {
  mockApi.memoryRecordsRefresh.mockReset()
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('MemoryPopover', () => {
  it('renders nothing and asks for nothing when no id is selected', () => {
    const { container } = renderPopover({ recordId: null })
    expect(container.querySelector('[role="dialog"]')).toBeNull()
    expect(mockApi.memoryRecordsRefresh).not.toHaveBeenCalled()
  })

  it('looks the id up by identity in the store it was given', async () => {
    mockApi.memoryRecordsRefresh.mockResolvedValue(episode(ID, 'the remembered text'))
    renderPopover({ store: 'member-bob' })
    await waitFor(() =>
      expect(mockApi.memoryRecordsRefresh).toHaveBeenCalledWith('member-bob', [
        { kind: 'episode', id: ID },
      ]),
    )
  })

  it('shows the loading line while the lookup is in flight', () => {
    // A promise that never settles keeps the query pending.
    mockApi.memoryRecordsRefresh.mockReturnValue(new Promise(() => {}))
    renderPopover()
    expect(screen.getByText(t('memory_detail_loading'))).toBeInTheDocument()
    // The id itself is shown in full even while loading — it is a store handle.
    expect(screen.getByText(ID)).toBeInTheDocument()
  })

  it('shows the memory text and the full id on an exact match', async () => {
    mockApi.memoryRecordsRefresh.mockResolvedValue(episode(ID, 'the remembered text'))
    renderPopover()
    expect(await screen.findByText('the remembered text')).toBeInTheDocument()
    expect(screen.getByText(ID)).toBeInTheDocument()
    // Not the not-found line, which shares the dialog body.
    expect(screen.queryByText(t('memory_detail_not_found'))).toBeNull()
  })

  it('names an empty memory in words rather than as a blank', async () => {
    mockApi.memoryRecordsRefresh.mockResolvedValue(episode(ID, ''))
    renderPopover()
    expect(await screen.findByText(t('memory_detail_empty'))).toBeInTheDocument()
  })

  it('reads a near miss as not found rather than showing another record', async () => {
    // `refresh` never fuzzy matches: a row whose id is not the one asked for is a
    // miss, and the defensive identity check keeps a mismatched row off the dialog.
    mockApi.memoryRecordsRefresh.mockResolvedValue({
      entries: [{ kind: 'episode', id: 'a-different-id', key: null, value_json: null, text: 'wrong' }],
      missing: [{ kind: 'episode', id: ID }],
    })
    renderPopover()
    expect(await screen.findByText(t('memory_detail_not_found'))).toBeInTheDocument()
    expect(screen.queryByText('wrong')).toBeNull()
  })

  it('surfaces a failed lookup through the shared error surface', async () => {
    // status 404 is a settled refusal, so `memoryQueryRetry` does not retry it and
    // the error resolves on the first failure.
    mockApi.memoryRecordsRefresh.mockRejectedValue(
      Object.assign(new Error('the store fell over'), { status: 404 }),
    )
    renderPopover()
    expect(await screen.findByText('the store fell over')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
    // A failure shows neither the text nor the not-found line.
    expect(screen.queryByText(t('memory_detail_not_found'))).toBeNull()
  })

  it('closes the dialog when the failure is handed to the agent', async () => {
    mockApi.memoryRecordsRefresh.mockRejectedValue(
      Object.assign(new Error('the store fell over'), { status: 404 }),
    )
    const { onClose } = renderPopover()
    const handoff = await screen.findByRole('button', { name: /ask the agent/i })
    fireEvent.click(handoff)
    // `onHandoff={onClose}`: the hand-off navigates to a chat this dialog sits over,
    // so it closes first rather than reading as a dead button behind the dialog.
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('closes on Escape', async () => {
    mockApi.memoryRecordsRefresh.mockResolvedValue(episode(ID, 'the remembered text'))
    const { onClose } = renderPopover()
    const dialog = await screen.findByRole('dialog')
    // Radix dismisses from a capture-phase document listener.
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(onClose).toHaveBeenCalledTimes(1)
  })
})
