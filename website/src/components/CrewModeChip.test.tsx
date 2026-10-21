import { describe, it, expect, vi } from 'vitest'
import { act, screen, fireEvent, within } from '@testing-library/react'
import { renderWithProviders } from '../test/helpers'
import { CrewModeChip } from './CrewModeChip'
import CrewModeWelcome, { CREW_WELCOME_PLAY_SECS } from './CrewModeWelcome'

describe('CrewModeChip', () => {
  it('reads off and reports it through aria-pressed', () => {
    renderWithProviders(<CrewModeChip onTurnOn={vi.fn()} />)
    const chip = screen.getByTestId('crew-mode-chip')
    expect(chip).toHaveAttribute('aria-pressed', 'false')
    // Only one label is displayed: the short name below `sm`, the explained one
    // from `sm` up.
    expect(within(chip).getByText('Crew Mode')).toHaveClass('sm:hidden')
    expect(within(chip).getByText('Crew Mode: split into tasks')).toHaveClass('hidden', 'sm:inline')
  })

  it('says what turning it on does in its hint', () => {
    renderWithProviders(<CrewModeChip onTurnOn={vi.fn()} />)
    expect(screen.getByTestId('crew-mode-chip')).toHaveAttribute(
      'title',
      'Turn on Crew Mode: each task runs in its own chat and reports back here.',
    )
  })

  it('has no on state and no off control', () => {
    renderWithProviders(<CrewModeChip onTurnOn={vi.fn()} />)
    const chip = screen.getByTestId('crew-mode-chip')
    expect(chip).not.toHaveTextContent('Crew Mode on')
    expect(chip.querySelector('svg.lucide-x')).toBeNull()
    expect(screen.queryByRole('button', { pressed: true })).toBeNull()
  })

  it('calls onTurnOn on click', () => {
    const onTurnOn = vi.fn()
    renderWithProviders(<CrewModeChip onTurnOn={onTurnOn} />)
    fireEvent.click(screen.getByTestId('crew-mode-chip'))
    expect(onTurnOn).toHaveBeenCalledTimes(1)
  })

  it('cannot be pressed while disabled', () => {
    const onTurnOn = vi.fn()
    renderWithProviders(<CrewModeChip onTurnOn={onTurnOn} disabled />)
    const chip = screen.getByTestId('crew-mode-chip')
    expect(chip).toBeDisabled()
    fireEvent.click(chip)
    expect(onTurnOn).not.toHaveBeenCalled()
  })

  it('stays focusable while a reply runs, and ignores clicks', () => {
    const onTurnOn = vi.fn()
    renderWithProviders(<CrewModeChip onTurnOn={onTurnOn} busy />)
    const chip = screen.getByTestId('crew-mode-chip')
    // aria-disabled, not disabled: a keyboard still reaches it and hears its hint.
    expect(chip).not.toBeDisabled()
    expect(chip).toHaveAttribute('aria-disabled', 'true')
    expect(chip.className).not.toContain('opacity-')
    expect(chip).toHaveClass('aria-disabled:cursor-not-allowed')
    expect(chip).toHaveAttribute('title', 'Available after the reply.')
    fireEvent.click(chip)
    expect(onTurnOn).not.toHaveBeenCalled()
  })

  it('keeps the accessible name Crew Mode', () => {
    renderWithProviders(<CrewModeChip onTurnOn={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Crew Mode', pressed: false })).toBeInTheDocument()
  })

  it('names the visible line that describes it', () => {
    renderWithProviders(<><CrewModeChip onTurnOn={vi.fn()} describedBy="note" /><span id="note">Available after the reply.</span></>)
    expect(screen.getByRole('button', { name: 'Crew Mode' })).toHaveAccessibleDescription('Available after the reply.')
  })

  it('fades when disabled for any other reason', () => {
    renderWithProviders(<CrewModeChip onTurnOn={vi.fn()} disabled />)
    expect(screen.getByTestId('crew-mode-chip')).toHaveClass('disabled:opacity-60')
  })
})

describe('CrewModeWelcome', () => {
  it('shows the title, the slogan, the flow labels and one button, and no other words', () => {
    renderWithProviders(<CrewModeWelcome onOpenTaskBoard={vi.fn()} />)
    const page = screen.getByTestId('crew-mode-welcome')
    expect(within(page).getByRole('heading', { name: 'Crew Mode' })).toBeInTheDocument()
    expect(within(page).getByRole('button', { name: 'Task board' })).toBeInTheDocument()
    expect(within(page).getAllByRole('button')).toHaveLength(1)
    // Every visible word on the page, in order: nothing beyond these.
    expect(page.textContent).toBe('Crew ModeYou set the goal. Your crew does the work.GoalSplitOwn chatsBoardNeeds youTask board')
  })

  it('reads the flow as an ordered list a screen reader can follow', () => {
    renderWithProviders(<CrewModeWelcome />)
    const flow = screen.getByRole('list', { name: 'How Crew Mode works' })
    const steps = within(flow).getAllByRole('listitem').map(li => li.textContent)
    expect(steps).toEqual(['Goal', 'Split', 'Own chats', 'Board', 'Needs you'])
  })

  it('keeps the art and the arrows out of the accessibility tree', () => {
    renderWithProviders(<CrewModeWelcome />)
    const page = screen.getByTestId('crew-mode-welcome')
    expect(within(page).queryAllByRole('img')).toHaveLength(0)
    expect(within(page).getAllByTestId('kiro-ghost-mark').every(g => g.closest('[aria-hidden="true"]'))).toBe(true)
  })

  it('draws no sample board', () => {
    renderWithProviders(<CrewModeWelcome onOpenTaskBoard={vi.fn()} />)
    const page = screen.getByTestId('crew-mode-welcome')
    expect(within(page).queryByRole('region')).toBeNull()
    expect(within(page).queryByText('Your crew')).toBeNull()
    for (const sample of ['Example', 'Which day works for you?', 'Email']) {
      expect(page).not.toHaveTextContent(sample)
    }
  })

  it('stops moving the moment the person starts on the composer', () => {
    renderWithProviders(<><CrewModeWelcome /><textarea aria-label="composer" /></>)
    expect(screen.getByTestId('crew-mode-welcome')).toHaveAttribute('data-motion', 'playing')
    fireEvent.focusIn(screen.getByRole('textbox', { name: 'composer' }))
    expect(screen.getByTestId('crew-mode-welcome')).toHaveAttribute('data-motion', 'still')
  })

  it('stops moving while typing, even without a focus change', () => {
    renderWithProviders(<><CrewModeWelcome /><textarea aria-label="composer" /></>)
    fireEvent.input(screen.getByRole('textbox', { name: 'composer' }), { target: { value: 'a' } })
    expect(screen.getByTestId('crew-mode-welcome')).toHaveAttribute('data-motion', 'still')
  })

  it('settles on its final frame once it has played, and stays still', () => {
    vi.useFakeTimers()
    try {
      renderWithProviders(<CrewModeWelcome />)
      expect(screen.getByTestId('crew-mode-welcome')).toHaveAttribute('data-motion', 'playing')
      act(() => { vi.advanceTimersByTime(CREW_WELCOME_PLAY_SECS * 1000) })
      expect(screen.getByTestId('crew-mode-welcome')).toHaveAttribute('data-motion', 'still')
      act(() => { vi.advanceTimersByTime(60_000) })
      expect(screen.getByTestId('crew-mode-welcome')).toHaveAttribute('data-motion', 'still')
    } finally {
      vi.useRealTimers()
    }
  })

  it('plays its entrance once per chat: a later mount for the same chat starts on the final frame', () => {
    const first = renderWithProviders(<CrewModeWelcome chatKey="chat-replay" />)
    expect(screen.getByTestId('crew-mode-welcome')).toHaveAttribute('data-motion', 'playing')
    first.unmount()
    renderWithProviders(<CrewModeWelcome chatKey="chat-replay" />)
    expect(screen.getByTestId('crew-mode-welcome')).toHaveAttribute('data-motion', 'still')
  })

  it('plays for a different chat', () => {
    renderWithProviders(<CrewModeWelcome chatKey="chat-replay" />).unmount()
    renderWithProviders(<CrewModeWelcome chatKey="chat-other" />)
    expect(screen.getByTestId('crew-mode-welcome')).toHaveAttribute('data-motion', 'playing')
  })

  it('plays for no more than about four seconds', () => {
    expect(CREW_WELCOME_PLAY_SECS).toBeLessThanOrEqual(4)
  })

  it('opens the task board from its one button', () => {
    const onOpenTaskBoard = vi.fn()
    renderWithProviders(<CrewModeWelcome onOpenTaskBoard={onOpenTaskBoard} />)
    fireEvent.click(screen.getByRole('button', { name: 'Task board' }))
    expect(onOpenTaskBoard).toHaveBeenCalledTimes(1)
  })

  it('shows the board button as its icon alone where room is tight, name kept', () => {
    renderWithProviders(<CrewModeWelcome onOpenTaskBoard={vi.fn()} />)
    const btn = screen.getByTestId('crew-mode-open-board')
    // The name lives on the button itself, so hiding the label never loses it.
    expect(btn).toHaveAttribute('aria-label', 'Task board')
    expect(btn).toHaveAttribute('title', 'Task board')
    const label = screen.getByTestId('crew-mode-open-board-label')
    expect(label).toHaveTextContent('Task board')
    // Hidden on a short screen and in a narrow column, by the page's own queries.
    expect(label.className).toContain('[@media(max-height:480px)]:hidden')
    expect(label.className).toContain('@max-[18rem]/crewwelcome:hidden')
    // On a short screen the page wraps into a row so the button sits beside the
    // title and the flow goes last, off the scroll box's bottom edge.
    const page = screen.getByTestId('crew-mode-welcome')
    expect(page.className).toContain('[@media(max-height:480px)]:flex-row')
    expect(screen.getByTestId('crew-mode-flow').parentElement!.className).toContain('[@media(max-height:480px)]:order-last')
    // A 44px target on touch, whatever the width.
    expect(btn.className).toContain('[@media(pointer:coarse)]:min-h-11')
    expect(btn.className).toContain('[@media(pointer:coarse)]:min-w-11')
  })

  it('leaves the button out when there is no board to open', () => {
    renderWithProviders(<CrewModeWelcome />)
    expect(screen.queryByTestId('crew-mode-open-board')).toBeNull()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })
})
