/**
 * The composer shelf's backend-picker group shrinks with the shelf.
 *
 * The shelf collapses its chips by a measured width (`useShelfMeasure`:
 * compact < 340px, tiny < 220px). A fixed-width backend group that ignored that
 * signal pushed the context readout and the model chip -- the only host of the
 * model picker -- off a narrow pane. These tests pin that the group may shrink
 * and is capped tighter as the shelf narrows.
 *
 * jsdom does no layout, so the shelf width is fed through a stubbed
 * ResizeObserver that reports one fixed content width.
 */
import { describe, it, expect, afterEach } from 'vitest'
import { screen, cleanup, act } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

const originalRO = window.ResizeObserver

function stubShelfWidth(width: number) {
  window.ResizeObserver = class {
    private cb: ResizeObserverCallback
    constructor(cb: ResizeObserverCallback) { this.cb = cb }
    observe(target: Element) {
      this.cb([{ contentRect: { width }, target, borderBoxSize: [{ blockSize: 32 }] } as unknown as ResizeObserverEntry], this as unknown as ResizeObserver)
    }
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

const props = {
  value: '',
  onChange: () => {},
  onSend: () => {},
  backendPicker: <span>picker</span>,
}

const group = () => screen.getByTestId('composer-backend-group')

describe('ChatInput backend-picker group on a narrow shelf', () => {
  afterEach(() => { window.ResizeObserver = originalRO; cleanup() })

  it('is never shrink-0, so the row can give it up before the model chip', async () => {
    stubShelfWidth(800)
    await act(async () => { renderWithProviders(<ChatInput {...props} />) })
    expect(group()).not.toHaveClass('shrink-0')
    expect(group()).toHaveClass('min-w-0')
    expect(group().className).not.toMatch(/max-w-/)
  })

  it('is capped on a compact shelf', async () => {
    stubShelfWidth(300)
    await act(async () => { renderWithProviders(<ChatInput {...props} />) })
    expect(group()).toHaveClass('max-w-[120px]')
  })

  it('is capped tighter on a tiny shelf', async () => {
    stubShelfWidth(200)
    await act(async () => { renderWithProviders(<ChatInput {...props} />) })
    expect(group()).toHaveClass('max-w-[72px]')
  })

  it('puts the picker notice on its own row, outside the capped group', async () => {
    stubShelfWidth(300)
    await act(async () => {
      renderWithProviders(<ChatInput {...props} backendPickerNotice={<span data-testid="notice">refused</span>} />)
    })
    const notice = screen.getByTestId('notice')
    expect(group().contains(notice)).toBe(false)
    expect(screen.getByTestId('composer-backend-notice-row').contains(notice)).toBe(true)
    expect(screen.getByTestId('composer-backend-notice-row').className).not.toMatch(/max-w-/)
  })
})
