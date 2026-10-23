/**
 * Page side of the service worker's push probe (`askConnected` in public/sw.js).
 *
 * When a Web Push arrives while a dashboard tab is visible, the worker asks that
 * tab whether its live connection is up. A connected tab receives the same note
 * over the WebSocket and shows it in-app, so the worker skips the OS banner; any
 * other answer (or none) makes it show the banner. The reply is read from the
 * store at the moment of the question, never cached, so a tab whose socket was
 * refused (an expired sign-in) answers `false` and the note is not lost.
 *
 * The look-preview frame (utils/lookPreview.ts) always answers `false`: its
 * socket stays open but it drops every frame unread, so it never shows the note.
 */
import { isLookPreviewFrame } from '../utils/lookPreview'

export const PUSH_PROBE_MESSAGE = 'kc-push-probe'

export function installSwConnectedProbe(isConnected: () => boolean): () => void {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return () => {}
  const container = navigator.serviceWorker
  const onMessage = (event: MessageEvent) => {
    const data = event.data as { type?: unknown } | null
    if (!data || data.type !== PUSH_PROBE_MESSAGE) return
    const port = event.ports && event.ports[0]
    if (!port) return
    port.postMessage({ connected: !isLookPreviewFrame() && isConnected() === true })
  }
  container.addEventListener('message', onMessage)
  return () => container.removeEventListener('message', onMessage)
}
