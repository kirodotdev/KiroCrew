/**
 * Image copy for an embedded instance pane, performed by the host window.
 *
 * The pane is the dashboard SPA on a tunnel's loopback port, framed by
 * InstancesViewport: a CROSS-ORIGIN iframe. The dashboard's response header
 * scopes the clipboard to its own origin (`clipboard-write=(self)`), and a
 * frame's `allow` attribute can only delegate what the embedding document's own
 * policy already grants that origin, so the async Clipboard API is refused
 * inside every pane however the iframe is declared. Text copy survives that:
 * the shared helper's `execCommand('copy')` fallback lands it. An IMAGE has no
 * such fallback (that path can carry only a string), so without this relay
 * image copy in a pane can only fail.
 *
 * The relay keeps the header exactly as it is and asks the host to write
 * instead. It is deliberately narrow, because a frame that can put content on
 * the OS clipboard without the person asking is a pastejacking surface:
 *
 * - **Images only, never bare text.** A request must carry a PNG; an optional
 *   caption may ride with it (the share card pastes both). Text alone keeps
 *   using the pane's own fallback, which already works.
 * - **Gesture in that frame.** The host writes only while it holds a live user
 *   activation AND its focused element is the requesting pane's iframe: the
 *   `hasGesture` test `useFrameOpenLink` applies to crew pages, shared. Activation alone is
 *   page-wide (typing in the host's composer sets it), so focus is what ties
 *   the click to the pane. A pane posting on its own, or on a loop, copies
 *   nothing.
 * - **Sender identity.** The host checks the message came from the pane's own
 *   `contentWindow`, from an origin that resolves to a warm tunnel, while that
 *   pane is the one on screen.
 * - **Write only.** Nothing here reads the clipboard; `clipboard-read` stays
 *   undelegated.
 *
 * The pane relays only to a host that advertised the relay in its
 * `mc-host-model` (`clipboardRelay`), addressed to that host's exact origin.
 * An older host never advertises it, so the pane keeps today's behaviour there
 * instead of waiting on an answer that cannot come.
 */
import { isEmbeddedPane } from './embedded'
import { parseLoopbackOriginPort } from './tunnelOrigin'

export const PANE_CLIPBOARD_IMAGE_TYPE = 'mc-clipboard-image'
export const PANE_CLIPBOARD_RESULT_TYPE = 'mc-clipboard-image-result'
export const PANE_CLIPBOARD_VERSION = 1

/** Upper bound on a relayed PNG. A share card at 2x is well under 5 MB; this
 *  only stops a malformed request from asking the host to stage something huge. */
export const PANE_CLIPBOARD_MAX_PNG_BYTES = 32 * 1024 * 1024
/** Upper bound on a caption riding with the image. */
export const PANE_CLIPBOARD_MAX_TEXT_CHARS = 64 * 1024
/** How long the pane waits for the host's answer before reporting "not copied".
 *  Under the browsers' 5s transient-activation window, so a pane never reports
 *  failure for a write the host is still allowed to make. */
export const PANE_CLIPBOARD_TIMEOUT_MS = 4000

const MAX_ID_LENGTH = 64

interface PolicyProbe {
  allowsFeature(feature: string): boolean
}

/**
 * Whether this document's Permissions Policy refuses the async clipboard write.
 *
 * True inside an embedded pane, false at top level. Where the browser exposes no
 * policy probe (`permissionsPolicy`, or Chromium's older `featurePolicy`) the
 * answer is false: the caller then tries the async API exactly as before and
 * reads its rejection, so an engine without the probe loses nothing. Asking
 * first is what keeps a pane from logging a Permissions-Policy violation on
 * every copy, since calling a refused API is itself the violation.
 */
export function clipboardWriteBlockedByPolicy(): boolean {
  if (typeof document === 'undefined') return false
  const doc = document as Document & { permissionsPolicy?: PolicyProbe; featurePolicy?: PolicyProbe }
  const probe = doc.permissionsPolicy ?? doc.featurePolicy
  if (!probe || typeof probe.allowsFeature !== 'function') return false
  try {
    return !probe.allowsFeature('clipboard-write')
  } catch {
    return false
  }
}

// ── Pane side ──────────────────────────────────────────────────────────────

let relayHostOrigin: string | null = null
let requestSeq = 0

/**
 * Record whether the host that just sent this pane its model performs image
 * copies. Called by EmbeddedHostBridge for every model from `window.parent`;
 * `origin` is that message's `event.origin`, so the address the pane later
 * posts to is one the host itself spoke from, and it must be a loopback http
 * origin (the only shape an Instances host has). Anything else clears it.
 */
export function noteClipboardRelayHost(origin: string | null, advertised: boolean): void {
  relayHostOrigin = advertised && typeof origin === 'string' && parseLoopbackOriginPort(origin) !== null
    ? origin
    : null
}

/** Whether an image copy here should go through the host. */
export function clipboardRelayAvailable(): boolean {
  return relayHostOrigin !== null && isEmbeddedPane()
}

/**
 * Ask the host to put `png` (and `text`, when given, as `text/plain` beside it)
 * on the clipboard. Resolves `true` only when the host reports the write landed
 * and `false` otherwise, never rejecting: the same contract as the shared copy
 * helpers, so callers keep their own fallback (a download, an error notice).
 */
export async function relayImageCopyToHost(png: Promise<Blob> | Blob, text?: string): Promise<boolean> {
  const target = relayHostOrigin
  if (target === null || !isEmbeddedPane()) return false
  let blob: Blob
  try {
    blob = await png
  } catch {
    return false
  }
  if (!(blob instanceof Blob) || blob.type !== 'image/png' || blob.size === 0) return false
  if (blob.size > PANE_CLIPBOARD_MAX_PNG_BYTES) return false
  if (text !== undefined && (typeof text !== 'string' || text.length > PANE_CLIPBOARD_MAX_TEXT_CHARS)) return false

  requestSeq += 1
  const id = `clip-${Date.now().toString(36)}-${requestSeq.toString(36)}`
  return new Promise<boolean>(resolve => {
    let settled = false
    let timer = 0
    const finish = (ok: boolean) => {
      if (settled) return
      settled = true
      window.removeEventListener('message', onMessage)
      window.clearTimeout(timer)
      resolve(ok)
    }
    const onMessage = (e: MessageEvent) => {
      if (e.source !== window.parent || e.origin !== target) return
      const d = e.data as { type?: unknown; v?: unknown; id?: unknown; ok?: unknown } | null
      if (!d || typeof d !== 'object') return
      if (d.type !== PANE_CLIPBOARD_RESULT_TYPE || d.v !== PANE_CLIPBOARD_VERSION || d.id !== id) return
      finish(d.ok === true)
    }
    window.addEventListener('message', onMessage)
    timer = window.setTimeout(() => finish(false), PANE_CLIPBOARD_TIMEOUT_MS)
    try {
      window.parent.postMessage(
        {
          type: PANE_CLIPBOARD_IMAGE_TYPE,
          v: PANE_CLIPBOARD_VERSION,
          id,
          png: blob,
          ...(text !== undefined ? { text } : null),
        },
        target,
      )
    } catch {
      finish(false)
    }
  })
}

// ── Host side ──────────────────────────────────────────────────────────────

export interface PaneClipboardImageRequest {
  id: string
  png: Blob
  text?: string
}

/**
 * Parse an untrusted `postMessage` payload as an image-copy request. Null unless
 * every field has exactly the expected type and is within bounds. The SENDER is
 * not checked here; the host does that against its own frame first, then
 * gates on `hasGesture` (useFrameOpenLink.ts) and writes through the same
 * helpers a top-level copy uses (`copyImageWithText`, `copyImageToClipboard`),
 * which take their direct path there because the host's policy allows it.
 */
export function parsePaneClipboardImageRequest(data: unknown): PaneClipboardImageRequest | null {
  if (!data || typeof data !== 'object') return null
  const d = data as Record<string, unknown>
  if (d.type !== PANE_CLIPBOARD_IMAGE_TYPE || d.v !== PANE_CLIPBOARD_VERSION) return null
  if (typeof d.id !== 'string' || !d.id || d.id.length > MAX_ID_LENGTH) return null
  const png = d.png
  if (!(png instanceof Blob) || png.type !== 'image/png' || png.size === 0) return null
  if (png.size > PANE_CLIPBOARD_MAX_PNG_BYTES) return null
  if (d.text !== undefined && (typeof d.text !== 'string' || d.text.length > PANE_CLIPBOARD_MAX_TEXT_CHARS)) return null
  return { id: d.id, png, ...(typeof d.text === 'string' ? { text: d.text } : null) }
}
