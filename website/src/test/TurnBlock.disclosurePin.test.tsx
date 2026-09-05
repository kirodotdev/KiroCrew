/**
 * TurnBlock disclosure state under running-flag churn.
 *
 * `turn.complete` is derived from the slot's running flag, which ChatPage
 * re-reconciles from EVERY slots broadcast. A broadcast that catches the slot
 * momentarily idle between tool calls flips `complete` true mid-turn, which
 * fires TurnBlock's auto-collapse. These tests lock in that an explicit user
 * click pins the disclosure state against that churn, while leaving the
 * automatic collapse intact for a group the user never touched.
 *
 * These tests drive the REAL pipeline (groupDisplayItems + applyRunningState +
 * TurnBlock) through ChatPage's own turn/loose dispatch, rather than poking
 * TurnBlock's props directly, so the turn objects under test are shaped exactly
 * the way production builds them.
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useEffect, useMemo, type ReactNode } from 'react'
import TurnBlock from '../pages/chat/TurnBlock'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import { virtualKeyFor } from '../pages/chat/ChatPageMessageContent'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

/** Mount counter, so a re-render (state preserved) is distinguishable from a
 *  remount (state destroyed). */
const mounts: Record<string, number> = {}
beforeEach(() => { for (const k of Object.keys(mounts)) delete mounts[k] })

function Probe({ id }: { id: string }) {
  useEffect(() => { mounts[id] = (mounts[id] ?? 0) + 1 }, [id])
  return <span data-testid={`probe-${id}`} />
}

/** Stable content-derived row identity, never the array index. */
const stableId = (it: TurnItem): string =>
  it.kind === 'single' ? `m${it.msg.ts}` : `g${it.startIdx}`

/** Mirrors ChatPage's renderTurnItem: a keyed wrapper around each row. */
const renderTurnItem = (it: TurnItem): ReactNode => (
  <div key={stableId(it)}><Probe id={stableId(it)} /></div>
)

/** Mirrors ChatPage's top-level dispatch: turns get a TurnBlock, loose items don't. */
function Transcript({ messages, running }: { messages: ChatMessage[]; running: boolean }) {
  const grouped = useMemo(() => groupDisplayItems(messages), [messages])
  const items: DisplayItem[] = applyRunningState(grouped, running)
  return (
    <>
      {items.map((item, i) => {
        const k = virtualKeyFor(item, i, (m: ChatMessage) => `m${m.ts}`)
        if (item.kind === 'turn') {
          return <div key={k}><TurnBlock turn={item} renderItem={renderTurnItem} /></div>
        }
        return <div key={k}>{renderTurnItem(item)}</div>
      })}
    </>
  )
}

let seq = 0
const tool = (): ChatMessage => ({ role: 'tool', content: '🔧 Running: shell', ts: `${++seq}` })
const text = (s: string): ChatMessage => ({ role: 'assistant', content: s, ts: `${++seq}` })
const user = (s: string): ChatMessage => ({ role: 'user', content: s, ts: `${++seq}` })

/** A turn long enough that flushTurn emits a `turn` object (items.length > 2). */
const aTurn = (): ChatMessage[] => { seq = 0; return [user('go'), tool(), text('a'), tool()] }

const label = () => screen.getByRole('button').textContent

describe('TurnBlock — user disclosure survives running-flag churn', () => {
  it('keeps the expand when a stale running:false frame lands mid-turn', () => {
    const m = aTurn()
    const { rerender } = render(<Transcript messages={m} running={false} />)
    fireEvent.click(screen.getByRole('button'))
    expect(label()).toContain('Hide')

    // Agent resumes: running -> flat branch, toggle hidden.
    rerender(<Transcript messages={m} running={true} />)
    expect(screen.queryAllByRole('button')).toHaveLength(0)

    // A slots broadcast observes the slot as momentarily idle. The message list
    // is identical and the user never clicked again.
    rerender(<Transcript messages={m} running={false} />)
    expect(label()).toContain('Hide')
  })

  it('keeps the expand across repeated running-flag oscillation', () => {
    const m = aTurn()
    const { rerender } = render(<Transcript messages={m} running={false} />)
    fireEvent.click(screen.getByRole('button'))

    for (let i = 0; i < 5; i++) {
      rerender(<Transcript messages={m} running={true} />)
      rerender(<Transcript messages={m} running={false} />)
    }
    expect(label()).toContain('Hide')
  })

  it('keeps the expand when the agent appends another step and finishes', () => {
    const m = aTurn()
    const { rerender } = render(<Transcript messages={m} running={false} />)
    fireEvent.click(screen.getByRole('button'))

    rerender(<Transcript messages={[...m, tool()]} running={true} />)
    rerender(<Transcript messages={[...m, tool()]} running={false} />)
    expect(label()).toContain('Hide')
  })

  it('keeps an explicit collapse when the user collapsed it themselves', () => {
    const m = aTurn()
    const { rerender } = render(<Transcript messages={m} running={false} />)
    fireEvent.click(screen.getByRole('button'))   // expand
    fireEvent.click(screen.getByRole('button'))   // collapse again
    expect(label()).toContain('tool call')

    rerender(<Transcript messages={m} running={true} />)
    rerender(<Transcript messages={m} running={false} />)
    expect(label()).toContain('tool call')
  })

  it('does not disturb an earlier turn when a new turn starts', () => {
    const m = aTurn()
    const { rerender } = render(<Transcript messages={m} running={false} />)
    fireEvent.click(screen.getByRole('button'))

    rerender(<Transcript messages={[...m, user('again'), tool(), text('b'), tool()]} running={true} />)
    const labels = screen.queryAllByRole('button').map(b => b.textContent)
    expect(labels.some(l => l?.includes('Hide'))).toBe(true)
  })

  it('still auto-collapses on completion when the user never touched it', () => {
    const m = aTurn()
    // Mount while running so `expanded` initialises true, then complete the turn.
    const { rerender } = render(<Transcript messages={m} running={true} />)
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    rerender(<Transcript messages={m} running={false} />)
    // No user interaction, so the default collapse must still apply.
    expect(label()).toContain('tool call')
    expect(label()).not.toContain('Hide')
  })
})

describe('TurnBlock — row identity across turn promotion', () => {
  // KNOWN DEFECT (tracked separately): flushTurn only emits a `turn` object once
  // the trailing group exceeds items.length > 2. Below that the items render
  // loose; above it they are wrapped in TurnBlock. Inserting a component into
  // the tree remounts everything beneath it, so row-local state dies once per
  // turn. virtualKeyFor deliberately keeps the ROW key stable across the
  // promotion, but a stable row key cannot prevent a remount caused by a
  // changed child tree shape, which is exactly what this pins.
  //
  // `it.fails` asserts the defect is STILL present: when the promotion is made
  // non-remounting this test starts failing and must be flipped to `it`.
  it.fails('promoting loose items into a turn does not remount the row', () => {
    seq = 0
    const m: ChatMessage[] = [user('go'), tool()]
    const firstId = `m${m[1].ts}`
    const { rerender } = render(<Transcript messages={m} running={true} />)
    expect(mounts[firstId]).toBe(1)

    // Two more items push the trailing group over the promotion threshold.
    rerender(<Transcript messages={[...m, text('thinking'), tool()]} running={true} />)
    expect(mounts[firstId]).toBe(1)
  })
})
