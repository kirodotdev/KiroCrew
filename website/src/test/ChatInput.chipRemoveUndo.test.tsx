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
})
