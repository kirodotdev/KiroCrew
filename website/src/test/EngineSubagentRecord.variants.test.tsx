import { describe, it, expect, vi } from 'vitest'
import { render, fireEvent, within } from '@testing-library/react'
import { renderAssistantBubble, type MessageRenderContext } from '../app-sdk/messageRenderers'
import type { ChatMessage } from '../types'

vi.mock('../components/MarkdownRenderer', () => ({
  default: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}))
vi.mock('../hooks/useSmoothStream', () => ({ useSmoothStream: (content: string) => content }))

/**
 * A regenerated reply drawn by the app SDK bubble browses its versions locally (the
 * host passes no `onSwitchVariant`). Each version shows the engine sub-agent record
 * of its own turn: the row's record is the newest version's, and an older version
 * carries its own beside its text (`variants[i].engine_subagents`).
 */
const record = (task: string) => ({
  children: [{ agent: 'kirocrew-helper', task, end: 'completed', tools: [], tool_count: 0 }],
})
const row = {
  role: 'assistant',
  content: 'new answer',
  ts: '2026-11-22T12:00:00Z',
  meta: { engine_subagents: record('new task') },
  variants: [
    { content: 'old answer', ts: '2026-11-22T11:00:00Z', engine_subagents: record('old task') },
    { content: 'new answer', ts: '2026-11-22T12:00:00Z' },
  ],
  variant_idx: 1,
} as unknown as ChatMessage
const ctx = { messages: [], index: 0, running: false } as unknown as MessageRenderContext

function shownTasks(view: ReturnType<typeof render>): string {
  const box = view.getByTestId('engine-subagent-record')
  fireEvent.click(within(box).getByTestId('engine-subagent-record-toggle'))
  const text = box.textContent ?? ''
  fireEvent.click(within(box).getByTestId('engine-subagent-record-toggle'))
  return text
}

describe('a locally browsed version shows its own engine sub-agent record', () => {
  it('the older version shows its record, and the newest its own again', () => {
    const view = render(<>{renderAssistantBubble(row, ctx)}</>)
    expect(view.getByTestId('md')).toHaveTextContent('new answer')
    expect(shownTasks(view)).toContain('new task')

    fireEvent.click(view.getByLabelText('Previous version'))
    expect(view.getByTestId('md')).toHaveTextContent('old answer')
    const older = shownTasks(view)
    expect(older).toContain('old task')
    expect(older).not.toContain('new task')

    fireEvent.click(view.getByLabelText('Next version'))
    expect(view.getByTestId('md')).toHaveTextContent('new answer')
    const newest = shownTasks(view)
    expect(newest).toContain('new task')
    expect(newest).not.toContain('old task')
  })
})
