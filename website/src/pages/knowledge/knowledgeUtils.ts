import type { Source } from './types'

export interface ParsedSourceProps {
  summary?: { topic?: string; themes?: string[] }
  summaryStatus?: string
  filesTotal?: number
  lastScan?: string
  recursive?: boolean
  wordCount?: number
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
  }
}

export function getSyncBadgeVariant(syncStatus: string): 'ok' | 'err' | 'aim' | 'warn' {
  if (syncStatus === 'synced') return 'ok'
  if (syncStatus === 'error') return 'err'
  if (syncStatus === 'paused') return 'warn'
  return 'aim'
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
