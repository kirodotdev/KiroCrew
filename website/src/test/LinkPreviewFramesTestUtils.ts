/**
 * Stand-ins for link-preview frames in the test DOM, which gives an iframe no
 * window: each frame gets a fake window that records what the dashboard posts to
 * it, and `fromFrame` answers as that frame.
 */
import { LINK_PREVIEW_TAG } from '../linkPreview/linkPreviewProtocol'

export type Sent = Record<string, unknown>

const windows = new WeakMap<HTMLIFrameElement, { postMessage: (m: Sent) => void; sent: Sent[] }>()

export function windowOf(el: HTMLIFrameElement) {
  let w = windows.get(el)
  if (!w) {
    const sent: Sent[] = []
    w = { sent, postMessage: (m: Sent) => { sent.push(m) } }
    windows.set(el, w)
  }
  return w
}

/** Give every iframe a stand-in window. Call once per test file. */
export function installFrameWindows(): void {
  Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
    configurable: true,
    get(this: HTMLIFrameElement) { return windowOf(this) },
  })
}

/** What the dashboard has posted to `el`. */
export function sentTo(el: HTMLIFrameElement): Sent[] {
  return windowOf(el).sent
}

export function postFrom(data: unknown, source: unknown): void {
  const event = new MessageEvent('message', { data })
  Object.defineProperty(event, 'source', { value: source })
  window.dispatchEvent(event)
}

/** A protocol message from `el` (or from some other window, to test that it is ignored). */
export function fromFrame(el: HTMLIFrameElement | Window | null, type: string, data: Record<string, unknown> = {}): void {
  postFrom({ [LINK_PREVIEW_TAG]: 1, type, ...data }, el instanceof HTMLIFrameElement ? el.contentWindow : el)
}

export function framesIn(doc: Document = document): HTMLIFrameElement[] {
  return [...doc.querySelectorAll<HTMLIFrameElement>('iframe[data-link-preview-frame]')]
}
