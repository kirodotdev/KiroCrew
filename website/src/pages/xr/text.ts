/**
 * Text handling for XR panels. Everything a Bot wrote is DATA: it is turned
 * into plain text here, drawn as pixels, and never parsed as markup or run.
 */

/** A backslash and a `u`: how an unprintable character is spelled out. */
const ESCAPE = String.fromCharCode(92, 117)

/** Glyphs the scene uses for its own state. Crew text may not imitate them. */
const STATE_GLYPHS = /[\u25CF\u25CB\u25C9\u2713\u2714\u2715\u2716\u26A0\u23F3\u{1F7E0}-\u{1F7EB}\u2B24]/gu

/**
 * Crew-written text made safe to draw: control and direction-override
 * characters are shown as escapes, and state glyphs are dropped.
 */
export function crewText(raw: string | undefined): string {
  if (!raw) return ''
  return raw
    .replace(STATE_GLYPHS, '')
    .replace(/[\u0000-\u0008\u000B\u000C\u000E-\u001F\u007F\u200E\u200F\u202A-\u202E\u2066-\u2069]/g, c =>
      ESCAPE + c.charCodeAt(0).toString(16).padStart(4, '0'))
}

/**
 * A chat message as plain text. Only styling goes: heading marks, bold marks,
 * and a link becomes "label (address)". Everything inside a code fence is kept
 * exactly, and angle brackets stay as the characters they are: the result is
 * drawn with `fillText`, so nothing in it can run.
 */
export function plainText(markdown: string | undefined): string {
  if (!markdown) return ''
  const parts = crewText(markdown).replace(/\[OPTIONS:[^\]]*\]/g, '').split(/(```[\s\S]*?```)/)
  return parts
    .map((part, i) => (i % 2
      ? part.replace(/^```[^\n]*\n?/, '').replace(/```$/, '')
      : part
        .replace(/!?\[([^\]]*)\]\(([^)\s]*)\)/g, (_m, label: string, url: string) => (label && label !== url ? `${label} (${url})` : url))
        .replace(/^#{1,6}\s+/gm, '')
        .replace(/(\*\*|__)/g, '')))
    .join('')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

export interface Wrapped { lines: string[]; clipped: boolean }

/**
 * Break text into lines no wider than `max`, at most `maxLines` of them.
 * `measure` is the canvas's own text measure, so the same function runs in
 * tests with a fixed-width stand-in.
 */
export function wrapLines(measure: (s: string) => number, text: string, max: number, maxLines = Infinity): Wrapped {
  const all: string[] = []
  for (const para of text.split('\n')) {
    let line = ''
    for (const word of para.split(/(\s+)/)) {
      if (!word) continue
      if (measure(line + word) <= max) { line += word; continue }
      if (line.trim()) all.push(line.trimEnd())
      let rest = word.trimStart()
      // A word wider than the line (a path, a hash) breaks by character.
      while (rest && measure(rest) > max) {
        const chars = [...rest]
        let n = 1
        while (n < chars.length && measure(chars.slice(0, n + 1).join('')) <= max) n += 1
        all.push(chars.slice(0, n).join(''))
        rest = chars.slice(n).join('')
      }
      line = rest
    }
    all.push(line.trimEnd())
  }
  if (all.length <= maxLines) return { lines: all, clipped: false }
  const lines = all.slice(0, maxLines)
  let last = lines[lines.length - 1] ?? ''
  while (last && measure(last + '…') > max) last = [...last].slice(0, -1).join('')
  if (lines.length) lines[lines.length - 1] = last + '…'
  return { lines, clipped: true }
}

/** True when the whole command can be read on the card: the rule for showing Approve. */
export function commandFits(measure: (s: string) => number, command: string, max: number, maxLines: number): boolean {
  return !wrapLines(measure, command, max, maxLines).clipped
}
