/**
 * The Crew Mode dispatch notice is never folded away with the tool steps that
 * surround it: it exists to make a mid-turn `session_create` visible, so a
 * collapsed turn (the default `collapseAllSteps`) still shows the notice row
 * and its "Open Crew tab" button in place, between the steps before and after.
 *
 * Regression for the review finding on PR #25932: the row matches none of
 * `isVisibleInline`'s predicates, so without an `isAlwaysVisible` exemption it
 * collapses into the "Worked through N steps" pane and the feature the PR adds
 * is invisible on the default path.
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

let seq = 0
const tool = (): ChatMessage => ({ role: 'tool', content: '🔧 Running: session_create', ts: `${++seq}` })
const text = (s: string): ChatMessage => ({ role: 'assistant', content: s, ts: `${++seq}` })
const dispatchNotice = (): ChatMessage => ({
  role: 'notice', content: 'Crew members were started.', cls: 'msg msg-info', ts: `${++seq}`,
  meta: { kind: 'crew_dispatch_notice' },
})

describe('a collapsed turn', () => {
  it('keeps the crew dispatch notice row visible where it was written', () => {
    seq = 0
    const messages: ChatMessage[] = [
      { role: 'user', content: 'dispatch a reviewer', ts: `${++seq}` },
      tool(), dispatchNotice(), tool(),
      text('I started a reviewer crew member; it is listed in the Crew tab of the side panel.'),
    ]
    const items = applyRunningState(groupDisplayItems(messages), false)
    const turn = items.find(i => i.kind === 'turn')
    expect(turn).toBeTruthy()
    render(<TurnBlock turn={turn as never} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
    // The steps fold behind the toggle; the dispatch notice does not.
    expect(screen.getByRole('button').textContent).toMatch(/\d/)
    const row = screen.getByTestId('row-notice')
    expect(row.textContent).toBe('Crew members were started.')
    // Collapsed rows stay mounted inside a height-0 section; the notice is outside it.
    expect(row.closest('[data-collapsed="true"]')).toBeNull()
    expect(screen.getAllByTestId('row-tool').every(t => t.closest('[data-collapsed="true"]'))).toBe(true)
  })
})
