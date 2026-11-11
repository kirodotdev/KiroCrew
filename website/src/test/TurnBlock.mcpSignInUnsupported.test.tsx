/**
 * The session-start notice that an MCP server's sign-in cannot work on this
 * harness is never folded away with the steps of the turn it lands in. The
 * gateway writes it as a `notice` row tagged `meta.kind: 'mcp_sign_in_unsupported'`
 * before the answer, and under the default `collapseAllSteps` every untagged row
 * there folds into the steps pane, where nobody finds it.
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

const NOTICE = 'ℹ Agent kirocrew sets mcpServers.slack.oauth, which this backend does not receive.'
const ANSWER = 'There are no Slack tools in this session, so I cannot read that message.'

let seq = 0
const tool = (): ChatMessage => ({ role: 'tool', content: '🔧 Running: tool_search', ts: `${++seq}` })
const notice = (meta?: Record<string, unknown>): ChatMessage => ({
  role: 'notice', content: NOTICE, cls: 'msg msg-info', ts: `${++seq}`, ...(meta ? { meta } : {}),
})

function collapsedTurn(rows: ChatMessage[]) {
  const messages: ChatMessage[] = [{ role: 'user', content: 'Read my last Slack message.', ts: 'u' }, ...rows]
  const turn = applyRunningState(groupDisplayItems(messages), false).find(i => i.kind === 'turn')
  expect(turn).toBeTruthy()
  return render(<TurnBlock turn={turn as never} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
}

describe('a collapsed turn', () => {
  it('keeps the sign-in notice visible', () => {
    seq = 0
    const { container } = collapsedTurn([
      notice({ kind: 'mcp_sign_in_unsupported' }), tool(), tool(), { role: 'assistant', content: ANSWER, ts: '90' },
    ])
    const row = screen.getByTestId('row-notice')
    expect(row.textContent).toBe(NOTICE)
    expect(row.closest('[data-collapsed="true"]')).toBeNull()
    // Only the two tool rows fold.
    expect(container.querySelectorAll('[data-collapsed="true"]')).toHaveLength(2)
  })

  it('folds the same row when it carries no tag', () => {
    seq = 0
    collapsedTurn([notice(), tool(), tool(), { role: 'assistant', content: ANSWER, ts: '90' }])
    expect(screen.getByTestId('row-notice').closest('[data-collapsed="true"]')).not.toBeNull()
  })
})
