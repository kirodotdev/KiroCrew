/**
 * knowledgeUtils — the pure derivations the Sources list draws from a raw
 * Source row. Each one has a branch the list depends on: properties arriving
 * as a JSON string vs an object vs malformed, the sync badge's four states,
 * the subtitle's directory-vs-uri fallback, and the word-count guard.
 */
import { describe, it, expect } from 'vitest'
import {
  parseSourceProps,
  getSyncBadgeVariant,
  formatSourceSubtitle,
  shouldShowWordCount,
} from '../pages/knowledge/knowledgeUtils'
import type { Source } from '../pages/knowledge/types'

const src = (over: Partial<Source> = {}): Source => ({
  id: 'zzq-1',
  name: 'zzq-source',
  source_type: 'local_folder',
  sync_status: 'synced',
  ...over,
})

describe('parseSourceProps', () => {
  it('parses properties delivered as a JSON string', () => {
    const parsed = parseSourceProps(src({
      properties: JSON.stringify({
        summary_status: 'zzq-done',
        files_total: 12,
        last_scan: 'zzq-scan-stamp',
        recursive: true,
        word_count: 4321,
      }),
    }))

    expect(parsed).toEqual({
      summary: undefined,
      summaryStatus: 'zzq-done',
      filesTotal: 12,
      lastScan: 'zzq-scan-stamp',
      recursive: true,
      wordCount: 4321,
    })
  })

  it('accepts properties delivered as an object', () => {
    const parsed = parseSourceProps(src({ properties: { files_total: 3, recursive: false } }))
    expect(parsed.filesTotal).toBe(3)
    expect(parsed.recursive).toBe(false)
  })

  it('falls back to empty props for a malformed JSON string', () => {
    const parsed = parseSourceProps(src({ properties: '{not json' }))
    expect(parsed.filesTotal).toBeUndefined()
    expect(parsed.summaryStatus).toBeUndefined()
  })

  it('falls back to empty props when properties is absent', () => {
    expect(parseSourceProps(src()).wordCount).toBeUndefined()
  })

  it('builds the summary from summary_topic plus parsed themes', () => {
    const parsed = parseSourceProps(src({
      summary_topic: 'zzq-topic',
      summary_themes: JSON.stringify(['zzq-a', 'zzq-b']),
    }))
    expect(parsed.summary).toEqual({ topic: 'zzq-topic', themes: ['zzq-a', 'zzq-b'] })
  })

  it('keeps the summary with empty themes when summary_themes is malformed', () => {
    const parsed = parseSourceProps(src({ summary_topic: 'zzq-topic', summary_themes: '[oops' }))
    expect(parsed.summary).toEqual({ topic: 'zzq-topic', themes: [] })
  })

  it('omits the summary entirely when there is no topic', () => {
    expect(parseSourceProps(src({ summary_themes: '["zzq"]' })).summary).toBeUndefined()
  })
})

describe('getSyncBadgeVariant', () => {
  it('maps each known sync status to its badge variant', () => {
    expect(getSyncBadgeVariant('synced')).toBe('ok')
    expect(getSyncBadgeVariant('error')).toBe('err')
    expect(getSyncBadgeVariant('paused')).toBe('warn')
  })

  it('falls back to the in-progress variant for anything else', () => {
    expect(getSyncBadgeVariant('syncing')).toBe('aim')
    expect(getSyncBadgeVariant('')).toBe('aim')
  })
})

describe('formatSourceSubtitle', () => {
  it('shows the file count for a directory-backed source', () => {
    expect(formatSourceSubtitle(src({ source_type: 'local_folder' }), 5)).toBe('5 files')
    expect(formatSourceSubtitle(src({ source_type: 'obsidian_vault' }), 2)).toBe('2 files')
  })

  it('joins the file count and the scan stamp', () => {
    expect(formatSourceSubtitle(src(), 5, 'zzq-stamp')).toBe('5 files · scanned zzq-stamp')
  })

  it('omits the file count for a non-directory source even when one is given', () => {
    expect(formatSourceSubtitle(src({ source_type: 'url' }), 9, 'zzq-stamp')).toBe('scanned zzq-stamp')
  })

  it('falls back to the uri when there is nothing else to show', () => {
    expect(formatSourceSubtitle(src({ source_type: 'url', uri: 'zzq://somewhere' }))).toBe('zzq://somewhere')
  })

  it('returns an empty string when there is neither a part nor a uri', () => {
    expect(formatSourceSubtitle(src({ source_type: 'url' }))).toBe('')
  })
})

describe('shouldShowWordCount', () => {
  it('shows only a positive count', () => {
    expect(shouldShowWordCount(1)).toBe(true)
    expect(shouldShowWordCount(0)).toBe(false)
  })

  it('hides null and undefined', () => {
    expect(shouldShowWordCount(null)).toBe(false)
    expect(shouldShowWordCount(undefined)).toBe(false)
  })
})
