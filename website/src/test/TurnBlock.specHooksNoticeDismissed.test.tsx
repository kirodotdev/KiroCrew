/**
 * A dismissed hooks-have-no-effect notice draws nothing, so a collapsed turn
 * neither folds nor counts it: "Worked through N steps" never promises a step
 * that opens onto an empty pane. Before the dismiss it is a step like any other
 * notice, and the count follows a dismiss made while the turn is on screen.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { act, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import TurnBlock from '../pages/chat/TurnBlock'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import { SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY } from '../pages/chat/SpecHooksNotice'
import type { TurnItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

const renderTurnItem = (it: TurnItem): ReactNode => (
  it.kind === 'single'
    ? <div key={`m${it.msg.ts}`} data-testid={`row-${it.msg.role}`}>{it.msg.content}</div>
    : <div key={`g${it.startIdx}`} data-testid="row-group" />
)

let seq = 0
const tool = (): ChatMessage => ({ role: 'tool', content: '🔧 Running: fs_read', ts: `${++seq}` })
const runNowhere = (): ChatMessage => ({
  role: 'notice',
  content: "ℹ️ Agent kirocrew has hooks on postToolUse. This backend does not run an agent's hooks, so they have no effect in this session.",
  cls: 'msg msg-info',
  ts: `${++seq}`,
  meta: { kind: 'spec_hooks', spec_hooks_notice: 'run_nowhere' },
})

function turnOf(): never {
  seq = 0
  const messages: ChatMessage[] = [
    { role: 'user', content: 'summarize the README', ts: `${++seq}` },
    runNowhere(), tool(),
    { role: 'assistant', content: 'The README explains how to install the gateway and start it.', ts: `${++seq}` },
  ]
  const turn = applyRunningState(groupDisplayItems(messages), false).find(i => i.kind === 'turn')
  expect(turn).toBeTruthy()
  return turn as never
}

const toggleText = () => screen.getByRole('button').textContent ?? ''

beforeEach(() => localStorage.clear())

describe('a collapsed turn holding the hooks notice', () => {
  it('counts the notice as a step until it is dismissed', () => {
    render(<TurnBlock turn={turnOf()} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
    expect(toggleText()).toMatch(/\b2\b/)
    expect(screen.getByTestId('row-notice').closest('[data-collapsed="true"]')).not.toBeNull()
  })

  it('leaves a dismissed notice out of the fold and the count', () => {
    localStorage.setItem(SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY, '1')
    render(<TurnBlock turn={turnOf()} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
    expect(toggleText()).toMatch(/\b1\b/)
    expect(toggleText()).not.toMatch(/\b2\b/)
  })

  it('recounts when another tab dismisses it while the turn is on screen', () => {
    render(<TurnBlock turn={turnOf()} renderItem={renderTurnItem} collapseAll disclosureKey="t" />)
    expect(toggleText()).toMatch(/\b2\b/)
    act(() => {
      localStorage.setItem(SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY, '1')
      window.dispatchEvent(new StorageEvent('storage', { key: SPEC_HOOKS_RUN_NOWHERE_DISMISSED_KEY, newValue: '1' }))
    })
    expect(toggleText()).toMatch(/\b1\b/)
  })
})
