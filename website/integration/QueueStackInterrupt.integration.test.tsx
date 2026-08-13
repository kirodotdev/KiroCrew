import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import React from 'react'
import type { ChatMessage } from '../src/types'

// Mock framer-motion to render children directly
vi.mock('framer-motion', () => ({
  AnimatePresence: ({ children }: any) => <>{children}</>,
  motion: { div: React.forwardRef(({ children, ...props }: any, ref: any) => <div ref={ref} {...props}>{children}</div>) },
  useMotionValue: () => ({ set: vi.fn(), get: () => 0, jump: vi.fn() }),
  useSpring: () => ({ set: vi.fn(), get: () => 0, jump: vi.fn() }),
}))

import QueueStack from '../src/components/QueueStack'

function makeMsg(id: string, content: string): ChatMessage {
  return { role: 'user', content, meta: { queueId: id } } as ChatMessage
}

describe('QueueStack interrupt button', () => {
  it('renders ⚡ Send now button when onInterrupt provided', () => {
    const onInterrupt = vi.fn()
    render(<QueueStack messages={[makeMsg('q1', 'hello')]} onInterrupt={onInterrupt} />)
    expect(screen.getByLabelText('Send now')).toBeInTheDocument()
  })

  it('does not render ⚡ button when onInterrupt is undefined', () => {
    render(<QueueStack messages={[makeMsg('q1', 'hello')]} />)
    expect(screen.queryByLabelText('Send now')).not.toBeInTheDocument()
  })

  it('calls onInterrupt with queueId when ⚡ clicked', async () => {
    const onInterrupt = vi.fn()
    render(<QueueStack messages={[makeMsg('q1', 'hello')]} onInterrupt={onInterrupt} />)
    await userEvent.click(screen.getByLabelText('Send now'))
    expect(onInterrupt).toHaveBeenCalledWith('q1')
  })

  it('renders cancel button with aria-label', () => {
    const onCancel = vi.fn()
    render(<QueueStack messages={[makeMsg('q1', 'hello')]} onCancel={onCancel} />)
    expect(screen.getByLabelText('Cancel queued message')).toBeInTheDocument()
  })
})
