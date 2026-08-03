/**
 * Pure helpers for the Notes app — no React, no DOM, so they are unit-testable.
 *
 * In the original single-file app version this logic lived inside the component
 * module and no test runner could import it; the editor behaviour (indent
 * measurement, list continuation, caret placement) was therefore unverifiable.
 */
import { INDENT_COL_PX, LIST_INDENT, TAB_STOP } from './constants'
import type { Note, Shortcut, TreeNode } from './types'
import { fmtDateFields } from '../../i18n/format'

/**
 * Leading indent + marker of a list item: bullet, task or ordered.
 * Group 1 is the indent, 2 the whole marker, 3 the ordinal (numbered only).
 */
export const LIST_MARKER_RE = /^(\s*)(- \[[ x]\] |[-*] |(\d+)\. )/

/** Frontmatter block at the top of a note. */
export const FM_RE = /^---\r?\n[\s\S]*?\r?\n---[ \t]*(?:\r?\n|$)/

/**
 * Pixel offset for a run of leading whitespace.
 *
 * Counting characters would score a tab the same as one space, which renders
 * real nesting shallower than a stray two-space indent. Expanding to tab stops
 * is what every editor (and Obsidian) does, so a tab-indented child sits a full
 * level in and leftover spaces land proportionally.
 */
export function indentPx(ws: string): number {
  let col = 0
  for (const ch of ws) {
    col = ch === '\t' ? col + TAB_STOP - (col % TAB_STOP) : col + 1
  }
  return col * INDENT_COL_PX
}

/**
 * Indent (or with `out`, outdent) the list item containing the caret.
 * Returns the rewritten text and new caret position, or null when the caret
 * is not on a list item — callers then leave the keystroke alone so Tab can
 * still move focus out of the editor.
 */
export function shiftListItem(
  text: string,
  pos: number,
  out: boolean,
): { text: string; pos: number } | null {
  const lineStart = text.lastIndexOf('\n', pos - 1) + 1
  const nl = text.indexOf('\n', pos)
  const lineEnd = nl === -1 ? text.length : nl
  const line = text.slice(lineStart, lineEnd)
  if (!LIST_MARKER_RE.test(line)) return null
  if (out) {
    // One level back: a tab, or up to a tab stop's worth of spaces.
    const m = /^(\t| {1,4})/.exec(line)
    if (!m) return null // already at the outermost level
    return {
      text: text.slice(0, lineStart) + line.slice(m[0].length) + text.slice(lineEnd),
      pos: Math.max(lineStart, pos - m[0].length),
    }
  }
  return {
    text: text.slice(0, lineStart) + LIST_INDENT + line + text.slice(lineEnd),
    pos: pos + LIST_INDENT.length,
  }
}

/**
 * The marker a new block should inherit when Enter splits a list item.
 * Numbers increment and a checked task starts unchecked. Empty string when the
 * line is not a list item.
 */
export function carriedMarker(before: string): string {
  const m = LIST_MARKER_RE.exec(before)
  if (!m) return ''
  return m[3] ? `${m[1]}${Number(m[3]) + 1}. ` : `${m[1]}${m[2].replace('[x]', '[ ]')}`
}

/** True when Enter on this line should leave the list rather than continue it. */
export function isEmptyListItem(before: string, after: string): boolean {
  const m = LIST_MARKER_RE.exec(before)
  return Boolean(m) && before.slice(m![0].length).trim() === '' && after === ''
}

/** Filename without directories or the .md extension. */
export function noteBasename(path: string): string {
  const file = path.split('/').pop() ?? path
  return file.replace(/\.md$/i, '')
}

/** Group a flat note list into a folder tree. */
export function buildTree(notes: Note[]): TreeNode {
  const root: TreeNode = { folders: new Map(), notes: [] }
  for (const n of notes) {
    const parts = n.path.split('/')
    let cur = root
    for (const part of parts.slice(0, -1)) {
      if (!cur.folders.has(part)) cur.folders.set(part, { folders: new Map(), notes: [] })
      cur = cur.folders.get(part)!
    }
    cur.notes.push(n)
  }
  return root
}

/** Absolute path a Knowledge source should watch (vault root + subfolder). */
export function vaultContentPath(v: { localPath: string; subfolder?: string }): string {
  return v.subfolder ? `${v.localPath}/${v.subfolder}` : v.localPath
}

/**
 * Relative timestamp for a note row: time today, weekday + time this week,
 * else the date (with the year when it is not the current one).
 */
export function relTime(ms: number): string {
  const d = new Date(ms)
  const now = new Date()
  // `fmtDateFields` formats in the APP's language; `toLocale*` would follow the
  // host's, so a user reading the dashboard in German saw English dates.
  const time = fmtDateFields(d, { hour: 'numeric', minute: '2-digit' })
  if (d.toDateString() === now.toDateString()) return time
  if (now.getTime() - d.getTime() < 7 * 86400e3) {
    return `${fmtDateFields(d, { weekday: 'short' })} ${time}`
  }
  return fmtDateFields(d, {
    month: 'short',
    day: 'numeric',
    ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}),
  })
}

/** Buckets for the "time since last sync" label. Rendered via i18n. */
export function agoBucket(ms: number): { unit: 'now' | 'm' | 'h' | 'd'; value: number } {
  const delta = Date.now() - ms
  if (delta < 45e3) return { unit: 'now', value: 0 }
  const mins = Math.round(delta / 60e3)
  if (mins < 60) return { unit: 'm', value: mins }
  const hrs = Math.round(mins / 60)
  if (hrs < 24) return { unit: 'h', value: hrs }
  return { unit: 'd', value: Math.round(hrs / 24) }
}

/** Human shortcut label in macOS modifier order: ⌃ ⌥ ⇧ ⌘ Key. */
export function formatShortcut(sc: Shortcut | null | undefined): string {
  if (!sc?.key) return '—'
  const parts: string[] = []
  if (sc.ctrl) parts.push('⌃')
  if (sc.alt) parts.push('⌥')
  if (sc.shift) parts.push('⇧')
  if (sc.meta) parts.push('⌘')
  parts.push(sc.key.length === 1 ? sc.key.toUpperCase() : sc.key)
  return parts.join(' ')
}

/** True when a keyboard event matches a recorded shortcut exactly. */
export function matchesShortcut(
  e: Pick<KeyboardEvent, 'key' | 'metaKey' | 'ctrlKey' | 'altKey' | 'shiftKey'>,
  sc: Shortcut | null | undefined,
): boolean {
  if (!sc?.key) return false
  return (
    e.key.toLowerCase() === sc.key.toLowerCase() &&
    e.metaKey === !!sc.meta &&
    e.ctrlKey === !!sc.ctrl &&
    e.altKey === !!sc.alt &&
    e.shiftKey === !!sc.shift
  )
}

/** Read a JSON value from localStorage, falling back when absent or corrupt. */
export function loadPref<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key)
    return raw === null ? fallback : (JSON.parse(raw) as T)
  } catch {
    return fallback
  }
}

/** Persist a JSON value, ignoring quota or privacy-mode failures. */
/**
 * The note the panel should open after `path` is deleted: the next one DOWN in
 * the visible order, or the one above when the deleted note was last. Returns
 * null when it was the only note, so the caller falls back to the empty state.
 *
 * Takes the VISIBLE order rather than the raw note list, because "next" has to
 * mean what the user sees — folders view, flat list and search results order
 * differently, and a collapsed folder's notes are not candidates at all.
 */
export function neighborAfterDelete(
  visible: readonly string[],
  path: string,
): string | null {
  const i = visible.indexOf(path)
  if (i === -1) return null
  return visible[i + 1] ?? visible[i - 1] ?? null
}

/**
 * Whether a captured note identity still refers to the note open right now.
 *
 * BOTH halves are compared because a note path is vault-RELATIVE: two vaults can
 * each hold `One.md`. Matching on path alone reports vault A's note as being
 * vault B's identically-named one, and every caller here is an async completion
 * deciding whether to touch the live editor — so a false match drops keystrokes,
 * renders an unrelated row un-openable, or offers "use the file on disk" against
 * a buffer from a different vault.
 *
 * Used by the in-flight DELETE guard and the in-flight SAVE conflict guard.
 */
export function targetsSameNote(
  captured: { vault: string | null; path: string } | null,
  vault: string | null,
  path: string | null,
): boolean {
  if (!captured || path === null) return false
  return captured.vault === vault && captured.path === path
}

/**
 * Which badge a note row shows, if any — the one slot on the row that means
 * "state of this file".
 *
 * `deleting` wins: it is the newer, more relevant fact than how the file
 * compares to git. `pending` is suppressed entirely when the vault has no remote
 * (`showSyncBadge` false): "differs from the last commit" is not a state the user
 * can act on there, it clears itself on the next autosave, and left visible it
 * reads as "not saved" — the opposite of the truth, since the badge only appears
 * once the file has reached disk.
 */
export function rowBadge(input: {
  deleting: boolean
  syncStatus: string
  showSyncBadge: boolean
}): 'deleting' | 'pending' | null {
  if (input.deleting) return 'deleting'
  if (!input.showSyncBadge || input.syncStatus === 'synced') return null
  return 'pending'
}

export function savePref(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value))
  } catch {
    /* storage unavailable — preferences simply do not persist */
  }
}
