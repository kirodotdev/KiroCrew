import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { useState } from 'react'
import { act, fireEvent, screen, within } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

vi.mock('../utils/isTouchDevice', () => ({ isTouchDevice: () => false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => false }))

/**
 * A chip remove is a parent-driven value change. When it lands inside the
 * 400 ms typing window, the undo recorder used to merge it into the tip entry,
 * overwriting the pre-remove text: Ctrl+Z then skipped past the state that
 * still held the mention, so the chip never revived. ChatInput now ends the
 * burst before calling the parent's remove handler.
 */
function Harness({ initial, files }: { initial: string; files: string[] }) {
  const [value, setValue] = useState(initial)
  const [pending, setPending] = useState(files)
  return (
    <ChatInput
      value={value}
      onChange={setValue}
      onSend={vi.fn()}
      pendingFiles={pending}
      onRemoveFile={path => {
        setPending(prev => prev.filter(p => p !== path))
        setValue(prev => prev.replace('@a.ts ', ''))
      }}
    />
  )
}

const input = () => screen.getByLabelText('Message input') as HTMLTextAreaElement
const undo = () => fireEvent.keyDown(input(), { key: 'z', ctrlKey: true })
const advance = (ms: number) => act(() => { vi.advanceTimersByTime(ms) })

beforeEach(() => { vi.useFakeTimers() })
afterEach(() => { vi.useRealTimers() })

describe('ChatInput chip remove undo boundary', () => {
  it('gives a chip remove right after a keystroke its own undo step', () => {
    renderWithProviders(<Harness initial="see @a.ts " files={['/p/a.ts']} />)
    advance(1000)
    fireEvent.change(input(), { target: { value: 'see @a.ts z' } })
    advance(100)
    const chip = screen.getByRole('group', { name: '/p/a.ts' })
    fireEvent.click(within(chip).getByRole('button', { name: 'Remove' }))
    expect(input().value).toBe('see z')

    undo()
    expect(input().value).toBe('see @a.ts z')
    undo()
    expect(input().value).toBe('see @a.ts ')
  })

  it('does not split two quick typed characters into separate undo steps', () => {
    renderWithProviders(<Harness initial="hi" files={[]} />)
    advance(1000)
    fireEvent.change(input(), { target: { value: 'hix' } })
    advance(100)
    fireEvent.change(input(), { target: { value: 'hixy' } })
    expect(input().value).toBe('hixy')

    undo()
    expect(input().value).toBe('hi')
  })

  // #14675 (GPT 6.1 review): after switching between two slots whose drafts are
  // BYTE-IDENTICAL, the value prop never changes, so the undo effect does not
  // re-run to clear its "settling" flag — the flag stays set. The next edit was
  // the atomic mention delete, whose onChange was not marked as a user edit, so
  // the settling branch reseeded history at the post-delete value and Ctrl+Z
  // had nothing to restore. keyboard.ts now sets valueFromUserRef before that
  // onChange, so the atomic delete records a real undo entry.
  it('an atomic mention delete after a same-draft slot switch is undoable (#14675, GPT 6.1 review)', () => {
    // A minimal atomic onMentionKey: a Backspace just past `@a.ts` removes the
    // whole mention (and the trailing space), exactly what the real handler does.
    const onMentionKey = (text: string, selStart: number, _selEnd: number, key: string) => {
      const at = text.indexOf('@a.ts')
      if (key !== 'Backspace' || at < 0) return null
      const end = at + '@a.ts'.length
      if (selStart < end || selStart > end + 1) return null
      let e = end
      if (text[e] === ' ') e++
      return { value: text.slice(0, at) + text.slice(e), caret: at }
    }
    function SwitchHarness() {
      const [value, setValue] = useState('see @a.ts here')
      const [afk, setAfk] = useState('slot-a')
      return (
        <>
          <button onClick={() => setAfk('slot-b')}>switch</button>
          <ChatInput
            value={value}
            onChange={setValue}
            onSend={vi.fn()}
            autoFocusKey={afk}
            onMentionKey={onMentionKey}
          />
        </>
      )
    }
    renderWithProviders(<SwitchHarness />)
    advance(1000)
    // Switch slots: the new slot's draft is byte-identical, so `value` does not
    // change and the undo effect leaves its settling flag set.
    act(() => { fireEvent.click(screen.getByText('switch')) })
    advance(100)
    // Atomic mention delete: caret just past `@a.ts`, Backspace removes it whole.
    const ta = input()
    const caret = 'see @a.ts'.length
    ta.setSelectionRange(caret, caret)
    act(() => { fireEvent.keyDown(ta, { key: 'Backspace' }) })
    advance(100)
    expect(ta.value).toBe('see here')

    // The removal must be undoable — it was recorded as a real user edit.
    undo()
    expect(input().value).toBe('see @a.ts here')
  })
})
