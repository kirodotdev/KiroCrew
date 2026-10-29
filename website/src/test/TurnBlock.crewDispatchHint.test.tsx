/**
 * The Crew Mode dispatch hint is never folded with the dispatch that wrote it:
 * it tells the person where the crew member just started is listed, so a
 * collapsed turn still shows it in place. A plain notice folds as before.
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
    ? <div key={`m${it.msg.ts}`} data-testid={`row-${it.msg.role}-${String(it.msg.meta?.kind ?? 'plain')}`}>{it.msg.content}</div>
    : <div key={`g${it.startIdx}`} data-testid="row-group" />
)

let seq = 0
const tool = (): ChatMessage => ({ role: 'tool', content: '🔧 session_create', ts: `${++seq}` })
const notice = (kind?: string): ChatMessage => ({
  role: 'notice', content: kind ? 'Crew started work.' : 'Model fell back.', cls: 'msg msg-info', ts: `${++seq}`,
  ...(kind ? { meta: { kind } } : {}),
})

function turnOf(rows: ChatMessage[]) {
  seq = 0
  const messages: ChatMessage[] = [
    { role: 'user', content: 'go', ts: `${++seq}` },
    ...rows,
    { role: 'assistant', content: 'A crew member is on it and will report back when it has a cause.', ts: `${++seq}` },
  ]
  return applyRunningState(groupDisplayItems(messages), false).find(i => i.kind === 'turn')
}

describe('a collapsed Crew Mode turn', () => {
  it.each([['default', false], ['collapseAll', true]] as const)('keeps the dispatch hint visible (%s)', (_mode, collapseAll) => {
    const turn = turnOf([tool(), notice('crew_dispatch_hint'), tool()])
    expect(turn).toBeTruthy()
    render(<TurnBlock turn={turn as never} renderItem={renderTurnItem} collapseAll={collapseAll} disclosureKey="t" />)
    const row = screen.getByTestId('row-notice-crew_dispatch_hint')
    expect(row.closest('[data-collapsed="true"]')).toBeNull()
  })

  it('still folds a plain notice with the steps around it', () => {
    const turn = turnOf([tool(), notice(), tool()])
    render(<TurnBlock turn={turn as never} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
    expect(screen.getByTestId('row-notice-plain').closest('[data-collapsed="true"]')).not.toBeNull()
  })
})
