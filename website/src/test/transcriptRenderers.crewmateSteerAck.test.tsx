/**
 * A crewmate's reply never carries the "Steered" chip (#17838).
 *
 * In a DM with one named peer every send while it works is a steer, so the
 * `[STEERING …]` ack kiro-cli emits would close nearly every reply with the
 * mechanics the surface hides. The crewmate `assistant` entry forces
 * `suppressSteerAck` on the SHARED bubble (`renderAssistantBubble`), so the
 * chip is gone while the marker is still stripped from the prose. The SDK's
 * own row — what the main chat and split panes draw — keeps the chip, pinned
 * here too so the override cannot leak past the crewmate surface.
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup } from '@testing-library/react'
import type { ChatMessage } from '../types'
import { mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))

afterEach(() => cleanup())

const ACKED: ChatMessage = {
  role: 'assistant',
  content: 'Picked the job up [STEERING steer-ab12: switched to the new issue]',
  cls: '',
  ts: '2026-10-07T20:00:00Z',
} as ChatMessage

/** Identity `row`/`wrapper` so the entry returns the bubble itself. */
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

function draw(opts: Parameters<typeof createTranscriptRenderers>[0]) {
  const entry = resolveRenderer(ACKED, mergeRenderers(createTranscriptRenderers(opts)))
  expect(entry?.id).toBe('assistant')
  render(<>{entry!.render(ACKED, ctx([ACKED]))}</>)
}

describe("the Steered chip on a crewmate's reply", () => {
  it('is never drawn, and the raw marker is still stripped', () => {
    draw({ slot: 'member-radar', crewmate: { name: 'Radar' }, crewmateTranscript: [ACKED] })
    expect(screen.queryByText('Steered')).toBeNull()
    expect(screen.queryByText(/switched to the new issue/)).toBeNull()
    const md = screen.getByTestId('md')
    expect(md).toHaveTextContent('Picked the job up')
    expect(md).not.toHaveTextContent('[STEERING')
  })

  it('still draws on an ordinary transcript (the override is the crewmate entry only)', () => {
    draw({ slot: 's1' })
    expect(screen.getByText('Steered')).toBeInTheDocument()
    expect(screen.getByText(/switched to the new issue/)).toBeInTheDocument()
    expect(screen.getByTestId('md')).not.toHaveTextContent('[STEERING')
  })
})
