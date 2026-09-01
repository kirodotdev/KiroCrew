import { describe, it, expect } from 'vitest'
import React from 'react'
import { readFileSync } from 'fs'
import path from 'path'
import { useState } from 'react'
import { render, fireEvent, screen } from '@testing-library/react'
import TurnBlock from '../pages/chat/TurnBlock'
import type { DisplayItem, TurnItem } from '../pages/chat/types'

// memo(TurnBlock) contract: when a host re-renders but hands TurnBlock the
// IDENTICAL turn/renderItem/disclosure props (which createTurnGrouper's
// structural sharing guarantees for settled turns across streaming flushes),
// the TurnBlock body must not re-execute. renderItem runs inside the body, so
// its invocation count is the render probe.

const makeTurn = (items: TurnItem[], complete = true): Extract<DisplayItem, { kind: 'turn' }> =>
  ({ kind: 'turn', items, complete })

const items: TurnItem[] = [
  { kind: 'single', msg: { role: 'assistant', content: 'working on it', ts: '1' }, idx: 0 },
  { kind: 'single', msg: { role: 'tool', content: '🔧 Running: ls', ts: '2' }, idx: 1 },
  { kind: 'single', msg: { role: 'assistant', content: 'the answer', ts: '3' }, idx: 2 },
]

// Module-level so its identity is stable across host re-renders — the same
// guarantee ChatPage provides by hoisting renderTurnItem into a useCallback.
const probe = { calls: 0 }
const renderItem = (it: TurnItem) => {
  probe.calls++
  return <div>{it.kind === 'single' ? it.msg.content : 'group'}</div>
}

function Host({ turn }: { turn: Extract<DisplayItem, { kind: 'turn' }> }) {
  const [, setTick] = useState(0)
  return (
    <div>
      <button onClick={() => setTick(t => t + 1)}>rerender</button>
      <TurnBlock turn={turn} renderItem={renderItem} />
    </div>
  )
}

describe('TurnBlock — memo bail-out', () => {
  it('a host re-render with an identical turn reference re-executes zero TurnBlock bodies', () => {
    const turn = makeTurn(items)
    probe.calls = 0
    render(<Host turn={turn} />)
    const afterMount = probe.calls
    expect(afterMount).toBeGreaterThan(0)
    fireEvent.click(screen.getByText('rerender'))
    fireEvent.click(screen.getByText('rerender'))
    expect(probe.calls).toBe(afterMount) // bailed out both times
  })

  it('a NEW turn object (the trailing turn during streaming) does re-render', () => {
    const turn = makeTurn(items)
    probe.calls = 0
    const { rerender } = render(<Host turn={turn} />)
    const afterMount = probe.calls
    rerender(<Host turn={makeTurn(items.slice())} />)
    expect(probe.calls).toBeGreaterThan(afterMount) // probe has teeth
  })
})

// The production-shaped guarantee the module-level renderItem above cannot
// pin: ChatPage builds renderTurnItem via useCallback whose transitive deps
// must NOT include the messages array (flush-volatile reads go through refs).
// This host mirrors that construction — a state-held messages array feeds a
// ref, renderItem's useCallback has no messages dep — and a flush-shaped
// re-render (replacing the array) must re-execute zero settled TurnBlock
// bodies. If someone re-adds messages to the dep chain, the identity churns
// and this fails.
describe('TurnBlock — memo survives a flush-shaped host re-render', () => {
  it('replacing the messages array re-executes zero settled turn bodies', () => {
    const { useRef, useCallback, useState: useStateReact } = React
    const flushProbe = { calls: 0 }
    const turn = makeTurn(items)
    function FlushHost() {
      const [messages, setMessages] = useStateReact<unknown[]>([{ role: 'user' }])
      const messagesRef = useRef(messages); messagesRef.current = messages
      // Mirrors ChatPage: volatile reads via ref, dep array without messages.
      const renderItemCb = useCallback((it: TurnItem) => {
        void messagesRef.current.length
        flushProbe.calls++
        return <div>{it.kind === 'single' ? it.msg.content : 'group'}</div>
      }, [])
      return (
        <div>
          <button onClick={() => setMessages(prev => [...prev, { role: 'chunk' }])}>flush</button>
          <TurnBlock turn={turn} renderItem={renderItemCb} />
        </div>
      )
    }
    flushProbe.calls = 0
    render(<FlushHost />)
    const afterMount = flushProbe.calls
    expect(afterMount).toBeGreaterThan(0)
    fireEvent.click(screen.getByText('flush'))
    fireEvent.click(screen.getByText('flush'))
    expect(flushProbe.calls).toBe(afterMount)
  })
})

// Source ratchet: the flush-volatile values must never return to
// renderMessage's dependency array — that chain (renderMessage ->
// renderTurnItem -> TurnBlock's renderItem prop) is what would defeat
// memo(TurnBlock) on every streaming flush.
describe('ChatPage renderMessage dep hygiene', () => {
  it('renderMessage does not depend on flush-volatile positional state', () => {
    const src = readFileSync(
      path.join(__dirname, '../pages/chat/useChatPageTranscriptController.tsx'), 'utf8')
    const depMatch = src.match(/\}, (\[[^\]]*handleFileOpen[^\]]*\])\)\n\n  \/\/ Hoisted out of the row map/)
    expect(depMatch, 'renderMessage dep array not found — update this ratchet if the anchor moved').toBeTruthy()
    const deps = depMatch![1]
    const depNames = new Set(deps.slice(1, -1).split(',').map(dep => dep.trim()))
    for (const banned of ['messages', 'visibleIndexMap', 'lastTextIdx', 'slotState']) {
      expect(depNames.has(banned), `"${banned}" is back in renderMessage's deps — read it through its ref instead`).toBe(false)
    }
  })
})
