import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* A crewmate's thread reply, and the parent it hangs off, are drawn by the real
 * markdown renderer with the blocked-link records the thread read carries, so a
 * flagged link there draws as the Blocked link chip and never as a live link. */

const mockDetail = vi.fn()

vi.mock('../../api/threads', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/threads')>()
  return { ...actual, threadsApi: { summary: vi.fn(), detail: (...args: unknown[]) => mockDetail(...args), reply: vi.fn() } }
})

import ThreadPanel from './ThreadPanel'

const URL = 'https://collect.example.com/report?q=' + 'x'.repeat(260)
const RECORD = { url: URL, domain: 'collect.example.com', rule: 'exfil_query_length', query_chars: 262, withheld: null }
const OTHER = 'https://docs.example.com/guide'

let qc: QueryClient
beforeEach(() => {
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

const renderPanel = () =>
  render(
    <QueryClientProvider client={qc}>
      <ThreadPanel slot="member-radar" mid="m-1" crewmateName="Radar" onClose={vi.fn()} />
    </QueryClientProvider>,
  )

describe('ThreadPanel draws a blocked link as its chip', () => {
  it('in a crewmate reply and in the parent, and leaves other links live', async () => {
    mockDetail.mockResolvedValue({
      parent: { mid: 'm-1', role: 'assistant', content: `Report: ${URL}`, ts: '2026-09-22T07:02:00Z', link_blocks: [RECORD] },
      replies: [
        { id: 'r1', role: 'user', content: 'where?', ts: '2026-09-22T07:41:00Z' },
        { id: 'r2', role: 'assistant', content: `Here: ${URL} and ${OTHER}`, ts: '2026-09-22T07:41:30Z', link_blocks: [RECORD] },
      ],
      in_flight: false,
    })
    const { container } = renderPanel()
    await screen.findByTestId('thread-parent')
    expect(await screen.findAllByTestId('blocked-link-chip')).toHaveLength(2)
    expect(container.querySelector(`a[href="${URL}"]`)).toBeNull()
    expect(container.querySelector(`a[href="${OTHER}"]`)).toBeTruthy()
  })

  it("offers the Blocked link card's Allow, the thread being this gateway's own slot", async () => {
    mockDetail.mockResolvedValue({
      parent: { mid: 'm-1', role: 'assistant', content: 'Overnight triage.', ts: '2026-09-22T07:02:00Z' },
      replies: [{ id: 'r2', role: 'assistant', content: `Here: ${URL}`, ts: '2026-09-22T07:41:30Z', link_blocks: [RECORD] }],
      in_flight: false,
    })
    renderPanel()
    fireEvent.click(await screen.findByTestId('blocked-link-inspect'))
    expect(await screen.findByTestId('blocked-link-allow')).toBeInTheDocument()
  })

  it('a reply with no record renders its link live', async () => {
    mockDetail.mockResolvedValue({
      parent: { mid: 'm-1', role: 'assistant', content: 'Overnight triage.', ts: '2026-09-22T07:02:00Z' },
      replies: [{ id: 'r2', role: 'assistant', content: `Here: ${URL}`, ts: '2026-09-22T07:41:30Z' }],
      in_flight: false,
    })
    const { container } = renderPanel()
    await screen.findByTestId('thread-parent')
    await screen.findAllByTestId('thread-reply')
    expect(screen.queryByTestId('blocked-link-chip')).toBeNull()
    expect(container.querySelector(`a[href="${URL}"]`)).toBeTruthy()
  })
})
