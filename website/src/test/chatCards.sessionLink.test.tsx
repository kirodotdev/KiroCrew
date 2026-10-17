/**
 * Regression test: a `/chat?sid=…` link inside a completion CARD switches session.
 *
 * Both cards render `MarkdownRenderer` directly, like the note row, so each
 * needed its own copy of the three session props. Without them
 * `resolveSessionChip` refuses at its first guard and the link gains
 * `target="_blank"` — an agent-composed hand-off link in a sub-agent payload or
 * a workflow result opened a second browser tab.
 *
 * The REAL renderer runs here: the assertion is on the anchor's attributes, and
 * each case is paired with the same card rendered WITHOUT the triple, so a card
 * that dropped `target` unconditionally would fail the control.
 */
import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import SubagentCompletionCard from '../pages/chat/SubagentCompletionCard'
import WorkflowCompletionCard from '../pages/chat/WorkflowCompletionCard'
import type { ChatMessage } from '../types'

const THERE = 'chat-2-1788000001'
const TITLE = 'Release notes draft'
const roster = (): ReadonlyMap<string, string> => new Map([[THERE, TITLE]])
const LINK = `[${TITLE}](/chat?sid=${THERE})`

/** A FAILED single completion: the card opens expanded, so the body renders. */
const SUBAGENT: ChatMessage = {
  role: 'subagent',
  cls: '',
  content: [
    '[Subagent completion event]',
    'Agent `53e3e5eb` (kirocrew) failed ❌',
    'Task: Draft the release notes',
    '',
    `Handed off. Next: ${LINK}`,
  ].join('\n'),
}

const WORKFLOW: ChatMessage = {
  role: 'assistant',
  cls: '',
  content: [
    '[Workflow completion event]',
    'Workflow `release-notes` (wf_abc123) → **finished**',
    '',
    `Handed off. Next: ${LINK}`,
  ].join('\n'),
}

const anchor = () => screen.getByText(TITLE).closest('a')!

describe('a sub-agent completion card carries the session wiring', () => {
  it('switches in place instead of opening a tab', () => {
    const onSessionOpen = vi.fn()
    renderWithProviders(
      <SubagentCompletionCard
        message={SUBAGENT}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        activeSession="chat-1-1788000000"
      />,
    )
    const a = anchor()
    expect(a).not.toHaveAttribute('target')
    expect(a.getAttribute('title')).toContain(TITLE)

    fireEvent.click(a, { button: 0 })
    expect(onSessionOpen).toHaveBeenCalledWith(THERE)
  })

  it('stays an external link without the triple, so the card is not always dropping target', () => {
    renderWithProviders(<SubagentCompletionCard message={SUBAGENT} />)
    expect(anchor()).toHaveAttribute('target', '_blank')
  })
})

describe('a workflow completion card carries the session wiring', () => {
  /** The body is folded by default; only the aria-expanded control opens it. */
  const expand = () => fireEvent.click(screen.getByRole('button', { expanded: false }))

  it('switches in place instead of opening a tab', () => {
    const onSessionOpen = vi.fn()
    renderWithProviders(
      <WorkflowCompletionCard
        message={WORKFLOW}
        onSessionOpen={onSessionOpen}
        sessions={roster()}
        activeSession="chat-1-1788000000"
      />,
    )
    expand()
    const a = anchor()
    expect(a).not.toHaveAttribute('target')
    expect(a.getAttribute('title')).toContain(TITLE)

    fireEvent.click(a, { button: 0 })
    expect(onSessionOpen).toHaveBeenCalledWith(THERE)
  })

  it('stays an external link without the triple, so the card is not always dropping target', () => {
    renderWithProviders(<WorkflowCompletionCard message={WORKFLOW} />)
    expand()
    expect(anchor()).toHaveAttribute('target', '_blank')
  })
})
