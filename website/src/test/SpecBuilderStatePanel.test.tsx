// SpecStatePanel — the decision tray below the documents. Covers the optimistic
// "sending…" mark that closes the window between a click and the agent rewriting
// .spec-state.json (up to a minute), its identity pinning, and rollback on a
// failed send.
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

import SpecStatePanel from '../apps/spec-builder/components/SpecStatePanel'
import type { SpecDetail } from '../apps/spec-builder/api'

const detailWith = (decisions: unknown[], extra: Record<string, unknown> = {}): SpecDetail => ({
  name: 'checkout',
  phase: 'requirements',
  status: 'planning',
  running: false,
  working_dir: '/proj/checkout',
  spec_dir: '/proj/checkout/.kiro/specs/checkout',
  slot_key: 'spec-builder-checkout-1',
  files: { 'requirements.md': '# r' },
  state: { decisions, ...extra },
  context: { turns: 1, tool_calls: 17 },
} as unknown as SpecDetail)

const GATE = [{ id: 'gate', title: 'Gate posture', options: ['Refuse by default', 'Warn only'], recommended: 'Refuse by default' }]

beforeEach(() => { vi.useFakeTimers({ shouldAdvanceTime: true }) })
afterEach(() => { vi.clearAllTimers(); vi.useRealTimers(); vi.restoreAllMocks() })

describe('SpecStatePanel decisions', () => {
  it('renders nothing when there is no structured state to show', () => {
    const { container } = render(<SpecStatePanel detail={null} answerDecision={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('marks the chosen option as sending until the agent records the answer', async () => {
    let release: (() => void) | undefined
    const answerDecision = vi.fn().mockImplementation(() => new Promise((res) => { release = () => res(undefined) }))
    const { rerender } = render(<SpecStatePanel detail={detailWith(GATE)} answerDecision={answerDecision} />)

    expect(screen.getByText('pending')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'How should “Gate posture” be answered?Refuse by default (recommended)' }))

    // The instruction still carries the title and the option verbatim, and now also the
    // decision id and the bare option: the backend records those two and refuses a
    // second answer for the same id.
    expect(answerDecision).toHaveBeenCalledWith(
      'gate',
      'Refuse by default',
      'Decision — Gate posture: Refuse by default',
    )
    // Feedback lands on the click, not on the agent's next write.
    await waitFor(() => expect(screen.getByText('sending…')).toBeInTheDocument())
    expect(screen.getByText('→ Refuse by default')).toBeInTheDocument()
    expect(screen.queryByText('pending')).not.toBeInTheDocument()

    release?.()
    // Still 'sending…' after the POST resolves: the answer is only real once the
    // agent has written it into .spec-state.json.
    await waitFor(() => expect(screen.getByText('sending…')).toBeInTheDocument())

    rerender(<SpecStatePanel detail={detailWith([{ ...GATE[0], answer: 'Refuse by default' }])} answerDecision={answerDecision} />)
    await waitFor(() => expect(screen.getByText('answered')).toBeInTheDocument())
    expect(screen.queryByText('sending…')).not.toBeInTheDocument()
  })

  it('reopens the question when the backend refuses before recording', async () => {
    // A refusal the backend NAMED is emitted before anything is recorded, so the options
    // come back. A codeless failure is ambiguous -- the write may have committed on the
    // way out -- and deliberately keeps the card locked instead; that half is covered in
    // SpecBuilderDecisionLock.test.tsx.
    const answerDecision = vi
      .fn()
      .mockRejectedValue(Object.assign(new Error('stale client'), { code: 'stale_client' }))
    render(<SpecStatePanel detail={detailWith(GATE)} answerDecision={answerDecision} />)

    fireEvent.click(screen.getByRole('button', { name: /Warn only/ }))
    // The instruction never reached the agent, so the options must come back.
    await waitFor(() => expect(screen.getByText('pending')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /Warn only/ })).toBeInTheDocument()
    expect(screen.queryByText('sending…')).not.toBeInTheDocument()
  })

  it('does not carry a sent mark onto a different question reusing the id', async () => {
    const answerDecision = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(<SpecStatePanel detail={detailWith(GATE)} answerDecision={answerDecision} />)
    fireEvent.click(screen.getByRole('button', { name: /Refuse by default/ }))
    await waitFor(() => expect(screen.getByText('sending…')).toBeInTheDocument())

    // The agent rewrites the whole file; the same id can come back as a NEW
    // question. Keyed on the id alone, the local mark would claim this one was
    // already answered and hide its options.
    rerender(
      <SpecStatePanel
        detail={detailWith([{ id: 'gate', title: 'Registry vs enum', options: ['Registry', 'Enum'], recommended: 'Registry' }])}
        answerDecision={answerDecision}
      />,
    )
    expect(screen.getByText('pending')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'How should “Registry vs enum” be answered?Registry (recommended)' })).toBeInTheDocument()
    expect(screen.queryByText('sending…')).not.toBeInTheDocument()
  })

  it('shows the blocking note and the context rows', () => {
    render(<SpecStatePanel detail={detailWith([], { blocking: 'awaiting your review' })} answerDecision={vi.fn()} />)
    expect(screen.getByText('BLOCKING')).toBeInTheDocument()
    expect(screen.getByText('awaiting your review')).toBeInTheDocument()
    expect(screen.getByText('CONTEXT')).toBeInTheDocument()
    expect(screen.getByText('17')).toBeInTheDocument()
  })
})
