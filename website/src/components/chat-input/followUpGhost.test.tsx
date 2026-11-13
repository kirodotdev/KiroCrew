import { describe, it, expect, vi } from 'vitest'
import { renderHook } from '@testing-library/react'
import type React from 'react'
import { useFollowUpGhost } from './followUpGhost'

const OPTIONS = ['Open an issue', 'Skip it']
const editor = document.createElement('textarea')
const sibling = document.createElement('button')

/** A keydown stand-in carrying only what the handler reads. */
function key(k: string, mods: Partial<{ shiftKey: boolean; metaKey: boolean; ctrlKey: boolean; altKey: boolean }> = {}, target: EventTarget = editor) {
  const e = {
    key: k, target, shiftKey: !!mods.shiftKey, metaKey: !!mods.metaKey, ctrlKey: !!mods.ctrlKey, altKey: !!mods.altKey,
    preventDefault: vi.fn(), stopPropagation: vi.fn(),
  }
  return e as unknown as React.KeyboardEvent & { preventDefault: ReturnType<typeof vi.fn> }
}

function setup(over: Partial<{ value: string; enabled: boolean; options: readonly string[]; imeOwnsKey: boolean }> = {}) {
  const onUserEdit = vi.fn()
  const setSelection = vi.fn()
  const claimKey = vi.fn(() => !over.imeOwnsKey)
  const { result } = renderHook(() => useFollowUpGhost({
    options: over.options ?? OPTIONS, value: over.value ?? '', enabled: over.enabled ?? true,
    onUserEdit, claimKey,
    composerControl: () => ({ focus: vi.fn(), getRootElement: () => editor, getSelection: () => null, setSelection }),
  }))
  return { result, onUserEdit, claimKey }
}

describe('useFollowUpGhost', () => {
  it('shows the first option in an empty box and Tab writes it as a user edit', () => {
    const { result, onUserEdit } = setup()
    expect(result.current.placeholder).toContain('Open an issue')
    const e = key('Tab')
    result.current.onKeyDownCapture(e)
    expect(onUserEdit).toHaveBeenCalledWith('Open an issue')
    expect(e.preventDefault).toHaveBeenCalled()
  })

  it.each([
    ['a draft is already typed', { value: 'w' }],
    ['it is disabled (touch, locked, busy)', { enabled: false }],
    ['there are no options', { options: [] }],
  ])('shows nothing and leaves Tab alone when %s', (_label, over) => {
    const { result, onUserEdit, claimKey } = setup(over)
    expect(result.current.placeholder).toBe('')
    const e = key('Tab')
    result.current.onKeyDownCapture(e)
    expect(onUserEdit).not.toHaveBeenCalled()
    expect(claimKey).not.toHaveBeenCalled()
    expect(e.preventDefault).not.toHaveBeenCalled()
  })

  it.each([
    ['Shift+Tab', { shiftKey: true }],
    ['Ctrl+Tab', { ctrlKey: true }],
  ])('keeps %s as a focus move', (_label, mods) => {
    const { result, onUserEdit } = setup()
    const e = key('Tab', mods)
    result.current.onKeyDownCapture(e)
    expect(onUserEdit).not.toHaveBeenCalled()
    expect(e.preventDefault).not.toHaveBeenCalled()
  })

  it('leaves a Tab the IME latch owns to the IME', () => {
    const { result, onUserEdit, claimKey } = setup({ imeOwnsKey: true })
    const e = key('Tab')
    result.current.onKeyDownCapture(e)
    expect(claimKey).toHaveBeenCalledWith(e)
    expect(onUserEdit).not.toHaveBeenCalled()
    expect(e.preventDefault).not.toHaveBeenCalled()
  })

  it('leaves a Tab on a sibling control in the wrapper alone', () => {
    const { result, onUserEdit, claimKey } = setup()
    const e = key('Tab', {}, sibling)
    result.current.onKeyDownCapture(e)
    expect(claimKey).not.toHaveBeenCalled()
    expect(onUserEdit).not.toHaveBeenCalled()
    expect(e.preventDefault).not.toHaveBeenCalled()
  })

  it('lets Escape then Tab leave the box', () => {
    const { result, onUserEdit } = setup()
    result.current.onKeyDownCapture(key('Escape'))
    const e = key('Tab')
    result.current.onKeyDownCapture(e)
    expect(onUserEdit).not.toHaveBeenCalled()
    expect(e.preventDefault).not.toHaveBeenCalled()
  })
})
