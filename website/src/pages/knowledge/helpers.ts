import { useState } from 'react'
import { fmtDateFields, fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

export function typeBadgeVariant(t: string): 'ok' | 'warn' | 'err' | 'aim' {
  if (['design_doc', 'code_doc'].includes(t)) return 'aim'
  if (['runbook', 'policy'].includes(t)) return 'warn'
  if (['report', 'presentation'].includes(t)) return 'ok'
  return 'ok'
}

export function formatDate(iso: string) {
  return fmtDateFields(iso, { month: 'short', day: 'numeric', year: 'numeric' })
}

export function formatRelativeDate(iso: string): string {
  const diff = Math.max(0, Date.now() - new Date(iso).getTime())
  const days = Math.floor(diff / (1000 * 60 * 60 * 24))
  if (days === 0) return 'today'
  if (days === 1) return 'yesterday'
  if (days < 7) return i18nT('pages.knowledge.helpers.days_ago', { n: days })
  if (days < 30) return i18nT('pages.knowledge.helpers.weeks_ago', { n: Math.floor(days / 7) })
  return i18nT('pages.knowledge.helpers.months_ago', { n: Math.floor(days / 30) })
}

export function copyText(text: string) {
  navigator.clipboard.writeText(text)
}

export function useCopy() {
  const [copied, setCopied] = useState(false)
  const copy = (text: string) => {
    navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }
  return { copied, copy }
}

// API type vocabulary, mirroring the backend's `item_type` enum. These are
// DISCRIMINANTS, not copy: they are submitted as the filter/create value and
// compared server-side, so the literals must stay verbatim in every locale.
export const ITEM_TYPES = ['design_doc', 'runbook', 'meeting_notes', 'code_doc', 'presentation', 'report', 'policy', 'personal_notes', 'external_reference', 'document']
export const STATUSES = ['active', 'archived']
// Status the list view opens on. This is the default view, NOT user narrowing,
// so the onboarding empty state treats this value as "no filter applied".
export const DEFAULT_STATUS_FILTER = 'active'
// Fallback shown/used only until GET /api/knowledge/config resolves. The
// backend's `FileReader.SUPPORTED` (src/kiro_crew/knowledge/readers.py) is the
// single source of truth for what upload ingests; this mirror exists so the
// file picker and the copy are not empty during the config round-trip.
// test/test_knowledge_formats_parity.py holds this list identical to
// `sorted(FileReader.SUPPORTED - {''})`, so it cannot silently drift.
export const FALLBACK_SUPPORTED_FORMATS = [
  '.c', '.cpp', '.csv', '.docx', '.go', '.h', '.htm', '.html', '.java', '.js',
  '.json', '.jsonl', '.log', '.md', '.ndjson', '.org', '.pdf', '.py', '.rb',
  '.rs', '.sh', '.ts', '.txt', '.yaml', '.yml',
]

/**
 * Render an extension list (from `/api/knowledge/config`, or the fallback
 * above) as a localized display string for the "Supported formats" copy. The
 * extensions themselves are DNT tokens; only the list separators localize.
 */
export function formatSupportedFormats(exts: readonly string[]): string {
  return fmtList(exts, { type: 'conjunction' })
}

/**
 * Onboarding copy for the Knowledge Library help dialog and empty state.
 *
 * GETTERS, not values: this object is built once at module load, so an
 * `i18nT()` call in a plain property initialiser would freeze the boot language
 * and never re-resolve on a language switch. A getter runs on every property
 * ACCESS instead, and both consumers (`index.tsx`) read these properties inside
 * JSX — so the lookup happens per render while the public shape stays exactly
 * what it was, and no call site has to change.
 */
export const ONBOARDING = {
  get title() {
    return i18nT('pages.knowledge.helpers.welcome_to_the_knowledge_library')
  },
  get description() {
    return i18nT('pages.knowledge.helpers.your_centralized_knowledge_base_with_entity_extr')
  },
  // A method rather than a getter because the caller supplies the formats
  // display string (derived from /api/knowledge/config). Like the getters, it
  // resolves i18nT per CALL, so a language switch still re-renders correctly.
  steps(formatsDisplay: string) {
    return [
      i18nT('pages.knowledge.helpers.drop_files_here_or_click_upload_to_ingest_docume'),
      i18nT('pages.knowledge.helpers.documents_are_chunked_entities_extracted_and_rel'),
      i18nT('pages.knowledge.helpers.search_across_all_knowledge_filter_by_type_or_ex'),
      // The format list itself is a set of DNT file extensions, interpolated
      // so only the sentence around it is translated.
      i18nT('pages.knowledge.helpers.supported_formats', { formats: formatsDisplay }),
    ]
  },
}
