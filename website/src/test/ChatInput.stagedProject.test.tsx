import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'

// #20350: a project picked while the composer is busy is staged for the next
// turn. The chip shows that as visible text (not only in a hover tooltip,
// which touch input cannot reach) and names it in its accessible name, until
// the composer goes idle and the picked project takes effect.

const START = '/home/u/work/StartRepo'
const OTHER = '/home/u/work/OtherRepo'
const THIRD = '/home/u/work/ThirdRepo'

const base = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
  onStop: vi.fn(),
  onProjectClick: vi.fn(),
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
})

const chip = () => screen.getByTestId('composer-project-chip')
const badge = () => screen.queryByTestId('composer-project-pending')
const status = () => screen.getByTestId('composer-project-switch-status')

describe('project chip staged-switch badge', () => {
  it('shows no badge while a reply runs without a pick, or when idle', () => {
    const { rerender } = renderWithProviders(<ChatInput {...base} project={START} isRunning />)
    expect(badge()).toBeNull()
    expect(chip()).not.toHaveAccessibleName(/Switching to/)
    // An idle pick applies at once: nothing is pending.
    rerender(<ChatInput {...base} project={OTHER} />)
    expect(badge()).toBeNull()
  })

  it('marks a mid-turn pick pending and clears it when the reply completes', () => {
    const { rerender } = renderWithProviders(<ChatInput {...base} project={START} isRunning />)
    rerender(<ChatInput {...base} project={OTHER} isRunning />)
    expect(badge()).toBeVisible()
    expect(badge()).toHaveTextContent('Next message')
    expect(chip()).toHaveTextContent('OtherRepo')
    expect(chip()).toHaveAccessibleName(/Switching to OtherRepo from your next message\./)
    expect(chip()).toHaveAccessibleName(/This reply finishes in StartRepo\./)
    expect(status()).toHaveTextContent('Switching to OtherRepo from your next message.')
    // The reply ends and nothing else holds the composer: the pick applies.
    rerender(<ChatInput {...base} project={OTHER} />)
    expect(badge()).toBeNull()
    expect(status()).toHaveTextContent('')
    // The next reply starts in the picked project, so it is not pending.
    rerender(<ChatInput {...base} project={OTHER} isRunning />)
    expect(badge()).toBeNull()
  })

  it('follows repeated picks and clears when the start project is picked back', () => {
    const { rerender } = renderWithProviders(<ChatInput {...base} project={START} isRunning />)
    rerender(<ChatInput {...base} project={OTHER} isRunning />)
    rerender(<ChatInput {...base} project={THIRD} isRunning />)
    expect(badge()).toBeVisible()
    expect(chip()).toHaveAccessibleName(/Switching to ThirdRepo from your next message\./)
    expect(chip()).not.toHaveAccessibleName(/OtherRepo/)
    expect(chip()).toHaveAccessibleName(/This reply finishes in StartRepo\./)
    rerender(<ChatInput {...base} project={`${START}/`} isRunning />)
    expect(badge()).toBeNull()
    expect(chip()).not.toHaveAccessibleName(/Switching to/)
  })

  it('stays pending while attached sub-agents keep the old directory in use', () => {
    const { rerender } = renderWithProviders(<ChatInput {...base} project={START} isRunning />)
    rerender(<ChatInput {...base} project={OTHER} isRunning />)
    // The reply ended but sub-agents are attached: the composer is still busy
    // and the chip locks, while the deferred reset stays armed on the server.
    rerender(<ChatInput {...base} project={OTHER} isRunning projectLocked />)
    expect(chip()).toBeDisabled()
    expect(badge()).toBeVisible()
    expect(chip()).toHaveAccessibleName(/Switching to OtherRepo from your next message\./)
    expect(chip()).toHaveAccessibleName(/The project can't change while sub-agents are running\./)
    expect(chip().getAttribute('title')).toBe(chip().getAttribute('aria-label'))
    // The sub-agents finish: only now does the pick take effect.
    rerender(<ChatInput {...base} project={OTHER} />)
    expect(badge()).toBeNull()
  })

  it('does not carry a pending pick into another chat', () => {
    const inSlot = (slot: string, project: string) => (
      <SlotProvider slotId={slot}>
        <ChatInput {...base} project={project} isRunning />
      </SlotProvider>
    )
    const { rerender } = renderWithProviders(inSlot('chat-a', START))
    rerender(inSlot('chat-a', OTHER))
    expect(badge()).toBeVisible()
    // Both chats are busy, so isRunning never flips; the slot change alone
    // must re-baseline against the other chat's project.
    rerender(inSlot('chat-b', THIRD))
    expect(badge()).toBeNull()
    expect(chip()).toHaveAccessibleName(/This reply finishes in ThirdRepo\./)
    rerender(inSlot('chat-b', OTHER))
    expect(badge()).toBeVisible()
    expect(chip()).toHaveAccessibleName(/This reply finishes in ThirdRepo\./)
  })

  it('is reachable by keyboard: focus names the pending pick and Enter reopens the picker', async () => {
    const user = userEvent.setup()
    const onProjectClick = vi.fn()
    const { rerender } = renderWithProviders(<ChatInput {...base} onProjectClick={onProjectClick} project={START} isRunning />)
    rerender(<ChatInput {...base} onProjectClick={onProjectClick} project={OTHER} isRunning />)
    chip().focus()
    expect(chip()).toHaveFocus()
    expect(chip()).toHaveAccessibleName(/Switching to OtherRepo from your next message\./)
    await user.keyboard('{Enter}')
    expect(onProjectClick).toHaveBeenCalledTimes(1)
  })

  it('is visible to touch input without any hover', () => {
    const onProjectClick = vi.fn()
    const { rerender } = renderWithProviders(<ChatInput {...base} onProjectClick={onProjectClick} project={START} isRunning />)
    rerender(<ChatInput {...base} onProjectClick={onProjectClick} project={OTHER} isRunning />)
    // No pointerenter/mouseover has fired: the badge is rendered text, not a
    // tooltip, and it is not screen-reader-only.
    const b = badge()!
    expect(b).toBeVisible()
    expect(b.className).not.toContain('sr-only')
    expect(chip()).toContainElement(b)
    fireEvent.pointerDown(chip(), { pointerType: 'touch' })
    fireEvent.click(chip())
    expect(onProjectClick).toHaveBeenCalledTimes(1)
  })
})
