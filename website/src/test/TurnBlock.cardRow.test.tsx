/**
 * A change card / guide offer is never folded away with the tool steps that
 * proposed it: only the user can confirm it, so a collapsed turn still shows
 * the card row in place, between the steps before and after it.
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
const tool = (): ChatMessage => ({ role: 'tool', content: '🔧 Running: propose_change', ts: `${++seq}` })
const text = (s: string): ChatMessage => ({ role: 'assistant', content: s, ts: `${++seq}` })
const card = (): ChatMessage => ({
  role: 'card', content: 'Shorter replies', ts: `${++seq}`,
  meta: { card: { surface: 'change', id: 'cc_1', slot: 's', kind: 'setting.change', title: 'Shorter replies', status: 'pending' } },
})

describe('a collapsed turn', () => {
  it('keeps the card row visible where it was proposed', () => {
    seq = 0
    const messages: ChatMessage[] = [
      { role: 'user', content: 'go', ts: `${++seq}` },
      tool(), text('Looking at your settings first, then I will propose the change.'), tool(), card(), tool(),
      text('I proposed a change to make replies shorter. Confirm it on the card above when you are ready.'),
    ]
    const items = applyRunningState(groupDisplayItems(messages), false)
    const turn = items.find(i => i.kind === 'turn')
    expect(turn).toBeTruthy()
    render(<TurnBlock turn={turn as never} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
    // The steps fold behind the toggle; the card does not.
    expect(screen.getByRole('button').textContent).toMatch(/\d/)
    const row = screen.getByTestId('row-card')
    expect(row.textContent).toBe('Shorter replies')
    // Collapsed rows stay mounted inside a height-0 section; the card is outside it.
    expect(row.closest('[data-collapsed="true"]')).toBeNull()
    expect(screen.getAllByTestId('row-tool').every(t => t.closest('[data-collapsed="true"]'))).toBe(true)
  })
})
