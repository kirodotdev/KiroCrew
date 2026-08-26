/** Copy code, trimming leading + trailing whitespace so a pasted command lands
 *  clean at the prompt — no leading indent, no trailing space. */
export function copyCode(text: string): Promise<boolean> {
  return copyToClipboard(text.trim())
}

/** Copy `text` to the clipboard. Resolves `true` on success and `false` when
 *  the legacy `execCommand('copy')` fallback reports failure without throwing
 *  (e.g. iOS Safari over plain HTTP, where the async Clipboard API is
 *  unavailable and the hidden-textarea fallback returns false). A genuine
 *  exception from the fallback still REJECTS — that is the pre-existing
 *  contract consumers like the Issue Radar copy actions catch and surface.
 *  The boolean (rather than a throw) for the returns-false case keeps the
 *  ~25 fire-and-forget call sites free of new unhandled rejections while
 *  giving success-aware callers (the mobile sign-in card, the tailnet copy
 *  button) a signal the old always-void contract silently swallowed. */
export async function copyToClipboard(text: string): Promise<boolean> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {}
  }

  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  try {
    ta.select()
    return document.execCommand('copy')
  } finally {
    document.body.removeChild(ta)
  }
}
