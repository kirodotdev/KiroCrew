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

  it('ignores Escape on a locked box with no running turn', async () => {
    const onStop = vi.fn()
    renderWithProviders(<ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onStop={onStop} typingLock={LOCK} />)
    await settle()
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Escape' })
    expect(onStop).not.toHaveBeenCalled()
  })
})
