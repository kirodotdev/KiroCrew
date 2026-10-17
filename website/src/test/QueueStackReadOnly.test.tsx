/**
 * A QueueStack handed no action callbacks is a read-only view (ChatEmbed renders
 * the queue this way). Issue #19920: a reader must not mistake it for the
 * dashboard's interactive stack, so a single read-only card carries no pointer
 * cursor, and every read-only card says in a tooltip (catalog string) that it is
 * waiting to send and that edit / cancel live in the dashboard chat. The
 * dashboard's stack, which always passes callbacks, keeps its cursor and has no
 * such tooltip.
 */
import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'
import type { ChatMessage } from '../types'
import en from '../i18n/locales/en.json'

vi.mock('framer-motion', () => ({
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  motion: {
    div: React.forwardRef(
      // Drop the motion-only props so they do not land on the DOM node.
      ({ children, animate: _a, transition: _t, initial: _i, exit: _e, ...props }: Record<string, unknown> & { children?: React.ReactNode }, ref: React.Ref<HTMLDivElement>) => (
        <div ref={ref} {...(props as React.HTMLAttributes<HTMLDivElement>)}>{children}</div>
      ),
    ),
  },
  useMotionValue: () => ({ set: vi.fn(), get: () => 0, jump: vi.fn() }),
  useSpring: () => ({ set: vi.fn(), get: () => 0, jump: vi.fn() }),
}))

import QueueStack from '../components/QueueStack'

const HINT = en.components.queueStack.read_only_waiting_to_send

function makeMsg(id: string, content: string): ChatMessage {
  return { role: 'queued', content, meta: { queueId: id } } as ChatMessage
}

/** The stack's clickable box: the parent of every queue card's motion wrapper. */
function stackBox(): HTMLElement {
  const card = screen.getAllByTestId('queue-card')[0]
  return card.parentElement!.parentElement!
}

describe('QueueStack read-only cue (#19920)', () => {
  it('ships the hint as English catalog copy naming send, edit and cancel', () => {
    expect(HINT).toMatch(/waiting to send/i)
    expect(HINT).toMatch(/edit/i)
    expect(HINT).toMatch(/cancel/i)
  })

  it('a single read-only card has no pointer cursor and carries the hint', () => {
    render(<QueueStack messages={[makeMsg('q1', 'check the logs')]} fuseBelow={false} />)
    expect(stackBox().className).not.toMatch(/cursor-pointer/)
    const card = screen.getByTestId('queue-card')
    expect(card).toHaveAttribute('title', HINT)
    // The tooltip is pointer-only, so a screen reader gets the same words as
    // text, once, outside the stack.
    expect(screen.getByTestId('queue-readonly-hint')).toHaveTextContent(HINT)
    expect(stackBox()).not.toContainElement(screen.getByTestId('queue-readonly-hint'))
  })

  it('a read-only stack of several cards stays expandable and hints on every card', () => {
    render(<QueueStack messages={[makeMsg('q1', 'one'), makeMsg('q2', 'two')]} fuseBelow={false} />)
    expect(stackBox().className).toMatch(/cursor-pointer/)
    for (const card of screen.getAllByTestId('queue-card')) expect(card).toHaveAttribute('title', HINT)
    // The expand button's accessible name (its text content) must not carry
    // the hint once per card: the screen-reader copy sits outside it, once.
    expect(stackBox()).not.toHaveTextContent(HINT)
    expect(screen.getAllByTestId('queue-readonly-hint')).toHaveLength(1)
  })

  it('leaves the dashboard\'s interactive stack unchanged: pointer cursor, no hint', () => {
    render(<QueueStack messages={[makeMsg('q1', 'hello')]} onCancel={vi.fn()} onEdit={vi.fn()} onInterrupt={vi.fn()} />)
    expect(stackBox().className).toMatch(/cursor-pointer/)
    const card = screen.getByTestId('queue-card')
    expect(card).not.toHaveAttribute('title')
    expect(screen.queryByTestId('queue-readonly-hint')).toBeNull()
  })

  it('wears the glass-warn-fg text colour that carries the light-theme contrast fix', () => {
    render(<QueueStack messages={[makeMsg('q1', 'hello')]} />)
    const card = screen.getByTestId('queue-card')
    expect(card.className).toMatch(/\bglass-warn-fg\b/)
    expect(card.className).not.toMatch(/\btext-warn\b/)
  })
})
