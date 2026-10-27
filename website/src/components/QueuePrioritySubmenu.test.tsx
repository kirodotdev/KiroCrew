import { screen, fireEvent, render } from '@testing-library/react'
import QueuePrioritySubmenu from './QueuePrioritySubmenu'
import type { ErrorReport } from '../utils/errorReport'

/**
 * happy-dom cannot drive a real Radix submenu open (no PointerEvent), so the
 * menu primitives are stubbed down to plain elements. The radio group keeps
 * Radix's contract: it hands `onValueChange` the picked item's `value`, and
 * each item reports whether it is the group's current value.
 */
vi.mock('./ui/dropdown-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...(await import('../test/stubRadioMenu')).stubRadioMenu('DropdownMenu'),
}))
vi.mock('./ui/context-menu', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  ...(await import('../test/stubRadioMenu')).stubRadioMenu('ContextMenu'),
}))

const handoff = vi.hoisted(() => vi.fn((_args: unknown) => true))
vi.mock('./AskAgentButton', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  handoffErrorToAgent: handoff,
}))

const radio = (name: RegExp) => screen.getByRole('menuitemradio', { name })

describe('QueuePrioritySubmenu', () => {
  it.each(['dropdown', 'context'] as const)('lists High, Medium and Low in the %s family', variant => {
    render(<QueuePrioritySubmenu variant={variant} onPick={vi.fn()} />)
    expect(screen.getByText('Sub-agent priority')).toBeInTheDocument()
    expect(screen.getByTestId('queue-priority-caption')).toHaveTextContent(
      "Higher-priority chats' waiting sub-agents start first. Resets to Medium on gateway restart.",
    )
    expect(screen.getAllByRole('menuitemradio').map(el => el.textContent)).toEqual([
      'High', 'Medium (default)', 'Low',
    ])
  })

  it('marks medium as current when the chat carries no priority', () => {
    render(<QueuePrioritySubmenu variant="dropdown" onPick={vi.fn()} />)
    expect(radio(/Medium/)).toHaveAttribute('aria-checked', 'true')
    expect(radio(/High/)).toHaveAttribute('aria-checked', 'false')
  })

  it('sends every pick once, including the checked tier', () => {
    // The check follows the gateway, so it can still show Low after a High pick:
    // picking Low again must reach the gateway or it stays on High.
    const onPick = vi.fn()
    render(<QueuePrioritySubmenu variant="context" current="low" onPick={onPick} />)
    expect(radio(/Low/)).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(radio(/High/))
    fireEvent.click(radio(/Low/))
    expect(onPick.mock.calls).toEqual([['high'], ['low']])
    // The pick keeps the menu open, so the moved check (or an error notice) stays visible.
    expect(document.body.dataset.lastRadioSelectPrevented).toBe('true')
  })

  it('shows the queue-priority failure with the agent hand-off', () => {
    render(<QueuePrioritySubmenu variant="dropdown" error="zzq save failed" onPick={vi.fn()} />)

    const notice = screen.getByRole('alert')
    expect(notice).toHaveTextContent("Couldn't change the sub-agent priority")
    expect(notice).toHaveTextContent('zzq save failed')
    expect(screen.getByRole('menuitem', { name: 'Ask the agent' })).toHaveAttribute(
      'aria-describedby', notice.id,
    )
    expect(screen.getAllByRole('menuitemradio')).toHaveLength(3)
  })

  it('hands the agent the structured report, not the localized line alone', () => {
    handoff.mockClear()
    const report: ErrorReport = {
      id: 'r1',
      at: 0,
      source: 'api',
      message: 'queue priority refused',
      endpoint: '/api/chat/slots/k/queue-priority',
      status: 409,
      code: 'queue_priority_system_lane',
    }
    render(
      <QueuePrioritySubmenu
        variant="context"
        error="zzq save failed"
        errorReport={report}
        onPick={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByRole('menuitem', { name: 'Ask the agent' }))
    expect(handoff).toHaveBeenCalledTimes(1)
    expect(handoff).toHaveBeenCalledWith({ message: 'zzq save failed', report })
  })

  it.each(['dropdown', 'context'] as const)('sends each pick exactly once, with or without an error (%s)', variant => {
    const onPick = vi.fn()
    const { rerender } = render(<QueuePrioritySubmenu variant={variant} current="high" onPick={onPick} />)
    fireEvent.click(radio(/High/))
    expect(onPick.mock.calls).toEqual([['high']])

    rerender(<QueuePrioritySubmenu variant={variant} current="high" error="zzq maybe saved" onPick={onPick} />)
    fireEvent.click(radio(/High/))
    fireEvent.click(radio(/Low/))
    expect(onPick.mock.calls).toEqual([['high'], ['high'], ['low']])
    expect(document.body.dataset.lastRadioSelectPrevented).toBe('true')
  })

  it('uses the heading it is given for an unconfirmed change', () => {
    render(
      <QueuePrioritySubmenu
        variant="context"
        error="zzq maybe saved"
        errorTitle="Priority not confirmed"
        onPick={vi.fn()}
      />,
    )

    const notice = screen.getByRole('alert')
    expect(notice).toHaveTextContent('Priority not confirmed')
    expect(notice).not.toHaveTextContent("Couldn't change the sub-agent priority")
  })

  it('shows no error surface without a failure', () => {
    render(<QueuePrioritySubmenu variant="context" onPick={vi.fn()} />)

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Ask the agent' })).not.toBeInTheDocument()
  })
})
