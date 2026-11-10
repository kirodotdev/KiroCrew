/**
 * An agent's greeting (`welcomeMessage`) is never folded away with the steps of
 * the turn it lands in. The gateway writes it as a `notice` row tagged
 * `meta.kind: 'agent_welcome'` when the agent starts answering: on a new chat
 * that is the first turn, right after the prompt, and under the default
 * `collapseAllSteps` every row before the answer folds unless it is exempt.
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import TurnBlock from '../pages/chat/TurnBlock'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import type { TurnItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

const renderTurnItem = (it: TurnItem): ReactNode => (
  it.kind === 'single'
    ? <div key={`m${it.msg.ts}`} data-testid={`row-${it.msg.role}`}>{it.msg.content}</div>
    : <div key={`g${it.startIdx}`} data-testid="row-group" />
)

const GREETING = 'Tell me which deck to build and I will draft the outline.'
const ANSWER = 'Here is a three-slide outline about tide pools, one idea per slide.'

let seq = 0
const tool = (): ChatMessage => ({ role: 'tool', content: '🔧 Running: read_file', ts: `${++seq}` })
const notice = (meta?: Record<string, unknown>): ChatMessage => ({
  role: 'notice', content: GREETING, cls: 'msg msg-info', ts: `${++seq}`, ...(meta ? { meta } : {}),
})
const welcome = (): ChatMessage => notice({ kind: 'agent_welcome' })

function collapsedTurn(rows: ChatMessage[]) {
  const messages: ChatMessage[] = [{ role: 'user', content: 'Build me a deck.', ts: 'u' }, ...rows]
  const turn = applyRunningState(groupDisplayItems(messages), false).find(i => i.kind === 'turn')
  expect(turn).toBeTruthy()
  return render(<TurnBlock turn={turn as never} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
}

describe('a collapsed turn', () => {
  it("keeps the agent's greeting visible at the top of the first turn", () => {
    seq = 0
    const { container } = collapsedTurn([welcome(), tool(), tool(), { role: 'assistant', content: ANSWER, ts: '90' }])
    expect(screen.getByRole('button').textContent).toMatch(/\d/)
    const row = screen.getByTestId('row-notice')
    expect(row.textContent).toBe(GREETING)
    expect(row.closest('[data-collapsed="true"]')).toBeNull()
    // Only the two tool rows fold.
    expect(container.querySelectorAll('[data-collapsed="true"]')).toHaveLength(2)
  })

  it('keeps a greeting that lands mid-turn, after an agent switch, visible too', () => {
    seq = 0
    collapsedTurn([tool(), welcome(), tool(), { role: 'assistant', content: ANSWER, ts: '90' }])
    expect(screen.getByTestId('row-notice').closest('[data-collapsed="true"]')).toBeNull()
  })

  it('folds the same row when it carries no agent_welcome tag', () => {
    // The tag is the whole exemption: an untagged notice before the answer folds.
    seq = 0
    collapsedTurn([notice(), tool(), tool(), { role: 'assistant', content: ANSWER, ts: '90' }])
    expect(screen.queryByTestId('row-notice')).not.toBeNull()
    expect(screen.getByTestId('row-notice').closest('[data-collapsed="true"]')).not.toBeNull()
  })
})
