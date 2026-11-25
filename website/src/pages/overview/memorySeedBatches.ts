/**
 * Copying memories from one store to another in route-sized batches.
 *
 * `POST /api/memory/seed` takes at most `MAX_SEED_ITEMS` (50) items per request
 * (dashboard/handlers/memory_member.py), a bound on one request body, not on
 * what a person may copy: each copy is one row write with its vector filled in
 * later by the backfill sweep. So the dialog sends any selection, or every
 * source row matching its search, as consecutive batches. Each batch commits
 * on the server before the next is sent, so stopping part way loses nothing,
 * and a second run skips what the first copied (`seed_item_if_absent`).
 */
import { api } from '../../api/client'

/** Items one seed request may carry: the route's `MAX_SEED_ITEMS`. */
export const SEED_BATCH = 50
/** Rows one source page returns (`memberMemoryPage`'s `limit`). */
const SOURCE_PAGE = 100

export type SeedItem = { kind: 'fact' | 'directive' | 'episode'; id: string }
type SeedBody = Awaited<ReturnType<typeof api.memorySeed>>
export type SeedResult = SeedBody['results'][number]
export type SeedCounts = { imported: number; existing: number; failed: number }
export type SeedOutcome = { results: SeedResult[]; counts: SeedCounts; partial: boolean; error?: unknown }

const FAILED = new Set(['unconfirmed', 'rejected', 'not_attempted'])
const tally = (results: SeedResult[]): SeedCounts => ({
  imported: results.filter(r => r.outcome === 'imported').length,
  existing: results.filter(r => r.outcome === 'existing').length,
  failed: results.filter(r => FAILED.has(r.outcome)).length,
})

type SourceRow = { key?: string; id?: string }
/** A source row as a seed item: a semantic row by key (lessons are `lesson.*`), an experience by id. */
export const seedItemOf = (row: SourceRow): SeedItem => row.key
  ? { kind: row.key.startsWith('lesson.') ? 'directive' : 'fact', id: row.key }
  : { kind: 'episode', id: row.id || '' }

/** Every source row matching `query` whose kind is in `kinds`, a page at a time:
 *  facts and lessons, then experiences. An unpicked experiences kind skips its pages. */
export async function* sourceRows(source: string, query: string, kinds: ReadonlySet<SeedItem['kind']> = new Set(['fact', 'directive', 'episode'])): AsyncGenerator<SeedItem[]> {
  for (const kind of ['semantic', 'episodic'] as const) {
    if (kind === 'semantic' ? !kinds.has('fact') && !kinds.has('directive') : !kinds.has('episode')) continue
    for (let offset = 0; ; offset += SOURCE_PAGE) {
      const page = await api.memberMemoryPage(source, kind, offset, query) as { entries?: SourceRow[] }
      const rows = page.entries || []
      const items = rows.map(seedItemOf).filter(item => kinds.has(item.kind))
      if (items.length) yield items
      if (rows.length < SOURCE_PAGE) break
    }
  }
}

async function* once(items: SeedItem[]) { yield items }

/** Copy `items` (a list, or source pages as `sourceRows` yields them) batch by
 *  batch, reporting running counts after each. A batch the server reports as
 *  partial, or a failed request, stops the run; a failure before anything was
 *  copied rejects, as one request did. */
export async function seedInBatches(source: string, store: string, items: SeedItem[] | AsyncIterable<SeedItem[]>, onProgress?: (counts: SeedCounts) => void): Promise<SeedOutcome> {
  const results: SeedResult[] = []
  let pending: SeedItem[] = []
  const send = async (batch: SeedItem[]): Promise<SeedOutcome | null> => {
    let body: SeedBody
    try {
      body = await api.memorySeed(source, store, batch)
    } catch (error) {
      if (!results.length) throw error
      return { results, counts: tally(results), partial: true, error }
    }
    results.push(...body.results)
    onProgress?.(tally(results))
    return body.partial ? { results, counts: tally(results), partial: true } : null
  }
  try {
    for await (const page of Array.isArray(items) ? once(items) : items) {
      pending.push(...page)
      while (pending.length >= SEED_BATCH) {
        const stop = await send(pending.slice(0, SEED_BATCH))
        if (stop) return stop
        pending = pending.slice(SEED_BATCH)
      }
    }
  } catch (error) {
    // A source page that failed to read: what was copied stays copied.
    if (!results.length) throw error
    return { results, counts: tally(results), partial: true, error }
  }
  if (pending.length) {
    const stop = await send(pending)
    if (stop) return stop
  }
  return { results, counts: tally(results), partial: false }
}
