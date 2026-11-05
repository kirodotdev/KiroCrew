/**
 * The replay's overlay and labels: what each recorded action draws over the
 * picture (a ripple and chip for a click, a path for a drag, an arrow for a
 * scroll, a chip for keys, a banner for a navigation) and the few words that
 * name it, including a quick ask the person answered.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import '../i18n/all'
import { i18nT } from '../i18n/t'
import { containSize, eventLabel, PictureBox, ReplayOverlay } from '../pages/chat/BrowserLiveReplayStage'
import type { ReplayEvent } from '../hooks/useBrowserLive.replay'

function ev(over: Partial<ReplayEvent>): ReplayEvent {
  return {
    id: 'e1', ts: 0, op: 'click', label: '', args: {}, point: null, path: null, url: '',
    status: 'ok', actor: 'agent', loop: false, ...over,
  }
}

const SPACE = { w: 200, h: 100 }

describe('eventLabel', () => {
  it('prefers the event\'s own label, else names its op', () => {
    expect(eventLabel(ev({ label: 'polygon tool' }))).toBe('polygon tool')
    for (const op of ['click', 'type', 'key', 'drag', 'scroll'] as const) {
      expect(eventLabel(ev({ op }))).toBe(i18nT(`pages.chat.browserLiveStage.op_${op}`))
    }
    expect(eventLabel(ev({ op: 'navigate', url: 'https://example.com/' }))).toBe('https://example.com/')
    expect(eventLabel(ev({ op: 'navigate' }))).toBe(i18nT('pages.chat.browserLiveStage.op_navigate'))
    expect(eventLabel(ev({ op: 'tabs' as ReplayEvent['op'] }))).toBe(i18nT('pages.chat.browserLiveStage.op_other'))
  })

  it('names a quick ask the person answered in our own words', () => {
    const filled = eventLabel(ev({ op: 'person', label: 'ignored', person: { kind: 'credentials', action: 'fill' } as ReplayEvent['person'] }))
    expect(filled).toBe(i18nT('pages.chat.quickAsk.replay_label', {
      kind: i18nT('pages.chat.quickAsk.replay_kind_credentials'),
      action: i18nT('pages.chat.quickAsk.replay_action_fill'),
    }))
    const unknown = eventLabel(ev({ op: 'person', person: { kind: 'other', action: 'nothing' } as ReplayEvent['person'] }))
    expect(unknown).toBe(i18nT('pages.chat.quickAsk.replay_kind_other'))
    expect(eventLabel(ev({ op: 'person' }))).toBe(i18nT('pages.chat.quickAsk.replay_kind_other'))
  })
})

describe('ReplayOverlay', () => {
  afterEach(() => vi.restoreAllMocks())

  it('draws nothing with no event', () => {
    const { container } = render(<ReplayOverlay event={null} space={SPACE} />)
    expect(container.firstChild).toBeNull()
  })

  it('a click marks its point and names it', () => {
    render(<ReplayOverlay event={ev({ op: 'click', point: { x: 50, y: 25 }, label: 'Save' })} space={SPACE} />)
    const pt = screen.getByTestId('browser-replay-overlay-point')
    expect(pt.dataset.left).toBe('25.00')
    expect(pt.dataset.top).toBe('25.00')
    expect(screen.getByTestId('browser-replay-overlay-label').textContent).toBe('Save')
  })

  it('a drag draws its path on a canvas sized to the frame', () => {
    const ctx = {
      clearRect: vi.fn(), beginPath: vi.fn(), moveTo: vi.fn(), lineTo: vi.fn(), stroke: vi.fn(),
      strokeStyle: '', lineWidth: 0, lineCap: '', lineJoin: '',
    }
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(ctx as unknown as CanvasRenderingContext2D)
    const path = [{ x: 0, y: 0 }, { x: 100, y: 50 }, { x: 150, y: 80 }]
    render(<ReplayOverlay event={ev({ op: 'drag', point: { x: 0, y: 0 }, path })} space={SPACE} />)
    const canvas = screen.getByTestId('browser-replay-overlay-path') as HTMLCanvasElement
    expect(canvas.width).toBe(200)
    expect(ctx.moveTo).toHaveBeenCalledWith(0, 0)
    expect(ctx.lineTo).toHaveBeenCalledTimes(2)
    expect(ctx.stroke).toHaveBeenCalled()
  })

  it('a scroll shows an arrow at its point; an error turns the marks red', () => {
    render(<ReplayOverlay event={ev({ op: 'scroll', point: { x: 100, y: 50 }, args: { dy: 300 }, status: 'error' })} space={SPACE} />)
    const pt = screen.getByTestId('browser-replay-overlay-point')
    expect(pt.dataset.left).toBe('50.00')
    expect(pt.getAttribute('style')).toContain('var(--danger)')
  })

  it('keys with no point sit in a chip at the bottom; with a point, beside it', () => {
    const { unmount } = render(<ReplayOverlay event={ev({ op: 'key', label: 'Enter' })} space={SPACE} />)
    expect(screen.getByTestId('browser-replay-overlay-label').textContent).toBe('Enter')
    unmount()
    render(<ReplayOverlay event={ev({ op: 'type', point: { x: 20, y: 10 }, label: 'hello' })} space={SPACE} />)
    expect(screen.getByTestId('browser-replay-overlay-label').textContent).toBe('hello')
  })

  it('a navigation shows its URL in a banner, or its label without one', () => {
    const { unmount } = render(<ReplayOverlay event={ev({ op: 'navigate', url: 'https://example.com/a' })} space={SPACE} />)
    expect(screen.getByTestId('browser-replay-overlay-url').textContent).toBe('https://example.com/a')
    unmount()
    render(<ReplayOverlay event={ev({ op: 'navigate' })} space={SPACE} />)
    expect(screen.getByTestId('browser-replay-overlay-url').textContent).toBe(i18nT('pages.chat.browserLiveStage.op_navigate'))
  })
})

describe('picture sizing', () => {
  it('containSize fits the ratio inside the box and refuses nonsense', () => {
    expect(containSize(400, 400, 2)).toEqual({ w: 400, h: 200 })
    expect(containSize(400, 100, 2)).toEqual({ w: 200, h: 100 })
    expect(containSize(0, 100, 2)).toEqual({ w: 0, h: 0 })
    expect(containSize(100, 100, 0)).toEqual({ w: 0, h: 0 })
  })

  it('PictureBox falls back to the ratio when the host cannot be measured', () => {
    render(<PictureBox ratio={2} testId="box"><span /></PictureBox>)
    expect(screen.getByTestId('box').getAttribute('style')).toContain('aspect-ratio: 2')
  })
})
