/**
 * Composer shelf: the context readout and model chip never paint over the agent
 * and project chips. The right group shrinks (its model name truncates); the
 * chip group keeps a floor sized for the chips' content once their labels have
 * truncated to nothing, one per shelf band. happy-dom does no layout, so this
 * pins the classes that decide it, and the chip geometry the floor is sized for.
 */
import { describe, it, expect, vi } from 'vitest'
import { act, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

vi.mock('../api/client', () => ({ api: {} }))

async function renderShelf() {
  await act(async () => {
    renderWithProviders(
      <ChatInput
        value="" onChange={vi.fn()} onSend={vi.fn()} connected
        onAgentClick={vi.fn()} agentName="kirocrew"
        onProjectClick={vi.fn()} projectBranch="main"
        onModelClick={vi.fn()} modelName="claude-opus-5.5" reasoningEffort="high" hasEffort
        contextPct={53} contextUsedTokens={524800} contextWindowTokens={1000000}
        showContextPct showContextTokens
      />,
    )
  })
  const agentChip = screen.getByRole('button', { name: 'Agent: kirocrew' })
  return {
    agentChip,
    projectChip: screen.getByTestId('composer-project-chip'),
    chipGroup: agentChip.parentElement as HTMLElement,
    rightGroup: screen.getByTestId('composer-model-chip').parentElement as HTMLElement,
  }
}

describe('ChatInput — shelf chips keep their room', () => {
  it('lets the readout and model chip give way instead of covering the chips', async () => {
    // No ResizeObserver in happy-dom: the shelf stays in its wide band.
    const { agentChip, projectChip, chipGroup, rightGroup } = await renderShelf()

    expect(rightGroup).toHaveClass('min-w-0')
    expect(rightGroup).not.toHaveClass('shrink-0')
    // Wide, labels truncated to nothing: 2 x (px-2.5 + 13px icon + gap-1.5) + gap-2 = 86,
    // + branch separator (gap-1.5 + '·' + gap-1.5) = 102, + the AWS segment's gap-1.5 = 108
    // <= min-w-28 (7rem = 112px at a 16px root).
    expect(chipGroup).toHaveClass('flex-1', 'gap-2', 'min-w-28')
    expect(chipGroup).not.toHaveClass('min-w-21')
    expect(chipGroup).toContainElement(projectChip)
    for (const chip of [agentChip, projectChip]) {
      expect(chip).toHaveClass('px-2.5', 'gap-1.5')
      expect(chip.querySelector('svg')).toHaveAttribute('width', '13')
    }
  })

  it('keeps an icon-sized floor once the shelf is compact', async () => {
    const observers = new Map<Element, ResizeObserverCallback>()
    vi.stubGlobal('ResizeObserver', class {
      constructor(private callback: ResizeObserverCallback) {}
      observe(target: Element) { observers.set(target, this.callback) }
      unobserve(target: Element) { observers.delete(target) }
      disconnect() {}
    })
    try {
      await renderShelf()
      const shelf = screen.getByTestId('composer-context-shelf')
      act(() => observers.get(shelf)?.([{
        target: shelf, contentRect: { width: 320, height: 32 },
      } as ResizeObserverEntry], {} as ResizeObserver))
      const agentChip = screen.getByRole('button', { name: 'Agent: kirocrew' })
      const projectChip = screen.getByTestId('composer-project-chip')
      const chipGroup = agentChip.parentElement as HTMLElement
      // Compact, icons only: 2 x (px-2.5 + 13px icon) + gap-2 = 74, + the pill's gap-1.5
      // before the AWS segment = 80 <= min-w-21 (5.25rem = 84px at a 16px root).
      expect(chipGroup).toHaveClass('flex-1', 'gap-2', 'min-w-21')
      expect(chipGroup).not.toHaveClass('min-w-28')
      expect(chipGroup).toContainElement(projectChip)
      for (const chip of [agentChip, projectChip]) {
        expect(chip).toHaveClass('px-2.5')
        expect(chip.querySelector('svg')).toHaveAttribute('width', '13')
      }
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
