/**
 * QueueStack reorder buttons - frontend wiring for
 * https://github.com/kirodotdev/KiroCrew/issues/2241
 *
 * Cards are absolutely-positioned and framer-motion-animated, so the reorder
 * affordance is a pair of move buttons (not dnd-kit sortable - two transform
 * systems would fight over the same elements). Index 0 runs first and renders
 * at the BOTTOM of the expanded stack: "run sooner" = ArrowDown, "run later"
 * = ArrowUp. Buttons only render when expanded with 2+ messages.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import React from 'react'
import type { ChatMessage } from '../src/types'

// Mock framer-motion to render children directly (same pattern as
// QueueStackInterrupt.integration.test.tsx).
vi.mock('framer-motion', () => ({
  AnimatePresence: ({ children }: any) => <>{children}</>,
  motion: {
    div: React.forwardRef(({ children, ...props }: any, ref: any) => (
      <div ref={ref} {...props}>{children}</div>
    )),
  },
  useMotionValue: () => ({ set: vi.fn(), get: () => 0, jump: vi.fn() }),
  useSpring: () => ({ set: vi.fn(), get: () => 0, jump: vi.fn() }),
}))

import QueueStack from '../src/components/QueueStack'

function makeMsg(id: string, content: string): ChatMessage {
  return { role: 'queued', content, cls: 'msg msg-queued', meta: { queueId: id } } as ChatMessage
}

const THREE = [makeMsg('q1', 'first'), makeMsg('q2', 'second'), makeMsg('q3', 'third')]

/** The stack starts collapsed with 2+ cards; click the front card to expand. */
function expand(container: HTMLElement) {
  const toggle = container.querySelector('[role="button"][aria-expanded]')
  expect(toggle).not.toBeNull()
  fireEvent.click(toggle!)
}

describe('QueueStack reorder buttons', () => {
  it('does not render arrows when onReorder is not provided', () => {
    const { container } = render(<QueueStack messages={THREE} />)
    expand(container)
    expect(screen.queryAllByLabelText('Run sooner')).toHaveLength(0)
    expect(screen.queryAllByLabelText('Run later')).toHaveLength(0)
  })

  it('does not render arrows for a single queued message', () => {
    render(<QueueStack messages={[makeMsg('q1', 'only')]} onReorder={vi.fn()} />)
    expect(screen.queryAllByLabelText('Run sooner')).toHaveLength(0)
  })

  it('does not render arrows while collapsed', () => {
    render(<QueueStack messages={THREE} onReorder={vi.fn()} />)
    expect(screen.queryAllByLabelText('Run sooner')).toHaveLength(0)
  })

  it('renders arrows on every card when expanded, with boundary buttons disabled', () => {
    const { container } = render(<QueueStack messages={THREE} onReorder={vi.fn()} />)
    expand(container)
    const sooner = screen.getAllByLabelText('Run sooner')
    const later = screen.getAllByLabelText('Run later')
    expect(sooner).toHaveLength(3)
    expect(later).toHaveLength(3)
    // Cards render in messages order: index 0 (runs first) cannot move sooner,
    // last index cannot move later.
    expect(sooner[0]).toBeDisabled()
    expect(sooner[1]).not.toBeDisabled()
    expect(later[2]).toBeDisabled()
    expect(later[0]).not.toBeDisabled()
  })

  it('calls onReorder with the queueId and direction', () => {
    const onReorder = vi.fn()
    const { container } = render(<QueueStack messages={THREE} onReorder={onReorder} />)
    expand(container)
    fireEvent.click(screen.getAllByLabelText('Run sooner')[1])
    expect(onReorder).toHaveBeenCalledWith('q2', 'next')
    fireEvent.click(screen.getAllByLabelText('Run later')[0])
    expect(onReorder).toHaveBeenCalledWith('q1', 'later')
  })

  it('clicking an arrow does not collapse the stack (stopPropagation)', () => {
    const { container } = render(<QueueStack messages={THREE} onReorder={vi.fn()} />)
    expand(container)
    fireEvent.click(screen.getAllByLabelText('Run later')[0])
    // Arrows still present = still expanded.
    expect(screen.getAllByLabelText('Run later')).toHaveLength(3)
  })
})
