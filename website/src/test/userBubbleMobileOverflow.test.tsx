/**
 * Regression tests for the user bubble that hung off the LEFT edge of a phone
 * viewport when it contained a fenced code block.
 *
 * The bubble is right-aligned inside a chain of fit-content flex items, and its
 * cap was a fixed 550px — so on a 390px viewport it stayed 550px wide and its
 * excess grew leftward to x = -180, where overflow-hidden ancestors and no
 * horizontal document scroll made it unreachable. The cap has to be
 * viewport-relative AND every box in the chain has to carry one, because a
 * percentage cap resolves against the parent's used width.
 *
 * happy-dom performs no layout, so these pin the class contract; the
 * narrow-width measurement lives in the capture script beside them.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import React from 'react'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import userEvent from '@testing-library/user-event'
import UserMessage from '../pages/chat/UserMessage'

const HERE = dirname(fileURLToPath(import.meta.url))
const src = (rel: string) => readFileSync(resolve(HERE, '..', rel), 'utf8')

/** Both hosts render the same UserMessage inside the same wrapper pair. */
const HOSTS = [
  ['ChatPage transcript controller', 'pages/chat/useChatPageTranscriptController.tsx'],
  ['app-sdk/ChatMessageList.tsx', 'app-sdk/ChatMessageList.tsx'],
] as const

function renderBubble(props: Partial<React.ComponentProps<typeof UserMessage>> = {}) {
  return render(
    <UserMessage
      content={'text\n\n```\nlong\n```'}
      renderContent={c => <pre>{c}</pre>}
      {...props}
    />,
  )
}

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('user bubble stays inside a phone viewport', () => {
  it('caps the bubble against the viewport, not at a fixed 550px', () => {
    const { container } = renderBubble()
    const bubble = container.querySelector('.message-bubble')!
    expect(bubble.className).toContain('max-w-[min(550px,100%)]')
    // A bare fixed cap is the defect: on a 390px viewport it holds the bubble
    // at 550px and pushes its left edge off-screen.
    expect(bubble.className).not.toMatch(/max-w-\[550px\]/)
  })

  it('caps the edit-mode box the same way', async () => {
    const user = userEvent.setup()
    const { container } = renderBubble({ canEdit: true, onEditResend: () => {}, messageIndex: 0, messageTs: '1' })
    await user.click(screen.getByLabelText('Edit & Resend'))
    const box = container.querySelector('.edit-grow')!
    expect(box.className).toContain('max-w-[min(550px,100%)]')
    expect(box.className).not.toMatch(/max-w-\[550px\]/)
  })

  it('caps its own wrapper, in both the read-only and the edit branch', async () => {
    const user = userEvent.setup()
    const { container } = renderBubble({ canEdit: true, onEditResend: () => {}, messageIndex: 0, messageTs: '1' })
    expect(container.querySelector('[data-role="user"]')!.className).toContain('max-w-full')
    await user.click(screen.getByLabelText('Edit & Resend'))
    expect(container.querySelector('[data-role="user"]')!.className).toContain('max-w-full')
  })

  it('caps the animation wrapper the steered-message path splices in', () => {
    const { container } = renderBubble({ meta: { steer: true } })
    // An uncapped box anywhere in the chain re-breaks the whole chain, and this
    // one only exists for a steered message. The wrapper carries the bubble's
    // own min(550px,100%) cap — the 100% arm is what keeps a phone viewport
    // capped, and the 550px arm is what keeps a long steered bubble on the
    // shared end edge in a wide column.
    const wrapper = container.querySelector('[data-role="user"] .relative')!
    expect(wrapper.className).toContain('max-w-[min(550px,100%)]')
  })

  it.each(HOSTS)('%s caps the transcript row wrapper it shares with the bubble', (_label, rel) => {
    const text = src(rel)
    const wrappers = text.match(/flex flex-col gap-0\.5 min-w-0 overflow-hidden[^`'"]*/g) ?? []
    expect(wrappers.length).toBeGreaterThan(0)
    for (const w of wrappers) expect(w).toContain('max-w-full')
  })
})
