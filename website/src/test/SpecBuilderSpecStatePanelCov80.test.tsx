// SpecStatePanel — the structured state card under the docs pane. Decisions are
// clickable until answered; a clicked decision collapses to its chosen option
// immediately (marked "sending…" until the agent writes the real answer back),
// answering posts a chat message through the parent's mutation, and the CONTEXT
// table only lists the rows the payload actually has.
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import type { SpecDetail } from '../apps/spec-builder/api'
import SpecStatePanel from '../apps/spec-builder/components/SpecStatePanel'

function detail(over: Partial<SpecDetail> = {}): SpecDetail {
  return { name: 'zz-spec', ...over }
}

describe('SpecStatePanel', () => {
  it('renders nothing when there is no state and no context', () => {
    const { container } = render(<SpecStatePanel detail={detail()} sendMessage={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('renders nothing for a missing detail', () => {
    const { container } = render(<SpecStatePanel detail={null} sendMessage={vi.fn()} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('lists pending options and marks the recommended one', () => {
    render(
      <SpecStatePanel
        detail={detail({
          state: {
            decisions: [{
              id: 'd1', title: 'zz-decision', options: ['zz-opt-a', 'zz-opt-b'], recommended: 'zz-opt-b',
            }],
          },
        })}
        sendMessage={vi.fn()}
      />,
    )
    expect(screen.getByText('zz-decision')).toBeInTheDocument()
    expect(screen.getByText('pending')).toBeInTheDocument()
    expect(screen.getByText('zz-opt-a')).toBeInTheDocument()
    expect(screen.getByText('RECOMMENDED')).toBeInTheDocument()
  })

  it('shows an answered decision as text instead of options', () => {
    render(
      <SpecStatePanel
        detail={detail({
          state: { decisions: [{ id: 'd1', title: 'zz-decision', options: ['zz-opt-a'], answer: 'zz-opt-a' }] },
        })}
        sendMessage={vi.fn()}
      />,
    )
    expect(screen.getByText('answered')).toBeInTheDocument()
    expect(screen.getByText('→ zz-opt-a')).toBeInTheDocument()
    expect(screen.queryByRole('group')).not.toBeInTheDocument()
  })

  it('tolerates a decision with no options list', () => {
    render(
      <SpecStatePanel
        detail={detail({ state: { decisions: [{ id: 'd1', title: 'zz-decision' }] } })}
        sendMessage={vi.fn()}
      />,
    )
    expect(screen.getByRole('group')).toBeEmptyDOMElement()
  })

  it('posts the chosen option through the parent mutation', async () => {
    const sendMessage = vi.fn().mockResolvedValue(undefined)
    render(
      <SpecStatePanel
        detail={detail({
          state: { decisions: [{ id: 'd1', title: 'zz-decision', options: ['zz-opt-a'] }] },
        })}
        sendMessage={sendMessage}
      />,
    )
    fireEvent.click(screen.getByText('zz-opt-a'))
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1))
    const sent = String(sendMessage.mock.calls[0][0])
    expect(sent).toContain('zz-decision')
    expect(sent.endsWith(': zz-opt-a')).toBe(true)
  })

  it('collapses the clicked decision to "sending…" and blocks the others while in flight', async () => {
    let release: (() => void) | undefined
    const sendMessage = vi.fn(() => new Promise<void>(res => { release = () => res() }))
    render(
      <SpecStatePanel
        detail={detail({
          state: {
            decisions: [
              { id: 'd1', title: 'zz-one', options: ['zz-opt-a', 'zz-opt-b'] },
              { id: 'd2', title: 'zz-two', options: ['zz-opt-c'] },
            ],
          },
        })}
        sendMessage={sendMessage}
      />,
    )
    fireEvent.click(screen.getByText('zz-opt-a'))
    // The clicked decision collapses to its chosen option right away — the
    // sibling option is gone, and the badge shows the send is under way.
    expect(screen.queryByText('zz-opt-b')).not.toBeInTheDocument()
    expect(screen.getByText('→ zz-opt-a')).toBeInTheDocument()
    expect(screen.getByText('sending…')).toBeInTheDocument()
    // The unrelated decision is dimmed and inert while another one is answering.
    expect(screen.getByText('zz-opt-c').closest('[aria-label]')).toHaveStyle({ opacity: '0.5' })
    fireEvent.click(screen.getByText('zz-opt-c'))
    expect(sendMessage).toHaveBeenCalledTimes(1)
    release?.()
    await waitFor(() => expect(screen.getByText('zz-opt-c').closest('[aria-label]')).toHaveStyle({ opacity: '1' }))
    fireEvent.click(screen.getByText('zz-opt-c'))
    expect(sendMessage).toHaveBeenCalledTimes(2)
  })

  it('a send failure reopens the decision so it can be answered again', async () => {
    const sendMessage = vi.fn().mockRejectedValue(new Error('zz-send-failed'))
    render(
      <SpecStatePanel
        detail={detail({ state: { decisions: [{ id: 'd1', title: 'zz-one', options: ['zz-opt-a'] }] } })}
        sendMessage={sendMessage}
      />,
    )
    fireEvent.click(screen.getByText('zz-opt-a'))
    // The instruction never reached the agent, so the local mark is dropped and
    // the option comes back clickable instead of sitting on a false "sending…".
    await waitFor(() => expect(screen.getByText('zz-opt-a')).toBeInTheDocument())
    expect(screen.queryByText('sending…')).not.toBeInTheDocument()
    expect(screen.getByText('pending')).toBeInTheDocument()
    fireEvent.click(screen.getByText('zz-opt-a'))
    expect(sendMessage).toHaveBeenCalledTimes(2)
  })

  it('the server answer wins over the local sending mark', async () => {
    const sendMessage = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(
      <SpecStatePanel
        detail={detail({ state: { decisions: [{ id: 'd1', title: 'zz-one', options: ['zz-opt-a', 'zz-opt-b'] }] } })}
        sendMessage={sendMessage}
      />,
    )
    fireEvent.click(screen.getByText('zz-opt-a'))
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1))
    rerender(
      <SpecStatePanel
        detail={detail({ state: { decisions: [{ id: 'd1', title: 'zz-one', options: ['zz-opt-a', 'zz-opt-b'], answer: 'zz-opt-b' }] } })}
        sendMessage={sendMessage}
      />,
    )
    expect(screen.getByText('answered')).toBeInTheDocument()
    expect(screen.getByText('→ zz-opt-b')).toBeInTheDocument()
  })

  it('an id that comes back carrying a different question is open again', async () => {
    // The agent rewrites .spec-state.json wholesale, so the same id can return
    // with a new question; the local mark is keyed on id+title and must not
    // claim the new question was already answered.
    const sendMessage = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(
      <SpecStatePanel
        detail={detail({ state: { decisions: [{ id: 'd1', title: 'zz-one', options: ['zz-opt-a'] }] } })}
        sendMessage={sendMessage}
      />,
    )
    fireEvent.click(screen.getByText('zz-opt-a'))
    await waitFor(() => expect(sendMessage).toHaveBeenCalledTimes(1))
    rerender(
      <SpecStatePanel
        detail={detail({ state: { decisions: [{ id: 'd1', title: 'zz-other-question', options: ['zz-opt-z'] }] } })}
        sendMessage={sendMessage}
      />,
    )
    expect(screen.getByText('pending')).toBeInTheDocument()
    expect(screen.getByText('zz-opt-z')).toBeInTheDocument()
  })

  it('surfaces a blocking note on its own', () => {
    render(<SpecStatePanel detail={detail({ state: { blocking: 'zz-blocked-on' } })} sendMessage={vi.fn()} />)
    expect(screen.getByText('BLOCKING')).toBeInTheDocument()
    expect(screen.getByText('zz-blocked-on')).toBeInTheDocument()
  })

  it('lists worktree and template rows when the payload carries them', () => {
    render(
      <SpecStatePanel
        detail={detail({
          state: { context: { template: 'zz-template' } },
          context: { worktree_branch: 'zz-branch', turns: 4, tool_calls: 7 },
        })}
        sendMessage={vi.fn()}
      />,
    )
    expect(screen.getByText('zz-branch')).toBeInTheDocument()
    expect(screen.getByText('zz-template')).toBeInTheDocument()
    expect(screen.getByText('4')).toBeInTheDocument()
    expect(screen.getByText('7')).toBeInTheDocument()
  })

  it('defaults the counters to zero and omits the optional rows', () => {
    render(<SpecStatePanel detail={detail({ context: {} })} sendMessage={vi.fn()} />)
    expect(screen.getByText('CONTEXT')).toBeInTheDocument()
    expect(screen.getAllByText('0')).toHaveLength(2)
    expect(screen.queryByText('zz-branch')).not.toBeInTheDocument()
  })
})
