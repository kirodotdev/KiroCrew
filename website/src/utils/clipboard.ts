/** Copy code, trimming leading + trailing whitespace so a pasted command lands
 *  clean at the prompt — no leading indent, no trailing space. */
export function copyCode(text: string): Promise<boolean> {
  return copyToClipboard(text.trim())
}

/** Why a copy ended the way it did, for the rare caller that must say something
 *  more actionable than "copy failed".
 *
 *  Most callers want `copyToClipboard`'s plain boolean. Reach for this only when
 *  a DISTINCT, ACTIONABLE remedy hangs off the reason — the terminal copy keys
 *  are the case it exists for: "serve this over HTTPS" and "allow clipboard
 *  access" are two different things the user can go and do, and collapsing them
 *  into one generic failure throws away the only guidance that would unblock
 *  them. Reading the reason is deliberately kept out of the common path so no
 *  caller has to reason about a rejection it does not surface. */
export interface CopyOutcome {
  /** Whether the text ACTUALLY reached the clipboard, by either layer. */
  ok: boolean
  /** Whether an async `writeText` existed to try at all. False means a
   *  non-secure context, whose remedy is a secure origin, not a permission. */
  hadAsyncApi: boolean
  /** The async layer's rejection, when it had one. A `NotAllowedError` here is
   *  a refused permission; anything else is an ordinary failure. Undefined when
   *  the async layer was absent or succeeded. */
  asyncError?: unknown
}

/** Copy `text` and report HOW it went. Attempts each layer exactly once, so a
 *  caller never has to write to the clipboard twice just to learn the reason. */
export async function copyWithOutcome(text: string): Promise<CopyOutcome> {
  const write = navigator.clipboard?.writeText
  if (!write) return { ok: execCommandCopy(text), hadAsyncApi: false }
  try {
    await navigator.clipboard.writeText(text)
    return { ok: true, hadAsyncApi: true }
  } catch (asyncError) {
    return { ok: execCommandCopy(text), hadAsyncApi: true, asyncError }
  }
}

/** Copy `text` to the clipboard. Resolves `true` only once the text is
 *  ACTUALLY on the clipboard, and `false` otherwise — never rejects, so a
 *  caller has exactly one failure signal to read and a fire-and-forget call
 *  site cannot raise an unhandled rejection.
 *
 *  Callers that render a confirmation MUST gate it on the returned boolean. A
 *  tick shown over an unchanged clipboard is worse than no affordance at all:
 *  the user walks away believing they hold the text and discovers otherwise at
 *  the moment they paste.
 *
 *  Two layers, because the async Clipboard API is unavailable in more of this
 *  product's real deployments than it is available: it needs a secure context,
 *  which a plain-HTTP LAN or remote gateway is not, and it needs the
 *  `clipboard-write` permission, which is refused to an opaque (sandboxed
 *  null-origin) document however secure the page embedding it is. Where it is
 *  missing or refused, `execCommandCopy` still works.
 *
 *  Use `copyWithOutcome` instead only when a distinct remedy hangs off WHY the
 *  copy failed. */
export async function copyToClipboard(text: string): Promise<boolean> {
  return (await copyWithOutcome(text)).ok
}

/** Re-encode `blob` as PNG, because PNG is the one image type the async
 *  Clipboard API accepts everywhere. Chromium writes only the types on its own
 *  allowlist and rejects the rest, so a JPEG or WebP handed over with its own
 *  type never reaches the clipboard — and the failure arrives as a rejected
 *  `write()`, indistinguishable from a refused permission.
 *
 *  The pixels go through a canvas, so this is a real re-encode rather than a
 *  relabelling. Rejects when the bytes cannot be decoded (a truncated file, an
 *  SVG with no intrinsic size), which the caller must surface as "not copied":
 *  writing a blank canvas instead would paste an empty rectangle, and the user
 *  would only find out in the document they pasted into. */
export async function imageBlobToPng(blob: Blob): Promise<Blob> {
  if (blob.type === 'image/png') return blob
  const bitmap = await createImageBitmap(blob)
  try {
    const canvas = document.createElement('canvas')
    canvas.width = bitmap.width
    canvas.height = bitmap.height
    const ctx = canvas.getContext('2d')
    if (!ctx) throw new Error('canvas 2d context unavailable')
    ctx.drawImage(bitmap, 0, 0)
    const png = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, 'image/png'))
    if (!png) throw new Error('PNG encode produced no blob')
    return png
  } finally {
    bitmap.close()
  }
}

/** Copy an IMAGE to the clipboard. Resolves `true` only once the bytes are
 *  actually on it and `false` otherwise, never rejecting — the same contract
 *  `copyToClipboard` has, and for the same reason: a tick shown over an
 *  unchanged clipboard is discovered at paste time.
 *
 *  Takes a PENDING promise rather than a `Blob` deliberately. The bytes have to
 *  be fetched and usually re-encoded first, and WebKit tests user activation
 *  when `write()` is called, so awaiting them at the call site spends the click
 *  and the write is refused on a gesture that plainly happened. Handing
 *  `ClipboardItem` the unresolved promise issues the write inside that gesture
 *  and lets the bytes land afterwards.
 *
 *  There is no `execCommand` fallback, unlike text: that path stages its payload
 *  in a `<textarea>`, which can hold nothing but a string. So on a non-secure
 *  origin — a plain-HTTP LAN or remote gateway, where `navigator.clipboard` does
 *  not exist at all — this returns `false` having attempted nothing, and the
 *  caller must keep offering whatever it offered before. */
export async function copyImageToClipboard(png: Promise<Blob>): Promise<boolean> {
  // `write()` consumes this promise, but every early return below leaves it with
  // no reader, and an unhandled rejection is a console error (and a fatal
  // `unhandledRejection` under the test runner) on a path already reported as
  // `false`. A promise may carry more than one handler, so this costs nothing.
  void png.catch(() => {})
  if (!navigator.clipboard?.write || typeof ClipboardItem === 'undefined') return false
  try {
    await navigator.clipboard.write([new ClipboardItem({ 'image/png': png })])
    return true
  } catch {
    return false
  }
}

/** Copy rich text: `html` as `text/html` for editors that paste formatting
 *  (Outlook, Word, Docs), with `plain` as the `text/plain` fallback for
 *  everything else. Same contract as `copyToClipboard`: resolves `true` only
 *  once both flavours are on the clipboard, never rejects.
 *
 *  The async `write()` needs `ClipboardItem` and a secure context. Where it is
 *  missing or refused, the `execCommand` fallback carries both flavours in its
 *  `copy` event, so a plain-HTTP gateway still pastes formatted. */
export async function copyRichToClipboard(html: string, plain: string): Promise<boolean> {
  if (navigator.clipboard?.write && typeof ClipboardItem !== 'undefined') {
    try {
      await navigator.clipboard.write([new ClipboardItem({
        'text/html': new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([plain], { type: 'text/plain' }),
      })])
      return true
    } catch {
      // Refused permission or an unsupported flavour: try the fallback.
    }
  }
  return execCommandCopy(plain, html)
}

/** `execCommand('copy')` fallback for the two cases the async Clipboard API
 *  cannot serve: a non-secure context (a plain-HTTP LAN or remote gateway,
 *  where `navigator.clipboard` does not exist at all) and a browser that
 *  refuses the `clipboard-write` permission.
 *
 *  Copying is a SIDE ERRAND, so this restores what it had to disturb:
 *
 *  - **Focus.** `select()` on the staging textarea moves focus off whatever the
 *    user was working in. Unrestored, that dismisses the on-screen keyboard on
 *    touch and collapses the layout mid-interaction, and it drops the terminal's
 *    keyboard focus on the desktop — which is why the terminal copy paths
 *    refused this fallback and were left with no working path at all below the
 *    async API. `preventScroll` keeps the restore from jumping the viewport.
 *  - **The document selection.** `select()` replaces the user's own selection
 *    ranges. The surfaces that copy a selection (the chat selection toolbar, the
 *    terminal selection copy) deliberately keep it highlighted afterwards so it
 *    can be re-copied or extended, so clearing it would break the affordance the
 *    copy belongs to.
 *
 *  The textarea is `readonly` so focusing it cannot raise a soft keyboard, and
 *  sized 1x1 at the viewport origin rather than left unsized, so no engine can
 *  lay it out large enough to flash. Returns whether the copy actually
 *  happened — never throws, so a caller may treat `false` as the only failure. */
function execCommandCopy(text: string, html?: string): boolean {
  if (typeof document.execCommand !== 'function') return false
  const previouslyFocused = document.activeElement
  const selection = document.getSelection()
  const savedRanges: Range[] = []
  if (selection) {
    for (let i = 0; i < selection.rangeCount; i++) savedRanges.push(selection.getRangeAt(i))
  }

  const ta = document.createElement('textarea')
  ta.value = text
  ta.readOnly = true
  ta.setAttribute('aria-hidden', 'true')
  // Set per property rather than one cssText literal: the i18n gate reads a
  // long quoted literal on an added line as user-visible copy.
  ta.style.position = 'fixed'
  ta.style.top = '0'
  ta.style.left = '0'
  ta.style.width = '1px'
  ta.style.height = '1px'
  ta.style.padding = '0'
  ta.style.border = '0'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  // Hand the text over in the `copy` event too. An open modal menu (Radix
  // FocusScope) pulls focus straight back off the textarea on `select()`, so
  // `execCommand('copy')` would copy the menu's empty selection and still
  // return true: a "Copied" tick over an unchanged clipboard. Writing
  // `clipboardData` in the event does not depend on where focus landed.
  const onCopy = (e: ClipboardEvent) => {
    if (!e.clipboardData) return
    e.clipboardData.setData('text/plain', text)
    if (html !== undefined) e.clipboardData.setData('text/html', html)
    e.preventDefault()
  }
  document.addEventListener('copy', onCopy, true)
  try {
    ta.select()
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
    document.removeEventListener('copy', onCopy, true)
    document.body.removeChild(ta)
    if (selection) {
      selection.removeAllRanges()
      for (const range of savedRanges) selection.addRange(range)
    }
    if (previouslyFocused instanceof HTMLElement) {
      try {
        previouslyFocused.focus({ preventScroll: true })
      } catch {}
    }
  }
}
