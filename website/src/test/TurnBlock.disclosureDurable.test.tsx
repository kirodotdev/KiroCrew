/**
 * Turn disclosure must survive the virtualizer unmounting the row.
 *
 * The transcript is virtualised: useVirtualChat renders a row only while
 * `item.mounted` is true and unmounts it once it leaves the window + overscan
 * band, which streaming does routinely as it scrolls content past. Disclosure
 * state therefore cannot live in the row. These tests mirror ChatPage's
 * arrangement, where the host holds the state keyed by the virtualizer's stable
 * row key and passes it down.
 */
import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { useCallback, useMemo, useState, type ReactNode } from 'react'
import TurnBlock from '../pages/chat/TurnBlock'
import { groupDisplayItems, applyRunningState } from '../pages/chat/groupDisplayItems'
import { virtualKeyFor } from '../pages/chat/ChatPageMessageContent'
import type { DisplayItem, TurnItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'

const renderTurnItem = (it: TurnItem): ReactNode => (
  <div key={it.kind === 'single' ? `m${it.msg.ts}` : `g${it.startIdx}`} />
)

/**
 * Mirrors ChatPage: disclosure is held by the host, keyed by the row key, and
 * `mounted` mirrors the virtualizer, where false means the row is not rendered.
 */
function Transcript({ messages, running, mounted }: { messages: ChatMessage[]; running: boolean; mounted: boolean }) {
  const grouped = useMemo(() => groupDisplayItems(messages), [messages])
  const items: DisplayItem[] = applyRunningState(grouped, running)
  const [disclosure, setDisclosure] = useState<Record<string, boolean>>({})
  const setFor = useCallback((key: string, expanded: boolean) => {
    setDisclosure(prev => (prev[key] === expanded ? prev : { ...prev, [key]: expanded }))
  }, [])
  return (
    <>
      {items.map((item, i) => {
        const k = virtualKeyFor(item, i, (m: ChatMessage) => `m${m.ts}`)
        if (!mounted) return <div key={k} data-spacer />
        if (item.kind === 'turn') {
          return (
            <div key={k}>
              <TurnBlock
                turn={item}
                renderItem={renderTurnItem}
                disclosure={disclosure[k]}
                disclosureKey={k} onDisclosureChange={setFor}
              />
            </div>
          )
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
const aTurn = (): ChatMessage[] => { seq = 0; return [user('go'), tool(), text('a'), tool()] }
const label = () => screen.getByRole('button').textContent

describe('turn disclosure survives virtualizer unmount', () => {
  it('keeps an expand when the row scrolls out of the mounted window and back', () => {
    const m = aTurn()
    const { rerender } = render(<Transcript messages={m} running={false} mounted={true} />)
    fireEvent.click(screen.getByRole('button'))
    expect(label()).toContain('Hide')

    // Row leaves the mounted window, then the user scrolls back to it.
    rerender(<Transcript messages={m} running={false} mounted={false} />)
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    rerender(<Transcript messages={m} running={false} mounted={true} />)

    expect(label()).toContain('Hide')
  })

  it('keeps an explicit collapse across an unmount too', () => {
    const m = aTurn()
    const { rerender } = render(<Transcript messages={m} running={false} mounted={true} />)
    fireEvent.click(screen.getByRole('button'))   // expand
    fireEvent.click(screen.getByRole('button'))   // collapse again
    expect(label()).toContain('tool call')

    rerender(<Transcript messages={m} running={false} mounted={false} />)
    rerender(<Transcript messages={m} running={false} mounted={true} />)

    expect(label()).toContain('tool call')
    expect(label()).not.toContain('Hide')
  })

  it('survives an unmount combined with running-flag churn', () => {
    const m = aTurn()
    const { rerender } = render(<Transcript messages={m} running={false} mounted={true} />)
    fireEvent.click(screen.getByRole('button'))

    // Agent resumes, the row scrolls away, a stale idle frame lands, row returns.
    rerender(<Transcript messages={m} running={true} mounted={true} />)
    rerender(<Transcript messages={m} running={true} mounted={false} />)
    rerender(<Transcript messages={m} running={false} mounted={false} />)
    rerender(<Transcript messages={m} running={false} mounted={true} />)

    expect(label()).toContain('Hide')
  })

  it('still auto-collapses on completion when the user never touched it', () => {
    const m = aTurn()
    const { rerender } = render(<Transcript messages={m} running={true} mounted={true} />)
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    rerender(<Transcript messages={m} running={false} mounted={true} />)
    // No entry was ever recorded, so the default still applies.
    expect(label()).toContain('tool call')
    expect(label()).not.toContain('Hide')
  })
})
