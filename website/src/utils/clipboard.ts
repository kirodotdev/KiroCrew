/** Copy code, trimming leading + trailing whitespace so a pasted command lands
 *  clean at the prompt — no leading indent, no trailing space. */
export function copyCode(text: string): Promise<void> {
  return copyToClipboard(text.trim())
}

export async function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text)
      return
    } catch {}
  }

  const ta = document.createElement('textarea')
  ta.value = text
  ta.style.position = 'fixed'
  ta.style.opacity = '0'
  document.body.appendChild(ta)
  try {
    ta.select()
    if (!document.execCommand('copy')) throw new Error('Copy failed')
  } finally {
    document.body.removeChild(ta)
  }
}
