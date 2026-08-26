import { describe, it, expect } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import WorkflowCompletionCard, {
  isWorkflowCompletionMessage,
  parseWorkflowCompletion,
} from '../pages/chat/WorkflowCompletionCard'
import type { RootState } from '../store'
import type { ChatMessage } from '../types'

type ChatState = RootState['chat']

const FINISHED = [
  '[Workflow completion event]',
  'Workflow `kirocrew-bug-deep-dive` (wf_000001) → **finished**',
  '',
  'Result:',
  '```json',
  '{\n  "total_confirmed": 16\n}',
  '```',
  '',
  "Use workflow_result('wf_000001') for the full event stream.",
].join('\n')

function completionMsg(content = FINISHED, overrides: Partial<ChatMessage> = {}): ChatMessage {
  return { role: 'assistant', content, cls: '', ...overrides }
}

describe('WorkflowCompletionCard parsing/detection', () => {
  it('detects an injected completion assistant message', () => {
    expect(isWorkflowCompletionMessage(completionMsg())).toBe(true)
  })

  it('ignores a normal assistant message and non-assistant roles', () => {
    expect(isWorkflowCompletionMessage(completionMsg('hello world'))).toBe(false)
    expect(isWorkflowCompletionMessage(completionMsg(FINISHED, { role: 'tool' }))).toBe(false)
  })

  it('parses name, run id, status and strips the trailing tool hint from the body', () => {
    const p = parseWorkflowCompletion(FINISHED)!
    expect(p.name).toBe('kirocrew-bug-deep-dive')
    expect(p.runId).toBe('wf_000001')
    expect(p.status).toBe('finished')
    expect(p.body).toContain('Result:')
    expect(p.body).not.toContain('Use workflow_result')
  })

  it('returns null when the header does not match', () => {
    expect(parseWorkflowCompletion('[Workflow completion event]\nmalformed')).toBeNull()
  })

  it('does NOT detect a prefixed message whose header cannot be parsed (falls back, no data loss)', () => {
    // Regression: detection must be gated on a successful parse, else ChatPage
    // branches to a card that renders null and the completion vanishes.
    const bad = completionMsg('[Workflow completion event]\nWorkflow (no backticks) done')
    expect(isWorkflowCompletionMessage(bad)).toBe(false)
  })

  it('tolerates a newline inside the workflow name', () => {
    const withNewline = FINISHED.replace('kirocrew-bug-deep-dive', 'deep\ndive')
    const p = parseWorkflowCompletion(withNewline)!
    expect(p).not.toBeNull()
    expect(p.runId).toBe('wf_000001')
    expect(p.status).toBe('finished')
  })
})

describe('WorkflowCompletionCard rendering', () => {
  it('renders a compact header with name + status, result folded by default', () => {
    const store = createTestStore({ chat: {} as unknown as ChatState })
    renderWithProviders(<WorkflowCompletionCard message={completionMsg()} />, { store })
    expect(screen.getByText('kirocrew-bug-deep-dive')).toBeTruthy()
    expect(screen.getByText('finished')).toBeTruthy()
    // Collapsed by default: the toggle offers to reveal, raw JSON not shown yet.
    expect(screen.getByText('Show result')).toBeTruthy()
    expect(screen.queryByText(/total_confirmed/)).toBeNull()
  })

  it('expands to reveal the full result on toggle', () => {
    const store = createTestStore({ chat: {} as unknown as ChatState })
    renderWithProviders(<WorkflowCompletionCard message={completionMsg()} />, { store })
    fireEvent.click(screen.getByText('Show result'))
    expect(screen.getByText('Hide result')).toBeTruthy()
  })

  it('clicking Panel opens the Workflows side panel', () => {
    const store = createTestStore({ chat: {} as unknown as ChatState })
    renderWithProviders(<WorkflowCompletionCard message={completionMsg()} />, { store })
    fireEvent.click(screen.getByTitle('Open in the Workflows panel'))
    expect(store.getState().chat.activityOpen).toBe(true)
    expect(store.getState().chat.activityTab).toBe('workflows')
  })
})

describe('card containment', () => {
  // jsdom computes no layout, so these are class-list contracts, the same
  // convention TranscriptRowGeometry.test.tsx pins: what must hold is that the
  // classes carrying the containment survive on the exact elements they guard.
  const store = () => createTestStore({ chat: {} as unknown as ChatState })

  /** Render and expand: the result body is folded by default. */
  function renderExpanded() {
    renderWithProviders(<WorkflowCompletionCard message={completionMsg()} />, { store: store() })
    fireEvent.click(screen.getByText('Show result'))
    return screen.getByTestId('workflow-completion-body')
  }

  it('bounds the expanded body so a long workflow result scrolls inside the card', () => {
    const body = renderExpanded()
    // The literal bound, not a prefix match: a looser assertion would accept
    // max-h-0, which hides the result entirely. 24rem = the 384px bound the
    // capture runner asserts (BODY_MAX_PX in
    // capture-workflow-completion-overflow.mjs).
    expect(body.classList.contains('max-h-[24rem]')).toBe(true)
    expect(body.classList.contains('overflow-y-auto')).toBe(true)
    // Explicit, because a non-visible y-axis computes x's `visible` to `auto`:
    // without this the body is a two-axis scroller nothing needs (long inline
    // code and body text both wrap).
    expect(body.classList.contains('overflow-x-hidden')).toBe(true)
  })

  it('makes the scroll region keyboard-reachable and named after the headline', () => {
    const body = renderExpanded()
    // Unfocusable, an inner scroller cannot be scrolled by a keyboard at all.
    expect(body.getAttribute('tabindex')).toBe('0')
    expect(body.getAttribute('role')).toBe('region')
    const labelId = body.getAttribute('aria-labelledby')
    expect(labelId).toBeTruthy()
    expect(document.getElementById(labelId!)?.textContent).toContain('kirocrew-bug-deep-dive')
    // The ring must be inset: the card root's overflow-hidden clips anything
    // painted outside the box, and the global :focus-visible outline is
    // disabled, so a non-inset ring renders as no indicator at all.
    expect(body.classList.contains('focus-visible:ring-2')).toBe(true)
    expect(body.classList.contains('focus-visible:ring-inset')).toBe(true)
    expect(body.classList.contains('focus-visible:ring-accent')).toBe(true)
  })
})
