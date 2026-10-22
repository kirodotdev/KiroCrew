/**
 * The shared transcript renderers (the SDK's own user row, which split panes
 * draw, and the transcript renderers' user entry, which the mate threads draw)
 * label a legacy row another chat sent the way the main chat does. That row is
 * read from its text envelope only when its write time predates the sender
 * record, so both entries must hand the row's `ts` to the bubble.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import type { ChatMessage } from '../types'
import { mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))

afterEach(() => cleanup())

const LEGACY: ChatMessage = {
  role: 'user',
  content: '[sent by session chat-316-1792176321 via session_send]\n\nold delivery',
  cls: 'msg msg-u',
  ts: '2026-10-01T12:00:00Z',
  meta: { mid: 'm-legacy' },
} as ChatMessage

const ctx = (messages: ChatMessage[]): MessageRenderContext => ({
  index: 0,
  messages,
  running: false,
  key: 'k0',
  hideCardOwnedOAuth: false,
  autoDeniedIds: new Set<string>(),
  wrapper: (children) => children,
  row: (children) => children,
})

function draw(opts: Parameters<typeof createTranscriptRenderers>[0] | null) {
  const entry = resolveRenderer(LEGACY, mergeRenderers(opts ? createTranscriptRenderers(opts) : []))
  expect(entry?.id).toBe('user')
  render(<>{entry!.render(LEGACY, ctx([LEGACY]))}</>)
}

describe('a legacy row another chat sent, in the shared renderers', () => {
  it('the SDK user row (split panes) labels it and hides the envelope', () => {
    draw(null)
    expect(screen.getByTestId('send-provenance').textContent).toBe('From chat-316')
    expect(document.body.textContent).not.toContain('[sent by session')
  })

  it("the transcript renderers' user entry (mate threads) labels it too", () => {
    draw({ slot: 'member-radar', crewmate: { name: 'Radar' }, crewmateTranscript: () => [LEGACY] })
    expect(screen.getByTestId('send-provenance').textContent).toBe('From chat-316')
    expect(document.body.textContent).not.toContain('[sent by session')
  })
})
