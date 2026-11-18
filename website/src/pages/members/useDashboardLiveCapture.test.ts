// `dashboard_capture`, this tab's half: a Bot reads its own live page (#36935).
// Pins: a tab answers only when the gateway asks, only while visible, only for the
// slug it shows, with the sign up while it does, and never past the gateway's caps.
import { act, renderHook } from '@testing-library/react'
import { useRef } from 'react'
import { api } from '../../api/client'
import { handleDashboardCapture } from '../../hooks/useWebSocket'
import {
  CAPTURE_FIELDS_MAX, CAPTURE_MESSAGE_TYPE, CAPTURE_TEXT_MAX, CAPTURED_MESSAGE_TYPE,
  SIGN_MIN_MS, capturedReply, useDashboardLiveCapture,
} from './useDashboardLiveCapture'

vi.mock('../../api/client', async importOriginal => {
  const mod = await importOriginal<typeof import('../../api/client')>()
  return { ...mod, api: { ...mod.api, memberDashboardCapture: vi.fn(() => Promise.resolve({ ok: true })) } }
})

const send = vi.mocked(api.memberDashboardCapture)

/** A stand-in frame window that answers a capture request the way `_CAPTURE_JS` does. */
function fakeFrame(answer: (nonce: string) => unknown) {
  const win = {
    postMessage: vi.fn((data: { type: string; nonce: string }) => {
      if (data.type !== CAPTURE_MESSAGE_TYPE) return
      const reply = answer(data.nonce)
      if (reply) window.dispatchEvent(new MessageEvent('message', { data: reply, source: win as unknown as Window }))
    }),
  }
  return win
}

/** *shown*: what the frame's own rendering says -- `false` is a pane kept mounted
 *  behind another side-panel view (`hidden`, so no client rects). *covered*: another
 *  layer (the reply-thread panel, a dialog) is the topmost thing over the frame. */
function mount(slug: string, win: unknown, runsScript = true, shown: boolean | 'hidden-style' = true, covered = false) {
  const frame = {
    contentWindow: win,
    getClientRects: () => (shown === false ? [] : [{}]),
    getBoundingClientRect: () => ({ left: 0, top: 0, right: 400, bottom: 300 }),
    checkVisibility: () => shown === true,
  }
  vi.spyOn(document, 'elementFromPoint').mockReturnValue(covered ? document.createElement('div') : (frame as unknown as Element))
  return renderHook(() => {
    const ref = useRef(frame as unknown as HTMLIFrameElement)
    return useDashboardLiveCapture({ slug, frameRef: ref, runsScript })
  })
}

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => state })
}

beforeEach(() => {
  send.mockClear()
  setVisibility('visible')
  vi.useFakeTimers()
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it('answers a request for the shown page with its text, and shows the sign while it does', async () => {
  const win = fakeFrame(nonce => ({ type: CAPTURED_MESSAGE_TYPE, nonce, text: 'Queue 3', fields: [{ label: 'Note', value: 'hi' }], truncated: false }))
  const { result } = mount('atlas', win)
  expect(result.current).toBe(false)
  await act(async () => { handleDashboardCapture({ slug: 'atlas', request_id: 'c_1' }) })
  expect(result.current).toBe(true)
  expect(send).toHaveBeenCalledWith('atlas', {
    request_id: 'c_1', slug: 'atlas', status: 'captured', text: 'Queue 3', fields: [{ label: 'Note', value: 'hi' }], truncated: false,
  })
  // The sign stays up for at least SIGN_MIN_MS, then goes.
  await act(async () => { vi.advanceTimersByTime(SIGN_MIN_MS - 100) })
  expect(result.current).toBe(true)
  await act(async () => { vi.advanceTimersByTime(200) })
  expect(result.current).toBe(false)
})

it('never reads or sends the page unless the gateway asked', async () => {
  const win = fakeFrame(() => null)
  mount('atlas', win)
  await act(async () => { vi.advanceTimersByTime(10_000) })
  expect(win.postMessage).not.toHaveBeenCalled()
  expect(send).not.toHaveBeenCalled()
})

it('a hidden tab stays silent, so the sign is never shown off screen', async () => {
  setVisibility('hidden')
  const win = fakeFrame(nonce => ({ type: CAPTURED_MESSAGE_TYPE, nonce, text: 'x', fields: [], truncated: false }))
  const { result } = mount('atlas', win)
  await act(async () => { handleDashboardCapture({ slug: 'atlas', request_id: 'c_1' }) })
  expect(result.current).toBe(false)
  expect(win.postMessage).not.toHaveBeenCalled()
  expect(send).not.toHaveBeenCalled()
})

it('a Dashboard pane kept mounted behind another side-panel view stays silent', async () => {
  const win = fakeFrame(nonce => ({ type: CAPTURED_MESSAGE_TYPE, nonce, text: 'x', fields: [], truncated: false }))
  const { result } = mount('atlas', win, true, false)
  await act(async () => { handleDashboardCapture({ slug: 'atlas', request_id: 'c_1' }) })
  expect(result.current).toBe(false)
  expect(win.postMessage).not.toHaveBeenCalled()
  expect(send).not.toHaveBeenCalled()
})

it('a frame the browser reports invisible (visibility or opacity) stays silent', async () => {
  const win = fakeFrame(nonce => ({ type: CAPTURED_MESSAGE_TYPE, nonce, text: 'x', fields: [], truncated: false }))
  mount('atlas', win, true, 'hidden-style')
  await act(async () => { handleDashboardCapture({ slug: 'atlas', request_id: 'c_1' }) })
  expect(win.postMessage).not.toHaveBeenCalled()
  expect(send).not.toHaveBeenCalled()
})

it('a frame under another layer (the reply-thread panel, a dialog) stays silent', async () => {
  const win = fakeFrame(nonce => ({ type: CAPTURED_MESSAGE_TYPE, nonce, text: 'x', fields: [], truncated: false }))
  const { result } = mount('atlas', win, true, true, true)
  await act(async () => { handleDashboardCapture({ slug: 'atlas', request_id: 'c_1' }) })
  expect(result.current).toBe(false)
  expect(win.postMessage).not.toHaveBeenCalled()
  expect(send).not.toHaveBeenCalled()
})

it('a request for another Bot is not answered from this page', async () => {
  const win = fakeFrame(nonce => ({ type: CAPTURED_MESSAGE_TYPE, nonce, text: 'x', fields: [], truncated: false }))
  mount('atlas', win)
  await act(async () => { handleDashboardCapture({ slug: 'scout', request_id: 'c_1' }) })
  expect(win.postMessage).not.toHaveBeenCalled()
  expect(send).not.toHaveBeenCalled()
})

it('a preview or template mount (no slug) answers nothing', async () => {
  const win = fakeFrame(() => null)
  mount('', win)
  await act(async () => { handleDashboardCapture({ slug: 'atlas', request_id: 'c_1' }) })
  expect(send).not.toHaveBeenCalled()
})

it('a page served without script is reported unavailable, never read', async () => {
  const win = fakeFrame(() => null)
  mount('atlas', win, false)
  await act(async () => { handleDashboardCapture({ slug: 'atlas', request_id: 'c_1' }) })
  expect(win.postMessage).not.toHaveBeenCalled()
  expect(send).toHaveBeenCalledWith('atlas', { request_id: 'c_1', slug: 'atlas', status: 'unavailable', reason: 'static_page' })
})

it('an answer with the wrong nonce is ignored and the tab reports not loaded', async () => {
  const win = fakeFrame(() => ({ type: CAPTURED_MESSAGE_TYPE, nonce: 'forged', text: 'x', fields: [], truncated: false }))
  mount('atlas', win)
  await act(async () => { handleDashboardCapture({ slug: 'atlas', request_id: 'c_1' }) })
  await act(async () => { vi.advanceTimersByTime(2000) })
  expect(send).toHaveBeenCalledWith('atlas', { request_id: 'c_1', slug: 'atlas', status: 'unavailable', reason: 'not_loaded' })
})

it('cuts an answer to the gateway caps and says it did', () => {
  const reply = capturedReply('c_1', 'atlas', {
    text: 'x'.repeat(CAPTURE_TEXT_MAX + 5),
    fields: Array.from({ length: CAPTURE_FIELDS_MAX + 3 }, () => ({ label: 'a', value: 'v' })),
    truncated: false,
  })
  expect(reply && reply.status === 'captured' && reply.text.length).toBe(CAPTURE_TEXT_MAX)
  expect(reply && reply.status === 'captured' && reply.fields.length).toBe(CAPTURE_FIELDS_MAX)
  expect(reply && reply.status === 'captured' && reply.truncated).toBe(true)
})
