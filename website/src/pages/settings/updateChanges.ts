/**
 * The changes a pending update brings, as one short line each, for the
 * "What you'd get" list under About > Updates.
 *
 * The input is changelog markdown the update itself carries: the desktop feed's
 * `releaseNotes` (the target version's `CHANGELOG.md` section, written by
 * `scripts/feed_release_notes.py`) or the lines a git checkout's check adds to
 * `CHANGELOG.md` (`/api/update/check`'s `changes`). Both follow the file's own
 * shape, so one reading serves both:
 *
 * - a top-level bullet is one change, named by its bold lead when it has one
 *   (`- **Live view**: ...` -> `Live view`);
 * - a paragraph that opens with a bold lead is one change
 *   (`**Chat and sessions.** ...` -> `Chat and sessions`), which is how the
 *   "Notable fixes" sections are written;
 * - a `###` section with neither is one change, named by its heading;
 * - version headings (`##`) and plain prose name nothing.
 */

const BULLET_RE = /^[-*+]\s+(.*)$/
const BOLD_LEAD_RE = /^\*\*(.+?)\*\*/
const H3_RE = /^###\s+(.*)$/

/** Markdown inline syntax removed, so a line reads as plain text. */
function plain(text: string): string {
  return text
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/[*_`]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

/** A bold lead without the punctuation that ends it (`Live view:` -> `Live view`). */
function lead(text: string): string {
  return plain(text).replace(/[.:;,\s]+$/, '')
}

function entryOf(text: string): string {
  const bold = BOLD_LEAD_RE.exec(text)
  return bold ? lead(bold[1]) : plain(text)
}

export function changeEntries(markdown: string): string[] {
  const out: string[] = []
  let heading = ''
  let sectionHasItems = false
  let previousBlank = true
  const closeSection = () => {
    if (heading && !sectionHasItems) out.push(heading)
    heading = ''
    sectionHasItems = false
  }
  for (const raw of markdown.split(/\r?\n/)) {
    const line = raw.trimEnd()
    if (!line.trim()) {
      previousBlank = true
      continue
    }
    const startsParagraph = previousBlank
    previousBlank = false
    if (line.startsWith('## ') || line === '##') {
      closeSection()
      continue
    }
    const h3 = H3_RE.exec(line)
    if (h3) {
      closeSection()
      heading = plain(h3[1])
      continue
    }
    // Indented lines continue a bullet or nest under it: not changes of their own.
    if (/^\s/.test(line)) continue
    const bullet = BULLET_RE.exec(line)
    const text = bullet ? bullet[1] : startsParagraph && BOLD_LEAD_RE.test(line) ? line : null
    if (text === null) continue
    const entry = entryOf(text)
    if (!entry) continue
    out.push(entry)
    sectionHasItems = true
  }
  closeSection()
  return out
}
