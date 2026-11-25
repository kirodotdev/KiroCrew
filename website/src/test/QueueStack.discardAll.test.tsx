/**
 * Discard all on the collapsed queue stack. With two or more queued messages
 * the front card offers one button that, after a confirm, hands every shown
 * card's id to `onCancelAll`. The host's per-entry cancel does the rest
 * (useQueuedMessageActions.test.tsx pins that half), and the cancels that did
 * not go through come back to be named above the stack.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import QueueStack from '../components/QueueStack'
import type { ChatMessage } from '../types'

function queued(queueId: string, content: string): ChatMessage {
  return { role: 'queued', content, cls: 'msg msg-queued', ts: '', meta: { queueId } } as ChatMessage
}

/** A card whose user row is already in the conversation (`meta.written`). */
function written(queueId: string, content: string): ChatMessage {
  return { role: 'queued', content, cls: 'msg msg-queued', ts: '', meta: { queueId, written: true } } as ChatMessage
}

const THREE = [queued('q1', 'run the tests'), queued('q2', 'then deploy'), queued('q3', 'then tag')]

/** The stack's own expand/collapse box. */
const stackBox = () => screen.getByRole('button', { expanded: false })

describe('QueueStack Discard all', () => {
  it('is offered on a collapsed stack of two or more, and only when the host can cancel them', () => {
    const { rerender } = render(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={async () => []} />)
    expect(screen.getByTestId('queue-discard-all')).toHaveAccessibleName('Discard all…')
    rerender(<QueueStack messages={THREE.slice(0, 1)} onCancel={() => {}} onCancelAll={async () => []} />)
    expect(screen.queryByTestId('queue-discard-all')).not.toBeInTheDocument()
    rerender(<QueueStack messages={THREE} onCancel={() => {}} />)
    expect(screen.queryByTestId('queue-discard-all')).not.toBeInTheDocument()
  })

  it('is not on the expanded stack, where each card has its own cancel', () => {
    render(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={async () => []} />)
    fireEvent.click(stackBox())
    expect(screen.queryByTestId('queue-discard-all')).not.toBeInTheDocument()
    expect(screen.getAllByLabelText('Cancel queued message')).toHaveLength(3)
  })

  it('asks first, and a cancelled dialog discards nothing and leaves the stack collapsed', async () => {
    const user = userEvent.setup()
    const onCancelAll = vi.fn(async () => [])
    render(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={onCancelAll} />)
    await user.click(screen.getByTestId('queue-discard-all'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Discard 3 queued messages?')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(onCancelAll).not.toHaveBeenCalled()
    expect(stackBox()).toHaveAttribute('aria-expanded', 'false')
  })

  it('on confirm hands over every card in run order', async () => {
    const user = userEvent.setup()
    const onCancelAll = vi.fn(async () => [])
    render(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={onCancelAll} />)
    await user.click(screen.getByTestId('queue-discard-all'))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard all' }))
    expect(onCancelAll).toHaveBeenCalledTimes(1)
    expect(onCancelAll).toHaveBeenCalledWith(['q1', 'q2', 'q3'])
    expect(stackBox()).toHaveAttribute('aria-expanded', 'false')
  })

  it('leaves out a card whose action is in flight, and is disabled when every card is', async () => {
    const user = userEvent.setup()
    const onCancelAll = vi.fn(async () => [])
    const { rerender } = render(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={onCancelAll} pendingIds={new Set(['q2'])} />)
    await user.click(screen.getByTestId('queue-discard-all'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Discard 2 queued messages?')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Discard all' }))
    expect(onCancelAll).toHaveBeenCalledWith(['q1', 'q3'])
    rerender(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={onCancelAll} pendingIds={new Set(['q1', 'q2', 'q3'])} />)
    expect(screen.getByTestId('queue-discard-all')).toBeDisabled()
  })

  it('does not discard a message queued while the dialog was open', async () => {
    const user = userEvent.setup()
    const onCancelAll = vi.fn(async () => [])
    const two = THREE.slice(0, 2)
    const { rerender } = render(<QueueStack messages={two} onCancel={() => {}} onCancelAll={onCancelAll} />)
    await user.click(screen.getByTestId('queue-discard-all'))
    const dialog = await screen.findByRole('dialog')
    rerender(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={onCancelAll} />)
    await user.click(within(dialog).getByRole('button', { name: 'Discard all' }))
    expect(onCancelAll).toHaveBeenCalledWith(['q1', 'q2'])
  })

  it('leaves out a card already in the conversation, which the server will not cancel', async () => {
    const user = userEvent.setup()
    const onCancelAll = vi.fn(async () => [])
    const mixed = [written('q1', 'run the tests'), THREE[1], written('q3', 'then tag'), queued('q4', 'then ship')]
    render(<QueueStack messages={mixed} onCancel={() => {}} onCancelAll={onCancelAll} />)
    await user.click(screen.getByTestId('queue-discard-all'))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText('Discard 2 queued messages?')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Discard all' }))
    expect(onCancelAll).toHaveBeenCalledWith(['q2', 'q4'])
  })

  it('is disabled when every card is already in the conversation', () => {
    const allWritten = [written('q1', 'run the tests'), written('q2', 'then deploy')]
    render(<QueueStack messages={allWritten} onCancel={() => {}} onCancelAll={async () => []} />)
    expect(screen.getByTestId('queue-discard-all')).toBeDisabled()
  })

  it('shows a visible Discard all… label, so its reach is not only in the tooltip', () => {
    render(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={async () => []} />)
    const button = screen.getByTestId('queue-discard-all')
    // The ellipsis says a dialog comes next; the dialog's own confirm is the
    // bare "Discard all".
    expect(button).toHaveTextContent('Discard all…')
    // The visible words are the name (voice control can say them), and the
    // longer tooltip is the description.
    expect(button).toHaveAccessibleName('Discard all…')
    expect(button).toHaveAccessibleDescription('Discard all queued messages')
  })

  it('drops the label to screen-reader-only on a narrow card, keeping the name', () => {
    // jsdom evaluates no container query; this pins the class that does it.
    render(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={async () => []} />)
    expect(screen.getByTestId('queue-discard-all-label').className).toContain('@max-[360px]/queuecard:sr-only')
    expect(screen.getByTestId('queue-discard-all')).toHaveAccessibleName('Discard all…')
  })

  it('names the cancels that did not go through, above the stack, until dismissed', async () => {
    const user = userEvent.setup()
    const onCancelAll = vi.fn(async () => [{ queueId: 'q2', reason: new Error('HTTP 500: gateway busy') }])
    render(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={onCancelAll} />)
    expect(screen.queryByTestId('queue-discard-all-error')).not.toBeInTheDocument()
    await user.click(screen.getByTestId('queue-discard-all'))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard all' }))
    const notice = await screen.findByTestId('queue-discard-all-error')
    expect(notice).toHaveTextContent('Could not discard 1 queued message')
    expect(notice).toHaveTextContent('HTTP 500: gateway busy')
    await user.click(within(notice).getByRole('button', { name: 'Dismiss' }))
    expect(screen.queryByTestId('queue-discard-all-error')).not.toBeInTheDocument()
    expect(stackBox()).toHaveAttribute('aria-expanded', 'false')
  })

  it('drops the error line once the card it names has left the stack', async () => {
    const user = userEvent.setup()
    const onCancelAll = vi.fn(async () => [{ queueId: 'q2', reason: new Error('lost') }])
    const { rerender } = render(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={onCancelAll} />)
    await user.click(screen.getByTestId('queue-discard-all'))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Discard all' }))
    expect(await screen.findByTestId('queue-discard-all-error')).toHaveTextContent('Could not discard 1 queued message')
    // The gateway did act on the lost request, or the message ran: its card is gone.
    rerender(<QueueStack messages={[THREE[0], THREE[2]]} onCancel={() => {}} onCancelAll={onCancelAll} />)
    expect(screen.queryByTestId('queue-discard-all-error')).not.toBeInTheDocument()
  })

  it('takes Enter for itself, so the key does not toggle the stack open', () => {
    render(<QueueStack messages={THREE} onCancel={() => {}} onCancelAll={async () => []} />)
    fireEvent.keyDown(screen.getByTestId('queue-discard-all'), { key: 'Enter' })
    expect(stackBox()).toHaveAttribute('aria-expanded', 'false')
  })
})
