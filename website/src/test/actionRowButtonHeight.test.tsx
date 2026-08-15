/**
 * Height contract for action-row buttons.
 *
 * `SendBtn` carried `h-9` — a fixed height around inline content. Measured in a
 * real browser at a width where the label needs two lines: the box stays 36px
 * while the text needs ~48px, so 12px is cut off and the label becomes
 * unreadable. `min-h-9` keeps the same resting height and grows instead.
 *
 * The trade is deliberate and worth stating: a grown button leaves its row's
 * heights uneven. Uneven is legible, clipped is not. The clean case needs the
 * ROW to wrap so no button wraps internally, which is why the control rows in
 * VectorMemoryCard carry `flex-wrap` — without it their `flex:1` input cannot
 * shrink past its intrinsic minimum, so the row overflows and the button leaves
 * the viewport entirely.
 *
 * happy-dom does no layout, so these pin the class contract that selects the
 * behaviour, the same way NotificationsPage.mobileScroll does.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { SendBtn } from '../components/ui'

describe('SendBtn height contract', () => {
  it('bounds its height from below, so a wrapped label grows the box instead of being clipped', () => {
    render(<SendBtn>Zur Warteliste hinzufügen</SendBtn>)
    const btn = screen.getByRole('button')
    expect(btn.className).toContain('min-h-9')
    // A fixed height is the defect: it cannot grow for a second line.
    expect(btn.className.split(/\s+/)).not.toContain('h-9')
  })

  it('still lets a caller override the height, since twMerge resolves the conflict', () => {
    render(<SendBtn className="min-h-11">Tall</SendBtn>)
    const btn = screen.getByRole('button')
    expect(btn.className).toContain('min-h-11')
    expect(btn.className.split(/\s+/)).not.toContain('min-h-9')
  })
})

describe('VectorMemoryCard control rows', () => {
  it('every input+button row can wrap', async () => {
    // Read the source rather than rendering: the card needs a live API and a
    // query client, and what is being pinned is the row contract, not behaviour.
    const src = await import('../pages/overview/VectorMemoryCard.tsx?raw').then(m => m.default as string)
    const rows = src.match(/className="flex gap-2 items-center[^"]*"/g) ?? []
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) expect(row).toContain('flex-wrap')
  })
})
