// SpecStatePanel — the structured state card under the docs pane. Decisions are
// clickable until answered, answering posts a chat message through the parent's
// mutation, and the CONTEXT table only lists the rows the payload actually has.
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

  it('blocks a second answer while one is in flight, then re-enables', async () => {
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
    // The clicked decision settles immediately on the local "sent" mark: its
    // options leave the DOM and the card shows the chosen answer as text, so
    // the sibling option cannot be double-clicked at all.
    expect(screen.queryByText('zz-opt-b')).not.toBeInTheDocument()
    expect(screen.getByText('→ zz-opt-a')).toBeInTheDocument()
    // The unrelated decision stays rendered but dimmed and inert while the
    // answer is in flight.
    fireEvent.click(screen.getByText('zz-opt-c'))
    expect(sendMessage).toHaveBeenCalledTimes(1)
    expect(screen.getByText('zz-opt-c').closest('[aria-label]')).toHaveStyle({ opacity: '0.5' })
    release?.()
    await waitFor(() => expect(screen.getByText('zz-opt-c').closest('[aria-label]')).toHaveStyle({ opacity: '1' }))
    fireEvent.click(screen.getByText('zz-opt-c'))
    expect(sendMessage).toHaveBeenCalledTimes(2)
  })

  it('swallows a send failure and re-enables the options', async () => {
    const sendMessage = vi.fn().mockRejectedValue(new Error('zz-send-failed'))
    render(
      <SpecStatePanel
        detail={detail({ state: { decisions: [{ id: 'd1', title: 'zz-one', options: ['zz-opt-a'] }] } })}
        sendMessage={sendMessage}
      />,
    )
    fireEvent.click(screen.getByText('zz-opt-a'))
    await waitFor(() => expect(screen.getByText('zz-opt-a').closest('[aria-label]')).toHaveStyle({ opacity: '1' }))
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
