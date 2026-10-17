/**
 * The group header's indicator slot (#14885, the revert of #14407).
 *
 * The deleted ToolGroupToggle.unified.test.tsx was the only pin on this host's
 * live states — the pending-approval ping on a warn ring, the running pulse on
 * the default ring — and on its disclosure indicator being a Lucide chevron
 * rather than a text glyph. The revert re-inlines the header, which is exactly
 * the edit that can drop one of those by accident, so the pins move here.
 */
import { describe, it, expect } from 'vitest'
import { render } from '@testing-library/react'
import CollapsibleToolGroup from '../pages/chat/CollapsibleToolGroup'

const headerOf = (c: HTMLElement) => c.querySelector<HTMLButtonElement>('button[aria-expanded]')!

describe('CollapsibleToolGroup — the header keeps its own chrome and states', () => {
  it('settled: a Lucide chevron, never a text glyph', () => {
    // AUTOSDE use-lucide-icons / no-emoji-as-icons are blocking on src/**/*.tsx,
    // and the pre-#14407 header used a "▶" character here.
    const { container } = render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    const btn = headerOf(container)
    expect(btn.querySelector('svg.lucide-chevron-right')).not.toBeNull()
    expect(btn.textContent).not.toMatch(/[▶▼]/)
    expect(btn.querySelector('.rotate-90')).toBeNull()
  })

  it('settled: this host keeps its pill chrome, unlike the main chat fold row', () => {
    const { container } = render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    const cls = headerOf(container).className
    expect(cls).toMatch(/\bbg-card\b/)
    expect(cls).toMatch(/\bring-1\b/)
    expect(cls).toMatch(/\bfont-mono\b/)
  })

  it('a pending approval shows the attention ping on a warn ring, not the chevron', () => {
    const { container } = render(
      <CollapsibleToolGroup count={1} hasPermission permissionMeta={{ tool_input: { command: 'rm -rf build' } }} onApprove={async () => undefined}>
        <div>row</div>
      </CollapsibleToolGroup>,
    )
    const btn = headerOf(container)
    expect(btn.className).toMatch(/\bring-warn\b/)
    expect(btn.querySelector('.animate-ping')).not.toBeNull()
    expect(btn.querySelector('svg.lucide-chevron-right')).toBeNull()
  })

  it('running tools show the pulsing dot on the default ring', () => {
    const { container } = render(<CollapsibleToolGroup count={1} isRunning><div>row</div></CollapsibleToolGroup>)
    const btn = headerOf(container)
    expect(btn.className).toMatch(/\bring-border\b/)
    expect(btn.querySelector('.animate-pulse')).not.toBeNull()
    expect(btn.querySelector('svg.lucide-chevron-right')).toBeNull()
  })

  it('carries the a11y contract: aria-expanded plus a verb-led name', () => {
    const { container } = render(<CollapsibleToolGroup count={2}><div>row</div></CollapsibleToolGroup>)
    const btn = headerOf(container)
    expect(btn.getAttribute('aria-expanded')).toBe('false')
    expect(btn.getAttribute('aria-label')).toMatch(/expand/i)
  })
})
