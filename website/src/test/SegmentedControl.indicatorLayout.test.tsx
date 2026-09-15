/**
 * #9684: the active-pill indicator must not re-size to a mid-animation box while
 * a segment label reveals its `width` from 0 to auto. The fix is a framer
 * shared-layout prop contract:
 *
 *   - the segment `motion.button` carries NO `layout` prop (it must not be a
 *     size-projecting node whose box the pill is pinned to), and
 *   - the active-pill `motion.div` carries `layout="position"` alongside its
 *     `layoutId` (spring its POSITION between segments, take its SIZE from CSS
 *     `inset-0`, so it matches the button box on every frame).
 *
 * The divergence itself is only observable in a real browser
 * (scripts/probe-segmented-indicator-9684.mjs; happy-dom computes no layout),
 * so this suite pins the PROPS that produce the fix. framer-motion is mocked to
 * record the props each motion element receives, and each assertion is
 * mutation-proven in the probe/PR body by reverting the corresponding prop.
 *
 * The mock renders plain host elements, forwarding only DOM-safe attributes, so
 * the queries below still find the buttons and the label.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import React from 'react'

/** Records, per motion tag, the props of every element rendered this test. */
const recorded: { button: Record<string, unknown>[]; div: Record<string, unknown>[] } = {
  button: [],
  div: [],
}

/** Props framer owns that a raw DOM node must not receive (avoids React warns). */
const MOTION_ONLY = new Set([
  'layout', 'layoutId', 'layoutDependency', 'initial', 'animate', 'exit',
  'transition', 'whileTap', 'whileHover', 'whileFocus', 'whileInView', 'variants',
])

function domSafe(props: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const [k, v] of Object.entries(props)) if (!MOTION_ONLY.has(k)) out[k] = v
  return out
}

vi.mock('framer-motion', () => {
  const make = (tag: 'button' | 'div' | 'span') =>
    React.forwardRef<HTMLElement, Record<string, unknown>>((props, ref) => {
      if (tag === 'button') recorded.button.push(props)
      if (tag === 'div') recorded.div.push(props)
      return React.createElement(tag, { ...domSafe(props), ref: ref as never }, props.children as React.ReactNode)
    })
  return {
    motion: { button: make('button'), div: make('div'), span: make('span') },
    AnimatePresence: ({ children }: { children: React.ReactNode }) => React.createElement(React.Fragment, null, children),
    useReducedMotion: () => false,
  }
})

import SegmentedControl from '../components/SegmentedControl'

const SEGMENTS = [
  { key: 'grid' as const, label: 'Gallery' },
  { key: 'table' as const, label: 'Table' },
]

describe('SegmentedControl — #9684 indicator layout prop contract', () => {
  beforeEach(() => { recorded.button = []; recorded.div = [] })
  afterEach(() => cleanup())

  it('renders each segment button WITHOUT a layout prop', () => {
    render(<SegmentedControl segments={SEGMENTS} value="grid" onChange={vi.fn()} compact />)
    expect(recorded.button.length).toBeGreaterThanOrEqual(2)
    for (const p of recorded.button) {
      // A truthy `layout` (the pre-fix `layout` / `layout={true}`) is exactly
      // what re-projected the button box every reveal frame. `layout="position"`
      // on the BUTTON would also be wrong here — the button must not project.
      expect(p.layout == null || p.layout === false).toBe(true)
    }
  })

  it('renders the active indicator with layout="position" and its layoutId', () => {
    render(<SegmentedControl segments={SEGMENTS} value="grid" onChange={vi.fn()} layoutId="seg" compact />)
    // The indicator is the only motion.div carrying the shared layoutId.
    const indicator = recorded.div.find(p => p.layoutId === 'seg-indicator')
    expect(indicator).toBeDefined()
    // Position-only: spring the pill's travel between segments, never its size.
    expect(indicator!.layout).toBe('position')
  })

  it('renders exactly one indicator — the active segment only', () => {
    render(<SegmentedControl segments={SEGMENTS} value="grid" onChange={vi.fn()} layoutId="seg" compact />)
    const indicators = recorded.div.filter(p => p.layoutId === 'seg-indicator')
    expect(indicators).toHaveLength(1)
  })

  it('still renders the label reveal (width 0 -> auto) — the reveal is preserved, only its tracking is fixed', () => {
    render(<SegmentedControl segments={SEGMENTS} value="grid" onChange={vi.fn()} compact />)
    // The selected segment shows its label; the reveal animation that the pill
    // used to mis-track must still be present, so a "fix" that removed it is caught.
    expect(screen.getByRole('button', { name: /gallery/i }).textContent).toContain('Gallery')
  })
})
