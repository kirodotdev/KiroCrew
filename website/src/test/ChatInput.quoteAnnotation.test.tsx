import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fireEvent, screen, waitFor } from '@testing-library/react'
import ChatInput from '../components/ChatInput'
import type { QuoteRef } from '../utils/quoteRefs'
import { renderWithProviders } from './helpers'

const INPUT_DRAG_MIN_H = 93
const MANUAL_H = 300
const QUOTE_STRIP_H = 37

const stagedQuote: QuoteRef = {
  key: 'quote-1',
  role: 'Assistant',
  time: '10:22',
  text: 'quoted context',
  mid: 'mid-1',
  ts: 'ts-1',
}

const defaultProps = {
  value: '',
  onChange: vi.fn(),
  onSend: vi.fn(),
}

function rect(height: number): DOMRect {
  return {
    height,
    width: 0,
    top: 0,
    left: 0,
    right: 0,
    bottom: height,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  } as DOMRect
}

function stubQuoteStripHeight(): void {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (
    this: HTMLElement,
  ) {
    const ownTestid = this.getAttribute('data-testid')
    const childTestid = this.firstElementChild?.getAttribute('data-testid')
    return rect(
      ownTestid === 'quote-annotation-strip' || childTestid === 'quote-annotation-strip'
        ? QUOTE_STRIP_H
        : 0,
    )
  })
  globalThis.ResizeObserver = class {
    constructor(private cb: ResizeObserverCallback) {}
    observe(target: Element) {
      this.cb([{ target } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver)
    }
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

beforeEach(() => {
  vi.restoreAllMocks()
  localStorage.clear()
  stubQuoteStripHeight()
})

describe('ChatInput staged quote integration', () => {
  it('enables and fires the idle Send action with only a quote staged', () => {
    const onSend = vi.fn()
    renderWithProviders(
      <ChatInput {...defaultProps} onSend={onSend} quotedReplies={[stagedQuote]} />,
    )

    const send = screen.getByRole('button', { name: 'Send' })
    expect(send).not.toBeDisabled()
    fireEvent.click(send)
    expect(onSend).toHaveBeenCalledTimes(1)
  })

  it('adds the measured quote strip to a manually resized composer floor', async () => {
    localStorage.setItem('mc-input-height', String(MANUAL_H))
    renderWithProviders(<ChatInput {...defaultProps} quotedReplies={[stagedQuote]} />)

    const textarea = screen.getByLabelText('Message input')
    const inputArea = textarea.closest('.input-area') as HTMLElement
    await waitFor(() => {
      expect(inputArea.style.minHeight).toBe(`${INPUT_DRAG_MIN_H + QUOTE_STRIP_H}px`)
    })
    expect(screen.getByTestId('input-wrapper').style.height).toBe(`${MANUAL_H}px`)
  })
})
