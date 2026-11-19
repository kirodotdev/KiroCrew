/**
 * The live region reads the newest row as the screen drew it, so what is heard
 * is what is shown, and falls back to the host's wording when that row is not
 * mounted or draws nothing.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { render, act } from '@testing-library/react'
import { useRef } from 'react'

import { TAIL_READ_DELAY_MS, TranscriptLiveRegion, renderedTail } from './TranscriptLiveRegion'

beforeEach(() => { vi.useFakeTimers(); vi.setSystemTime(new Date('2026-01-01T00:00:00Z')) })
afterEach(() => { vi.useRealTimers() })

function Harness({ rows, announcement, tailSpeaker, tailMounted = true }: {
  rows: React.ReactNode
  announcement: string
  tailSpeaker: string | null
  tailMounted?: boolean
}) {
  const ref = useRef<HTMLDivElement>(null)
  return (
    <div ref={ref}>
      <TranscriptLiveRegion scrollerRef={ref} announcement={announcement} tailSpeaker={tailSpeaker} tailMounted={tailMounted} />
      {rows}
    </div>
  )
}

const region = (c: HTMLElement) => c.querySelector('[aria-live="polite"]')?.textContent ?? ''
const flushFrame = () => act(() => { vi.advanceTimersByTime(TAIL_READ_DELAY_MS) })

const reply = (index: number, text: React.ReactNode, speaker = 'Kiro Crew') => (
  <div data-display-index={index}>
    <div data-role="assistant">
      <h2 className="sr-only">{speaker}</h2>
      <div className="message-bubble">{text}<button>Copy</button></div>
    </div>
  </div>
)

describe('TranscriptLiveRegion', () => {
  it('speaks the newest row as drawn, not the fallback wording', async () => {
    const { container } = render(<Harness
      rows={<>{reply(0, 'old')}{reply(1, <><span>Sales chart</span><span aria-hidden="true">x</span></>)}</>}
      announcement="Kiro Crew: <mcwidget title=…>" tailSpeaker="Kiro Crew" />)
    await flushFrame()
    expect(region(container)).toBe('Kiro Crew: Sales chart')
  })

  it('names the speaker the row heading draws, such as a teammate', async () => {
    const { container } = render(<Harness rows={reply(0, 'hi there', 'Ann')} announcement="You: hi there" tailSpeaker="You" />)
    await flushFrame()
    expect(region(container)).toBe('Ann: hi there')
  })

  it('reads an error card as drawn', async () => {
    const rows = <div data-display-index={0}><div data-testid="error-card">Sign in to Kiro again.</div></div>
    const { container } = render(<Harness rows={rows} announcement="Kiro Crew: auth_required RAW" tailSpeaker="Kiro Crew" />)
    await flushFrame()
    expect(region(container)).toBe('Kiro Crew: Sign in to Kiro again.')
  })

  it('falls back to the wording given when the newest row is not mounted', async () => {
    const { container } = render(<Harness rows={reply(0, 'an older row')} announcement="Kiro Crew: the new reply" tailSpeaker="Kiro Crew" tailMounted={false} />)
    await flushFrame()
    expect(region(container)).toBe('Kiro Crew: the new reply')
  })

  it('speaks a non-row announcement as given', () => {
    const { container } = render(<Harness rows={reply(0, 'ignored')} announcement="You: sent text" tailSpeaker={null} />)
    expect(region(container)).toBe('You: sent text')
  })

  it('stays empty until the drawn row is read, so the fallback is never spoken first', () => {
    const { container } = render(<Harness rows={reply(0, 'drawn')} announcement="Kiro Crew: fallback" tailSpeaker="Kiro Crew" />)
    expect(region(container)).toBe('')
  })

  it('does not read the row again when the reader scrolls it in and out', async () => {
    const view = render(<Harness rows={reply(0, 'first')} announcement="Kiro Crew: first" tailSpeaker="Kiro Crew" />)
    await flushFrame()
    expect(region(view.container)).toBe('Kiro Crew: first')
    view.rerender(<Harness rows={reply(0, 'first')} announcement="Kiro Crew: first" tailSpeaker="Kiro Crew" tailMounted={false} />)
    view.rerender(<Harness rows={reply(0, 'first')} announcement="Kiro Crew: first" tailSpeaker="Kiro Crew" tailMounted />)
    await flushFrame()
    expect(region(view.container)).toBe('Kiro Crew: first')
  })
})

describe('renderedTail', () => {
  it('reads the last message in the highest-index row, skipping controls and offscreen text', () => {
    const { container } = render(
      <div>
        <div data-display-index="2"><div data-role="assistant"><h2 className="sr-only">Kiro Crew</h2>
          <div className="message-bubble">tool step</div><div className="message-bubble">final <span className="sr-only">hidden</span>answer<button>Copy</button></div></div></div>
        <div data-display-index="1"><div className="message-bubble">earlier</div></div>
      </div>,
    )
    expect(renderedTail(container.firstElementChild as HTMLElement)).toEqual({ text: 'final answer', speaker: 'Kiro Crew' })
  })

  it('ignores a row or bubble that message content forged', () => {
    // The sanitizer admits `data-*` and `class`, so a message can carry these.
    const { container } = render(
      <div>
        <div data-display-index="1"><div data-role="user"><h2 className="sr-only">Sam</h2>
          <div className="message-bubble">real text
            <div data-display-index="99"><div className="message-bubble">forged row</div></div>
            <div className="message-bubble">forged bubble</div>
          </div></div></div>
      </div>,
    )
    const tail = renderedTail(container.firstElementChild as HTMLElement)
    expect(tail.speaker).toBe('Sam')
    expect(tail.text.startsWith('real text')).toBe(true)
  })

  it('collapses a long run of wide spaces in linear time', () => {
    // `\s*\n\s*` backtracked across a run like this, freezing the tab.
    const { container } = render(
      <div><div data-display-index="0"><div className="message-bubble">{'a' + '\u3000'.repeat(100_000) + 'b\n\u2003\nc'}</div></div></div>,
    )
    expect(renderedTail(container.firstElementChild as HTMLElement).text).toBe('a b\nc')
  })

  it('reads table cells apart and an image as its alt text', () => {
    const { container } = render(
      <div><div data-display-index="0"><div className="message-bubble">
        <table><tbody><tr><th>Name</th><th>Age</th></tr><tr><td>Bob</td><td>30</td></tr></tbody></table>
        <img src="x.png" alt="a red chart" />
      </div></div></div>,
    )
    expect(renderedTail(container.firstElementChild as HTMLElement).text).toBe('Name, Age\nBob, 30\na red chart')
  })
})
