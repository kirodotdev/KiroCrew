import { describe, expect, it } from 'vitest'
import { localPage } from '../pages/overview/memoryPage/memoryLocalList'
import type { MemoryRecord, MemoryRecordSummary } from '../types/memoryEditing'

/** A record as a `summary=1` page carries it (`memory_edit._summary`). */
function record(kind: MemoryRecord['kind'], id: string, over: Partial<MemoryRecordSummary> = {}, facets: Partial<MemoryRecordSummary['facets']> = {}): MemoryRecord {
  const summary: MemoryRecordSummary = {
    kind, id, key: kind === 'episode' ? '' : id, created_at: '2026-11-01', updated_at: '2026-11-01', confidence: kind === 'episode' ? null : 0.8, importance: kind === 'episode' ? 0.5 : null,
    search: id.toLowerCase(), ref: `${id}ref`.slice(0, 10).padEnd(10, '0'),
    facets: { category: null, applies: null, scope: null, source: 'consolidation', surface: null, crew: null, tag: [], ...facets }, ...over,
  }
  return { kind, id, key: summary.key || undefined, text: id, revision: 'r', summary }
}

const RECORDS = [
  record('fact', 'user.editor', { updated_at: '2026-11-03', search: 'user.editor vim' }),
  record('directive', 'lesson.a', { updated_at: '2026-11-05', confidence: 0.4 }, { category: 'tool', applies: 'always', scope: '' }),
  record('directive', 'lesson.b', { updated_at: '2026-11-02', confidence: 0.9 }, { category: 'preference', applies: 'on_topic', scope: 'website' }),
  record('episode', 'ep-1', { updated_at: '2026-11-04', search: 'worked on the editor' }, { tag: ['ci', 'release'] }),
]
const ids = (page: { entries: MemoryRecord[] }) => page.entries.map(entry => entry.id)

describe('the memory list computed in the browser', () => {
  it('sorts by the picked field both ways, with unscored rows last', () => {
    expect(ids(localPage(RECORDS, { q: '', kind: 'all' }, 0, 50, 'updated', true))).toEqual(['lesson.a', 'ep-1', 'user.editor', 'lesson.b'])
    expect(ids(localPage(RECORDS, { q: '', kind: 'all' }, 0, 50, 'confidence', false))).toEqual(['lesson.a', 'user.editor', 'lesson.b', 'ep-1'])
    expect(ids(localPage(RECORDS, { q: '', kind: 'all' }, 0, 50, 'confidence', true))).toEqual(['lesson.b', 'user.editor', 'lesson.a', 'ep-1'])
  })

  it('searches every word, filters by facet and counts each group without its own filter', () => {
    expect(ids(localPage(RECORDS, { q: 'Editor', kind: 'all' }, 0, 50, 'updated', true))).toEqual(['ep-1', 'user.editor'])
    const page = localPage(RECORDS, { q: '', kind: 'directive', filters: { category: ['tool'] } }, 0, 50, 'updated', true)
    expect(ids(page)).toEqual(['lesson.a'])
    expect(page.facets.category).toEqual({ tool: 1, preference: 1 })
    expect(page.facets.kind).toEqual({ directive: 1 })
    expect(localPage(RECORDS, { q: '', kind: 'all', filters: { tag: ['ci'] } }, 0, 50, 'updated', true).total).toBe(1)
    expect(ids(localPage(RECORDS, { q: '', kind: 'all', filters: { conf_min: 0.5 } }, 0, 50, 'updated', true))).toEqual(['user.editor', 'lesson.b'])
  })

  it('lists exactly a graph selection, and pages without skipping', () => {
    const refs = [RECORDS[0].summary!.ref, RECORDS[3].summary!.ref]
    expect(ids(localPage(RECORDS, { q: '', kind: 'all', filters: { ref: refs } }, 0, 50, 'updated', true))).toEqual(['ep-1', 'user.editor'])
    const first = localPage(RECORDS, { q: '', kind: 'all' }, 0, 3, 'updated', true)
    const second = localPage(RECORDS, { q: '', kind: 'all' }, 3, 3, 'updated', true)
    expect(first.has_more).toBe(true)
    expect([...ids(first), ...ids(second)]).toEqual(['lesson.a', 'ep-1', 'user.editor', 'lesson.b'])
  })
})
