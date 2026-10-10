import { screen, fireEvent, render } from '@testing-library/react'
import QueuePrioritySubmenu from './QueuePrioritySubmenu'

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

const radio = (name: RegExp) => screen.getByRole('menuitemradio', { name })

describe('QueuePrioritySubmenu', () => {
  it.each(['dropdown', 'context'] as const)('lists High, Medium and Low in the %s family', variant => {
    render(<QueuePrioritySubmenu variant={variant} onPick={vi.fn()} />)
    expect(screen.getByText('Sub-agent priority')).toBeInTheDocument()
    expect(screen.getByTestId('queue-priority-caption')).toHaveTextContent(/High chats start first/)
    expect(screen.getAllByRole('menuitemradio').map(el => el.textContent)).toEqual([
      'High', 'Medium (default)', 'Low',
    ])
  })

  it('marks medium as current when the chat carries no priority', () => {
    render(<QueuePrioritySubmenu variant="dropdown" onPick={vi.fn()} />)
    expect(radio(/Medium/)).toHaveAttribute('aria-checked', 'true')
    expect(radio(/High/)).toHaveAttribute('aria-checked', 'false')
  })

  it('reports the picked tier and ignores a re-pick of the current one', () => {
    const onPick = vi.fn()
    render(<QueuePrioritySubmenu variant="context" current="low" onPick={onPick} />)
    expect(radio(/Low/)).toHaveAttribute('aria-checked', 'true')
    fireEvent.click(radio(/Low/))
    expect(onPick).not.toHaveBeenCalled()
    fireEvent.click(radio(/High/))
    expect(onPick).toHaveBeenCalledWith('high')
    // The pick keeps the menu open, so the moved check (or an error notice) stays visible.
    expect(document.body.dataset.lastRadioSelectPrevented).toBe('true')
  })

  it('shows the queue-priority failure and hand-off item when present', () => {
    render(<QueuePrioritySubmenu variant="dropdown" error="zzq save failed" onPick={vi.fn()} />)

    const notice = screen.getByRole('alert')
    expect(notice).toHaveTextContent("Couldn't change the sub-agent priority")
    expect(notice).toHaveTextContent('zzq save failed')
    expect(screen.getByRole('menuitem', { name: 'Ask the agent' })).toHaveAttribute(
      'aria-describedby', notice.id,
    )
  })

  it('shows no error surface without a failure', () => {
    render(<QueuePrioritySubmenu variant="context" onPick={vi.fn()} />)

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('menuitem', { name: 'Ask the agent' })).not.toBeInTheDocument()
  })
})
