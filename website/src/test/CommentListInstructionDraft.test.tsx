import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import { CommentList, instructionDraftKeyFor, type InlineComment } from '../components/CommentOverlay'
import { scrollMemoryKeyFor } from '../hooks/useScrollMemory'
import '../i18n'

// The side panel can unmount a file tab's body for a moment (a placement
// refresh that fails withholds the tab until it is read again). The typed
// "overall instruction" must survive that, as the pending comments do, without
// ever leaving memory or showing in another chat.

const COMMENTS: InlineComment[] = [{ id: 'c1', anchor: 'first paragraph', text: 'tighten this' }]
const PATH = '/workspace/project/README.md'
const keyIn = (slot: string, tabId = 'file:1') => instructionDraftKeyFor(scrollMemoryKeyFor(slot, tabId), PATH)

function list(draftKey: string | undefined, { onSubmitAll = vi.fn(), onRemove = vi.fn(), onEdit = vi.fn(), comments = COMMENTS } = {}) {
  return (
    <CommentList comments={comments} onEdit={onEdit} onRemove={onRemove} onSubmitAll={onSubmitAll} enableExtraPrompt draftKey={draftKey} />
  )
}

function typeInstruction(text: string) {
  fireEvent.click(screen.getByRole('button', { name: 'Add overall instruction' }))
  fireEvent.change(screen.getByLabelText('Overall instruction'), { target: { value: text } })
}

const field = () => screen.queryByLabelText('Overall instruction')

afterEach(() => { vi.restoreAllMocks() })

describe('CommentList overall instruction draft', () => {
  it('keeps the typed instruction across an unmount of the same chat\'s document', () => {
    const view = render(list(keyIn('chat-a')))
    typeInstruction('make it shorter')
    view.unmount()
    const onSubmitAll = vi.fn()
    render(list(keyIn('chat-a'), { onSubmitAll }))
    expect(field()).toHaveValue('make it shorter')
    fireEvent.click(screen.getByText(/Submit All/))
    expect(onSubmitAll).toHaveBeenCalledWith('make it shorter')
  })

  it('never shows one chat\'s draft in another chat with the same file open', () => {
    expect(keyIn('chat-a')).not.toBe(keyIn('chat-b'))
    const view = render(list(keyIn('chat-a')))
    typeInstruction('only for chat a')
    view.unmount()
    const other = render(list(keyIn('chat-b')))
    expect(field()).toBeNull()
    other.unmount()
    // Nor without a tab identity to scope it to: then nothing is kept.
    expect(instructionDraftKeyFor(undefined, PATH)).toBeUndefined()
  })

  it('is cleared by Submit All', () => {
    const first = render(list(keyIn('chat-c')))
    typeInstruction('sent already')
    fireEvent.click(screen.getByText(/Submit All/))
    first.unmount()
    render(list(keyIn('chat-c')))
    expect(field()).toBeNull()
  })

  it('is cleared when the box is closed, which discards it', () => {
    const first = render(list(keyIn('chat-d')))
    typeInstruction('changed my mind')
    fireEvent.click(screen.getByRole('button', { name: 'Add overall instruction' }))
    first.unmount()
    render(list(keyIn('chat-d')))
    expect(field()).toBeNull()
  })

  it('is cleared when every pending comment is removed', () => {
    const first = render(list(keyIn('chat-e')))
    typeInstruction('for comments that are gone')
    first.rerender(list(keyIn('chat-e'), { comments: [] }))
    first.unmount()
    render(list(keyIn('chat-e')))
    expect(field()).toBeNull()
  })

  it('is never written to browser storage', () => {
    const local = vi.spyOn(Storage.prototype, 'setItem')
    const view = render(list(keyIn('chat-f')))
    typeInstruction('stays in memory')
    view.unmount()
    expect(local.mock.calls.filter(([, v]) => String(v).includes('stays in memory'))).toEqual([])
  })
})

// The same unmount must not lose an inline edit of a pending comment that was
// open and not yet saved (saving happens on blur or Enter, neither of which an
// unmount delivers).
describe('CommentList unfinished row edit', () => {
  const editBox = () => screen.queryByDisplayValue('tighten this paragraph')
  function startEdit(text: string) {
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByDisplayValue('tighten this'), { target: { value: text } })
  }

  it('reopens the edit with the typed text after an unmount, and saves it on Enter', () => {
    const view = render(list(keyIn('chat-r1')))
    startEdit('tighten this paragraph')
    view.unmount()
    const onEdit = vi.fn()
    render(list(keyIn('chat-r1'), { onEdit }))
    const input = editBox()
    expect(input).not.toBeNull()
    fireEvent.keyDown(input!, { key: 'Enter' })
    expect(onEdit).toHaveBeenCalledWith('c1', 'tighten this paragraph')
  })

  it('is not kept once saved, cancelled, or for another chat', () => {
    const saved = render(list(keyIn('chat-r2')))
    startEdit('tighten this paragraph')
    fireEvent.keyDown(screen.getByDisplayValue('tighten this paragraph'), { key: 'Enter' })
    saved.unmount()
    render(list(keyIn('chat-r2'))).unmount()
    expect(editBox()).toBeNull()

    const cancelled = render(list(keyIn('chat-r3')))
    startEdit('tighten this paragraph')
    fireEvent.keyDown(screen.getByDisplayValue('tighten this paragraph'), { key: 'Escape' })
    cancelled.unmount()
    const back = render(list(keyIn('chat-r3')))
    expect(editBox()).toBeNull()
    back.unmount()

    const a = render(list(keyIn('chat-r4')))
    startEdit('tighten this paragraph')
    a.unmount()
    const b = render(list(keyIn('chat-r5')))
    expect(editBox()).toBeNull()
    b.unmount()
    // No list key, nothing kept.
    const bare = render(list(undefined))
    startEdit('tighten this paragraph')
    bare.unmount()
    render(list(undefined))
    expect(editBox()).toBeNull()
  })

  it('is dropped when its comment is no longer pending', () => {
    const view = render(list(keyIn('chat-r6')))
    startEdit('tighten this paragraph')
    view.unmount()
    const other: InlineComment[] = [{ id: 'c2', anchor: 'second paragraph', text: 'other' }]
    render(list(keyIn('chat-r6'), { comments: other })).unmount()
    render(list(keyIn('chat-r6')))
    expect(editBox()).toBeNull()
  })

  it('is never written to browser storage', () => {
    const local = vi.spyOn(Storage.prototype, 'setItem')
    const view = render(list(keyIn('chat-r7')))
    startEdit('tighten this paragraph')
    view.unmount()
    expect(local.mock.calls.filter(([, v]) => String(v).includes('tighten this paragraph'))).toEqual([])
  })
})
