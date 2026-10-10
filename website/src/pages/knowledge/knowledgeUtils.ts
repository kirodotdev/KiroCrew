import type { Source } from './types'

export interface ParsedSourceProps {
  summary?: { topic?: string; themes?: string[] }
  summaryStatus?: string
  filesTotal?: number
  lastScan?: string
  recursive?: boolean
  wordCount?: number
  ignorePatterns: string[]
  /** Outcome of the latest opt-in purge, recorded by the gateway under its ``purge_id``. */
  lastPurge?: { id: string; removed?: number; failed: boolean }
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
    ignorePatterns: Array.isArray(props.ignore_patterns)
      ? (props.ignore_patterns as unknown[]).filter((p): p is string => typeof p === 'string')
      : [],
    lastPurge: parseLastPurge(props.last_purge),
  }
}

function parseLastPurge(raw: unknown): ParsedSourceProps['lastPurge'] {
  if (!raw || typeof raw !== 'object') return undefined
  const p = raw as Record<string, unknown>
  if (typeof p.id !== 'string') return undefined
  return { id: p.id, removed: typeof p.removed === 'number' ? p.removed : undefined, failed: p.failed === true }
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
}

/** Readable label for a source's sync_status; an unknown value shows as-is. */
export function syncStatusLabel(syncStatus: string, t: (key: string) => string): string {
  const key = SYNC_STATUS_LABEL_KEYS[syncStatus]
  return key ? t(key) : syncStatus
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
