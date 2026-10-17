/** The Artifacts page's list logic, free of React: the kind filter's
 *  options, the table's column sort (comparator and its remembered choice) and
 *  the wording for a refused "Add Artifact" pick. */
import { safeGetItem } from '../utils/safeStorage'
import { compareText } from '../i18n/format'
import { IMPORTABLE_EXT_LIST, MAX_IMPORT_BYTES, type ImportRejection } from '../lib/artifactImport'
import type { Artifact } from '../types'
import type { SortKey, SortState } from '../components/library/LibraryTable'
import { i18nT } from '../i18n/t'

export const KIND_OPTIONS = ['', 'widget', 'html', 'markdown', 'svg', 'json', 'text', 'webapp', 'image'] as const

/** Explain a refused "Add Artifact" pick in the library's error banner.
 *
 * Kept next to the page rather than inside `lib/artifactImport.ts` so that
 * module stays free of catalog lookups and unit-testable without i18n. */
export function importRejectionText(reason: ImportRejection): string {
  switch (reason) {
    case 'unsupported-type':
      return `${i18nT('pages.artifactsPage.add_artifact_error_unsupported_type')} ${IMPORTABLE_EXT_LIST}`
    case 'too-large':
      return i18nT('pages.artifactsPage.add_artifact_error_too_large', {
        limit: Math.floor(MAX_IMPORT_BYTES / (1024 * 1024)),
      })
    case 'empty':
      return i18nT('pages.artifactsPage.add_artifact_error_empty')
    case 'not-text':
      return i18nT('pages.artifactsPage.add_artifact_error_not_text')
    case 'unreadable':
      return i18nT('pages.artifactsPage.add_artifact_error_unreadable')
  }
}

// ── Table column sorting ─────────────────────────────────────────────────
// Clicking a header cycles asc → desc → default (the server's order). The
// star and Actions columns are controls, not data, and stay unsortable.

export const ARTIFACT_SORT_STORAGE_KEY = 'mc-artifacts-sort'

const SORT_KEYS = new Set<SortKey>(['name', 'slug', 'kind', 'source', 'version', 'tags', 'updated'])

export function readPersistedSort(): SortState {
  try {
    const value: unknown = JSON.parse(safeGetItem(ARTIFACT_SORT_STORAGE_KEY) ?? 'null')
    if (!value || typeof value !== 'object') return null
    const { key, dir } = value as { key?: unknown; dir?: unknown }
    return typeof key === 'string' && SORT_KEYS.has(key as SortKey) && (dir === 'asc' || dir === 'desc')
      ? { key: key as SortKey, dir }
      : null
  } catch {
    return null
  }
}

/** Type-aware comparator: numeric for version, chronological for updated,
 * locale-collated natural string for the rest (compareText names the active
 * UI locale — never the host's). Direction is applied by the caller. */
function compareArtifacts(a: Artifact, b: Artifact, key: SortKey): number {
  switch (key) {
    case 'version':
      return a.version - b.version
    case 'updated': {
      // Byte-order ISO-8601 compare (same backend format, +00:00 offset):
      // chronological AND keeps the microsecond precision Date.parse drops,
      // so two artifacts updated within the same second still order correctly.
      const au = a.updated_at || ''
      const bu = b.updated_at || ''
      return au < bu ? -1 : au > bu ? 1 : 0
    }
    case 'source':
      return compareText(a.session_title || a.source || '', b.session_title || b.source || '')
    case 'tags':
      return compareText((a.tags || []).join(', '), (b.tags || []).join(', '))
    case 'slug':
      return compareText(a.slug, b.slug)
    case 'kind':
      return compareText(a.kind, b.kind)
    default:
      return compareText(a.name, b.name)
  }
}

export function sortArtifacts(items: Artifact[], sort: SortState): Artifact[] {
  if (!sort) return items
  const mul = sort.dir === 'desc' ? -1 : 1
  // Array.prototype.sort is stable, so equal rows keep the server's order.
  return [...items].sort((a, b) => compareArtifacts(a, b, sort.key) * mul)
}
