/**
 * Label lookups for the Notes app.
 *
 * Every key is a literal: the project forbids assembling an i18n key from parts
 * (a composed key is invisible to the extractor, so it reads as dead and can
 * never be checked), and module-level i18nT() calls would freeze the language
 * at import time. Hence explicit switches called during render.
 */
import { i18nT } from '../../i18n/t'

/**
 * Catalog key for each sort option, indexed by the persisted sort id from
 * `SORTS`. A module-level map of full literal keys (the `STATUS_LABEL_KEY`
 * pattern in McpToolsPanel.tsx) so the i18n key-reference gate can resolve
 * and verify every key statically.
 */
const SORT_LABEL_KEY: Record<string, string> = {
  'modified-desc': 'apps.mdNotebook.sort.modifiedNewOld',
  'modified-asc': 'apps.mdNotebook.sort.modifiedOldNew',
  'created-desc': 'apps.mdNotebook.sort.createdNewOld',
  'created-asc': 'apps.mdNotebook.sort.createdOldNew',
  'name-asc': 'apps.mdNotebook.sort.nameAZ',
  'name-desc': 'apps.mdNotebook.sort.nameZA',
}

/** Label for one sort option, by its persisted sort id. */
export function sortLabel(sortId: string): string {
  return i18nT(SORT_LABEL_KEY[sortId])
}

/** Label for the folders / flat-list choice. */
export function listViewLabel(view: 'folders' | 'list'): string {
  return view === 'folders'
    ? i18nT('apps.mdNotebook.panel.view_folders')
    : i18nT('apps.mdNotebook.panel.view_list')
}

/** Label for the rendered / raw-markdown choice. */
export function paneViewLabel(mode: 'rendered' | 'raw'): string {
  return mode === 'rendered'
    ? i18nT('apps.mdNotebook.header.view_rendered')
    : i18nT('apps.mdNotebook.header.view_raw')
}

/** "Synced N ago" for the sync button, by unit bucket. */
export function syncedAgoLabel(unit: 'now' | 'm' | 'h' | 'd', n: number): string {
  switch (unit) {
    case 'now':
      return i18nT('apps.mdNotebook.sync.justNow')
    case 'm':
      return i18nT('apps.mdNotebook.sync.ago_m', { n })
    case 'h':
      return i18nT('apps.mdNotebook.sync.ago_h', { n })
    default:
      return i18nT('apps.mdNotebook.sync.ago_d', { n })
  }
}

/** Hint under a ` ```mermaid ` block whose source failed to render. */
export function mermaidErrorLabel(): string {
  return i18nT('apps.mdNotebook.preview.mermaidError')
}
