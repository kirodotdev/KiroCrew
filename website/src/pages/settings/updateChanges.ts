/**
 * The changes a pending update brings, as one short line each, for the
 * "What you'd get" list under About > Updates.
 *
 * The input is changelog markdown the update itself carries: the desktop feed's
 * `releaseNotes` (the target version's `CHANGELOG.md` section, written by
 * `scripts/feed_release_notes.py`), the lines a git checkout's check adds to
 * `CHANGELOG.md` (`/api/update/check`'s `changes`), or the release list's
 * sections cut down by `notesSince`. All follow the file's own shape, so one
 * reading serves them all:
 *
 * - a top-level bullet is one change, named by its bold lead when it has one
 *   (`- **Live view**: ...` -> `Live view`);
 * - a paragraph that opens with a bold lead is one change
 *   (`**Chat and sessions.** Chats resume. ...` -> `Chat and sessions: Chats resume.`), which is how the
 *   "Notable fixes" sections are written;
 * - a `###` section with neither is one change, named by its heading;
 * - version headings (`##`) and plain prose name nothing.
 */

import { baseRelease, compareVersions } from '../../utils/releaseVersion'

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

function entryOf(text: string, paragraph: boolean): string {
  const bold = BOLD_LEAD_RE.exec(text)
  if (!bold) return plain(text)
  const name = lead(bold[1])
  if (!paragraph) return name
  const detail = plain(text.slice(bold[0].length)).replace(/^[.:;,\s]+/, '')
  const sentence = detail.match(/^.*?\.(?:\s|$)/)?.[0].trim() || detail
  return sentence ? `${name}: ${sentence}` : name
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
  const lines = markdown.split(/\r?\n/)
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trimEnd()
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
    let text = bullet ? bullet[1] : startsParagraph && BOLD_LEAD_RE.test(line) ? line : null
    if (text === null) continue
    if (!bullet) {
      // A paragraph's first sentence can wrap onto the following lines.
      while (i + 1 < lines.length && lines[i + 1].trim()
        && !/^#{1,6}\s/.test(lines[i + 1]) && !BULLET_RE.test(lines[i + 1])) {
        text += ` ${lines[++i].trim()}`
      }
    }
    const entry = entryOf(text, !bullet)
    if (!entry) continue
    out.push(entry)
    sectionHasItems = true
  }
  closeSection()
  return out
}

const SECTION_RE = /^##\s+\[([^\]]+)\]/

/**
 * Changelog sections cut down to the releases the running build lacks.
 *
 * Reads notes with one `## [X.Y.Z]` heading per release, as `archiveNotes`
 * builds them from the release list. Keeps the sections newer than
 * `runningVersion`, and the running release's own section when the running
 * build is a genuine prerelease of it, not a promoted stable release that
 * retains its prerelease stamp. Notes with no such heading are one release's
 * section, returned whole and not described as a range.
 */
export function notesSince(notes: string, runningVersion: string, bytesAreStable: boolean): { notes: string; sinceBuild: boolean } {
  const lines = notes.split(/\r?\n/)
  if (!lines.some(line => SECTION_RE.test(line))) return { notes, sinceBuild: false }
  const running = baseRelease(runningVersion)
  // A running version that names no release cannot bound the range: show the
  // notes whole, as one release's, rather than cut them all away.
  if (running === null) return { notes, sinceBuild: false }
  const isPrerelease = !bytesAreStable && compareVersions(runningVersion, running) === -1
  const kept: string[] = []
  let keep = false
  for (const line of lines) {
    // Every level-2 heading ends the preceding section, as in the shell's
    // post-update filter. That filter's installed-build bounds differ from
    // this pending-update range, so only its version utilities are shared.
    if (/^##\s+\S/.test(line)) {
      const heading = SECTION_RE.exec(line)
      const order = heading ? compareVersions(baseRelease(heading[1]) ?? '', running) : null
      keep = order !== null && (order > 0 || (order === 0 && isPrerelease))
    }
    if (keep) kept.push(line)
  }
  return { notes: kept.join('\n').trim(), sinceBuild: true }
}

/** The `/api/releases` fields `archiveNotes` reads. */
export interface ArchiveRelease { version: string; date: string; body: string; is_current: boolean }

/**
 * The update's changes from this install's own release list, for an update that
 * carries no notes: the sections newer than the running release, up to and
 * including `target`, newest first. Empty when the list has none of them, which
 * is the usual case: the bundled `CHANGELOG.md` stops at the running build.
 * Desktop callers skip the gateway-current cutoff and let `notesSince` apply
 * their own running version instead.
 */
export function archiveNotes(releases: ArchiveRelease[] | undefined, target: string, cutAtGatewayCurrent = true): string {
  if (!releases?.length) return ''
  const current = cutAtGatewayCurrent ? releases.findIndex(r => r.is_current) : releases.length
  if (current <= 0) return ''
  const goal = baseRelease(target)
  return releases
    .slice(0, current)
    .filter(r => {
      const order = goal ? compareVersions(baseRelease(r.version) ?? '', goal) : null
      return r.body && order !== null && order <= 0
    })
    // Rebuilt in CHANGELOG.md's own heading shape, which `changeEntries` and
    // `notesSince` read: markup, not copy.
    .map(r => [['##', `[${r.version}]`, ...(r.date ? ['-', r.date] : [])].join(' '), r.body].join('\n\n'))
    .join('\n\n')
}
