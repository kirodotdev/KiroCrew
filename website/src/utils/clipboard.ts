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
function execCommandCopy(text: string): boolean {
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
  try {
    ta.select()
    return document.execCommand('copy')
  } catch {
    return false
  } finally {
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
