/**
 * A crewmate's chat colours its two speakers like iMessage (#17839): the
 * user's bubble is filled with the theme accent, the crewmate's with a neutral
 * gray. The user half is `tone="accent"` on UserMessage; the crewmate half is
 * `crewmateBubbleClass` (pinned in crewmateBubbles.test.ts). This pins the
 * user half and the single seam that turns it on: the crewmate `user` entry
 * of `createTranscriptRenderers`.
 *
 * happy-dom performs no layout and resolves no CSS, so the pin is the class
 * contract — the `user-bubble-accent` hook `index.css` keys its token
 * redefinition on, and the `bg-accent` / `text-accent-fg` utilities. The
 * measured contrast per theme is in the issue; the real-browser capture is
 * `scripts/capture-crewmate-run.mjs`.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import React from 'react'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import UserMessage from '../pages/chat/UserMessage'
import { mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'
import type { ChatMessage } from '../types'

const HERE = dirname(fileURLToPath(import.meta.url))
const INDEX_CSS = readFileSync(resolve(HERE, '..', 'index.css'), 'utf8')

const bubbleOf = (container: HTMLElement) => container.querySelector('.message-bubble.msg-content') as HTMLElement

function renderBubble(props: Partial<React.ComponentProps<typeof UserMessage>> = {}) {
  const view = render(<UserMessage content="PR #16974 status? see `pr-readiness.yml`" renderContent={c => <p>{c}</p>} {...props} />)
  return bubbleOf(view.container)
}

const ctx = (over: Partial<MessageRenderContext> = {}): MessageRenderContext => ({
  index: 0,
  messages: [],
  running: false,
  key: 'k0',
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: children => children,
  row: children => children,
  ...over,
})

const userRow: ChatMessage = { role: 'user', content: 'Anything new on the nightly?', cls: 'msg msg-u', ts: '2026-10-07T07:40:00Z' } as ChatMessage

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('UserMessage tone', () => {
  it('default keeps the neutral surface the single-chat page has always drawn', () => {
    const el = renderBubble()
    expect(el.classList.contains('user-bubble')).toBe(true)
    expect(el.classList.contains('bg-card')).toBe(true)
    expect(el.classList.contains('text-card-fg')).toBe(true)
    expect(el.classList.contains('user-bubble-accent')).toBe(false)
    expect(el.classList.contains('bg-accent')).toBe(false)
  })

  it('accent fills the bubble with the theme accent and keeps the theme hook', () => {
    const el = renderBubble({ tone: 'accent' })
    expect(el.classList.contains('bg-accent')).toBe(true)
    expect(el.classList.contains('text-accent-fg')).toBe(true)
    expect(el.classList.contains('user-bubble-accent')).toBe(true)
    // Still a `.user-bubble`: the pinned-prompt stand-in and theme rules key on it.
    expect(el.classList.contains('user-bubble')).toBe(true)
    expect(el.classList.contains('bg-card')).toBe(false)
  })

  it('a steer drawn WITH its badge keeps the steer tint even when accent is asked for', () => {
    // The accent fill is for a surface that hides the steer badge; the
    // single-chat page's steer tint is a different statement and must not be
    // overridden by the tone.
    const el = renderBubble({ tone: 'accent', meta: { steer: true, steerState: 'settled' } })
    expect(el.classList.contains('bg-accent-subtle')).toBe(true)
    expect(el.classList.contains('user-bubble-accent')).toBe(false)
  })
})

describe("the crewmate chat's user entry", () => {
  it('turns the accent tone on, and only when a crewmate is drawn', () => {
    const crewmate = { name: 'kirocrew-radar', label: 'Radar' }
    const withCrewmate = mergeRenderers(createTranscriptRenderers({ slot: 's1', hideSteerBadge: true, crewmate, crewmateTranscript: [userRow] }))
    const entry = resolveRenderer(userRow, withCrewmate)!
    expect(entry.id).toBe('user')
    const { container } = render(<>{entry.render(userRow, ctx({ messages: [userRow] }))}</>)
    expect(bubbleOf(container).classList.contains('user-bubble-accent')).toBe(true)

    // The steer badge hidden on its own (no crewmate) is not the iMessage pairing.
    const badgeOnly = mergeRenderers(createTranscriptRenderers({ slot: 's1', hideSteerBadge: true }))
    const plain = render(<>{resolveRenderer(userRow, badgeOnly)!.render(userRow, ctx({ messages: [userRow] }))}</>)
    expect(bubbleOf(plain.container).classList.contains('user-bubble-accent')).toBe(false)
    expect(bubbleOf(plain.container).classList.contains('bg-card')).toBe(true)
  })
})

describe('the index.css hook', () => {
  it('scopes the crewmate bubble\'s nested surfaces one step off its --bg-hover fill', () => {
    // Kiro-light paints inline code with var(--bg-hover), the fill itself; `hover:bg-bg-hover`
    // controls do the same. Without this scope they vanish inside the bubble (Design review on #17918).
    expect(INDEX_CSS).toMatch(/:has\(>\.message-bubble\.crewmate-bubble\)\{--crewmate-fill:var\(--bg-hover\)\}/)
    const at = INDEX_CSS.indexOf('.message-bubble.crewmate-bubble{')
    const block = INDEX_CSS.slice(at, INDEX_CSS.indexOf('}', at))
    expect(block).toMatch(/background-color:var\(--crewmate-fill\)/)
    for (const token of ['--bg-hover', '--bg-elevated', '--card']) {
      expect(block, `${token} must be re-pointed off the fill`).toMatch(new RegExp(`${token}:color-mix\\(in srgb,var\\(--text\\) \\d+%,var\\(--crewmate-fill\\)\\)`))
    }
    // Kiro dark's code patch is a literal (#28242e == its --bg-hover), which no token reaches.
    expect(INDEX_CSS).toMatch(/\[data-theme="kiro-dark"\] \.message-bubble\.crewmate-bubble :not\(pre\)>code\{background:var\(--bg-hover\)\}/)
  })

  it('redefines the theme tokens inside the accent bubble so every descendant inherits accent-fg colours', () => {
    const at = INDEX_CSS.indexOf('.msg-content.user-bubble.user-bubble-accent{')
    expect(at).toBeGreaterThan(-1)
    const block = INDEX_CSS.slice(at, INDEX_CSS.indexOf('}', at))
    // The fill reads a snapshot taken on the PARENT, because `--accent` is re-pointed in this block.
    expect(INDEX_CSS).toMatch(/:has\(>\.msg-content\.user-bubble\.user-bubble-accent\)\{--bubble-accent:var\(--accent\)\}/)
    expect(block).toMatch(/background-color:var\(--bubble-accent\);color:var\(--accent-fg\)/)
    // Every token the user-message subtree is known to read (QuoteCard, LinkChip, InlineCode, chips).
    for (const token of ['--text', '--text-strong', '--card-fg', '--muted', '--accent', '--accent-subtle', '--border', '--bg', '--bg-elevated', '--bg-hover', '--card']) {
      expect(block, `${token} must be redefined inside the accent bubble`).toMatch(new RegExp(`${token}:`))
    }
    expect(block).toMatch(/--accent:var\(--accent-fg\)/)
    expect(block).toMatch(/--muted:color-mix\(in srgb,var\(--accent-fg\) 90%/)
    // Surfaces darken the fill (translucent black), never lighten it toward the text colour.
    expect(block).toMatch(/--bg-hover:rgba\(0,0,0,\.18\)/)
  })

  it('keeps a link signal and reaches the Kiro literals tokens cannot', () => {
    const rules = INDEX_CSS.split('\n').filter(l => l.includes('.msg-content.user-bubble.user-bubble-accent'))
    const joined = rules.join('\n')
    // Kiro dark paints code and links with literal purples, which no token redefinition reaches.
    expect(joined).toMatch(/user-bubble-accent :not\(pre\)>code\{background:var\(--bg-hover\);color:var\(--accent-fg\)\}/)
    // Colour alone no longer says "link" on an accent fill, so the underline and the
    // link pill's patch + outline are the link signal (UX review on #17918).
    expect(joined).toMatch(/user-bubble-accent a\{color:var\(--accent-fg\);text-decoration:underline/)
    expect(joined).toMatch(/user-bubble-accent span\.group:has\(>a\)\{background:var\(--bg-hover\);border-color:var\(--border\)\}/)
    expect(joined).toMatch(/user-bubble-accent span\.group>a\.no-underline\{text-decoration:underline\}/)
    // Order settles the (0,3,2) tie with the Kiro chip rules: the hook comes AFTER them.
    const hookAt = INDEX_CSS.indexOf('.msg-content.user-bubble.user-bubble-accent{')
    const chipAt = INDEX_CSS.lastIndexOf('code[data-chip-action="navigate"]{')
    expect(hookAt).toBeGreaterThan(chipAt)
  })
})
