/**
 * The session-expired banner's "Scan QR code" button (#21631).
 *
 * The desktop "Connect your phone" QR carries the same `https://<host>/?token=…`
 * URL the banner's paste field accepts. On a phone the camera app opens that URL in
 * the default browser, so when the dashboard runs in another browser the session is
 * restored in the wrong one. The button scans in the page and feeds the decoded
 * string into the SAME exchange the paste uses, which is what these cases pin: the
 * request shape, the refusal wording and the banner teardown are the paste's, and
 * the camera is released on every exit.
 *
 * `BarcodeDetector` and `navigator.mediaDevices` are installed per case; happy-dom
 * ships neither, so the "no button" case is also the environment every other
 * banner test runs in.
 */
import { createElement } from 'react'
import { render } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import {
  checkSessionExpired,
  removeAuthBanner,
  __resetAuthRecoveryStateForTests,
} from '../api/client'
import { qrScanSupported } from '../api/qrScan'
import { ConfirmDialog } from '../apps/md-notebook/ConfirmDialog'
import { res, fetchMock } from './ApiClient.coverageTestUtils'

const banner = (): HTMLElement | null => document.getElementById('mc-session-expired')
const scanButton = (): HTMLButtonElement | null =>
  Array.from(banner()?.querySelectorAll('button') ?? []).find(
    (b) => b.textContent === 'Scan QR code',
  ) ?? null
const overlay = (): HTMLElement | null =>
  document.querySelector('video')?.closest<HTMLElement>('[role="dialog"]') ?? null
const cancelScanButton = (): HTMLButtonElement | null =>
  overlay()?.querySelector<HTMLButtonElement>('button') ?? null
const failureText = (): string => banner()?.querySelector('[role="status"]')?.textContent ?? ''
const videoStream = (): unknown =>
  (overlay()?.querySelector('video') as HTMLVideoElement | null)?.srcObject

/**
 * Settle the banner's own support check. The banner calls `qrScanSupported()` when
 * it mounts and appends the button in its `.then`; this later call to the same
 * function resolves after that one, so by the time it answers, the earlier
 * callback has run and a button that was going to appear has appeared.
 */
async function settleSupportCheck(expected: boolean): Promise<void> {
  await expect(qrScanSupported()).resolves.toBe(expected)
}

class FakeDetector {
  static formats: string[] = ['qr_code']
  static decode: () => Promise<Array<{ rawValue: string }>> = async () => []
  static async getSupportedFormats(): Promise<string[]> {
    return FakeDetector.formats
  }
  detect(): Promise<Array<{ rawValue: string }>> {
    return FakeDetector.decode()
  }
}

function installDetector(formats = ['qr_code']): void {
  FakeDetector.formats = formats
  FakeDetector.decode = async () => []
  vi.stubGlobal('BarcodeDetector', FakeDetector)
}

/** One fake camera: the stream `getUserMedia` resolves and the spy on its track. */
function installCamera(impl?: () => Promise<MediaStream>) {
  const stop = vi.fn()
  // A real happy-dom MediaStream, since `video.srcObject` type-checks its value;
  // the track list is replaced so the test can watch `stop()`.
  const stream = new MediaStream()
  ;(stream as unknown as { getTracks: () => Array<{ stop: () => void }> }).getTracks = () => [{ stop }]
  const getUserMedia = vi.fn(impl ?? (() => Promise.resolve(stream)))
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: { getUserMedia },
  })
  return { stop, getUserMedia, stream }
}

function namedError(name: string): Error {
  const e = new Error(name)
  e.name = name
  return e
}

async function raiseBanner(): Promise<HTMLElement> {
  fetchMock.mockResolvedValue(res(401, 'revoked'))
  checkSessionExpired(res(403, '', { headers: { 'X-Auth-Required': 'true' } }))
  await vi.waitFor(() => expect(banner()).not.toBeNull())
  return banner() as HTMLElement
}

/** Resolve the decoder on the next frame with *value*, then click the button. */
async function scan(value: string | null): Promise<void> {
  await vi.waitFor(() => expect(scanButton()).not.toBeNull())
  FakeDetector.decode = async () => (value === null ? [] : [{ rawValue: value }])
  fetchMock.mockClear()
  scanButton()!.click()
  await vi.waitFor(() => expect(overlay()).not.toBeNull())
}

describe('session-expired banner: scan QR code', () => {
  let originalMediaDevices: PropertyDescriptor | undefined

  beforeEach(() => {
    originalMediaDevices = Object.getOwnPropertyDescriptor(navigator, 'mediaDevices')
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
    __resetAuthRecoveryStateForTests()
  })

  afterEach(() => {
    overlay()?.remove()
    vi.unstubAllGlobals()
    if (originalMediaDevices) Object.defineProperty(navigator, 'mediaDevices', originalMediaDevices)
    else delete (navigator as unknown as { mediaDevices?: unknown }).mediaDevices
    __resetAuthRecoveryStateForTests()
  })

  it('renders no button when BarcodeDetector is absent', async () => {
    installCamera()
    const el = await raiseBanner()
    await settleSupportCheck(false)
    expect(scanButton()).toBeNull()
    expect(el.querySelector('input')).not.toBeNull()
  })

  it('renders no button when the detector does not list qr_code', async () => {
    installDetector(['ean_13'])
    installCamera()
    const el = await raiseBanner()
    await settleSupportCheck(false)
    expect(scanButton()).toBeNull()
    expect(el.querySelector('input')).not.toBeNull()
  })

  it('renders no button when there is a detector but no getUserMedia', async () => {
    installDetector()
    Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: {} })
    await raiseBanner()
    await settleSupportCheck(false)
    expect(scanButton()).toBeNull()
  })

  it('renders the button once the detector reports qr_code support', async () => {
    installDetector()
    installCamera()
    const el = await raiseBanner()
    await vi.waitFor(() => expect(scanButton()).not.toBeNull())
    // Next to the paste field, and the paste field is still there.
    expect(el.querySelector('input')?.nextElementSibling).toBe(scanButton())
  })

  it('exchanges a scanned sign-in URL exactly like a paste and drops the banner', async () => {
    installDetector()
    const { getUserMedia, stop } = installCamera()
    await raiseBanner()
    fetchMock.mockResolvedValue(res(200, { token_accepted: true, owner_ok: true }))
    await scan('https://desk.example:6776/?token=abc&x=1')
    expect(getUserMedia).toHaveBeenCalledWith({ video: { facingMode: 'environment' } })
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(fetchMock.mock.calls[0][0]).toBe('/api/auth/me?token=abc')
    expect((fetchMock.mock.calls[0][1] as RequestInit).credentials).toBe('include')
    await vi.waitFor(() => expect(banner()).toBeNull())
    expect(overlay()).toBeNull()
    expect(stop).toHaveBeenCalled()
  })

  it('accepts a bare token from the code, as the paste field does', async () => {
    installDetector()
    installCamera()
    await raiseBanner()
    fetchMock.mockResolvedValue(res(200, { token_accepted: true, owner_ok: true }))
    await scan('rawtoken')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalled())
    expect(fetchMock.mock.calls[0][0]).toBe('/api/auth/me?token=rawtoken')
  })

  it('shows the paste refusal when the gateway refuses the scanned token', async () => {
    installDetector()
    installCamera()
    await raiseBanner()
    fetchMock.mockResolvedValue(res(401, 'expired'))
    await scan('https://desk.example:6776/?token=stale')
    await vi.waitFor(() => expect(failureText()).toContain('sign-in URL was not accepted'))
    expect(banner()).not.toBeNull()
    expect((banner()!.querySelector('input') as HTMLInputElement).disabled).toBe(false)
  })

  it('says not accepted for a code that is neither a sign-in URL nor a token', async () => {
    installDetector()
    installCamera()
    await raiseBanner()
    // A URL with no `token` parses, yields nothing to send, and must still get an
    // answer: a scanned code that silently does nothing reads as a broken button.
    await scan('https://example.org/menu')
    await vi.waitFor(() => expect(failureText()).toContain('sign-in URL was not accepted'))
    expect(fetchMock).not.toHaveBeenCalled()
    expect(overlay()).toBeNull()
  })

  it('words a denied camera permission and leaves the paste field usable', async () => {
    installDetector()
    installCamera(() => Promise.reject(namedError('NotAllowedError')))
    const el = await raiseBanner()
    await vi.waitFor(() => expect(scanButton()).not.toBeNull())
    fetchMock.mockClear()
    scanButton()!.click()
    await vi.waitFor(() => expect(failureText()).toContain('Camera access was denied'))
    expect(overlay()).toBeNull()
    const input = el.querySelector('input') as HTMLInputElement
    expect(input.disabled).toBe(false)
    expect(scanButton()!.disabled).toBe(false)
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('words a missing camera differently from a refused one', async () => {
    installDetector()
    installCamera(() => Promise.reject(namedError('NotFoundError')))
    await raiseBanner()
    await vi.waitFor(() => expect(scanButton()).not.toBeNull())
    scanButton()!.click()
    await vi.waitFor(() => expect(failureText()).toContain('camera could not be opened'))
    expect((banner()!.querySelector('input') as HTMLInputElement).disabled).toBe(false)
  })

  it('stops the camera track and closes the viewfinder on cancel, with no exchange', async () => {
    installDetector()
    const { stop } = installCamera()
    await raiseBanner()
    await scan(null)
    // The stream is attached before the cancel, so the cancel is what stops it.
    await vi.waitFor(() => expect(videoStream()).toBeTruthy())
    expect(stop).not.toHaveBeenCalled()
    const cancel = Array.from(overlay()!.querySelectorAll('button')).find(
      (b) => b.textContent === 'Cancel scanning',
    )
    expect(cancel).toBeDefined()
    cancel!.click()
    expect(overlay()).toBeNull()
    expect(stop).toHaveBeenCalledTimes(1)
    expect(fetchMock).not.toHaveBeenCalled()
    // Still on the banner, button usable again, nothing on the status line.
    expect(banner()).not.toBeNull()
    await vi.waitFor(() => expect(scanButton()!.disabled).toBe(false))
    expect(failureText()).toBe('')
  })

  it('stops the camera when the banner is removed while the viewfinder is open', async () => {
    installDetector()
    const { stop } = installCamera()
    const el = await raiseBanner()
    await scan(null)
    await vi.waitFor(() => expect(videoStream()).toBeTruthy())
    // A 2xx elsewhere, or the dismiss, tears the banner down without going through
    // the scan: the next frame must notice and release the camera.
    el.remove()
    await vi.waitFor(() => expect(stop).toHaveBeenCalledTimes(1))
    expect(overlay()).toBeNull()
  })

  it('restores the page when auth recovery removes the banner before camera permission settles', async () => {
    installDetector()
    const frames = new Map<number, FrameRequestCallback>()
    let nextFrame = 1
    vi.stubGlobal('requestAnimationFrame', ((callback: FrameRequestCallback) => {
      const id = nextFrame++
      frames.set(id, callback)
      return id
    }) as typeof requestAnimationFrame)
    vi.stubGlobal('cancelAnimationFrame', ((id: number) => {
      frames.delete(id)
    }) as typeof cancelAnimationFrame)
    let resolveCamera: (stream: MediaStream) => void = () => {}
    const { getUserMedia, stop, stream } = installCamera(
      () => new Promise<MediaStream>((resolve) => { resolveCamera = resolve }),
    )
    const page = document.createElement('main')
    document.body.append(page)
    try {
      await raiseBanner()
      await vi.waitFor(() => expect(scanButton()).not.toBeNull())
      scanButton()!.click()
      await vi.waitFor(() => expect(overlay()).not.toBeNull())
      expect(getUserMedia).toHaveBeenCalledOnce()
      expect(page.hasAttribute('inert')).toBe(true)

      removeAuthBanner()
      await vi.waitFor(() => expect(banner()).toBeNull())
      expect(overlay()).not.toBeNull()

      const due = Array.from(frames.values())
      frames.clear()
      expect(due.length).toBeGreaterThan(0)
      for (const callback of due) callback(0)
      expect(overlay()).toBeNull()
      expect(page.hasAttribute('inert')).toBe(false)

      resolveCamera(stream)
      await vi.waitFor(() => expect(stop).toHaveBeenCalledTimes(1))
    } finally {
      page.remove()
    }
  })

  it('never navigates: the scanned URL is exchanged in place', async () => {
    installDetector()
    installCamera()
    const href = window.location.href
    await raiseBanner()
    fetchMock.mockResolvedValue(res(200, { token_accepted: true, owner_ok: true }))
    await scan('https://desk.example:6776/?token=abc')
    await vi.waitFor(() => expect(banner()).toBeNull())
    expect(window.location.href).toBe(href)
  })

  it('sends one exchange when a second code or paste arrives while the first is in flight', async () => {
    installDetector()
    installCamera()
    const el = await raiseBanner()
    let release: (r: Response) => void = () => {}
    fetchMock.mockReturnValue(new Promise<Response>((resolve) => { release = resolve }))
    await scan('https://desk.example:6776/?token=first')
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    // Decoded, viewfinder gone, exchange still out: both entry points are held.
    expect(overlay()).toBeNull()
    expect(scanButton()!.disabled).toBe(true)
    const input = el.querySelector('input') as HTMLInputElement
    expect(input.disabled).toBe(true)
    // A second attempt through either path, while the first is unanswered, must not
    // start a second exchange: the running one is the answer to both.
    scanButton()!.click()
    input.value = 'https://desk.example:6776/?token=second'
    input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }))
    await Promise.resolve()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(overlay()).toBeNull()
    release(res(200, { token_accepted: true, owner_ok: true }))
    await vi.waitFor(() => expect(banner()).toBeNull())
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(fetchMock.mock.calls[0][0]).toBe('/api/auth/me?token=first')
  })

  it('stops the camera when the page is hidden while the viewfinder is open', async () => {
    installDetector()
    const { stop } = installCamera()
    await raiseBanner()
    await scan(null)
    await vi.waitFor(() => expect(videoStream()).toBeTruthy())
    expect(stop).not.toHaveBeenCalled()
    // A switched tab or a locked phone: the document reports hidden and fires
    // `visibilitychange`; the scan has nothing to look at and must release the camera.
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' })
    try {
      document.dispatchEvent(new Event('visibilitychange'))
    } finally {
      delete (document as unknown as { visibilityState?: unknown }).visibilityState
    }
    expect(stop).toHaveBeenCalledTimes(1)
    expect(overlay()).toBeNull()
    expect(fetchMock).not.toHaveBeenCalled()
    expect(banner()).not.toBeNull()
  })

  it('returns focus to the scan button when the viewfinder closes', async () => {
    installDetector()
    installCamera()
    await raiseBanner()
    await vi.waitFor(() => expect(scanButton()).not.toBeNull())
    scanButton()!.focus()
    expect(document.activeElement).toBe(scanButton())
    await scan(null)
    // Focus moved into the dialog, onto its one control.
    expect(document.activeElement?.textContent).toBe('Cancel scanning')
    ;(document.activeElement as HTMLButtonElement).click()
    expect(overlay()).toBeNull()
    expect(document.activeElement).toBe(scanButton())
  })

  it('makes the rest of the page inert while the viewfinder is open, and not after', async () => {
    installDetector()
    installCamera()
    const el = await raiseBanner()
    // A sibling that was already inert must come back inert, not enabled.
    const alreadyInert = document.createElement('div')
    alreadyInert.setAttribute('inert', '')
    document.body.append(alreadyInert)
    try {
      await scan(null)
      const input = el.querySelector('input') as HTMLInputElement
      expect(input.closest('[inert]')).not.toBeNull()
      expect(scanButton()!.closest('[inert]')).not.toBeNull()
      expect(overlay()!.closest('[inert]')).toBeNull()
      // `aria-modal` is a hint to assistive tech; `inert` is what keeps a tap on the
      // paste field, under the overlay, from landing.
      input.focus()
      expect(document.activeElement).not.toBe(input)
      Array.from(overlay()!.querySelectorAll('button'))
        .find((b) => b.textContent === 'Cancel scanning')!
        .click()
      expect(overlay()).toBeNull()
      expect(input.closest('[inert]')).toBeNull()
      expect(el.hasAttribute('inert')).toBe(false)
      expect(alreadyInert.hasAttribute('inert')).toBe(true)
    } finally {
      alreadyInert.remove()
    }
  })

  it('makes a node mounted while the viewfinder is open inert too, and not after', async () => {
    installDetector()
    installCamera()
    await raiseBanner()
    await scan(null)
    const late = document.createElement('div')
    try {
      document.body.append(late)
      await vi.waitFor(() => expect(late.hasAttribute('inert')).toBe(true))
      Array.from(overlay()!.querySelectorAll('button'))
        .find((b) => b.textContent === 'Cancel scanning')!
        .click()
      expect(overlay()).toBeNull()
      expect(late.hasAttribute('inert')).toBe(false)
    } finally {
      late.remove()
    }
  })

  it('blocks navigation chords from page shortcuts while the viewfinder is open', async () => {
    installDetector()
    installCamera()
    const pageKeydown = vi.fn()
    document.addEventListener('keydown', pageKeydown)
    try {
      await raiseBanner()
      await scan(null)
      const target = cancelScanButton()!
      for (const init of [
        { key: ',', code: 'Comma', altKey: true },
        { key: '1', code: 'Digit1', altKey: true },
        { key: 'ArrowLeft', code: 'ArrowLeft', altKey: true },
      ]) {
        target.dispatchEvent(new KeyboardEvent('keydown', {
          ...init,
          bubbles: true,
          cancelable: true,
        }))
      }
      expect(pageKeydown).not.toHaveBeenCalled()
    } finally {
      document.removeEventListener('keydown', pageKeydown)
      cancelScanButton()?.click()
    }
  })

  it('runs before window capture handlers that were registered before the scan opens', async () => {
    installDetector()
    installCamera()
    const underlyingDialog = vi.fn()
    window.addEventListener('keydown', underlyingDialog, true)
    try {
      await raiseBanner()
      await scan(null)
      cancelScanButton()!.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true }),
      )
      cancelScanButton()!.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
      )
      await vi.waitFor(() => expect(overlay()).toBeNull())
      expect(underlyingDialog).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener('keydown', underlyingDialog, true)
      cancelScanButton()?.click()
    }
  })

  it('closes the scanner without dismissing a ConfirmDialog underneath', async () => {
    installDetector()
    installCamera()
    const onCancel = vi.fn()
    const view = render(createElement(ConfirmDialog, {
      title: 'Delete note',
      body: 'Move this note to trash?',
      confirmLabel: 'Delete',
      cancelLabel: 'Cancel',
      onConfirm: () => {},
      onCancel,
    }))
    try {
      await raiseBanner()
      await scan(null)
      cancelScanButton()!.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
      )
      await vi.waitFor(() => expect(overlay()).toBeNull())
      expect(onCancel).not.toHaveBeenCalled()
    } finally {
      cancelScanButton()?.click()
      view.unmount()
    }
  })

  it('leaves Tab and Enter defaults intact and Enter cancels the scan', async () => {
    installDetector()
    installCamera()
    const user = userEvent.setup()
    await raiseBanner()
    await scan(null)
    const cancel = cancelScanButton()!
    cancel.focus()
    const tab = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    cancel.dispatchEvent(tab)
    expect(tab.defaultPrevented).toBe(false)
    const enter = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true })
    cancel.dispatchEvent(enter)
    expect(enter.defaultPrevented).toBe(false)
    await user.keyboard('{Enter}')
    await vi.waitFor(() => expect(overlay()).toBeNull())
  })

  it('releases keyboard events after the scan closes', async () => {
    installDetector()
    installCamera()
    const windowCapture = vi.fn()
    const documentBubble = vi.fn()
    window.addEventListener('keydown', windowCapture, true)
    document.addEventListener('keydown', documentBubble)
    try {
      await raiseBanner()
      await scan(null)
      cancelScanButton()!.click()
      expect(overlay()).toBeNull()
      document.body.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true }),
      )
      expect(windowCapture).toHaveBeenCalledTimes(1)
      expect(documentBubble).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener('keydown', windowCapture, true)
      document.removeEventListener('keydown', documentBubble)
      cancelScanButton()?.click()
    }
  })
})
