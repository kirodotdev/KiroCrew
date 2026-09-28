/**
 * Settings → Chat → Content Width governs the WHOLE transcript (#8398).
 *
 * The transcript row already clamps everything it renders to
 * `var(--mc-content-width)` — ChatPage and app-sdk/ChatMessageList set it from
 * `CONTENT_WIDTH[chatConfig.contentWidth]` — and agent output carries no cap of
 * its own, so it simply fills that column. The user's bubble used to carry a
 * PRIVATE `max-w-[min(550px,100%)]` cap at three sites (the read-only bubble,
 * the edit box and the steer wrapper), so a long prompt stayed a tall 550px
 * column at every setting value while the reply beside it followed the setting.
 *
 * The contract: every box in the bubble's shrink-wrap chain carries the SAME
 * percentage cap as the row wrapper and the root (`max-w-full`), so the bubble's
 * maximum IS the column — the setting moves it, and there is no second width
 * table. A short message still hugs its text (`w-fit`); only the maximum grows.
 * The pinned copy of the bubble (PinnedPrompt) is a pixel-for-pixel stand-in for
 * the hidden row, so its cap moves in lockstep.
 *
 * happy-dom performs no layout, so this pins the class contract; the measured
 * before/after is in the PR's pod screenshots.
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

/** Any arbitrary-value width cap in pixels: `max-w-[550px]`, `max-w-[min(550px,100%)]`, … */
const PIXEL_CAP = /max-w-\[[^\]]*\d+px[^\]]*\]/

const LONG_PROMPT = Array.from({ length: 6 }, (_, i) =>
  `Paragraph ${i + 1}: a multi-paragraph prompt that is long enough to hit any width cap the bubble carries.`,
).join('\n\n')

function renderBubble(props: Partial<React.ComponentProps<typeof UserMessage>> = {}) {
  return render(
    <UserMessage
      content={LONG_PROMPT}
      renderContent={c => <p>{c}</p>}
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

describe('the Content Width setting reaches the user bubble', () => {
  it('the bubble carries the column cap, not a private pixel cap', () => {
    const { container } = renderBubble()
    const bubble = container.querySelector('.message-bubble')!
    expect(bubble.className).toContain('max-w-full')
    expect(bubble.className).not.toMatch(PIXEL_CAP)
    // Only the MAXIMUM moves: a short message still shrink-wraps its text.
    expect(bubble.className).toContain('w-fit')
  })

  it('the edit box follows the same cap as the bubble it replaces', async () => {
    const user = userEvent.setup()
    const { container } = renderBubble({ canEdit: true, onEditResend: () => {}, messageIndex: 0, messageTs: '1' })
    await user.click(screen.getByLabelText('Edit & Resend'))
    const box = container.querySelector('.edit-grow')!
    expect(box.className).toContain('max-w-full')
    expect(box.className).not.toMatch(PIXEL_CAP)
    expect(box.className).toContain('w-fit')
  })

  it('the steer wrapper follows the same cap as the bubble it wraps', () => {
    const { container } = renderBubble({ meta: { steer: true } })
    const wrapper = container.querySelector('[data-role="user"] .relative')!
    const bubble = container.querySelector('.message-bubble')!
    expect(wrapper.className).toContain('max-w-full')
    expect(wrapper.className).not.toMatch(PIXEL_CAP)
    // The chain invariant: the wrapper and the bubble share ONE cap, so the
    // capped bubble never lands on the wrapper's left edge.
    const cap = (cls: string) => cls.split(/\s+/).filter(c => c.startsWith('max-w-'))
    expect(cap(wrapper.className)).toEqual(cap(bubble.className))
  })

  it('UserMessage owns no width table of its own', () => {
    const text = src('pages/chat/UserMessage.tsx')
    // The column is the only clamp: no pixel cap anywhere in the file, and no
    // private lookup of CONTENT_WIDTH — the setting reaches the bubble through
    // the row's `--mc-content-width`, exactly as it reaches agent output.
    expect(text).not.toMatch(PIXEL_CAP)
    expect(text).not.toMatch(/\b550\b/)
    expect(text).not.toMatch(/CONTENT_WIDTH/)
  })

  it('the pinned copy of the bubble keeps pixel parity with it', () => {
    const text = src('pages/chat/PinnedPrompt.tsx')
    // The card stands in for the hidden transcript row at hand-off; a card that
    // kept the old 550px cap would visibly change width the moment a long
    // prompt pins. Scoped to the card: the thumbnail's own `max-w-[160px]` is
    // an image size, not a bubble cap.
    expect(/data-testid="pinned-prompt"[\s\S]{0,1200}?className="pointer-events-auto max-w-full min-w-0"/.test(text)).toBe(true)
    expect(text).not.toMatch(/max-w-\[(?:min\()?550px/)
  })
})
