/**
 * A crewmate's reply offers Read aloud in its More menu, like a Sessions reply
 * (#29689). The crewmate `assistant` entry hands the host's `onSpeak` to the
 * shared bubble (`renderAssistantBubble`).
 */
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, fireEvent } from '@testing-library/react'
import type { ChatMessage } from '../types'
import { mergeRenderers, resolveRenderer, type MessageRenderContext } from '../app-sdk/messageRenderers'
import { createTranscriptRenderers } from '../pages/chat/transcriptRenderers'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))

afterEach(() => cleanup())

const REPLY: ChatMessage = {
  role: 'assistant',
  content: 'The nightly build is green again.',
  cls: '',
  ts: '2026-11-04T20:00:00Z',
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

function draw(onSpeak?: (content: string) => void) {
  const opts = { slot: 'member-radar', crewmate: { name: 'Radar' }, crewmateTranscript: () => [REPLY], onSpeak }
  const entry = resolveRenderer(REPLY, mergeRenderers(createTranscriptRenderers(opts)))
  expect(entry?.id).toBe('assistant')
  render(<>{entry!.render(REPLY, ctx([REPLY]))}</>)
}

const openMore = () =>
  fireEvent.pointerDown(screen.getByTitle('More actions'), { button: 0, ctrlKey: false, pointerType: 'mouse' })

describe("a mate's reply while the browser blocks playback", () => {
  it('shows its action row without hover', () => {
    const opts = { slot: 'member-radar', crewmate: { name: 'Radar' }, crewmateTranscript: () => [REPLY], onSpeak: vi.fn(), revealActions: true }
    const entry = resolveRenderer(REPLY, mergeRenderers(createTranscriptRenderers(opts)))
    render(<>{entry!.render(REPLY, ctx([REPLY]))}</>)
    expect(screen.getByTitle('More actions').closest('div.flex')).toHaveClass('!opacity-100')
  })
})

describe("Read aloud on a mate's reply", () => {
  it('is offered and reads the reply it is picked on', () => {
    const onSpeak = vi.fn()
    draw(onSpeak)
    openMore()
    fireEvent.click(screen.getByRole('menuitem', { name: 'Read aloud' }))
    expect(onSpeak).toHaveBeenCalledWith('The nightly build is green again.')
  })
})
