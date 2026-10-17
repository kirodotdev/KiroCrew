// Over the context window, the composer holds the first send and lets a repeat
// of the same draft through; under it, a send goes straight out.
import { afterEach, beforeEach, describe, it, expect, vi } from 'vitest'
import { fireEvent, screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

const OVER = 'x'.repeat(4_400) // ~1,100 tokens against a 1,000-token window

describe('ChatInput over-limit send', () => {
  // Each Date.now() read moves the clock a second on, so two sends in a row are
  // never inside the confirm dead time unless a test pins the clock.
  let clock = 0
  beforeEach(() => { clock = 0; vi.spyOn(Date, 'now').mockImplementation(() => (clock += 1_000)) })
  afterEach(() => { vi.restoreAllMocks() })

  it('keeps a quick double Enter held', () => {
    vi.spyOn(Date, 'now').mockReturnValue(5_000)
    const onSend = vi.fn()
    renderWithProviders(<ChatInput value={OVER} onChange={vi.fn()} onSend={onSend} contextPct={10} contextWindowTokens={1_000} />)
    const input = screen.getByLabelText('Message input')
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('holds the first Enter over the limit and sends on the second', () => {
    const onSend = vi.fn()
    renderWithProviders(<ChatInput value={OVER} onChange={vi.fn()} onSend={onSend} contextPct={10} contextWindowTokens={1_000} />)
    const input = screen.getByLabelText('Message input')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
    const notice = screen.getByTestId('prompt-length-notice')
    expect(notice).toHaveTextContent(/^Send held\. Prompt is ~\S+ over the 1K-token limit this model accepts\. Send again to send it anyway\.$/)
    expect(notice).not.toHaveTextContent('Trim it before sending.')
    expect(notice).toHaveAttribute('data-held', 'true')
    expect(screen.getByTestId('prompt-length-live')).toHaveTextContent('Send held')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('holds a Send button click the same way', () => {
    const onSend = vi.fn()
    renderWithProviders(<ChatInput value={OVER} onChange={vi.fn()} onSend={onSend} contextPct={10} contextWindowTokens={1_000} />)
    const send = screen.getAllByLabelText('Send')[0]
    fireEvent.click(send)
    expect(onSend).not.toHaveBeenCalled()
    fireEvent.click(send)
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  // Holding Enter with the Send button focused re-activates it: the browser
  // fires plain clicks with no repeat flag for the keydown guard to catch.
  it('holds a repeat train of Send button clicks', () => {
    let t = 5_000
    vi.spyOn(Date, 'now').mockImplementation(() => t)
    const onSend = vi.fn()
    renderWithProviders(<ChatInput value={OVER} onChange={vi.fn()} onSend={onSend} contextPct={10} contextWindowTokens={1_000} />)
    const send = screen.getAllByLabelText('Send')[0]
    fireEvent.click(send)
    t = 5_500 // the first auto-repeat
    for (; t <= 7_000; t += 30) fireEvent.click(send)
    expect(onSend).not.toHaveBeenCalled()
    t += 600 // key released, deliberate second press
    fireEvent.click(send)
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('holds a steer while running the same way', () => {
    const onSteer = vi.fn()
    const onSend = vi.fn()
    renderWithProviders(<ChatInput value={OVER} onChange={vi.fn()} onSend={onSend} onSteer={onSteer} onStop={vi.fn()} isRunning canSteer contextPct={10} contextWindowTokens={1_000} />)
    const input = screen.getByLabelText('Message input')
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSteer).not.toHaveBeenCalled()
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onSteer).toHaveBeenCalledTimes(1)
    expect(onSend).not.toHaveBeenCalled()
  })

  it('asks again when the draft changes between the two sends', () => {
    const onSend = vi.fn()
    const { rerender } = renderWithProviders(<ChatInput value={OVER} onChange={vi.fn()} onSend={onSend} contextPct={10} contextWindowTokens={1_000} />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    rerender(<ChatInput value={OVER + 'y'} onChange={vi.fn()} onSend={onSend} contextPct={10} contextWindowTokens={1_000} />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('does not treat a held Enter key auto-repeat as the confirming send', () => {
    const onSend = vi.fn()
    renderWithProviders(<ChatInput value={OVER} onChange={vi.fn()} onSend={onSend} contextPct={10} contextWindowTokens={1_000} />)
    const input = screen.getByLabelText('Message input')
    fireEvent.keyDown(input, { key: 'Enter' })
    fireEvent.keyDown(input, { key: 'Enter', repeat: true })
    expect(onSend).not.toHaveBeenCalled()
  })

  it('sends an ordinary prompt on the first Enter', () => {
    const onSend = vi.fn()
    renderWithProviders(<ChatInput value="short prompt" onChange={vi.fn()} onSend={onSend} contextPct={10} contextWindowTokens={1_000} />)
    fireEvent.keyDown(screen.getByLabelText('Message input'), { key: 'Enter' })
    expect(onSend).toHaveBeenCalledTimes(1)
  })
})
