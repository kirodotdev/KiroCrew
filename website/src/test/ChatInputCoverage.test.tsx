import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent, waitFor } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import { api } from '../api/client'
import { SlotProvider } from '../providers/SlotContext'
import type { PasteBlock } from '../utils/pasteTokens'

// Collapsed-paste tokens are the composer's most intricate keyboard surface:
// the token is a literal string in the textarea, so every delete / arrow /
// selection gesture has to treat it as ONE atom or the user is left with a
// half-sliced "[ Paste #1 · 4" literal that no longer maps to a PasteBlock.
// The handlers all commit through onChange / onPasteBlocksChange and move the
// caret in a requestAnimationFrame, so each test drives a real controlled
// harness and awaits one frame before reading the selection.

const block: PasteBlock = { id: 'p1', seq: 1, lines: 40, content: 'TRACEBACK: boom\nsecond line' }
const token = '[ Paste #1 · 40 lines ]'

/** Controlled harness mirroring ChatPage: `value` and `pasteBlocks` are two
 *  separate pieces of parent state wired to the two callbacks. */
function PasteHarness({
  initial,
  initialBlocks,
  onBlocks,
}: {
  initial: string
  initialBlocks: PasteBlock[]
  onBlocks?: (b: PasteBlock[]) => void
}) {
  const [v, setV] = React.useState(initial)
  const [blocks, setBlocks] = React.useState<PasteBlock[]>(initialBlocks)
  return (
    <ChatInput
      value={v}
      onChange={setV}
      onSend={vi.fn()}
      pasteBlocks={blocks}
      onPasteBlocksChange={b => { onBlocks?.(b); setBlocks(b) }}
    />
  )
}

/** Resolve after one animation frame — the handlers schedule their caret
 *  restore there. Frame-driven, not a timed sleep, so it cannot flake. */
const nextFrame = () => new Promise<void>(resolve => { requestAnimationFrame(() => resolve()) })

const mountTokens = (initial: string, blocks: PasteBlock[] = [block], onBlocks?: (b: PasteBlock[]) => void) => {
  renderWithProviders(<PasteHarness initial={initial} initialBlocks={blocks} onBlocks={onBlocks} />)
  return screen.getByLabelText('Message input') as HTMLTextAreaElement
}

beforeEach(() => {
  localStorage.clear()
})

describe('ChatInput paste tokens: atomic delete gestures', () => {
  it('Cmd+Backspace (line-back delete) removes the whole token, not a slice of it', async () => {
    const onBlocks = vi.fn()
    const ta = mountTokens(`hello ${token}`, [block], onBlocks)
    ta.setSelectionRange(ta.value.length, ta.value.length)
    fireEvent.keyDown(ta, { key: 'Backspace', metaKey: true })
    // Line-back deletion is extended to the token's start, so nothing of the
    // literal survives and the backing block is dropped with it.
    expect(ta.value).toBe('')
    expect(onBlocks).toHaveBeenLastCalledWith([])
    await nextFrame()
    expect(ta.selectionStart).toBe(0)
  })

  it('Cmd+Backspace leaves a line with no token on it to the browser', () => {
    const onBlocks = vi.fn()
    const ta = mountTokens(`${token}\nplain tail`, [block], onBlocks)
    const pos = ta.value.length
    ta.setSelectionRange(pos, pos)
    fireEvent.keyDown(ta, { key: 'Backspace', metaKey: true })
    // No range intersects the caret-to-line-start span, so the handler bows out
    // and native line-back deletion applies (a no-op under happy-dom).
    expect(ta.value).toBe(`${token}\nplain tail`)
    expect(onBlocks).not.toHaveBeenCalled()
  })

  it('Alt+Backspace (word-back delete) next to a token deletes the token atomically', () => {
    const ta = mountTokens(`x${token}`)
    ta.setSelectionRange(ta.value.length, ta.value.length)
    fireEvent.keyDown(ta, { key: 'Backspace', altKey: true })
    expect(ta.value).toBe('x')
  })

  it('Ctrl+Backspace behaves the same as Alt+Backspace (Windows/Linux word-back)', () => {
    const ta = mountTokens(`x${token}`)
    ta.setSelectionRange(ta.value.length, ta.value.length)
    fireEvent.keyDown(ta, { key: 'Backspace', ctrlKey: true })
    expect(ta.value).toBe('x')
  })

  it('Delete with the caret just BEFORE a token removes the whole token', async () => {
    const onBlocks = vi.fn()
    const ta = mountTokens(`${token} tail`, [block], onBlocks)
    ta.setSelectionRange(0, 0)
    fireEvent.keyDown(ta, { key: 'Delete' })
    expect(ta.value).toBe(' tail')
    expect(onBlocks).toHaveBeenLastCalledWith([])
    await nextFrame()
    expect(ta.selectionStart).toBe(0)
  })

  it('Cmd+Delete (forward line-delete) extends over an intersecting token', async () => {
    const ta = mountTokens(`${token} rest\nsecond`)
    ta.setSelectionRange(0, 0)
    fireEvent.keyDown(ta, { key: 'Delete', metaKey: true })
    // Everything to the end of the line goes, including the token.
    expect(ta.value).toBe('\nsecond')
    await nextFrame()
    expect(ta.selectionStart).toBe(0)
  })

  it('Cmd+Delete on a token-free line is left to the browser', () => {
    const onBlocks = vi.fn()
    const ta = mountTokens(`plain head\n${token}`, [block], onBlocks)
    ta.setSelectionRange(0, 0)
    fireEvent.keyDown(ta, { key: 'Delete', metaKey: true })
    expect(ta.value).toBe(`plain head\n${token}`)
    expect(onBlocks).not.toHaveBeenCalled()
  })

  it('Alt+Delete (word-forward delete) next to a token deletes the token atomically', () => {
    const ta = mountTokens(`${token}y`)
    ta.setSelectionRange(0, 0)
    fireEvent.keyDown(ta, { key: 'Delete', altKey: true })
    expect(ta.value).toBe('y')
  })

  it('a modified Backspace away from any token changes nothing', () => {
    const onBlocks = vi.fn()
    const ta = mountTokens(`${token} tail`, [block], onBlocks)
    ta.setSelectionRange(ta.value.length, ta.value.length)
    fireEvent.keyDown(ta, { key: 'Backspace', altKey: true })
    expect(ta.value).toBe(`${token} tail`)
    expect(onBlocks).not.toHaveBeenCalled()
  })

  it('deletes only the addressed token when two are present', () => {
    const b2: PasteBlock = { id: 'p2', seq: 2, lines: 9, content: 'other paste' }
    const t2 = '[ Paste #2 · 9 lines ]'
    const onBlocks = vi.fn()
    const ta = mountTokens(`${token}\n${t2}`, [block, b2], onBlocks)
    ta.setSelectionRange(token.length, token.length)
    fireEvent.keyDown(ta, { key: 'Backspace' })
    expect(ta.value).toBe(`\n${t2}`)
    // Block #2 survives — its token is still in the text.
    expect(onBlocks).toHaveBeenLastCalledWith([b2])
  })
})

describe('ChatInput paste tokens: caret and selection navigation', () => {
  it('ArrowLeft steps over the whole token in one press', async () => {
    const ta = mountTokens(token)
    ta.setSelectionRange(token.length, token.length)
    fireEvent.keyDown(ta, { key: 'ArrowLeft' })
    await nextFrame()
    expect(ta.selectionStart).toBe(0)
    expect(ta.selectionEnd).toBe(0)
    expect(ta.value).toBe(token) // navigation never edits
  })

  it('ArrowRight steps over the whole token in one press', async () => {
    const ta = mountTokens(token)
    ta.setSelectionRange(0, 0)
    fireEvent.keyDown(ta, { key: 'ArrowRight' })
    await nextFrame()
    expect(ta.selectionStart).toBe(token.length)
    expect(ta.selectionEnd).toBe(token.length)
  })

  it('ArrowLeft not adjacent to a token is left to the browser', async () => {
    const ta = mountTokens(`${token} tail`)
    ta.setSelectionRange(2, 2)
    fireEvent.keyDown(ta, { key: 'ArrowLeft' })
    await nextFrame()
    expect(ta.selectionStart).toBe(2) // untouched
  })

  it('Shift+ArrowLeft extends the selection past the whole token', async () => {
    const ta = mountTokens(`ab${token}`)
    ta.setSelectionRange(0, ta.value.length)
    fireEvent.keyDown(ta, { key: 'ArrowLeft', shiftKey: true })
    await nextFrame()
    // Active endpoint sat at the token's end, so it jumps to the token's start
    // rather than shrinking one character into the literal.
    expect(ta.selectionStart).toBe(0)
    expect(ta.selectionEnd).toBe(2)
  })

  it('Shift+ArrowRight extends the selection past the whole token', async () => {
    const ta = mountTokens(`ab${token}`)
    const full = ta.value.length
    ta.setSelectionRange(0, 2)
    fireEvent.keyDown(ta, { key: 'ArrowRight', shiftKey: true })
    await nextFrame()
    expect(ta.selectionStart).toBe(0)
    expect(ta.selectionEnd).toBe(full)
  })

  it('Home snaps a caret stranded inside a token back to its start', async () => {
    const ta = mountTokens(`pre ${token} post`)
    const inside = 4 + 5
    ta.setSelectionRange(inside, inside)
    fireEvent.keyDown(ta, { key: 'Home' })
    await nextFrame()
    // Leftward motion, so the caret is pushed out to the token's leading edge.
    expect(ta.selectionStart).toBe(4)
    expect(ta.selectionEnd).toBe(4)
  })

  it('End snaps a caret stranded inside a token forward to its end', async () => {
    const ta = mountTokens(`pre ${token} post`)
    const inside = 4 + 5
    ta.setSelectionRange(inside, inside)
    fireEvent.keyDown(ta, { key: 'End' })
    await nextFrame()
    expect(ta.selectionStart).toBe(4 + token.length)
  })

  it('a nav key that lands cleanly outside every token is not snapped', async () => {
    const ta = mountTokens(`pre ${token} post`)
    ta.setSelectionRange(2, 2)
    fireEvent.keyDown(ta, { key: 'End' })
    await nextFrame()
    expect(ta.selectionStart).toBe(2)
  })

  it('single click on a token selects it as one atom instead of dropping a caret inside', async () => {
    const ta = mountTokens(token)
    ta.setSelectionRange(4, 4)
    fireEvent.click(ta, { detail: 1 })
    await nextFrame()
    expect(ta.selectionStart).toBe(0)
    expect(ta.selectionEnd).toBe(token.length)
    expect(ta.value).toBe(token) // first click never expands
  })

  it('a click away from every token is ignored by the token click handler', async () => {
    const ta = mountTokens(`${token} tail`)
    const pos = ta.value.length
    ta.setSelectionRange(pos, pos)
    fireEvent.click(ta, { detail: 1 })
    await nextFrame()
    expect(ta.selectionStart).toBe(pos)
  })

  it('drag-select ending inside a token snaps the endpoint to the nearer edge', () => {
    const ta = mountTokens(`${token} tail`)
    // Start endpoint sits 3 chars into the token: nearer the leading edge.
    ta.setSelectionRange(3, token.length + 3)
    fireEvent.select(ta)
    expect(ta.selectionStart).toBe(0)
    expect(ta.selectionEnd).toBe(token.length + 3)
  })

  it('snaps to the trailing edge when the endpoint is nearer the token end', () => {
    const ta = mountTokens(`${token} tail`)
    ta.setSelectionRange(token.length - 2, token.length + 3)
    fireEvent.select(ta)
    expect(ta.selectionStart).toBe(token.length)
  })

  it('a selection already flush with the token edges is left alone', () => {
    const ta = mountTokens(`${token} tail`)
    ta.setSelectionRange(0, token.length)
    fireEvent.select(ta)
    expect(ta.selectionStart).toBe(0)
    expect(ta.selectionEnd).toBe(token.length)
  })

  it('a collapsed caret inside a token is left to the click expander, not snapped', () => {
    const ta = mountTokens(token)
    ta.setSelectionRange(5, 5)
    fireEvent.select(ta)
    expect(ta.selectionStart).toBe(5)
  })

  it('a selection is not snapped when the tracked blocks have no token left in the text', () => {
    // The block still exists in parent state but its token literal is gone
    // (mid-edit), so there is no range to snap against.
    const ta = mountTokens('plain text, no token here')
    ta.setSelectionRange(0, 5)
    fireEvent.select(ta)
    expect(ta.selectionStart).toBe(0)
    expect(ta.selectionEnd).toBe(5)
  })

  it('double-click expansion leaves the caret just after the restored content', async () => {
    const ta = mountTokens(token)
    ta.setSelectionRange(4, 4)
    fireEvent.click(ta, { detail: 2 })
    expect(ta.value).toBe(block.content)
    await nextFrame()
    expect(ta.selectionStart).toBe(block.content.length)
  })

  it('the token click handler stands down when the composer tracks no pastes', async () => {
    const ta = mountTokens('nothing collapsed here', [])
    ta.setSelectionRange(3, 3)
    fireEvent.click(ta, { detail: 2 })
    await nextFrame()
    expect(ta.value).toBe('nothing collapsed here')
    expect(ta.selectionStart).toBe(3)
  })
})

describe('ChatInput composer keyboard and scroll plumbing', () => {
  const base = { value: 'draft text', onChange: vi.fn(), onSend: vi.fn() }

  it('Cmd+Shift+Enter swallows the newline even while the gateway is offline', () => {
    const onSend = vi.fn()
    renderWithProviders(<ChatInput {...base} onSend={onSend} connected={false} />)
    const ta = screen.getByLabelText('Message input')
    fireEvent.keyDown(ta, { key: 'Enter', metaKey: true, shiftKey: true })
    // The combo is claimed unconditionally so a stray newline never leaks into
    // the draft; only the optimize action itself is gated on the connection.
    expect(onSend).not.toHaveBeenCalled()
  })

  it('Enter does not send while the composer is disabled', () => {
    const onSend = vi.fn()
    renderWithProviders(<ChatInput {...base} onSend={onSend} disabled />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('Enter sends the draft on the default send mode', () => {
    const onSend = vi.fn()
    renderWithProviders(<ChatInput {...base} onSend={onSend} />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('mirrors the textarea scroll offset onto the paste-highlight backdrop', () => {
    renderWithProviders(<ChatInput {...base} />)
    const ta = screen.getByLabelText('Message input') as HTMLTextAreaElement
    ta.scrollTop = 24
    fireEvent.scroll(ta)
    // No throw and no crash: the mirror is kept in lockstep so chip backgrounds
    // stay aligned with their tokens on a scrolled composer.
    expect(ta.scrollTop).toBe(24)
  })

  it('forwards drag-leave to the host without letting it bubble further', () => {
    const onDragLeave = vi.fn()
    renderWithProviders(<ChatInput {...base} onDragLeave={onDragLeave} />)
    fireEvent.dragLeave(screen.getByLabelText('Message input'))
    expect(onDragLeave).toHaveBeenCalledTimes(1)
  })
})

describe('ChatInput paste: caret lands after the inserted content', () => {
  const pasteText = (el: HTMLElement, text: string) =>
    fireEvent.paste(el, { clipboardData: { types: ['text/plain'], items: [], getData: () => text } })

  beforeEach(() => {
    // happy-dom's execCommand is an unreliable no-op, so force the documented
    // controlled-value fallback that owns the caret restore.
    ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = vi.fn(() => false)
  })

  it('a trailing-blank-trimmed paste leaves the caret at the end of the cleaned text', async () => {
    const ta = mountTokens('', [])
    ta.focus()
    pasteText(ta, 'one line\n\n\n')
    expect(ta.value).toBe('one line')
    await nextFrame()
    expect(ta.selectionStart).toBe('one line'.length)
  })

  it('a collapsed large paste leaves the caret after the token it inserted', async () => {
    const onBlocks = vi.fn()
    const ta = mountTokens('', [], onBlocks)
    ta.focus()
    // Long enough to collapse into a "[ Paste #1 · N lines ]" chip.
    pasteText(ta, Array.from({ length: 12 }, (_, i) => `payload line ${i}`).join('\n'))
    expect(onBlocks).toHaveBeenCalledTimes(1)
    expect(ta.value).toMatch(/^\[ Paste #1 · 12 lines \]$/)
    await nextFrame()
    expect(ta.selectionStart).toBe(ta.value.length)
  })
})

describe('ChatInput paste tokens: blockquote prefix keeps the chip on the quote line', () => {
  // Typing "> " and then pasting used to force the chip onto its own line,
  // stranding the quote marker above it (`> ⏎ [ Paste #1 … ]`) — the user had
  // to delete the injected newline every time to make the quote read right
  // (issue: collapse-paste prepends a blank line after a quote prefix).
  const bigPaste = Array.from({ length: 12 }, (_, i) => `payload line ${i}`).join('\n')
  const pasteText = (el: HTMLElement, text: string) =>
    fireEvent.paste(el, { clipboardData: { types: ['text/plain'], items: [], getData: () => text } })

  beforeEach(() => {
    ;(document as unknown as { execCommand: (...a: unknown[]) => boolean }).execCommand = vi.fn(() => false)
  })

  it('pasting after "> " flows the chip on the quote line — no injected newline', async () => {
    const ta = mountTokens('> ', [])
    ta.focus()
    ta.setSelectionRange(2, 2)
    pasteText(ta, bigPaste)
    expect(ta.value).toBe('> [ Paste #1 · 12 lines ]')
    await nextFrame()
    expect(ta.selectionStart).toBe(ta.value.length)
  })

  it('a nested quote prefix ("> > ") also flows', () => {
    const ta = mountTokens('> > ', [])
    ta.focus()
    ta.setSelectionRange(4, 4)
    pasteText(ta, bigPaste)
    expect(ta.value).toBe('> > [ Paste #1 · 12 lines ]')
  })

  it('a quote prefix on a LATER line flows too (earlier lines untouched)', () => {
    const initial = 'as discussed:\n> '
    const ta = mountTokens(initial, [])
    ta.focus()
    ta.setSelectionRange(initial.length, initial.length)
    pasteText(ta, bigPaste)
    expect(ta.value).toBe('as discussed:\n> [ Paste #1 · 12 lines ]')
  })

  it('an indented quote prefix ("  > ") flows', () => {
    const ta = mountTokens('  > ', [])
    ta.focus()
    ta.setSelectionRange(4, 4)
    pasteText(ta, bigPaste)
    expect(ta.value).toBe('  > [ Paste #1 · 12 lines ]')
  })

  it('ordinary text before the caret still pushes the chip to its own line', () => {
    // The own-line behavior is deliberate for prose — only a bare quote
    // prefix opts into inline flow. Pins that this fix does not regress it.
    const ta = mountTokens('see: ', [])
    ta.focus()
    ta.setSelectionRange(5, 5)
    pasteText(ta, bigPaste)
    expect(ta.value).toBe('see: \n[ Paste #1 · 12 lines ]')
  })

  it('a line of only whitespace (no ">") still pushes the chip to its own line', () => {
    // Whitespace alone is not a quote — without a marker the prefix rule must
    // not fire, or an indented caret would silently change long-standing shape.
    const ta = mountTokens('   ', [])
    ta.focus()
    ta.setSelectionRange(3, 3)
    pasteText(ta, bigPaste)
    expect(ta.value).toBe('   \n[ Paste #1 · 12 lines ]')
  })
})

describe('ChatInput paste tokens: clipboard copy and cut', () => {
  const clip = () => {
    const box = { data: '' }
    return {
      box,
      clipboardData: { setData: (_type: string, d: string) => { box.data = d } },
    }
  }

  it('cut of a fully covered token writes the EXPANDED content and excises the token', async () => {
    const c = clip()
    const ta = mountTokens(`head ${token} tail`)
    ta.setSelectionRange(5, 5 + token.length)
    fireEvent.cut(ta, { clipboardData: c.clipboardData })
    expect(c.box.data).toBe(block.content)
    expect(ta.value).toBe('head  tail')
    await nextFrame()
    expect(ta.selectionStart).toBe(5)
  })

  it('cut carries the surrounding plain text through alongside the expansion', () => {
    const c = clip()
    const ta = mountTokens(`head ${token} tail`)
    ta.setSelectionRange(0, ta.value.length)
    fireEvent.cut(ta, { clipboardData: c.clipboardData })
    expect(c.box.data).toBe(`head ${block.content} tail`)
    expect(ta.value).toBe('')
  })

  it('cut of a partially overlapping token falls back to the native literal slice', () => {
    const c = clip()
    const ta = mountTokens(`${token} tail`)
    ta.setSelectionRange(0, 6) // stops mid-token: nothing fully covered
    fireEvent.cut(ta, { clipboardData: c.clipboardData })
    expect(c.box.data).toBe('') // handler declined
    expect(ta.value).toBe(`${token} tail`)
  })

  it('copy of a collapsed caret writes nothing (no selection to expand)', () => {
    const c = clip()
    const ta = mountTokens(token)
    ta.setSelectionRange(3, 3)
    fireEvent.copy(ta, { clipboardData: c.clipboardData })
    expect(c.box.data).toBe('')
  })

  it('copy expands every fully covered token in a multi-token selection', () => {
    const b2: PasteBlock = { id: 'p2', seq: 2, lines: 9, content: 'second payload' }
    const t2 = '[ Paste #2 · 9 lines ]'
    const c = clip()
    const ta = mountTokens(`${token}\n${t2}`, [block, b2])
    ta.setSelectionRange(0, ta.value.length)
    fireEvent.copy(ta, { clipboardData: c.clipboardData })
    expect(c.box.data).toBe(`${block.content}\n${b2.content}`)
  })
})

describe('ChatInput + menu trigger shortcuts', () => {
  const base = { value: '', onChange: vi.fn(), onSend: vi.fn(), onUploadFiles: vi.fn() }
  const openPlusMenu = () => fireEvent.click(screen.getByTitle('Add files & options'))

  it('the Command item inserts the slash sigil, replacing the whole draft', () => {
    const onChange = vi.fn()
    renderWithProviders(<ChatInput {...base} value="stale draft" onChange={onChange} />)
    openPlusMenu()
    fireEvent.click(screen.getByTitle('Slash commands'))
    // Slash commands are whole-input, so the draft is replaced rather than appended to.
    expect(onChange).toHaveBeenCalledWith('/')
  })

  it('the File item appends the @ sigil at a word boundary', () => {
    const onChange = vi.fn()
    renderWithProviders(<ChatInput {...base} value="look at" onChange={onChange} onFileSelect={vi.fn()} />)
    openPlusMenu()
    fireEvent.click(screen.getByTitle('Reference a file'))
    expect(onChange).toHaveBeenCalledWith('look at @')
  })

  it('the File item is withheld when the host provides no file-select handler', () => {
    renderWithProviders(<ChatInput {...base} />)
    openPlusMenu()
    expect(screen.queryByTitle('Reference a file')).toBeNull()
  })

  it('the Skill item appends the $ sigil without doubling an existing space', () => {
    const onChange = vi.fn()
    renderWithProviders(<ChatInput {...base} value="run " onChange={onChange} />)
    openPlusMenu()
    fireEvent.click(screen.getByTitle('Use a skill'))
    expect(onChange).toHaveBeenCalledWith('run $')
  })

  it('the Skill item on an empty draft inserts the bare sigil', async () => {
    const onChange = vi.fn()
    renderWithProviders(<ChatInput {...base} onChange={onChange} />)
    openPlusMenu()
    fireEvent.click(screen.getByTitle('Use a skill'))
    expect(onChange).toHaveBeenCalledWith('$')
    await nextFrame()
    // The composer is refocused with the caret parked at the end, so the user
    // can type the skill name straight after clicking.
    expect(document.activeElement).toBe(screen.getByLabelText('Message input'))
  })

  it('picking a trigger closes the + menu', () => {
    renderWithProviders(<ChatInput {...base} onChange={vi.fn()} />)
    openPlusMenu()
    expect(screen.getByTitle('Use a skill')).toBeInTheDocument()
    fireEvent.click(screen.getByTitle('Use a skill'))
    expect(screen.queryByTitle('Use a skill')).toBeNull()
  })

  it('a mousedown outside the portaled menu closes it', () => {
    renderWithProviders(<ChatInput {...base} onChange={vi.fn()} />)
    openPlusMenu()
    expect(screen.getByTitle('Slash commands')).toBeInTheDocument()
    fireEvent.mouseDown(document.body)
    expect(screen.queryByTitle('Slash commands')).toBeNull()
  })

  it('a mousedown INSIDE the portaled menu keeps it open', () => {
    renderWithProviders(<ChatInput {...base} onChange={vi.fn()} />)
    openPlusMenu()
    fireEvent.mouseDown(screen.getByTitle('Slash commands'))
    expect(screen.getByTitle('Slash commands')).toBeInTheDocument()
  })

  it('forwards a picked file to the upload handler and clears the input for re-selection', () => {
    const onUploadFiles = vi.fn()
    renderWithProviders(<ChatInput {...base} onUploadFiles={onUploadFiles} />)
    const picker = screen.getByLabelText('Attach files') as HTMLInputElement
    const file = new File(['x'], 'notes.txt', { type: 'text/plain' })
    fireEvent.change(picker, { target: { files: [file] } })
    expect(onUploadFiles).toHaveBeenCalledWith([file])
    expect(picker.value).toBe('')
  })

  it('an empty file-picker change does not call the upload handler', () => {
    const onUploadFiles = vi.fn()
    renderWithProviders(<ChatInput {...base} onUploadFiles={onUploadFiles} />)
    fireEvent.change(screen.getByLabelText('Attach files'), { target: { files: [] } })
    expect(onUploadFiles).not.toHaveBeenCalled()
  })
})

describe('ChatInput dictation: Escape cancels from anywhere', () => {
  const base = { value: '', onChange: vi.fn(), onSend: vi.fn() }

  it('a document-level Escape cancels an in-flight recording', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceCancel={onVoiceCancel} onVoiceToggle={vi.fn()} />)
    // Deliberately dispatched on document, not the textarea: starting a
    // recording leaves focus on the mic button, so a textarea-scoped listener
    // would advertise "Esc to cancel" and do nothing.
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onVoiceCancel).toHaveBeenCalledTimes(1)
  })

  it('falls back to the toggle handler when no dedicated cancel is supplied', () => {
    const onVoiceToggle = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceToggle={onVoiceToggle} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onVoiceToggle).toHaveBeenCalledTimes(1)
  })

  it('defers to an open dialog — Escape belongs to the topmost dismissible surface', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceCancel={onVoiceCancel} />)
    const dialog = document.createElement('div')
    dialog.setAttribute('role', 'dialog')
    document.body.appendChild(dialog)
    try {
      fireEvent.keyDown(document, { key: 'Escape' })
      expect(onVoiceCancel).not.toHaveBeenCalled()
    } finally {
      dialog.remove()
    }
  })

  it('ignores a key that is not Escape', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(<ChatInput {...base} voiceRecording onVoiceCancel={onVoiceCancel} />)
    fireEvent.keyDown(document, { key: 'a' })
    expect(onVoiceCancel).not.toHaveBeenCalled()
  })

  it('does nothing while no recording is in flight', () => {
    const onVoiceCancel = vi.fn()
    renderWithProviders(<ChatInput {...base} onVoiceCancel={onVoiceCancel} />)
    fireEvent.keyDown(document, { key: 'Escape' })
    expect(onVoiceCancel).not.toHaveBeenCalled()
  })
})

describe('ChatInput context-usage popover', () => {
  // The context chip lives on the shelf row, which only mounts when the host
  // supplies at least one shelf control — onProjectClick is the cheapest.
  const base = { value: '', onChange: vi.fn(), onSend: vi.fn(), onProjectClick: vi.fn() }

  it('opens a breakdown with used and remaining token counts', () => {
    renderWithProviders(
      <ChatInput {...base} contextPct={42} contextWindowTokens={200_000} contextUsedTokens={84_000} />,
    )
    fireEvent.click(screen.getByLabelText('Context usage'))
    expect(screen.getByText('Context window')).toBeInTheDocument()
    expect(screen.getByText('42%')).toBeInTheDocument()
    expect(screen.getByText('84K')).toBeInTheDocument()
    expect(screen.getByText('116K')).toBeInTheDocument()
  })

  it('derives an approximate used count when the host reports only a percentage', () => {
    renderWithProviders(<ChatInput {...base} contextPct={40} contextWindowTokens={100_000} />)
    fireEvent.click(screen.getByLabelText('Context usage'))
    // No exact used-token figure, so both figures are derived from the
    // percentage and flagged approximate with a leading tilde.
    expect(screen.getByText('~40K')).toBeInTheDocument()
    expect(screen.getByText('~60K')).toBeInTheDocument()
    expect(screen.getByText('100K')).toBeInTheDocument()
  })

  it('clicking the chip again closes the popover', () => {
    renderWithProviders(<ChatInput {...base} contextPct={10} contextWindowTokens={100_000} />)
    const chip = screen.getByLabelText('Context usage')
    fireEvent.click(chip)
    expect(screen.getByText('Context window')).toBeInTheDocument()
    fireEvent.click(chip)
    expect(screen.queryByText('Context window')).toBeNull()
  })

  it('a mousedown outside the popover closes it', () => {
    renderWithProviders(<ChatInput {...base} contextPct={10} contextWindowTokens={100_000} />)
    fireEvent.click(screen.getByLabelText('Context usage'))
    fireEvent.mouseDown(document.body)
    expect(screen.queryByText('Context window')).toBeNull()
  })

  it('renders no context chip at all when the host reports no percentage', () => {
    renderWithProviders(<ChatInput {...base} />)
    expect(screen.queryByLabelText('Context usage')).toBeNull()
  })

  // ── per-session auto-compact threshold (slider section) ──────────────────
  // The section fetches lazily on popover open, so each test mocks the GET
  // and wraps ChatInput in a SlotProvider to give it a slot to fetch for.

  const renderWithSlot = (ui: React.ReactElement) =>
    renderWithProviders(<SlotProvider slotId="test-slot">{ui}</SlotProvider>)

  it('shows the override value with a reset-to-global link when overridden', async () => {
    vi.spyOn(api, 'chatSlotAutocompact').mockResolvedValue({ pct: 85, global_pct: 70, min: 5, max: 90 })
    const post = vi.spyOn(api, 'setChatSlotAutocompact').mockResolvedValue({ ok: true, pct: null, global_pct: 70 })
    renderWithSlot(<ChatInput {...base} contextPct={42} contextWindowTokens={200_000} />)
    fireEvent.click(screen.getByLabelText('Context usage'))
    expect(await screen.findByText('Auto-compact at')).toBeInTheDocument()
    expect(screen.getByText('85%')).toBeInTheDocument()
    // Clearing posts null for THIS slot (debounced, hence waitFor).
    fireEvent.click(screen.getByText('Reset to global (70%)'))
    await waitFor(() => expect(post).toHaveBeenCalledWith('test-slot', null), { timeout: 1500 })
  })

  it('shows the global value and a following-global note when not overridden', async () => {
    vi.spyOn(api, 'chatSlotAutocompact').mockResolvedValue({ pct: null, global_pct: 70, min: 5, max: 90 })
    renderWithSlot(<ChatInput {...base} contextPct={42} contextWindowTokens={200_000} />)
    fireEvent.click(screen.getByLabelText('Context usage'))
    expect(await screen.findByText('Following global (70%)')).toBeInTheDocument()
    expect(screen.getByText('70%')).toBeInTheDocument()
    expect(screen.queryByText(/Reset to global/)).toBeNull()
  })

  it('explains a failed threshold fetch instead of silently omitting the section', async () => {
    vi.spyOn(api, 'chatSlotAutocompact').mockRejectedValue(new Error('offline'))
    renderWithSlot(<ChatInput {...base} contextPct={42} contextWindowTokens={200_000} />)
    fireEvent.click(screen.getByLabelText('Context usage'))
    expect(await screen.findByText('Context window')).toBeInTheDocument()
    await waitFor(() => expect(api.chatSlotAutocompact).toHaveBeenCalled())
    expect(screen.queryByText('Auto-compact at')).toBeNull()
    // The learned control must not vanish unexplained: a muted one-liner
    // takes the section's place.
    expect(await screen.findByText("Couldn't load the auto-compact setting")).toBeInTheDocument()
  })

  it('refetches the applied threshold when a write is rejected (no false cache)', async () => {
    // Regression: a rejected POST used to leave the optimistic value cached,
    // showing a threshold the session never applied.
    const get = vi
      .spyOn(api, 'chatSlotAutocompact')
      .mockResolvedValue({ pct: 85, global_pct: 70, min: 5, max: 90 })
    vi.spyOn(api, 'setChatSlotAutocompact').mockRejectedValue(new Error('rejected'))
    renderWithSlot(<ChatInput {...base} contextPct={42} contextWindowTokens={200_000} />)
    fireEvent.click(screen.getByLabelText('Context usage'))
    expect(await screen.findByText('85%')).toBeInTheDocument()
    // Trigger a write via the reset link; the POST rejects.
    fireEvent.click(screen.getByText('Reset to global (70%)'))
    // onError invalidates the query -> a refetch restores server truth.
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThanOrEqual(2), { timeout: 2500 })
    expect(await screen.findByText('85%')).toBeInTheDocument()
  })

  it('surfaces a rejected write as a visible notice, like the sibling slot settings', async () => {
    // Regression (UX review): a failed POST used to snap the slider back with
    // no explanation — and with the popover closed, no visible change at all,
    // while the session kept compacting at the old threshold.
    vi.spyOn(api, 'chatSlotAutocompact').mockResolvedValue({ pct: 85, global_pct: 70, min: 5, max: 90 })
    vi.spyOn(api, 'setChatSlotAutocompact').mockRejectedValue(new Error('threshold rejected'))
    const { store } = renderWithSlot(<ChatInput {...base} contextPct={42} contextWindowTokens={200_000} />)
    fireEvent.click(screen.getByLabelText('Context usage'))
    expect(await screen.findByText('85%')).toBeInTheDocument()
    fireEvent.click(screen.getByText('Reset to global (70%)'))
    await waitFor(() =>
      expect(store.getState().chat.agentSwitchNotice?.message).toBe('threshold rejected'),
    { timeout: 2500 })
  })

  it('flushes a pending debounced write on unmount instead of discarding it', async () => {
    // Regression: the unmount cleanup cancelled the sole POST, so changing
    // the slider and navigating away within 400ms silently kept the old
    // threshold on the server.
    vi.spyOn(api, 'chatSlotAutocompact').mockResolvedValue({ pct: 85, global_pct: 70, min: 5, max: 90 })
    const post = vi.spyOn(api, 'setChatSlotAutocompact').mockResolvedValue({ ok: true, pct: null, global_pct: 70 })
    post.mockClear()
    const { unmount } = renderWithSlot(<ChatInput {...base} contextPct={42} contextWindowTokens={200_000} />)
    fireEvent.click(screen.getByLabelText('Context usage'))
    expect(await screen.findByText('85%')).toBeInTheDocument()
    // Schedule the debounced write, then unmount before the 400ms timer fires.
    fireEvent.click(screen.getByText('Reset to global (70%)'))
    expect(post).not.toHaveBeenCalled()
    unmount()
    await waitFor(() => expect(post).toHaveBeenCalledWith('test-slot', null))
  })

  it('flushes a pending write for another slot when a new slot writes within the debounce window', async () => {
    // Regression (GPT round 5): the debounce clearTimeout discarded a pending
    // write unconditionally, so drag on session A -> switch to B -> drag on B
    // within 400ms silently kept A's old threshold on the server. Debouncing
    // must only supersede same-slot writes; a different slot's pending write
    // is flushed immediately.
    vi.spyOn(api, 'chatSlotAutocompact').mockResolvedValue({ pct: 85, global_pct: 70, min: 5, max: 90 })
    const post = vi.spyOn(api, 'setChatSlotAutocompact').mockResolvedValue({ ok: true, pct: null, global_pct: 70 })
    post.mockClear()
    const { rerender } = renderWithProviders(
      <SlotProvider slotId="slot-a"><ChatInput {...base} contextPct={42} contextWindowTokens={200_000} /></SlotProvider>,
    )
    fireEvent.click(screen.getByLabelText('Context usage'))
    expect(await screen.findByText('85%')).toBeInTheDocument()
    // Schedule a debounced write for slot-a, then switch the active slot
    // before the 400ms timer fires.
    fireEvent.click(screen.getByText('Reset to global (70%)'))
    expect(post).not.toHaveBeenCalled()
    rerender(
      <SlotProvider slotId="slot-b"><ChatInput {...base} contextPct={42} contextWindowTokens={200_000} /></SlotProvider>,
    )
    // A write on slot-b inside the window must flush slot-a's pending write.
    fireEvent.click(await screen.findByText('Reset to global (70%)'))
    await waitFor(() => expect(post).toHaveBeenCalledWith('slot-a', null))
    // slot-b's own write still goes out on its debounce.
    await waitFor(() => expect(post).toHaveBeenCalledWith('slot-b', null), { timeout: 1500 })
  })

  it('serializes concurrent writes so a delayed earlier POST cannot overwrite a newer value', async () => {
    // Regression (GPT round 8): the debounced mutation, cross-slot flush and
    // unmount flush each POSTed independently, so two writes for the same slot
    // could be in flight at once and commit on the server in reverse order —
    // a delayed "reset" POST landing after a newer slider value silently
    // reverted the user's latest choice. Writes are now chained per slot:
    // a later write is not issued until the prior one settles.
    vi.spyOn(api, 'chatSlotAutocompact').mockResolvedValue({ pct: 85, global_pct: 70, min: 5, max: 90 })
    let resolveFirst!: (v: { ok: boolean; pct: number | null; global_pct: number }) => void
    const firstPost = new Promise<{ ok: boolean; pct: number | null; global_pct: number }>(res => { resolveFirst = res })
    const post = vi.spyOn(api, 'setChatSlotAutocompact')
    post.mockClear()
    post.mockReturnValueOnce(firstPost).mockResolvedValue({ ok: true, pct: 71, global_pct: 70 })
    renderWithSlot(<ChatInput {...base} contextPct={42} contextWindowTokens={200_000} />)
    fireEvent.click(screen.getByLabelText('Context usage'))
    expect(await screen.findByText('85%')).toBeInTheDocument()
    // First write: reset to global. Its POST stays in flight (deferred).
    fireEvent.click(screen.getByText('Reset to global (70%)'))
    await waitFor(() => expect(post).toHaveBeenCalledWith('test-slot', null), { timeout: 1500 })
    expect(post).toHaveBeenCalledTimes(1)
    // Second write while the first POST is still pending: nudge the slider.
    fireEvent.keyDown(screen.getByRole('slider'), { key: 'ArrowRight' })
    // Let the 400ms debounce elapse — WITHOUT serialization the second POST
    // fires here while the first is unresolved (post count hits 2).
    await new Promise(r => setTimeout(r, 600))
    expect(post).toHaveBeenCalledTimes(1)
    // Settle the first write; only then may the second be issued.
    resolveFirst({ ok: true, pct: null, global_pct: 70 })
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2), { timeout: 1500 })
    expect(post).toHaveBeenLastCalledWith('test-slot', 71)
  })
})
