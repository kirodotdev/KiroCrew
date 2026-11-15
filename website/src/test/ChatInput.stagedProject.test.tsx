import { describe, it, expect, vi, beforeEach } from 'vitest'
import { act, screen, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { SlotProvider } from '../providers/SlotContext'
import { stagedProjectTurnKey } from '../components/chat-input/stagedProject'
import type { ChatMessage } from '../types'

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
    // The badge says on its own that the switch is still to come, so it reads
    // without the tooltip a touch user cannot open (a bare "Next message"
    // named when but not what).
    expect(badge()).toHaveTextContent(/^Starts next message$/)
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

  it('keeps a pending pick when the user leaves a busy chat and comes back', () => {
    const inSlot = (slot: string, project: string) => (
      <SlotProvider slotId={slot}>
        <ChatInput {...base} project={project} isRunning />
      </SlotProvider>
    )
    const { rerender } = renderWithProviders(inSlot('chat-a', START))
    rerender(inSlot('chat-a', OTHER))
    rerender(inSlot('chat-b', THIRD))
    expect(badge()).toBeNull()
    // Back in chat A, still busy: its reply still runs in StartRepo, so the
    // pick is still waiting, not adopted as the start project.
    rerender(inSlot('chat-a', OTHER))
    expect(badge()).toBeVisible()
    expect(chip()).toHaveAccessibleName(/Switching to OtherRepo from your next message\./)
    expect(chip()).toHaveAccessibleName(/This reply finishes in StartRepo\./)
    expect(status()).toHaveTextContent('Switching to OtherRepo from your next message.')
  })

  it('clears the badge when a queued message opens the next turn without the composer going idle', () => {
    const { rerender } = renderWithProviders(<ChatInput {...base} project={START} isRunning projectTurnKey="user:t1" />)
    rerender(<ChatInput {...base} project={OTHER} isRunning projectTurnKey="user:t1" />)
    expect(badge()).toBeVisible()
    // The queued message starts its turn in OtherRepo: the pick has applied.
    rerender(<ChatInput {...base} project={OTHER} isRunning projectTurnKey="user:t2" />)
    expect(badge()).toBeNull()
    expect(chip()).toHaveAccessibleName(/This reply finishes in OtherRepo\./)
    expect(status()).toHaveTextContent('')
  })

  it('keeps the pick pending when a sub-agent completion opens a turn while the chip is locked', () => {
    const { rerender } = renderWithProviders(<ChatInput {...base} project={START} isRunning projectTurnKey="user:t1" />)
    rerender(<ChatInput {...base} project={OTHER} isRunning projectTurnKey="user:t1" />)
    rerender(<ChatInput {...base} project={OTHER} isRunning projectLocked projectTurnKey="user:t1" />)
    // One of two sub-agents finishes: its completion row opens a turn, but the
    // server keeps the reset deferred while the other is attached, so that
    // turn still runs in StartRepo.
    rerender(<ChatInput {...base} project={OTHER} isRunning projectLocked projectTurnKey="subagent:t2" />)
    expect(badge()).toBeVisible()
    expect(chip()).toHaveAccessibleName(/Switching to OtherRepo from your next message\./)
    expect(status()).toHaveTextContent('Switching to OtherRepo from your next message.')
    // The last sub-agent detaches while that turn still runs: still StartRepo.
    rerender(<ChatInput {...base} project={OTHER} isRunning projectTurnKey="subagent:t2" />)
    expect(badge()).toBeVisible()
    expect(chip()).toHaveAccessibleName(/This reply finishes in StartRepo\./)
    // The next turn opens unlocked: the pick applies.
    rerender(<ChatInput {...base} project={OTHER} isRunning projectTurnKey="user:t3" />)
    expect(badge()).toBeNull()
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

  it('names the picked folder next to a clock on a compact shelf, where the chip drops its folder name', () => {
    const observers = new Map<Element, ResizeObserverCallback>()
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: ResizeObserverCallback) {}
      observe(target: Element) { observers.set(target, this.callback) }
      unobserve(target: Element) { observers.delete(target) }
      disconnect() {}
    })
    try {
      const { rerender } = renderWithProviders(<ChatInput {...base} project={START} isRunning />)
      const shelf = screen.getByTestId('composer-context-shelf')
      act(() => observers.get(shelf)?.([{ target: shelf, contentRect: { width: 300, height: 32 } } as ResizeObserverEntry], {} as ResizeObserver))
      rerender(<ChatInput {...base} project={OTHER} isRunning />)
      // The chip's own folder label is gone; the pending marker carries the
      // picked folder's name as visible text, since a clock glyph alone did
      // not say what is waiting and a touch screen cannot show the tooltip.
      expect(badge()).toBeVisible()
      expect(badge()).toHaveTextContent(/^OtherRepo$/)
      expect(badge()!.className).not.toContain('sr-only')
      // A glyph, not a bare colour dot: the clock shape marks "waiting" for a
      // reader who cannot tell the accent colour apart.
      expect(screen.getByTestId('composer-project-pending-clock')).toHaveClass('lucide-clock')
      expect(badge()).toContainElement(screen.getByTestId('composer-project-pending-clock'))
      expect(badge()).toHaveAttribute('aria-hidden', 'true')
      expect(chip()).toContainElement(badge())
      expect(chip()).toHaveAccessibleName(/Switching to OtherRepo from your next message\./)
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('stagedProjectTurnKey', () => {
  it('names the last turn opener and skips steers and parked sends', () => {
    const rows = [
      { role: 'user', content: 'a', ts: 't1' },
      { role: 'assistant', content: 'r', ts: 't2' },
      { role: 'user', content: 's', ts: 't3', meta: { steer: true } },
      { role: 'user', content: 'q', ts: 't4', meta: { queuedEntryId: 'x' } },
      { role: 'queued', content: 'q2', ts: 't5' },
    ] as ChatMessage[]
    expect(stagedProjectTurnKey(rows)).toBe('user:t1')
    expect(stagedProjectTurnKey([...rows, { role: 'user', content: 'q', ts: 't6' } as ChatMessage])).toBe('user:t6')
    expect(stagedProjectTurnKey([])).toBe('')
  })
})
