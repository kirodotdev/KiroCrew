/**
 * A crewmate's reply never carries the "N files changed" chips (#30233).
 *
 * The mate's own writes (notes, scripts, its page) are not file work the user
 * reviews in its chat, so the crewmate `assistant` entry sets
 * `hideFileChanges` on the SHARED bubble (`renderAssistantBubble`). The SDK's
 * own row, which the main chat and worker sessions draw, keeps the chips,
 * pinned here too so the override cannot leak past the crewmate surface.
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
vi.mock('../components/FileChangeChips', () => ({
  default: ({ fileChanges }: { fileChanges: { path: string }[] }) => (
    <div data-testid="fcc">{fileChanges.map(fc => fc.path).join(',')}</div>
  ),
}))

afterEach(() => cleanup())

const WROTE: ChatMessage = {
  role: 'assistant',
  content: 'Saved my notes.',
  cls: '',
  ts: '2026-11-05T09:00:00Z',
  meta: { file_changes: [{ path: '/mate/notes.md', before: 'a', after: 'b' }] },
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
  const entry = resolveRenderer(WROTE, mergeRenderers(createTranscriptRenderers(opts)))
  expect(entry?.id).toBe('assistant')
  render(<>{entry!.render(WROTE, ctx([WROTE]))}</>)
}

describe("the file-change chips on a mate's reply", () => {
  it('are never drawn, and the reply text still is', () => {
    draw({ slot: 'member-radar', crewmate: { name: 'Radar' }, crewmateTranscript: () => [WROTE] })
    expect(screen.queryByTestId('fcc')).toBeNull()
    expect(screen.getByTestId('md')).toHaveTextContent('Saved my notes.')
  })

  it('still draw on an ordinary transcript (the override is the mate entry only)', () => {
    draw({ slot: 's1' })
    expect(screen.getByTestId('fcc')).toHaveTextContent('/mate/notes.md')
  })
})
