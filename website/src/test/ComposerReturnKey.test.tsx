import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'
import CrewComposer from '../pages/members/CrewComposer'
import { stubStripHeights } from './stripHeights'

/* The Return key in the two composers a person types into: the session chat's
 * ChatInput and a Bot's chat (CrewComposer, the same ChatInput without the
 * session chips). Both read one rule (useComposerSendMode) and one IME guard
 * (useImeGuard), so every case runs against both. */

const env = vi.hoisted(() => ({ mobile: false, touch: false }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => env.mobile }))
vi.mock('../hooks/useIsTouchDevice', () => ({ useIsTouchDevice: () => env.touch }))

const composers = [
  ['session composer', ChatInput],
  ["Bot's composer", CrewComposer],
] as const

function mount(Composer: (typeof composers)[number][1]) {
  const onSend = vi.fn()
  const onChange = vi.fn()
  renderWithProviders(<Composer value="draft" onChange={onChange} onSend={onSend} />)
  return { ta: screen.getByLabelText('Message input'), onSend, onChange }
}

beforeEach(() => {
  vi.restoreAllMocks()
  stubStripHeights()
  localStorage.clear()
  env.mobile = false
  env.touch = false
})

describe.each(composers)('%s', (_name, Composer) => {
  describe('on a phone', () => {
    beforeEach(() => { env.mobile = true; env.touch = true })

    it('Return inserts a new line and does not send', () => {
      const { ta, onSend } = mount(Composer)
      // `fireEvent` returns true when nothing called preventDefault: the
      // textarea keeps the key and adds the line.
      expect(fireEvent.keyDown(ta, { key: 'Enter' })).toBe(true)
      expect(onSend).not.toHaveBeenCalled()
    })

    it('the Send button still sends', () => {
      const { onSend } = mount(Composer)
      fireEvent.click(screen.getByRole('button', { name: /^send/i }))
      expect(onSend).toHaveBeenCalledOnce()
    })
  })

  // An enterprise in-app browser on an iPhone: the page is drawn 980px wide
  // (desktop form) and the user agent names no phone, but the device is
  // touch-first and its screen is a phone's.
  describe('on a phone whose browser draws the page desktop-wide', () => {
    function phoneScreen(width: number, height: number) {
      env.touch = true
      vi.spyOn(window, 'matchMedia').mockImplementation((q: string) => ({
        matches: q === '(pointer: coarse)' || q === '(hover: none)',
        media: q, onchange: null,
        addListener: vi.fn(), removeListener: vi.fn(),
        addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
      }) as MediaQueryList)
      vi.spyOn(window.screen, 'width', 'get').mockReturnValue(width)
      vi.spyOn(window.screen, 'height', 'get').mockReturnValue(height)
      vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko)')
    }

    it('Return inserts a new line and does not send', () => {
      phoneScreen(393, 852)
      const { ta, onSend } = mount(Composer)
      expect(fireEvent.keyDown(ta, { key: 'Enter' })).toBe(true)
      expect(onSend).not.toHaveBeenCalled()
    })

    it('a tablet screen keeps Return as send', () => {
      phoneScreen(820, 1180)
      const { ta, onSend } = mount(Composer)
      fireEvent.keyDown(ta, { key: 'Enter' })
      expect(onSend).toHaveBeenCalledOnce()
    })

    it('an iPad mini screen keeps Return as send', () => {
      phoneScreen(744, 1133)
      const { ta, onSend } = mount(Composer)
      fireEvent.keyDown(ta, { key: 'Enter' })
      expect(onSend).toHaveBeenCalledOnce()
    })
  })

  describe('on a desktop', () => {
    it('Return sends', () => {
      const { ta, onSend } = mount(Composer)
      fireEvent.keyDown(ta, { key: 'Enter' })
      expect(onSend).toHaveBeenCalledOnce()
    })
  })

  describe.each([['a phone', true], ['a desktop', false]])('a pinyin candidate Enter on %s never sends', (_where, phone) => {
    beforeEach(() => { env.mobile = phone; env.touch = phone })

    it('while composing (isComposing set)', () => {
      const { ta, onSend } = mount(Composer)
      fireEvent.compositionStart(ta)
      // The IME is consuming the key itself, so its default is left alone.
      expect(fireEvent.keyDown(ta, { key: 'Enter', isComposing: true })).toBe(true)
      expect(onSend).not.toHaveBeenCalled()
    })

    it('with keyCode 229 and no isComposing', () => {
      const { ta, onSend } = mount(Composer)
      expect(fireEvent.keyDown(ta, { key: 'Enter', keyCode: 229 })).toBe(true)
      expect(onSend).not.toHaveBeenCalled()
    })

    it('when compositionend fires before the Enter (Safari and macOS)', () => {
      const { ta, onSend, onChange } = mount(Composer)
      fireEvent.compositionStart(ta)
      fireEvent.compositionEnd(ta, { data: '你好' })
      // On a desktop the send key is claimed and consumed, so no line lands
      // either. On a phone plain Return is not the send key and is left to
      // the browser: an Android keyboard ends a word's composition on that
      // same Return, and swallowing it would lose the person's new line.
      expect(fireEvent.keyDown(ta, { key: 'Enter', keyCode: 13 })).toBe(phone)
      expect(onSend).not.toHaveBeenCalled()
      expect(onChange).not.toHaveBeenCalled()
    })

    it('a later Enter, past the window, acts again', () => {
      vi.useFakeTimers()
      try {
        const { ta, onSend } = mount(Composer)
        fireEvent.compositionStart(ta)
        fireEvent.compositionEnd(ta, { data: '你' })
        vi.advanceTimersByTime(60)
        const kept = fireEvent.keyDown(ta, { key: 'Enter' })
        if (phone) {
          expect(kept).toBe(true)
          expect(onSend).not.toHaveBeenCalled()
        } else {
          expect(onSend).toHaveBeenCalledOnce()
        }
      } finally {
        vi.useRealTimers()
      }
    })
  })
})
