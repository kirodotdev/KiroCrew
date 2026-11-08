import { describe, expect, it, vi } from 'vitest'
import { fireEvent, render, screen } from '@testing-library/react'
import { CrewEyebrow, CrewGhostBadge, CrewModeComposerChip, CrewStatusDot, CrewWorkerStack } from './CrewModeMarks'

const reducedMotion = vi.hoisted(() => ({ value: false }))
vi.mock('../../hooks/useReducedMotion', () => ({ useReducedMotion: () => reducedMotion.value }))

describe('CrewModeMarks', () => {
  it('draws the ghost badge on the crew gradient at the size asked', () => {
    render(<CrewGhostBadge size={20} />)
    const badge = screen.getByTestId('crew-ghost-badge')
    expect(badge.className).toContain('crew-grad')
    expect(badge.style.width).toBe('20px')
    expect(badge.getAttribute('aria-hidden')).toBe('true')
  })

  it('the eyebrow is the gradient CREW MODE line', () => {
    render(<CrewEyebrow />)
    const eyebrow = screen.getByTestId('crew-eyebrow')
    expect(eyebrow.className).toContain('crew-eyebrow-text')
    expect(eyebrow.className).toContain('tracking-[1px]')
    expect(eyebrow.textContent).toBe('Ultra Mode')
    // Idle by default: the gradient sits still.
    expect(eyebrow).toHaveAttribute('data-flowing', 'false')
  })

  it('the eyebrow flows only while active, and stops when the work ends', () => {
    const { rerender } = render(<CrewEyebrow active />)
    expect(screen.getByTestId('crew-eyebrow')).toHaveAttribute('data-flowing', 'true')
    rerender(<CrewEyebrow active={false} />)
    expect(screen.getByTestId('crew-eyebrow')).toHaveAttribute('data-flowing', 'false')
  })

  it('the eyebrow never flows under reduced motion, even while active', () => {
    reducedMotion.value = true
    try {
      render(<CrewEyebrow active />)
      expect(screen.getByTestId('crew-eyebrow')).toHaveAttribute('data-flowing', 'false')
    } finally {
      reducedMotion.value = false
    }
  })

  it('the status dot names the state in its tone, as text or for screen readers only', () => {
    const { rerender } = render(<CrewStatusDot group="approval" />)
    let dot = screen.getByTestId('crew-status')
    expect(dot.getAttribute('data-group')).toBe('approval')
    expect(dot.className).toContain('crew-tone-warn')
    expect(dot.textContent).toBe('Approval needed')
    expect(dot.querySelector('.sr-only')).toBeNull()

    rerender(<CrewStatusDot group="waiting" withLabel={false} />)
    dot = screen.getByTestId('crew-status')
    // A waiting worker's dot is hollow; the word stays for a screen reader.
    expect(dot.querySelector('[aria-hidden]')?.className).toContain('border-current')
    expect(dot.querySelector('.sr-only')?.textContent).toBe('Waiting')
  })

  it('the worker stack shows up to `max` ghosts in their tones and counts the rest', () => {
    render(<CrewWorkerStack groups={['approval', 'working', 'done', 'waiting']} max={3} />)
    const stack = screen.getByTestId('crew-worker-stack')
    const ghosts = stack.querySelectorAll('[data-group]')
    expect([...ghosts].map(g => g.getAttribute('data-group'))).toEqual(['approval', 'working', 'done'])
    expect(ghosts[0].className).toContain('crew-tone-warn')
    expect(stack.textContent).toContain('+1')
  })

  it('a full stack shows no count', () => {
    render(<CrewWorkerStack groups={['working']} />)
    expect(screen.getByTestId('crew-worker-stack').textContent).not.toContain('+')
  })

  it('the composer chip is a pressed toggle while it can still be turned off, and a read-only mark after', () => {
    const onTurnOff = vi.fn()
    const { rerender } = render(<CrewModeComposerChip onTurnOff={onTurnOff} />)
    const chip = screen.getByTestId('crew-mode-composer-chip')
    expect(chip.tagName).toBe('BUTTON')
    expect(chip.getAttribute('aria-pressed')).toBe('true')
    expect(chip.getAttribute('title')).toContain('Turn off Ultra Mode')
    fireEvent.click(chip)
    expect(onTurnOff).toHaveBeenCalledTimes(1)

    rerender(<CrewModeComposerChip />)
    const mark = screen.getByTestId('crew-mode-composer-chip')
    expect(mark.tagName).toBe('SPAN')
    expect(mark.getAttribute('role')).toBe('img')
    expect(mark.getAttribute('aria-label')).toBe('Ultra Mode is on for this chat')
    expect(mark.textContent).toBe('Ultra Mode')
  })

  it('marks Ultra Mode with a lightning bolt, not the ghost', () => {
    render(<CrewModeComposerChip />)
    const chip = screen.getByTestId('crew-mode-composer-chip')
    expect(chip.querySelector('.lucide-zap')).not.toBeNull()
    expect(chip.querySelector('[data-testid="kiro-ghost-mark"]')).toBeNull()
  })
})
