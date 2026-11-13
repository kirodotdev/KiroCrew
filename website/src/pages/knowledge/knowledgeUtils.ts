import type { IngestionJob, Source } from './types'
import { i18nT } from '../../i18n/t'

export interface ParsedSourceProps {
  summary?: { topic?: string; themes?: string[] }
  summaryStatus?: string
  filesTotal?: number
  lastScan?: string
  recursive?: boolean
  wordCount?: number
  ignorePatterns?: string[]
  /** Why a 'pending' row waits; the gateway writes `import_budget` when the
   *  explicit-import budget deferred its ingest. */
  pendingReason?: string
}

export function parseSourceProps(s: Source): ParsedSourceProps {
  const raw = s.properties
  const props: Record<string, unknown> = typeof raw === 'string'
    ? (() => { try { return JSON.parse(raw) } catch { return {} } })()
    : (raw || {})
  return {
    summary: s.summary_topic
      ? { topic: s.summary_topic, themes: (() => { try { return JSON.parse(s.summary_themes || '[]') } catch { return [] } })() }
      : undefined,
    summaryStatus: props.summary_status as string | undefined,
    filesTotal: props.files_total as number | undefined,
    lastScan: props.last_scan as string | undefined,
    recursive: props.recursive as boolean | undefined,
    wordCount: props.word_count as number | undefined,
    ignorePatterns: Array.isArray(props.ignore_patterns) ? props.ignore_patterns.filter((p): p is string => typeof p === 'string') : undefined,
    pendingReason: typeof props.pending_reason === 'string' ? props.pending_reason : undefined,
  }
}

export function getSyncBadgeVariant(syncStatus: string): 'ok' | 'err' | 'aim' | 'warn' {
  if (syncStatus === 'synced') return 'ok'
  if (syncStatus === 'error') return 'err'
  if (syncStatus === 'paused') return 'warn'
  return 'aim'
}

const SYNC_STATUS_LABEL_KEYS: Record<string, string> = {
  active: 'pages.knowledge.sourcesList.status.active',
  synced: 'pages.knowledge.sourcesList.status.synced',
  error: 'pages.knowledge.sourcesList.status.error',
  paused: 'pages.knowledge.sourcesList.status.paused',
  syncing: 'pages.knowledge.sourcesList.status.syncing',
}

/** The status a source row shows and polls on. A folder scan stamps 'synced'
 *  when its first file lands, and 'error' when one file fails, so a row that
 *  still reports `scanning` reads as 'syncing' until the scan ends; an error
 *  shows once it does. Paused and pending rows keep their own. */
export function displaySyncStatus(s: Pick<Source, 'sync_status' | 'scanning'>): string {
  if (s.scanning && (s.sync_status === 'synced' || s.sync_status === 'active' || s.sync_status === 'error')) return 'syncing'
  return s.sync_status
}

/** Readable label for a source's sync_status; an unknown value shows as-is. */
export function syncStatusLabel(syncStatus: string, t: (key: string) => string): string {
  const key = SYNC_STATUS_LABEL_KEYS[syncStatus]
  return key ? t(key) : syncStatus
}

/** The visible label for a row still uploading or finished. A failed row is
 *  an error and renders through `ErrorNotice` instead, so it has no label here. */
export function ingestionStatusLabel(status: Exclude<IngestionJob['status'], 'error'>): string {
  if (status === 'done') return i18nT('pages.knowledge.sourcesList.ingest_status_done')
  return i18nT('pages.knowledge.sourcesList.ingest_status_uploading')
}

export function formatSourceSubtitle(source: Source, filesTotal?: number, lastScan?: string): string {
  const isDir = source.source_type === 'local_folder' || source.source_type === 'obsidian_vault'
  const parts: string[] = []
  if (isDir && filesTotal) parts.push(`${filesTotal} files`)
  if (lastScan) parts.push(`scanned ${lastScan}`)
  if (!parts.length && source.uri) return source.uri
  return parts.join(' · ')
}

export function shouldShowWordCount(wordCount: number | undefined | null): boolean {
  return wordCount != null && wordCount > 0
}
