/**
 * Copying many memories from one store to another in route-sized batches.
 *
 * `POST /api/memory/seed` takes at most `MAX_SEED_ITEMS` (50) items per request
 * (dashboard/handlers/memory_member.py), a bound on one request body, not on
 * what a person may copy: each copy is one row write with its vector filled in
 * later by the backfill sweep. So the dialog sends a larger selection as
 * consecutive batches and stops at the first batch it cannot confirm.
 */
import { api } from '../../api/client'

/** Items one seed request may carry: the route's `MAX_SEED_ITEMS`. */
export const SEED_BATCH = 50
/** Items one copy may select. */
export const SEED_LIMIT = 1000

export type SeedItem = { kind: 'fact' | 'directive' | 'episode'; id: string }
type SeedBody = Awaited<ReturnType<typeof api.memorySeed>>
export type SeedResult = SeedBody['results'][number]
export type SeedOutcome = { results: SeedResult[]; partial: boolean; error?: unknown }

/** Copy `items` batch by batch. A batch that reports a partial copy or fails
 *  stops the run: its items keep their own outcomes, and every later item is
 *  `not_attempted`. A failure before anything was copied rejects, as one
 *  request did. */
export async function seedInBatches(source: string, store: string, items: SeedItem[], onProgress?: (done: number) => void): Promise<SeedOutcome> {
  const results: SeedResult[] = []
  for (let start = 0; start < items.length; start += SEED_BATCH) {
    const batch = items.slice(start, start + SEED_BATCH)
    let body: SeedBody
    try {
      body = await api.memorySeed(source, store, batch)
    } catch (error) {
      if (!results.length) throw error
      return { results: [...results, ...notAttempted(items.slice(start))], partial: true, error }
    }
    results.push(...body.results)
    onProgress?.(start + batch.length)
    if (body.partial) return { results: [...results, ...notAttempted(items.slice(start + SEED_BATCH))], partial: true }
  }
  return { results, partial: false }
}

const notAttempted = (items: SeedItem[]): SeedResult[] => items.map(item => ({ id: item.id, outcome: 'not_attempted' }))
