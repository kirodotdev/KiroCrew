/**
 * The Toggle's `tone` prop.
 *
 * Pinned because the visual regression it guards is invisible to every other
 * test: a list of switches rendered with the accent fill still satisfies every
 * behavioural assertion (role, aria-checked, click handler), so only a class
 * assertion catches a silent revert to `bg-accent`.
 */
import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { Toggle } from '../components/ui'

const switchEl = () => screen.getByRole('switch')

describe('Toggle tone', () => {
  it('fills with the accent by default, so a lone switch keeps its emphasis', () => {
    render(<Toggle checked onChange={vi.fn()} label="t" />)
    expect(switchEl().className).toContain('bg-accent')
    expect(switchEl().className).not.toContain('bg-border-strong')
  })

  it('fills muted when tone="muted", so a list of switches does not shout', () => {
    render(<Toggle checked onChange={vi.fn()} label="t" tone="muted" />)
    expect(switchEl().className).toContain('bg-border-strong')
    expect(switchEl().className).not.toContain('bg-accent')
  })

  it('reads unchecked the same way in both tones — the knob carries the state', () => {
    const { unmount } = render(<Toggle checked={false} onChange={vi.fn()} label="a" />)
    const plain = switchEl().className
    unmount()
    render(<Toggle checked={false} onChange={vi.fn()} label="b" tone="muted" />)
    expect(switchEl().className).toBe(plain)
  })

  it('draws an edge on the off track only, so off does not read as disabled', () => {
    const { unmount } = render(<Toggle checked={false} onChange={vi.fn()} label="a" />)
    expect(switchEl().className).toContain('shadow-[inset_0_0_0_1.5px_var(--muted)]')
    unmount()
    render(<Toggle checked onChange={vi.fn()} label="b" />)
    expect(switchEl().className).not.toContain('shadow-[inset')
  })

  it('leaves a disabled off switch edgeless, so it never reads as an available off one', () => {
    render(<Toggle checked={false} onChange={vi.fn()} label="a" disabled />)
    expect(switchEl().className).not.toContain('shadow-[inset')
    expect(switchEl().className).toContain('opacity-40')
  })

  it('keeps aria-checked truthful regardless of tone', () => {
    render(<Toggle checked onChange={vi.fn()} label="t" tone="muted" />)
    expect(switchEl()).toHaveAttribute('aria-checked', 'true')
  })
})
