import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

/* A Bot thread reply, and the parent it hangs off, draw a removed credential as
 * the lock tag from the credential records the thread read carries, so
 * the raw placeholder text never shows. A reply with no record keeps the text.
 * An Allow or Undo on this slot reads the thread again. */

const mockDetail = vi.fn()

vi.mock('../../api/threads', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../../api/threads')>()
  return { ...actual, threadsApi: { summary: vi.fn(), detail: (...args: unknown[]) => mockDetail(...args), reply: vi.fn() } }
})

import ThreadPanel from './ThreadPanel'
import { api } from '../../api/client'

const SLOT = 'member-radar'
const CRED_PH = '[REDACTED: credential]'
const TEXT = `Use the token ${CRED_PH} for the upload.`
const record = (n: string) => ({
  ordinal: 0, rule: 'github_token', label: '', source: null, view_command: null, profile_command: null,
  reveal_id: 'rv_' + n.repeat(22),
})

let qc: QueryClient
afterEach(() => vi.restoreAllMocks())

beforeEach(() => {
  mockDetail.mockReset()
  qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
})

const renderPanel = () =>
  render(
    <QueryClientProvider client={qc}>
      <ThreadPanel slot={SLOT} mid="m-1" crewmateName="Radar" onClose={vi.fn()} />
    </QueryClientProvider>,
  )

describe('ThreadPanel draws a removed credential as its lock tag', () => {
  it('in a Bot reply and in the parent, never as the placeholder text', async () => {
    mockDetail.mockResolvedValue({
      parent: { mid: 'm-1', role: 'assistant', content: TEXT, ts: '2026-11-23T07:02:00Z', redactions: [record('a')] },
      replies: [
        { id: 'r1', role: 'user', content: 'which token?', ts: '2026-11-23T07:41:00Z' },
        { id: 'r2', role: 'assistant', content: TEXT, ts: '2026-11-23T07:41:30Z', redactions: [record('b')] },
      ],
      in_flight: false,
    })
    renderPanel()
    await screen.findByTestId('thread-parent')
    expect(await screen.findAllByTestId('credential-tag')).toHaveLength(2)
    expect(document.body.textContent).not.toContain(CRED_PH)
  })

  it("Show asks the gateway for the thread's own slot and the reply's handle", async () => {
    // Placeholder-only: the value the fake gateway answers is not a token shape.
    const spy = vi.spyOn(api, 'revealRedactedValue').mockResolvedValue({ value: 'shown-value' })
    const reply = record('c')
    mockDetail.mockResolvedValue({
      parent: { mid: 'm-1', role: 'assistant', content: 'Overnight triage.', ts: '2026-11-23T07:02:00Z' },
      replies: [{ id: 'r2', role: 'assistant', content: TEXT, ts: '2026-11-23T07:41:30Z', redactions: [reply] }],
      in_flight: false,
    })
    renderPanel()
    fireEvent.click(await screen.findByTestId('credential-tag'))
    fireEvent.click(await screen.findByTestId('reveal-show'))
    await waitFor(() => expect(screen.getByTestId('reveal-value').textContent).toBe('shown-value'))
    expect(spy).toHaveBeenCalledWith(SLOT, reply.reveal_id)
  })

  it('a reply with no record keeps the placeholder text', async () => {
    mockDetail.mockResolvedValue({
      parent: { mid: 'm-1', role: 'assistant', content: 'Overnight triage.', ts: '2026-11-23T07:02:00Z' },
      replies: [{ id: 'r2', role: 'assistant', content: TEXT, ts: '2026-11-23T07:41:30Z' }],
      in_flight: false,
    })
    renderPanel()
    expect(await screen.findByText(CRED_PH, { exact: false })).toBeInTheDocument()
    expect(screen.queryByTestId('credential-tag')).toBeNull()
  })

  it('reads the thread again after an Allow or Undo on its slot only', async () => {
    mockDetail.mockResolvedValue({
      parent: { mid: 'm-1', role: 'assistant', content: 'Overnight triage.', ts: '2026-11-23T07:02:00Z' },
      replies: [],
      in_flight: false,
    })
    renderPanel()
    await screen.findByTestId('thread-parent')
    expect(mockDetail).toHaveBeenCalledTimes(1)
    await act(async () => { window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: { slot: 'other' } })) })
    expect(mockDetail).toHaveBeenCalledTimes(1)
    act(() => { window.dispatchEvent(new CustomEvent('mc:redaction-hosts-changed', { detail: { slot: SLOT } })) })
    await waitFor(() => expect(mockDetail).toHaveBeenCalledTimes(2))
    expect(mockDetail).toHaveBeenLastCalledWith(SLOT, 'm-1')
  })
})
