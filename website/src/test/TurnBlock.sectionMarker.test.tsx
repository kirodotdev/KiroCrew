/**
 * A section marker is never folded away with the steps around it. It marks the
 * boundary between two units of work, so a collapsed or fan-out-folded turn that
 * hid it would show no boundary at all.
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
const tool = (): ChatMessage => ({ role: 'tool', content: '🔧 Running: grep', ts: `${++seq}` })
const text = (s: string): ChatMessage => ({ role: 'assistant', content: s, ts: `${++seq}` })
const marker = (): ChatMessage => ({
  role: 'section_marker', content: '— End of section: item-41 —', ts: `${++seq}`,
  meta: { label: 'item-41' },
})

describe('a collapsed turn', () => {
  it('keeps a section marker visible where it was written', () => {
    seq = 0
    const messages: ChatMessage[] = [
      { role: 'user', content: 'review the queue', ts: `${++seq}` },
      tool(), marker(), tool(),
      text('item-41 is done and item-42 is next in the queue.'),
    ]
    const items = applyRunningState(groupDisplayItems(messages), false)
    const turn = items.find(i => i.kind === 'turn')
    expect(turn).toBeTruthy()
    const { container } = render(<TurnBlock turn={turn as never} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
    const row = screen.getByTestId('row-section_marker')
    expect(row.closest('[data-collapsed="true"]')).toBeNull()
    const turnItems = (turn as Extract<typeof turn, { kind: 'turn' }>).items
    const toolRows = turnItems.flatMap((it, i) => (it.kind === 'single' && it.msg.role === 'tool' ? [i] : []))
    const foldRows = container.querySelectorAll('[data-fold-row]')
    expect(toolRows.length).toBe(2)
    expect(toolRows.every(i => foldRows[i].getAttribute('data-collapsed') === 'true')).toBe(true)
  })
})
