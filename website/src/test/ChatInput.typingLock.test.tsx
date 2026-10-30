/**
 * The composer while someone else holds the session's typing lease: read-only,
 * the button row replaced by "<name> is typing…", the user's own draft kept,
 * focus kept through the unlock, and Stop and Steer still live for a running
 * turn.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { act, fireEvent, screen } from '@testing-library/react'
import ChatInput from '../components/ChatInput'
import { renderWithProviders } from './helpers'
import { stubStripHeights } from './stripHeights'
import { LAZY_COMPOSER_MOUNT } from './lazyComposerReady'
import { TREE_ENTRY_DRAG_TYPE, encodeTreeEntry } from '../lib/treeEntryDrag'

const LOCK = { holderName: 'Alice', holderInitials: 'A' }

/** Let the providers' own async first reads land inside a barrier. */
const settle = () => act(async () => { await new Promise(resolve => setTimeout(resolve, 0)) })

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
})

describe('ChatInput typing lock', () => {
  it('shows the calm banner and makes the box read-only with the draft kept', async () => {
    const onSend = vi.fn()
    const onChange = vi.fn()
    renderWithProviders(<ChatInput value="my own draft" onChange={onChange} onSend={onSend} typingLock={LOCK} />)
    await settle()
    const banner = screen.getByTestId('composer-typing-lock')
    expect(banner).toHaveAttribute('role', 'status')
    expect(banner).toHaveTextContent('Alice is typing…')
    expect(banner).toHaveTextContent('A')
    const box = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(box.value).toBe('my own draft')
    expect(box.readOnly).toBe(true)
    expect(box).toHaveAttribute('aria-readonly', 'true')
    // No Send in the row, and Enter sends nothing.
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull()
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('names the holder in the placeholder of an empty locked box', async () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} typingLock={LOCK} />)
    await settle()
    expect(screen.getByLabelText('Message input')).toHaveAttribute('placeholder', 'Alice is typing…')
  })

  it('unlocks in place with focus kept when the lease ends', async () => {
    const onSend = vi.fn()
    const { rerender } = renderWithProviders(<ChatInput value="kept" onChange={vi.fn()} onSend={onSend} typingLock={LOCK} />)
    await settle()
    const box = screen.getByLabelText('Message input') as HTMLTextAreaElement
    box.focus()
    expect(document.activeElement).toBe(box)
    rerender(<ChatInput value="kept" onChange={vi.fn()} onSend={onSend} typingLock={null} />)
    const after = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(after).toBe(box)
    expect(document.activeElement).toBe(after)
    expect(after.readOnly).toBe(false)
    expect(after.value).toBe('kept')
    expect(screen.queryByTestId('composer-typing-lock')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    expect(onSend).toHaveBeenCalled()
  })

  it('keeps Stop for a running turn', async () => {
    const onStop = vi.fn()
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} isRunning onStop={onStop} typingLock={LOCK} />)
    await settle()
    expect(screen.getByTestId('composer-typing-lock')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /stop/i }))
    expect(onStop).toHaveBeenCalled()
  })

  it('keeps Steer for a running turn', async () => {
    const onSteer = vi.fn()
    const onSend = vi.fn()
    renderWithProviders(
      <ChatInput value="turn left" onChange={vi.fn()} onSend={onSend} isRunning canSteer onSteer={onSteer} busyMode="steer-only" onStop={vi.fn()} typingLock={LOCK} />,
    )
    await settle()
    fireEvent.click(screen.getByTestId('steer-only-send'))
    expect(onSteer.mock.calls.length + onSend.mock.calls.length).toBeGreaterThan(0)
  })

  it('takes no drop of any kind while locked, so the kept draft never changes', async () => {
    const onTreeEntryDrop = vi.fn()
    const onDrop = vi.fn()
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="my own draft" onChange={onChange} onSend={vi.fn()} project="/repo" onTreeEntryDrop={onTreeEntryDrop} onDrop={onDrop} typingLock={LOCK} />,
    )
    await settle()
    const tree = {
      types: ['text/plain', TREE_ENTRY_DRAG_TYPE],
      getData: (t: string) => (t === TREE_ENTRY_DRAG_TYPE ? encodeTreeEntry({ path: '/repo/src', kind: 'dir' }) : '/repo/src'),
      setData: vi.fn(), items: [], files: [], effectAllowed: 'copyMove', dropEffect: 'none',
    }
    const files = { types: ['Files'], getData: () => '', setData: vi.fn(), items: [{ kind: 'file' }], files: [], effectAllowed: 'copyMove', dropEffect: 'none' }
    for (const target of [screen.getByLabelText('Message input'), screen.getByTestId('input-wrapper')]) {
      fireEvent.drop(target, { dataTransfer: tree })
      fireEvent.drop(target, { dataTransfer: files })
    }
    expect(onTreeEntryDrop).not.toHaveBeenCalled()
    expect(onDrop).not.toHaveBeenCalled()
    expect(onChange).not.toHaveBeenCalled()
  })

  it('steers a running turn with the send key from the locked rich editor', async () => {
    const onSteer = vi.fn()
    const onSend = vi.fn()
    renderWithProviders(
      <ChatInput value="turn left" onChange={vi.fn()} onSend={onSend} lexicalComposer isRunning canSteer onSteer={onSteer} busyMode="steer-only" onStop={vi.fn()} typingLock={LOCK} />,
    )
    await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    // The non-editable editor drops focus to the body; the press is aimed inside the composer.
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter' })
    expect(onSteer.mock.calls.length + onSend.mock.calls.length).toBe(1)
    fireEvent.keyDown(screen.getByRole('textbox'), { key: 'Enter', shiftKey: true })
    expect(onSteer.mock.calls.length + onSend.mock.calls.length).toBe(1)
  })

  it('copies a paste token on cut in the locked box, and leaves the draft alone', async () => {
    const token = '[ Paste #1 · 3 lines ]'
    const onChange = vi.fn()
    const pasteBlocks = [{ id: 'c1', seq: 1, lines: 3, content: 'one\ntwo\nthree' }]
    renderWithProviders(
      <ChatInput value={`${token} rest`} onChange={onChange} onSend={vi.fn()} pasteBlocks={pasteBlocks} onPasteBlocksChange={vi.fn()} typingLock={LOCK} />,
    )
    await settle()
    const box = screen.getByLabelText('Message input') as HTMLTextAreaElement
    box.focus()
    box.setSelectionRange(0, token.length)
    const setData = vi.fn()
    fireEvent.cut(box, { clipboardData: { setData, getData: () => '' } })
    expect(setData).toHaveBeenCalledWith('text/plain', expect.stringContaining('one\ntwo\nthree'))
    expect(onChange).not.toHaveBeenCalled()
  })

  it('leaves Enter on a button beside the locked rich editor to that button', async () => {
    const onSteer = vi.fn()
    const onSend = vi.fn()
    renderWithProviders(
      <ChatInput value="turn left" onChange={vi.fn()} onSend={onSend} lexicalComposer isRunning canSteer onSteer={onSteer} busyMode="steer-only" onStop={vi.fn()} typingLock={LOCK} />,
    )
    await screen.findByRole('textbox', undefined, LAZY_COMPOSER_MOUNT)
    const button = screen.getByTestId('input-wrapper').querySelector('button')
    expect(button).not.toBeNull()
    const notPrevented = fireEvent.keyDown(button!, { key: 'Enter' })
    expect(notPrevented).toBe(true)
    expect(onSteer.mock.calls.length + onSend.mock.calls.length).toBe(0)
  })

  it('stops a running turn with Escape from the locked box', async () => {
    const onStop = vi.fn()
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} isRunning onStop={onStop} typingLock={LOCK} />)
    await settle()
    const box = screen.getByLabelText('Message input')
    box.focus()
    fireEvent.keyDown(box, { key: 'Escape' })
    expect(onStop).toHaveBeenCalled()
  })

  it('steers a running turn with Enter from the locked box, and leaves the draft alone', async () => {
    const onSteer = vi.fn()
    const onSend = vi.fn()
    const onChange = vi.fn()
    renderWithProviders(
      <ChatInput value="turn left" onChange={onChange} onSend={onSend} isRunning canSteer onSteer={onSteer} busyMode="steer-only" onStop={vi.fn()} typingLock={LOCK} />,
    )
    await settle()
    const box = screen.getByLabelText('Message input')
    fireEvent.keyDown(box, { key: 'Enter' })
    expect(onSteer.mock.calls.length + onSend.mock.calls.length).toBeGreaterThan(0)
    fireEvent.keyDown(box, { key: 'ArrowUp' })
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true })
    expect(onChange).not.toHaveBeenCalled()
  })

  it('shows no Take over without the preview hand-off', async () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} typingLock={LOCK} />)
    await settle()
    expect(screen.queryByTestId('composer-take-over')).toBeNull()
  })

  it('offers Take over, which calls the takeover and leaves the draft alone', async () => {
    const onTakeOver = vi.fn()
    const onChange = vi.fn()
    renderWithProviders(<ChatInput value="my own draft" onChange={onChange} onSend={vi.fn()} typingLock={{ ...LOCK, onTakeOver }} />)
    await settle()
    const button = screen.getByRole('button', { name: 'Take over typing from Alice. Their draft stays with them.' })
    expect(button).toHaveTextContent('Take over')
    fireEvent.click(button)
    expect(onTakeOver).toHaveBeenCalledTimes(1)
    expect(onChange).not.toHaveBeenCalled()
    expect((screen.getByLabelText('Message input') as HTMLTextAreaElement).value).toBe('my own draft')
  })

  it('tells the previous holder who took over, with their draft kept', async () => {
    renderWithProviders(<ChatInput value="half a thought" onChange={vi.fn()} onSend={vi.fn()} typingLock={{ ...LOCK, tookOver: true, onTakeOver: vi.fn() }} />)
    await settle()
    const banner = screen.getByTestId('composer-typing-lock')
    expect(banner).toHaveTextContent('Alice took over typing')
    expect(banner).toHaveTextContent('Your draft is kept.')
    const box = screen.getByLabelText('Message input') as HTMLTextAreaElement
    expect(box.value).toBe('half a thought')
    expect(box.readOnly).toBe(true)
  })

  it('shows a failed takeover as an inline notice with the agent hand-off', async () => {
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} typingLock={{ ...LOCK, onTakeOver: vi.fn(), takeOverError: 'HTTP 500' }} />)
    await settle()
    const notice = screen.getByTestId('composer-take-over-error')
    expect(notice).toHaveTextContent('HTTP 500')
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('ignores Escape on a locked box with no running turn', async () => {
    const onStop = vi.fn()
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onStop={onStop} typingLock={LOCK} />)
    await settle()
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Escape' })
    expect(onStop).not.toHaveBeenCalled()
  })
})
